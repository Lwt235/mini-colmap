"""Configure the downloaded Middlebury Dino 363 images without changing pixels.

SfM manifests contain only K/photos. Explicit *_posed_* manifests expose the
published world-to-camera poses for direct NeRF training, never estimated poses.
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
    calibration = root / 'reference/dino_par.txt'
    angles_path = root / 'reference/dino_ang.txt'
    lines = [line.split() for line in calibration.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if not lines or lines[0] != ['363'] or len(lines) != 364:
        raise ValueError('Expected the full Dino calibration with 363 records')
    records, checksums, dimensions = [], {}, set()
    rotation_errors = []
    for row in lines[1:]:
        match = re.fullmatch(r'dino(\d{4})\.png', row[0])
        if len(row) != 22 or match is None:
            raise ValueError('Expected Dino filename and row-major K/R/t (21 numbers)')
        values = np.asarray(row[1:], dtype=np.float64)
        K, raw_R, raw_t = values[:9].reshape(3, 3), values[9:18].reshape(3, 3), values[18:]
        error = float(np.max(np.abs(raw_R @ raw_R.T - np.eye(3))))
        if not np.isfinite(values).all() or error > 1e-5 or np.linalg.det(raw_R) <= 0:
            raise ValueError(f'Calibration is not a near-rigid camera: {row[0]}')
        # Published Dino matrices miss orthogonality by up to 1.66e-6, slightly
        # beyond Pose's 1e-6 gate. Preserve raw calibration and camera centres;
        # only derived pose manifests use the nearest proper rotation.
        U, _, Vt = np.linalg.svd(raw_R)
        R = U @ Vt
        center = -np.linalg.solve(raw_R, raw_t)
        t = -R @ center
        rotation_errors.append(error)
        pose = Pose(R, t)
        image_path = root / 'images' / row[0]
        raw = image_path.read_bytes()
        pixels = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if pixels is None:
            raise ValueError(f'Cannot decode {image_path}')
        height, width = pixels.shape[:2]
        Camera(1, width, height, K)
        dimensions.add((width, height))
        checksums[row[0]] = hashlib.sha256(raw).hexdigest()
        records.append({'id': int(match[1]), 'name': row[0], 'K': K, 'R': pose.R, 't': pose.t})
    records.sort(key=lambda row: row['id'])
    if [row['id'] for row in records] != list(range(1, 364)):
        raise ValueError('Image IDs must be unique and cover 0001..0363')
    if dimensions != {(640, 480)}:
        raise ValueError(f'Original Dino images must be 640x480, found {dimensions}')
    K = records[0]['K']
    if not all(np.array_equal(row['K'], K) for row in records):
        raise ValueError('This preparation requires the published shared Dino intrinsics')
    angle_rows = [row.split() for row in angles_path.read_text().splitlines() if row.strip()]
    angles = {row[2]: tuple(map(float, row[:2])) for row in angle_rows}
    if len(angles) != 363 or set(angles) != set(checksums):
        raise ValueError('Capture angles and photos must have the same 363 names')
    if len({angles[row['name']][0] for row in records[:49]}) != 1:
        raise ValueError('The first 49 views must follow one capture band')
    for before, after in zip(records[:19], records[1:20]):
        step = abs((angles[after['name']][1] - angles[before['name']][1] + 180) % 360 - 180)
        if not 0 < step < 10:
            raise ValueError('The default input must have adjacent capture angles below 10 degrees')
    camera = {'id': 1, 'width': 640, 'height': 480, 'K': K.tolist()}

    def manifest(selected, posed=False):
        result = {'cameras': [camera], 'images': [
            {'id': row['id'], 'name': row['name'], 'camera_id': 1, 'path': 'images/' + row['name']}
            for row in selected]}
        if posed:
            result.update(pose_convention='world_to_camera', poses=[
                {'image_id': row['id'], 'R': row['R'].tolist(), 't': row['t'].tolist()} for row in selected])
        return result

    selections = {'dataset.json': records[:8], 'dataset_20.json': records[:20],
                  'dataset_band49.json': records[:49], 'dataset_all.json': records}
    documents = {name: manifest(selected) for name, selected in selections.items()}
    documents.update({'dataset_posed_band49.json': manifest(records[:49], True),
                      'dataset_posed_all.json': manifest(records, True),
                      'reference/poses.json': {'purpose': 'Published reference, not SfM estimates',
                          'pose_convention': 'world_to_camera',
                          'rotation_preprocessing': 'Nearest proper rotation by SVD, preserving camera centre; '
                                                    'raw calibration remains in dino_par.txt',
                          'poses': manifest(records, True)['poses']},
                      'provenance.json': {
                          'dataset': 'Middlebury Dino full 363-view set',
                          'source_page': 'https://vision.middlebury.edu/mview/data/',
                          'images': 363, 'width': 640, 'height': 480, 'shared_K': K.tolist(),
                          'calibration_sha256': hashlib.sha256(calibration.read_bytes()).hexdigest(),
                          'image_sha256': checksums,
                          'preprocessing': 'None: no resize, crop, rotation or second undistortion. '
                                           'Publisher states radial distortion is already removed.',
                          'pose_usage': 'dataset*.json excludes poses except explicitly named dataset_posed_*.json',
                          'pose_rotation_preprocessing': 'Nearest SO(3) rotation by SVD, preserving the original '
                              'camera centre -solve(raw_R,raw_t); raw dino_par.txt is unchanged.',
                          'raw_rotation_max_orthogonality_error': max(rotation_errors),
                          'selections': {name: [row['id'] for row in selected] for name, selected in selections.items()},
                          'band49_note': 'First capture band of full Dino; NOT the separately published DinoRing dataset',
                          'verification_scope': 'Local file integrity fingerprints and format validation; '
                                                'not an official archive signature or a reconstruction success claim',
                      }})
    for name, document in documents.items():
        target = root / name
        if target.exists() and json.loads(target.read_text(encoding='utf-8-sig')) != document:
            raise ValueError(f'Refusing to overwrite different existing data: {target}')
    for name, document in documents.items():
        target = root / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    summary = []
    for name in documents:
        if name.startswith('dataset'):
            data = load_dataset(root / name)
            poses = len(load_posed_dataset(root / name).poses) if name.startswith('dataset_posed_') else 0
            summary.append({'manifest': name, 'images': len(data['images']), 'published_poses': poses})
    return {'root': str(root), 'validated_images': 363, 'dimensions': [640, 480],
            'K': K.tolist(), 'manifests': summary}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1] / 'data/dino')
    print(json.dumps(prepare(parser.parse_args().root), ensure_ascii=False, indent=2))
