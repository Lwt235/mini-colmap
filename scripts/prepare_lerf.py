"""Prepare the official LangSplat LERF-OVS archive for Mini-COLMAP.

Only undistorted SIMPLE_PINHOLE/PINHOLE sparse models are accepted. Original
archive contents remain in the cache; imported reconstructions are explicitly
labelled as official COLMAP results, never as Mini-COLMAP reconstructions.
Binary layout: https://colmap.github.io/format.html (little endian).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import struct
import zipfile

import cv2
import numpy as np

from minicolmap.io import load_dataset, save_reconstruction
from minicolmap.models import Camera, ImageRecord, Point3D, Pose, Reconstruction, validate_reconstruction
from minicolmap.rendering.data import load_posed_dataset

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://github.com/minghanqin/LangSplat'
DOWNLOAD = 'https://drive.google.com/file/d/1QF1Po5p5DwTjFHu6tnTeYs_G0egMVmHt/view'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n')


class Reader:
    def __init__(self, path):
        self.data = memoryview(path.read_bytes())
        self.offset = 0

    def take(self, size):
        if size < 0 or size > len(self.data) - self.offset:
            raise ValueError('Truncated COLMAP binary data')
        result = self.data[self.offset:self.offset + size]
        self.offset += size
        return result

    def unpack(self, fmt):
        return struct.unpack('<' + fmt, self.take(struct.calcsize('<' + fmt)))

    def count(self, minimum_record_bytes):
        count, = self.unpack('Q')
        if count > (len(self.data) - self.offset) // minimum_record_bytes:
            raise ValueError('COLMAP record count exceeds remaining data')
        return count

    def name(self):
        chars = bytearray()
        while True:
            byte, = self.unpack('B')
            if byte == 0:
                break
            chars.append(byte)
        name = chars.decode('utf-8')
        if not name or '\\' in name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
            raise ValueError('Unsafe COLMAP image name')
        return name

    def finish(self):
        if self.offset != len(self.data):
            raise ValueError('Unexpected trailing COLMAP data')


def read_model(directory, audit=None):
    """Keep stable IDs/tracks; translate COLMAP half-pixel to OpenCV centers."""
    model = Reconstruction()
    reader = Reader(directory / 'cameras.bin')
    for _ in range(reader.count(48)):
        camera_id, kind, width, height = reader.unpack('iiQQ')
        if kind == 0:
            focal, cx, cy = reader.unpack('3d')
            fx = fy = focal
        elif kind == 1:
            fx, fy, cx, cy = reader.unpack('4d')
        else:
            raise ValueError(f'Camera model {kind} has distortion; use scene/sparse/0, not distorted/sparse/0')
        if camera_id in model.cameras:
            raise ValueError('Duplicate camera ID')
        model.cameras[camera_id] = Camera(camera_id, width, height,
            [[fx, 0, cx - .5], [0, fy, cy - .5], [0, 0, 1]])
    reader.finish()
    reader = Reader(directory / 'images.bin')
    associations = {}
    for _ in range(reader.count(73)):
        image_id, qw, qx, qy, qz, tx, ty, tz, camera_id = reader.unpack('i7di')
        name = reader.name()
        count = reader.count(24)
        entries = np.frombuffer(reader.take(count * 24), dtype=[('xy', '<f8', (2,)), ('point', '<i8')])
        q = np.array([qw, qx, qy, qz])
        if not np.isfinite(q).all() or not np.isclose(np.linalg.norm(q), 1, atol=1e-6):
            raise ValueError('Invalid COLMAP quaternion')
        w, x, y, z = q / np.linalg.norm(q)
        R = np.array([[1-2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y)],
                      [2*(x*y+w*z), 1-2*(x*x+z*z), 2*(y*z-w*x)],
                      [2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)]])
        if image_id in model.images:
            raise ValueError('Duplicate image ID')
        model.images[image_id] = ImageRecord(image_id, name, camera_id, entries['xy'] - .5)
        model.poses[image_id] = Pose(R, [tx, ty, tz])
        associations[image_id] = entries['point'].copy()
    reader.finish()
    reader = Reader(directory / 'points3D.bin')
    for _ in range(reader.count(51)):
        values = reader.unpack('Q3d3Bd')
        point_id = values[0]
        count = reader.count(8)
        track = struct.iter_unpack('<ii', reader.take(count * 8))
        observations = tuple(track)
        for image_id, index in observations:
            if (image_id not in associations or index < 0 or index >= len(associations[image_id])
                    or associations[image_id][index] != point_id):
                raise ValueError('COLMAP point track disagrees with image associations')
        if point_id in model.points:
            raise ValueError('Duplicate point ID')
        # Some official tracks contain two features from the same photograph.
        # Mini-COLMAP requires one observation per image. Retain the feature
        # nearest the fixed official projection; record every omitted entry.
        grouped = {}
        for image_id, index in observations:
            grouped.setdefault(image_id, []).append(index)
        cleaned = []
        for image_id, indices in grouped.items():
            pose = model.poses[image_id]
            image = model.images[image_id]
            projected = model.cameras[image.camera_id].K @ (pose.R @ values[1:4] + pose.t)
            if len(indices) > 1 and abs(projected[2]) < 1e-12:
                raise ValueError('Cannot resolve duplicate observations with degenerate depth')
            kept = min(indices, key=lambda index: np.linalg.norm(image.keypoints[index] - projected[:2] / projected[2])) if len(indices) > 1 else indices[0]
            cleaned.append((image_id, kept))
            if len(indices) > 1 and audit is not None:
                audit.append(dict(point_id=point_id, image_id=image_id, original_indices=indices, retained_index=kept))
        model.points[point_id] = Point3D(point_id, values[1:4], values[4:7], tuple(cleaned))
    reader.finish()
    validate_reconstruction(model)
    return model


def extract(archive, destination):
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f'Extraction destination must be empty: {destination}')
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            name = PurePosixPath(member.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in member.filename or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unsafe ZIP member')
        # ZipFile verifies CRC as each member is extracted.
        source.extractall(destination)


def prepare_scene(scene, destination, output):
    for path in (destination, output):
        if path.exists() and any(path.iterdir()):
            raise ValueError(f'Refusing to overwrite existing data: {path}')
    audit = []
    model = read_model(scene / 'sparse' / '0', audit)
    images = []
    hashes = {}
    for image in sorted(model.images.values(), key=lambda image: image.name):
        source = scene / 'images' / image.name
        pixels = cv2.imread(str(source), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        camera = model.cameras[image.camera_id]
        if pixels is None or pixels.shape[:2] != (camera.height, camera.width):
            raise ValueError(f'Image dimensions disagree with undistorted camera: {source}')
        target = destination / 'images' / image.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[image.name] = hashlib.sha256(target.read_bytes()).hexdigest()
        images.append(dict(id=image.id, name=image.name, camera_id=image.camera_id, path='images/' + image.name))
    cameras = [dict(id=c.id, width=c.width, height=c.height, K=c.K.tolist()) for c in model.cameras.values()]
    for filename, selected in [('dataset_all.json', images), ('dataset_12.json', images[:12])]:
        write_json(destination / filename, dict(cameras=cameras, images=selected))
        load_dataset(destination / filename)
    write_json(destination / 'dataset_posed_all.json', dict(cameras=cameras, images=images,
        pose_convention='world_to_camera', poses=[dict(image_id=i, R=p.R.tolist(), t=p.t.tolist()) for i, p in model.poses.items()]))
    load_posed_dataset(destination / 'dataset_posed_all.json')
    output.mkdir(parents=True, exist_ok=True)
    save_reconstruction(model, output / 'model.json')
    write_json(output / 'import_audit.json', dict(duplicate_observations=audit,
        policy='Retain the lowest reprojection error observation for each point/image; official geometry unchanged.'))
    summary = dict(pipeline='sfm', image_count=len(images), registered_images=len(model.poses), point_count=len(model.points),
                   origin='Official LangSplat/COLMAP import; not computed by Mini-COLMAP')
    write_json(output / 'summary.json', summary)
    write_json(output / 'run_status.json', dict(status='completed', stage='done', pipeline='sfm',
        dataset=str((destination / 'dataset_all.json').resolve()), origin=summary['origin']))
    labels = []
    by_name = {image.name: image for image in model.images.values()}
    for annotation in sorted((scene.parent / 'label' / scene.name).glob('*.json')):
        document = json.loads(annotation.read_text())
        info = document['info']
        image = by_name.get(info['name'])
        if image is None:
            raise ValueError(f'Label photograph absent from registered images: {annotation}')
        camera = model.cameras[image.camera_id]
        if (info['width'], info['height']) != (camera.width, camera.height):
            raise ValueError(f'Label dimensions differ from training image: {annotation}')
        labels.append(dict(image_id=image.id, image_name=image.name, annotation=str(annotation.resolve()),
            categories=sorted({obj['category'] for obj in document['objects']}), objects=len(document['objects'])))
    write_json(destination / 'semantic_annotations.json', dict(format='Official info/objects polygon JSON; not SAM/CLIP features',
        note='Labels are not used to estimate geometry or fit RGB; preserve official semantic evaluation split separately.', frames=labels))
    write_json(destination / 'provenance.json', dict(source=SOURCE, download=DOWNLOAD, scene=scene.name,
        image_sha256=hashes, annotation_frames=len(labels), duplicate_track_groups=len(audit), preprocessing='Original undistorted image bytes. K principal point and keypoints shifted -0.5 pixels from COLMAP to Mini-COLMAP/OpenCV integer centers. R/t, IDs, RGB and 3D coordinates preserved. Duplicate same-image track observations resolved by minimum reprojection error; see output import_audit.json.',
        reference_model=str((output / 'model.json').resolve()), **summary))
    return dict(scene=scene.name, dataset=str(destination), model=str(output / 'model.json'), annotation_frames=len(labels), **summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=ROOT / '.cache/datasets/lerf_ovs.zip')
    parser.add_argument('--raw', type=Path, default=ROOT / '.cache/datasets/lerf_ovs_raw')
    parser.add_argument('--scene', action='append', help='Default: every scene containing sparse/0/cameras.bin')
    args = parser.parse_args()
    extract(args.archive, args.raw)
    scenes = sorted(path.parent.parent.parent for path in args.raw.glob('**/sparse/0/cameras.bin') if 'distorted' not in path.parts)
    if args.scene:
        scenes = [scene for scene in scenes if scene.name in args.scene]
        if {scene.name for scene in scenes} != set(args.scene):
            raise ValueError('Requested scene absent from archive')
    if not scenes:
        raise ValueError('Archive contains no undistorted COLMAP scenes')
    results = []
    for scene in scenes:
        result = prepare_scene(scene, ROOT / 'data/lerf_ovs' / scene.name, ROOT / 'outputs' / ('lerf_' + scene.name + '_official_colmap'))
        results.append(result)
        print(json.dumps(result), flush=True)
    with args.archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    write_json(ROOT / '.cache/datasets/lerf-preparation.json', dict(source=SOURCE, download=DOWNLOAD,
        archive_sha256=digest,
        archive_bytes=args.archive.stat().st_size, scenes=results))


if __name__ == '__main__':
    main()
