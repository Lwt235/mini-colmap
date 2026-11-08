"""Incremental SfM orchestration using the project's geometry and mapping algorithms."""
import json
import logging
from itertools import combinations
from pathlib import Path
from time import perf_counter
from typing import Callable

import numpy as np

from .config import DEFAULT, PipelineConfig
from .export import write_colmap_text, write_ply
from .frontend import extract_features, load_rgb, match_descriptors, save_match_preview
from .geometry import ransac_fundamental
from .io import load_dataset, reconstruction_to_dict, save_reconstruction
from .mapper import VerifiedPair, initialize_map, register_next_image, triangulate_new_tracks
from .metrics import reconstruction_summary
from .models import Reconstruction, validate_reconstruction
from .optimization import refine_model
from .tracks import DSU, build_tracks
from .timing import PipelineTimer


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _validate_map(model):
    validate_reconstruction(model)
    if any(len(point.observations) < 2 for point in model.points.values()):
        raise ValueError('Every mapped point needs at least two registered image observations')


def matching_components(image_ids, pairs):
    """Connected image sets, including isolated images, using admitted edges only."""
    graph = DSU()
    for pair in pairs:
        graph.union(pair.image_id1, pair.image_id2)
    components = {}
    for image_id in sorted(image_ids):
        components.setdefault(graph.find(image_id), []).append(image_id)
    return sorted(components.values(), key=lambda ids: (-len(ids), ids))


