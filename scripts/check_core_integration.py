"""Deterministic geometry/mapper/BA acceptance probe; known matches, no SIFT.

GT creates pixels and scores recovered poses; it never enters estimation.
Initial poses/points are estimated by G5/G6/G8/M1 and two-view refinement;
all subsequent poses and points come from M2/M3, followed by the real B1.
The first two views see 60 points; later views see 120, forcing M3 to add points.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from minicolmap.config import load_config
from minicolmap.geometry import ransac_fundamental
from minicolmap.mapper import VerifiedPair, initialize_map, register_next_image, triangulate_new_tracks
from minicolmap.metrics import reconstruction_summary
from minicolmap.models import Camera, ImageRecord, Reconstruction, validate_reconstruction
from minicolmap.optimization import refine_model
from minicolmap.tracks import build_tracks


def check(noise_px: float, *, seed: int = 73, ba_max_nfev: int | None = None,
          require_ba_convergence: bool = False) -> dict:
    if not np.isfinite(noise_px) or noise_px < 0:
        raise ValueError('noise_px must be finite and nonnegative')
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError('seed must be a nonnegative integer')
    if ba_max_nfev is not None and (isinstance(ba_max_nfev, bool)
                                  or not isinstance(ba_max_nfev, int) or ba_max_nfev < 1):
        raise ValueError('ba_max_nfev must be a positive integer')
    rng = np.random.default_rng(seed)
    truth_points = rng.uniform([-.55, -.5, 5.], [.55, .5, 8.], (120, 3))
    K = np.array([[800., 0., 320.], [0., 800., 240.], [0., 0., 1.]])
    centers = [-.7, .3, -.45, -.2, 0., .5, .65, .8]
    ids = [3, 11, 27, 41, 59, 73, 89, 103]
    model = Reconstruction(cameras={1: Camera(1, 640, 480, K)})
    colors = {}
    truth_poses = {}
    rgb = rng.integers(32, 256, (120, 3), dtype=np.uint8)
    for k, (image_id, cx) in enumerate(zip(ids, centers)):
        truth_R = Rotation.from_rotvec([0., cx * .08, 0.]).as_matrix()
        truth_t = -truth_R @ np.array([cx, .03 * np.sin(k), 0.])
        truth_poses[image_id] = (truth_R, truth_t)
        count = 60 if k < 2 else 120
        h = (truth_points[:count] @ truth_R.T + truth_t) @ K.T
        pixels = h[:, :2] / h[:, 2:] + rng.normal(0., noise_px, (count, 2))
        model.images[image_id] = ImageRecord(image_id, f'{image_id}.png', 1, pixels)
        colors[image_id] = rgb[:count].copy()

    result = {'seed': seed, 'noise_px': noise_px, 'input_images': 8,
              'input_scene_points': 120, 'known_correspondences': True,
              'stages': [], 'warnings': []}
    stage = 'pair_geometry'
    try:
        matches = {}
        initial = None
        for a, b in zip(ids, ids[1:]):
            count = min(len(model.images[a].keypoints), len(model.images[b].keypoints))
            F, mask = ransac_fundamental(model.images[a].keypoints[:count],
                                         model.images[b].keypoints[:count], 1.5, 200, 0)
            indices = np.column_stack([np.arange(count), np.arange(count)])[mask]
            matches[a, b] = indices
            if a == ids[0]:
                initial = VerifiedPair(a, b, indices, F)
        tracks = build_tracks(matches)
        settings = load_config()
        if ba_max_nfev is not None:
            settings['bundle_adjustment']['max_nfev'] = ba_max_nfev
        result['ba_max_nfev'] = settings['bundle_adjustment']['max_nfev']
        stage = 'initialization'
        initialization = {'candidates': []}
        result['initialization'] = initialization
        initialize_map(model, [initial], tracks, settings, colors,
                       diagnostics=initialization['candidates'])
        anchors = tuple(model.poses)
        initialization['summary'] = reconstruction_summary(model)
        first, second = (model.poses[image_id] for image_id in anchors)
        initialization['gauge'] = {
            'first_rotation_identity_error': float(np.linalg.norm(first.R - np.eye(3))),
            'first_translation_norm': float(np.linalg.norm(first.t)),
            'second_translation_norm': float(np.linalg.norm(second.t)),
        }
        # GT is used only here, after M1, to evaluate relative pose accuracy.
        true_r1, true_t1 = truth_poses[anchors[0]]
        true_r2, true_t2 = truth_poses[anchors[1]]
        true_R = true_r2 @ true_r1.T
        true_t = true_t2 - true_R @ true_t1
        estimated_R = second.R @ first.R.T
        estimated_t = second.t - estimated_R @ first.t
        cosine = (estimated_t @ true_t) / (np.linalg.norm(estimated_t) * np.linalg.norm(true_t))
        initialization['ground_truth_errors'] = {
            'rotation_deg': float(np.rad2deg(Rotation.from_matrix(estimated_R @ true_R.T).magnitude())),
            'translation_direction_deg': float(np.rad2deg(np.arccos(np.clip(cosine, -1., 1.)))),
        }
        result['stages'].append({'registered': len(model.poses), 'points': len(model.points)})
        stage = 'incremental_mapping'
        while len(model.poses) < len(ids):
            if not register_next_image(model, tracks, settings):
                break
            added = triangulate_new_tracks(model, tracks, settings, colors)
            validate_reconstruction(model)
            result['stages'].append({'registered': len(model.poses), 'points': len(model.points),
                                     'new_points': added})
        result['before_ba'] = reconstruction_summary(model)
        if len(model.poses) != len(ids):
            raise ValueError('Not all eight cameras registered')
        if len(model.points) <= result['stages'][0]['points']:
            raise ValueError('No new points were triangulated after initialization')
        stage = 'bundle_adjustment'
        result['ba'] = refine_model(model, anchors, settings['bundle_adjustment'])
        result['after_ba'] = reconstruction_summary(model)
        if result['after_ba']['positive_depth_ratio'] != 1.:
            raise ValueError('Final map has nonpositive-depth observations')
        if not result['ba']['accepted']:
            raise ValueError('BA candidate was rejected; inspect the BA report')
        if not result['ba']['solver_success']:
            result['warnings'].append('BA candidate accepted, but the solver did not converge')
            if require_ba_convergence:
                raise ValueError('BA convergence required, but solver_success is false')
        result.update(status='passed', stage='done')
    except ValueError as exc:
        result.update(status='failed', stage=stage, error=str(exc))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--noise-px', type=float, default=0.)
    parser.add_argument('--seed', type=int, default=73, help='Synthetic scene/noise seed')
    parser.add_argument('--ba-max-nfev', type=int, help='Override BA budget (default: project config)')
    parser.add_argument('--require-ba-convergence', action='store_true',
                        help='Fail if BA is accepted but has not converged')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not np.isfinite(args.noise_px) or args.noise_px < 0 or args.seed < 0:
        parser.error('--noise-px must be finite/nonnegative and --seed nonnegative')
    if args.ba_max_nfev is not None and args.ba_max_nfev < 1:
        parser.error('--ba-max-nfev must be positive')
    result = check(args.noise_px, seed=args.seed, ba_max_nfev=args.ba_max_nfev,
                   require_ba_convergence=args.require_ba_convergence)
    text = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
    if args.output:
        if args.output.exists():
            parser.error('--output already exists; choose a new filename')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
