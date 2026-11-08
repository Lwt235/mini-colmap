"""CPU-only input identity and sparse reprojection self-consistency audit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'reports/3dgs-official-ablation-20261020'


def read(path):
    return json.loads(path.read_text())


def main():
    source = ROOT / 'outputs/ui_3dgs_20261019_230921_797f2a/source_model.json'
    model = read(source)
    dataset = read(ROOT / 'data/lerf_ovs/waldo_kitchen/dataset_posed_all.json')
    images = {row['id']: row for row in model['images']}
    poses = {row['image_id']: row for row in model['poses']}
    cameras = {row['id']: row for row in model['cameras']}
    other_images = {row['id']: row for row in dataset['images']}
    other_poses = {row['image_id']: row for row in dataset['poses']}
    other_cameras = {row['id']: row for row in dataset['cameras']}
    checks = {'same_image_ids': set(images) == set(other_images),
              'same_pose_ids': set(poses) == set(other_poses),
              'same_image_names': all(images[i]['name'] == other_images[i]['name'] for i in images),
              'same_camera_records': cameras == other_cameras,
              'same_poses': all(poses[i] == other_poses[i] for i in poses)}
    observed = {i: [] for i in images}
    for point in model['points']:
        for image_id, index in point['observations']:
            observed[image_id].append((point['xyz'], images[image_id]['keypoints'][index]))
    rows, all_errors, behind = [], [], 0
    for image_id, observations in observed.items():
        pose, camera = poses[image_id], cameras[images[image_id]['camera_id']]
        xyz = np.array([row[0] for row in observations])
        xy = np.array([row[1] for row in observations])
        view = xyz @ np.array(pose['R']).T + np.array(pose['t'])
        behind += int((view[:, 2] <= 0).sum())
        projected = view @ np.array(camera['K']).T
        errors = np.linalg.norm(projected[:, :2]/projected[:, 2:] - xy, axis=1)
        all_errors.extend(errors)
        rows.append({'image_id': image_id, 'name': images[image_id]['name'], 'observations': len(errors),
                     'median_error_px': float(np.median(errors)), 'mean_error_px': float(np.mean(errors)),
                     'p95_error_px': float(np.quantile(errors, .95))})
    errors = np.array(all_errors)
    result = {'scope': 'Sparse self-consistency in native pixels, not geometry ground truth or dense alignment.',
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'identity_checks': checks,
              'observations': len(errors), 'behind_camera': behind,
              'mean_error_px': float(errors.mean()), 'median_error_px': float(np.median(errors)),
              'p95_error_px': float(np.quantile(errors, .95)),
              'percent_over_2px': float(100*np.mean(errors > 2)), 'per_view': rows}
    (REPORT / 'source-geometry-audit.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({key: value for key, value in result.items() if key != 'per_view'}, indent=2))
    if not all(checks.values()):
        raise RuntimeError('Input identity differs; inspect audit before interpretation')


if __name__ == '__main__':
    main()