def reconstruct(dataset_path, output, settings: PipelineConfig, *,
                on_event: Callable[[dict], None] | None = None):
    """Run incremental SfM with optional read-only progress notifications.

    A fresh output directory is required. Notifications contain stage names,
    actual completed/total counts, and independent serialized model snapshots.
    An observer that raises is logged and disabled; it cannot fail a reconstruction.
    With no observer, intermediate models are not serialized.
    """
    timer = PipelineTimer(clock=perf_counter)
    dataset = load_dataset(dataset_path)
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f'Output directory is not empty; choose a new run directory: {output}')
    output.mkdir(parents=True, exist_ok=True)
    timer.stage('features')
    status = {'status': 'running', 'stage': 'features', 'dataset': str(Path(dataset_path).resolve())}

    def notify(kind, **values):
        nonlocal on_event
        if on_event is not None:
            try:
                on_event({'kind': kind, 'stage': status['stage'], **values})
            except Exception:
                logging.getLogger(__name__).warning('Reconstruction observer failed; disabling it', exc_info=True)
                on_event = None

    def stage(name):
        previous = status['stage']
        timer.stage(name)
        report = timer.report()
        _write_json(output / 'timings.json', report)
        notify('timing', message=f"{previous}: {report['stages_seconds'][previous]:.3f} s")
        status['stage'] = name
        _write_json(output / 'run_status.json', status)
        notify('stage', status=status['status'])

    def snapshot(label):
        if on_event is not None:
            notify('snapshot', label=label, model=reconstruction_to_dict(model))

    _write_json(output / 'config.json', settings)
    _write_json(output / 'run_status.json', status)
    _write_json(output / 'timings.json', timer.report())
    notify('stage', status='running')
    try:
        model = Reconstruction(cameras=dataset['cameras'], images=dataset['images'], poses={}, points={})
        descriptors, colors = {}, {}
        (output / 'features').mkdir()
        for completed, (image_id, image) in enumerate(sorted(model.images.items()), 1):
            xy, desc, rgb = extract_features(dataset['image_paths'][image_id], settings['features']['max_features'])
            camera = model.cameras[image.camera_id]
            if rgb.shape[:2] != (camera.height, camera.width):
                raise ValueError(f'Image {image_id} dimensions do not match its Camera; update K and manifest')
            image.keypoints = xy
            descriptors[image_id] = desc
            ij = np.rint(xy).astype(int)
            ij[:, 0] = np.clip(ij[:, 0], 0, camera.width - 1)
            ij[:, 1] = np.clip(ij[:, 1], 0, camera.height - 1)
            colors[image_id] = rgb[ij[:, 1], ij[:, 0]]
            np.savez_compressed(output / 'features' / f'{image_id}.npz', keypoints=xy, descriptors=desc, colors=colors[image_id])
            notify('progress', completed=completed, total=len(model.images), image_id=image_id,
                   message=f'Extracted {len(xy)} features from image {image_id}')
        stage('pair_geometry')
        pairs, pair_report = [], []
        (output / 'matches').mkdir()
        pair_total = len(model.images) * (len(model.images) - 1) // 2
        for completed, (i, j) in enumerate(combinations(sorted(model.images), 2), 1):
            with timer.measure('descriptor_matching'):
                indices = match_descriptors(descriptors[i], descriptors[j], settings['features']['ratio'])
            item = {'images': [i, j], 'candidates': len(indices), 'inliers': 0}
            if len(indices) < 8:
                item['reason'] = 'fewer than 8 mutual matches'
                pair_report.append(item)
                notify('progress', completed=completed, total=pair_total, image_ids=[i, j],
                       message=item['reason'])
                continue
            xy1, xy2 = model.images[i].keypoints[indices[:, 0]], model.images[j].keypoints[indices[:, 1]]
            try:
                with timer.measure('fundamental_ransac'):
                    F, mask = ransac_fundamental(xy1, xy2, settings['geometry']['threshold_px'],
                                                  settings['geometry']['max_trials'], settings['seed'],
                                                  confidence=settings['geometry'].get('confidence', DEFAULT['geometry']['confidence']),
                                                  min_trials=settings['geometry'].get('min_trials', DEFAULT['geometry']['min_trials']))
            except ValueError as exc:
                item['reason'] = str(exc)
                pair_report.append(item)
                notify('progress', completed=completed, total=pair_total, image_ids=[i, j],
                       message=item['reason'])
                continue
            inliers = indices[mask]
            item['inliers'] = len(inliers)
            np.savez_compressed(output / 'matches' / f'{i}_{j}.npz', candidates=indices, inlier_mask=mask, F=F)
            pair_min = settings['geometry'].get('min_pair_inliers', settings['geometry']['min_inliers'])
            if len(inliers) >= pair_min:
                pairs.append(VerifiedPair(i, j, inliers, F))
                if len(pairs) <= 10:
                    rgb1 = load_rgb(dataset['image_paths'][i])
                    rgb2 = load_rgb(dataset['image_paths'][j])
                    save_match_preview(output / 'matches' / f'{i}_{j}.png', rgb1, rgb2,
                                       model.images[i].keypoints, model.images[j].keypoints, inliers)
            else:
                item['reason'] = 'inlier support below configured minimum'
            pair_report.append(item)
            notify('progress', completed=completed, total=pair_total, image_ids=[i, j],
                   message=f'Pair {i}, {j}: {len(inliers)} inliers')
        _write_json(output / 'pairs.json', pair_report)
        stage('tracks')
        tracks = build_tracks({(p.image_id1, p.image_id2): p.indices for p in pairs},
                              conflict_policy=settings['mapping'].get('track_conflict_policy', 'reject'))
        components = matching_components(model.images, pairs)
        graph_report = {'pair_min_inliers': settings['geometry'].get('min_pair_inliers', settings['geometry']['min_inliers']),
                        'track_conflict_policy': settings['mapping'].get('track_conflict_policy', 'reject'),
                        'accepted_pairs': len(pairs), 'track_count': len(tracks),
                        'component_sizes': [len(ids) for ids in components], 'components': components}
        _write_json(output / 'matching_graph.json', graph_report)
        notify('progress', completed=len(tracks), total=len(tracks), message=f'Built {len(tracks)} tracks')
        stage('initialization')
        initialization_report = []
        try:
            initialize_map(model, pairs, tracks, settings, colors, diagnostics=initialization_report)
        finally:
            _write_json(output / 'initialization.json', {'candidates': initialization_report})
        _validate_map(model)
        if len(model.poses) != 2 or not model.points:
            raise ValueError('M1 must initialize exactly two cameras and a nonempty valid map')
        snapshot('initialization')
        anchors = tuple(model.poses)
        reachable = next(ids for ids in components if anchors[0] in ids)
        graph_report.update(seed_image_ids=list(anchors), seed_component_size=len(reachable),
                            unreachable_image_ids=sorted(set(model.images) - set(reachable)))
        _write_json(output / 'matching_graph.json', graph_report)
        if len(reachable) < len(model.images):
            notify('warning', message=f'Initial map can reach {len(reachable)}/{len(model.images)} images in the verified matching graph',
                   unreachable_image_ids=graph_report['unreachable_image_ids'])
        stage('incremental_mapping')
        notify('progress', completed=len(model.poses), total=len(model.images),
               message=f'Registered {len(model.poses)} images')
        while len(model.poses) < len(model.images):
            before = set(model.poses)
            progressed = register_next_image(model, tracks, settings)
            after = set(model.poses)
            if not progressed:
                if after != before:
                    raise ValueError('M2 changed poses despite reporting no progress')
                break
            if not before < after or len(after - before) != 1:
                raise ValueError('M2 must add exactly one pose per successful registration')
            triangulate_new_tracks(model, tracks, settings, colors)
            _validate_map(model)
            image_id = next(iter(after - before))
            notify('progress', completed=len(model.poses), total=len(model.images), image_id=image_id,
                   message=f'Registered image {image_id}; {len(model.points)} map points')
            snapshot(f'registered_{image_id}')
        snapshot('incremental_complete')
        if len(model.poses) < len(model.images):
            notify('warning', message='Some images could not be registered',
                   unregistered_image_ids=sorted(set(model.images) - set(model.poses)))
        summary = reconstruction_summary(model)
        if summary['positive_depth_ratio'] != 1.0:
            raise ValueError('Map contains points behind registered cameras; repair M1-M3 filters')
        if settings['bundle_adjustment']['enabled']:
            stage('bundle_adjustment')
            snapshot('before_ba')
            ba_report = refine_model(model, anchors, settings['bundle_adjustment'])
            _write_json(output / 'ba.json', ba_report)
            snapshot('after_ba')
            if ba_report['accepted'] and not ba_report['solver_success']:
                notify('warning', message='BA candidate accepted, but solver has not converged',
                       ba=ba_report.copy())
            elif not ba_report['accepted']:
                notify('warning', message='BA candidate rejected; retaining the previous map',
                       ba=ba_report.copy())
        stage('export')
        save_reconstruction(model, output / 'model.json')
        write_ply(model, output / 'points.ply')
        write_colmap_text(model, output / 'sparse' / '0')
        summary = reconstruction_summary(model)
        summary['unregistered_image_ids'] = sorted(set(model.images) - set(model.poses))
        timer.finish('completed')
        summary['timings'] = timer.report()
        summary['elapsed_seconds'] = summary['timings']['total_seconds']
        summary['tracks'] = len(tracks)
        _write_json(output / 'summary.json', summary)
        _write_json(output / 'timings.json', summary['timings'])
        notify('timing', message=f"export: {summary['timings']['stages_seconds']['export']:.3f} s; total: {summary['elapsed_seconds']:.3f} s")
        status.update(status='completed', stage='done', elapsed_seconds=summary['elapsed_seconds'])
        _write_json(output / 'run_status.json', status)
        notify('stage', status='completed')
        return summary
    except Exception as exc:
        timer.finish('failed')
        report = timer.report()
        _write_json(output / 'timings.json', report)
        status.update(status='todo' if isinstance(exc, NotImplementedError) else 'failed',
                      error=str(exc), elapsed_seconds=report['total_seconds'])
        _write_json(output / 'run_status.json', status)
        notify('warning', status=status['status'], message=str(exc))
        raise

