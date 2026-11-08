"""Prepare the downloaded Middlebury Temple 312-view set without changing images.

SfM inputs contain K/photos only. Explicit dataset_posed_* inputs retain published
world-to-camera calibration for direct NeRF training; they contain no point cloud.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import cv2
import numpy as np

from minicolmap.io import load_dataset
from minicolmap.models import Camera, Pose
from minicolmap.rendering.data import load_posed_dataset


def prepare(root: Path) -> dict:
    root = root.resolve()
    calibration = root / 'reference/temple_par.txt'
    angle_file = root / 'reference/temple_ang.txt'
    lines = [line.split() for line in calibration.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if not lines or lines[0] != ['312'] or len(lines) != 313:
        raise ValueError('Expected the full Temple calibration with 312 records')
    records, checksums = [], {}
    for row in lines[1:]:
        match = re.fullmatch(r'temple(\d{4})\.png', row[0])
        if len(row) != 22 or match is None:
            raise ValueError('Expected Temple filename and row-major K/R/t (21 numbers)')
        values = np.asarray(row[1:], dtype=np.float64)
        K, R, t = values[:9].reshape(3, 3), values[9:18].reshape(3, 3), values[18:]
        pose = Pose(R, t)  # These Temple rotations already satisfy the pose contract.
        raw = (root / 'images' / row[0]).read_bytes()
        pixels = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if pixels is None or pixels.shape[:2] != (480, 640):
            raise ValueError(f'Expected a decodable original 640x480 image: {row[0]}')
        Camera(1, 640, 480, K)
        checksums[row[0]] = hashlib.sha256(raw).hexdigest()
        records.append({'id': int(match[1]), 'name': row[0], 'K': K, 'R': pose.R, 't': pose.t})
    records.sort(key=lambda row: row['id'])
    if [row['id'] for row in records] != list(range(1, 313)):
        raise ValueError('Image IDs must be unique and cover 0001..0312')
    if {path.name for path in (root / 'images').glob('*.png')} != set(checksums):
        raise ValueError('Photo directory must contain exactly the calibrated 312 PNG files')
    K = records[0]['K']
    if not all(np.array_equal(row['K'], K) for row in records):
        raise ValueError('Expected shared Temple intrinsics')
    angles = {}
    for row in (line.split() for line in angle_file.read_text().splitlines() if line.strip()):
        if len(row) != 3 or row[2] in angles:
            raise ValueError('Expected unique latitude/longitude/filename angle records')
        angle = np.asarray(row[:2], dtype=float)
        if not np.isfinite(angle).all():
            raise ValueError('Nonfinite capture angles')
        angles[row[2]] = angle
    if set(angles) != set(checksums):
        raise ValueError('Capture angles and photos must have the same 312 names')

    # Filename order contains capture gaps. Select a genuinely continuous arc
    # for small SfM tests using acquisition metadata, without supplying R/t.
    arcs, start = [], 0
    for i in range(1, len(records)):
        before, after = (angles[row['name']] for row in records[i - 1:i + 1])
        gap = abs((after[1] - before[1] + 180) % 360 - 180)
        if before[0] != after[0] or not 0 < gap < 10:
            arcs.append(records[start:i])
            start = i
    arcs.append(records[start:])
    arc = max(arcs, key=len)
    band = [row for row in records if angles[row['name']][0] == angles[records[0]['name']][0]]
    if len(arc) != 19 or len(band) != 27:
        raise ValueError('Unexpected full Temple capture layout (19-view arc / 27-view first band)')
    camera = {'id': 1, 'width': 640, 'height': 480, 'K': K.tolist()}

    def manifest(selected, posed=False):
        result = {'cameras': [camera], 'images': [
            {'id': row['id'], 'name': row['name'], 'camera_id': 1, 'path': 'images/' + row['name']}
            for row in selected]}
        if posed:
            result.update(pose_convention='world_to_camera', poses=[
                {'image_id': row['id'], 'R': row['R'].tolist(), 't': row['t'].tolist()} for row in selected])
        return result

    selections = {'dataset.json': arc[:8], 'dataset_arc19.json': arc,
                  'dataset_band27.json': band, 'dataset_all.json': records,
                  'dataset_posed_arc19.json': arc, 'dataset_posed_band27.json': band,
                  'dataset_posed_all.json': records}
    documents = {name: manifest(selected, name.startswith('dataset_posed_'))
                 for name, selected in selections.items()}
    documents['reference/poses.json'] = {
        'purpose': 'Published calibration, not SfM estimates', 'pose_convention': 'world_to_camera',
        'poses': manifest(records, True)['poses']}
    documents['provenance.json'] = {
        'dataset': 'Middlebury Temple full 312-view set (Temple of the Dioskouroi)',
        'source_page': 'https://vision.middlebury.edu/mview/data/',
        'images': 312, 'width': 640, 'height': 480, 'shared_K': K.tolist(),
        'calibration_sha256': hashlib.sha256(calibration.read_bytes()).hexdigest(),
        'capture_angles_sha256': hashlib.sha256(angle_file.read_bytes()).hexdigest(),
        'image_sha256': checksums,
        'preprocessing': 'None: no resize, crop, rotation, re-encoding or second undistortion. '
                         'Publisher states radial distortion is already removed.',
        'pose_usage': 'Only dataset_posed_*.json exposes published K[R|t] world-to-camera poses. '
                      'Other dataset manifests contain photos and K only.',
        'pose_preprocessing': 'None; original Temple R/t already satisfy the rigid-pose contract.',
        'selections': {name: [row['id'] for row in selected] for name, selected in selections.items()},
        'selection_reason': 'Default eight views are the start of the longest filename-contiguous '
                            'same-latitude arc with adjacent longitude gaps below 10 degrees.',
        'band27_note': 'First capture band of full Temple; NOT the separately published TempleRing set.',
        'verification_scope': 'Local file fingerprints and format validation, not an official archive '
                              'signature or a reconstruction/training quality claim.'}
    # Check all existing documents before writing anything. Re-running is safe;
    # user-edited manifests and original images/calibration are never overwritten.
    for name, document in documents.items():
        target = root / name
        if target.exists() and json.loads(target.read_text(encoding='utf-8-sig')) != document:
            raise ValueError(f'Refusing to overwrite different existing data: {target}')
    for name, document in documents.items():
        target = root / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                              encoding='utf-8')
    summary = []
    for name in selections:
        dataset = load_dataset(root / name)
        poses = len(load_posed_dataset(root / name).poses) if name.startswith('dataset_posed_') else 0
        summary.append({'manifest': name, 'images': len(dataset['images']), 'published_poses': poses})
    return {'root': str(root), 'validated_images': 312, 'dimensions': [640, 480],
            'K': K.tolist(), 'manifests': summary}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1] / 'data/temple')
    print(json.dumps(prepare(parser.parse_args().root), ensure_ascii=False, indent=2))
