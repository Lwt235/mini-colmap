"""Read-only observer and in-memory configuration contracts for the local UI."""

import copy
import json

import numpy as np
import pytest

from minicolmap import pipeline
from minicolmap.config import DEFAULT, load_config, resolve_config
from minicolmap.io import load_reconstruction, reconstruction_to_dict
from minicolmap.models import Camera, ImageRecord, Point3D, Pose


@pytest.fixture
def wired_scene(monkeypatch, tmp_path):
    """Mock algorithm boundaries, retaining validation, metrics and all exports."""
    ids = (11, 29, 53)
    K = np.array([[20., 0, 8], [0, 20., 6], [0, 0, 1]])
    xyz = np.column_stack((np.linspace(-.3, .3, 8), np.linspace(-.1, .2, 8), np.full(8, 5.)))
    poses = {image_id: Pose(np.eye(3), [-.1 * index, 0, 0]) for index, image_id in enumerate(ids)}
    keypoints = {}
    for image_id, pose in poses.items():
        h = (xyz @ pose.R.T + pose.t) @ K.T
        keypoints[image_id] = h[:, :2] / h[:, 2:]
    dataset = {
        'cameras': {7: Camera(7, 16, 12, K)},
        'images': {i: ImageRecord(i, f'{i}.png', 7) for i in ids},
        'image_paths': {i: tmp_path / f'{i}.png' for i in ids},
    }
    monkeypatch.setattr(pipeline, 'load_dataset', lambda path: copy.deepcopy(dataset))
    monkeypatch.setattr(pipeline, 'extract_features', lambda path, maximum: (
        keypoints[int(path.stem)].copy(), np.eye(8, 128, dtype=np.float32),
        np.full((12, 16, 3), [50, 100, 200], dtype=np.uint8)))
    indices = np.column_stack((np.arange(8), np.arange(8)))
    monkeypatch.setattr(pipeline, 'match_descriptors', lambda *args: indices.copy())
    monkeypatch.setattr(pipeline, 'ransac_fundamental', lambda *args, **kwargs: (np.eye(3), np.ones(8, bool)))
    monkeypatch.setattr(pipeline, 'load_rgb', lambda path: np.zeros((12, 16, 3), np.uint8))
    monkeypatch.setattr(pipeline, 'save_match_preview', lambda *args, **kwargs: None)

    def initialize(model, pairs, tracks, settings, colors, diagnostics):
        model.poses.update({i: copy.deepcopy(poses[i]) for i in ids[:2]})
        model.points.update({j + 100: Point3D(j + 100, p, [50, 100, 200],
                                             ((11, j), (29, j))) for j, p in enumerate(xyz)})
        diagnostics.append({'accepted': True})

    def register(model, tracks, settings):
        model.poses[53] = copy.deepcopy(poses[53])
        for j, point in enumerate(model.points.values()):
            point.observations += ((53, j),)
        return True

    monkeypatch.setattr(pipeline, 'initialize_map', initialize)
    monkeypatch.setattr(pipeline, 'register_next_image', register)
    monkeypatch.setattr(pipeline, 'triangulate_new_tracks', lambda *args: 0)
    settings = resolve_config({'geometry': {'min_inliers': 8}})
    return tmp_path / 'dataset.json', settings


def test_observer_counts_snapshots_and_original_result_compatibility(wired_scene, tmp_path):
    manifest, settings = wired_scene
    events = []
    plain = tmp_path / 'plain'
    watched = tmp_path / 'watched'
    baseline = pipeline.reconstruct(manifest, plain, settings)

    def observe(event):
        assert watched.is_dir()
        events.append(copy.deepcopy(event))
        if event['kind'] == 'snapshot':
            # Even topology and intrinsics in a snapshot must not alias the map.
            event['model']['points'][0]['xyz'][0] = 9999
            event['model']['points'][0]['observations'].clear()
            event['model']['cameras'][0]['K'][0][0] = -10
            event['model']['poses'].clear()

    result = pipeline.reconstruct(manifest, watched, settings, on_event=observe)
    assert {k: v for k, v in result.items() if k not in ('elapsed_seconds', 'timings')} == {
        k: v for k, v in baseline.items() if k not in ('elapsed_seconds', 'timings')}
    assert (watched / 'model.json').read_bytes() == (plain / 'model.json').read_bytes()
    assert (watched / 'points.ply').read_bytes() == (plain / 'points.ply').read_bytes()
    assert [e['stage'] for e in events if e['kind'] == 'stage'] == [
        'features', 'pair_geometry', 'tracks', 'initialization', 'incremental_mapping', 'export', 'done']
    for stage in ('features', 'pair_geometry'):
        progress = [e for e in events if e['kind'] == 'progress' and e['stage'] == stage]
        assert [(e['completed'], e['total']) for e in progress] == [(1, 3), (2, 3), (3, 3)]
    snapshots = [e for e in events if e['kind'] == 'snapshot']
    assert [e['label'] for e in snapshots] == ['initialization', 'registered_53', 'incremental_complete']
    assert [len(e['model']['poses']) for e in snapshots] == [2, 3, 3]
    assert all(e['model']['schema_version'] == 1 for e in snapshots)
    assert len(snapshots[0]['model']['points'][0]['observations']) == 2
    assert len(snapshots[-1]['model']['points'][0]['observations']) == 3
    assert events[-1] == {'kind': 'stage', 'stage': 'done', 'status': 'completed'}


def test_sparse_pair_gate_is_independent_from_initial_point_gate(wired_scene, tmp_path, monkeypatch):
    manifest, settings = wired_scene
    settings['geometry'].update(min_inliers=30, min_pair_inliers=8)
    original = pipeline.initialize_map
    received = []

    def inspect(model, pairs, tracks, config, colors, **kwargs):
        received.append((len(pairs), config['geometry']['min_inliers']))
        return original(model, pairs, tracks, config, colors, **kwargs)

    monkeypatch.setattr(pipeline, 'initialize_map', inspect)
    output = tmp_path / 'sparse-bridge'
    pipeline.reconstruct(manifest, output, settings)
    assert received == [(3, 30)]
    graph = json.loads((output / 'matching_graph.json').read_text())
    assert graph['pair_min_inliers'] == 8
    assert graph['seed_component_size'] == 3
    assert graph['unreachable_image_ids'] == []


def test_cli_path_does_not_serialize_intermediate_snapshots(wired_scene, tmp_path, monkeypatch):
    def forbidden(model):
        raise AssertionError('Snapshot serializer must stay unused without an observer')
    monkeypatch.setattr(pipeline, 'reconstruction_to_dict', forbidden)
    manifest, settings = wired_scene
    summary = pipeline.reconstruct(manifest, tmp_path / 'run', settings)
    assert summary['registered_images'] == 3


def test_failed_observer_is_disabled_and_preserves_algorithm_result(wired_scene, tmp_path, caplog):
    manifest, settings = wired_scene
    calls = []

    def broken(event):
        calls.append(event)
        raise RuntimeError('UI connection disappeared')

    assert pipeline.reconstruct(manifest, tmp_path / 'run', settings, on_event=broken)['point_count'] == 8
    assert len(calls) == 1
    assert 'disabling it' in caplog.text


def test_failure_reports_the_actual_stage_and_rethrows(wired_scene, tmp_path, monkeypatch):
    manifest, settings = wired_scene
    events = []

    def fail(*args, **kwargs):
        raise ValueError('No suitable initial image pair found')

    monkeypatch.setattr(pipeline, 'initialize_map', fail)
    with pytest.raises(ValueError, match='No suitable'):
        pipeline.reconstruct(manifest, tmp_path / 'failed', settings, on_event=events.append)
    assert events[-1] == {
        'kind': 'warning', 'stage': 'initialization', 'status': 'failed',
        'message': 'No suitable initial image pair found'}
    assert not any(e['kind'] == 'snapshot' for e in events)
    status = json.loads((tmp_path / 'failed' / 'run_status.json').read_text())
    assert status['stage'] == 'initialization' and status['status'] == 'failed'


def test_partial_registration_is_a_warning_not_a_failed_run(wired_scene, tmp_path, monkeypatch):
    manifest, settings = wired_scene
    monkeypatch.setattr(pipeline, 'register_next_image', lambda *args: False)
    events = []
    result = pipeline.reconstruct(manifest, tmp_path / 'partial', settings, on_event=events.append)
    assert result['unregistered_image_ids'] == [53]
    warning, = [e for e in events if e['kind'] == 'warning']
    assert warning['unregistered_image_ids'] == [53]
    assert events[-1]['status'] == 'completed'


def test_ba_snapshots_and_accepted_without_convergence_warning(wired_scene, tmp_path, monkeypatch):
    manifest, settings = wired_scene
    settings['bundle_adjustment']['enabled'] = True
    report = {'accepted': True, 'solver_success': False, 'nfev': 30,
              'before_mean_px': 0., 'candidate_mean_px': 0.,
              'message': 'The maximum number of function evaluations is exceeded.', 'rejection_reason': None}
    monkeypatch.setattr(pipeline, 'refine_model', lambda *args: report)
    events = []
    pipeline.reconstruct(manifest, tmp_path / 'ba', settings, on_event=events.append)
    labels = [e['label'] for e in events if e['kind'] == 'snapshot']
    assert labels[-2:] == ['before_ba', 'after_ba']
    warning, = [e for e in events if e['kind'] == 'warning']
    assert warning['stage'] == 'bundle_adjustment'
    assert warning['ba'] == report and warning['ba'] is not report
    assert 'not converged' in warning['message']


def test_rejected_match_pairs_still_report_actual_progress(wired_scene, tmp_path, monkeypatch):
    manifest, settings = wired_scene
    calls = 0

    def matches(*args):
        nonlocal calls
        calls += 1
        n = 7 if calls == 1 else 8
        return np.column_stack((np.arange(n), np.arange(n)))

    monkeypatch.setattr(pipeline, 'match_descriptors', matches)
    events = []
    pipeline.reconstruct(manifest, tmp_path / 'run', settings, on_event=events.append)
    pairs = [e for e in events if e['kind'] == 'progress' and e['stage'] == 'pair_geometry']
    assert [e['completed'] for e in pairs] == [1, 2, 3]
    assert pairs[0]['message'] == 'fewer than 8 mutual matches'


def test_in_memory_config_matches_json_validation_and_is_independent(tmp_path):
    supplied = {'seed': 23, 'features': {'ratio': .6}, 'bundle_adjustment': {'enabled': True}}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(supplied))
    config = resolve_config(supplied)
    assert config == load_config(path)
    config['features']['ratio'] = .2
    assert supplied['features']['ratio'] == .6
    assert resolve_config()['features']['ratio'] == DEFAULT['features']['ratio'] == .75
    for invalid in ([], {'unknown': {}}, {'features': {'ratio': 1}}, {'seed': True},
                    {'bundle_adjustment': {'max_nfev': 0}}):
        path.write_text(json.dumps(invalid))
        with pytest.raises(ValueError):
            resolve_config(invalid)
        with pytest.raises(ValueError):
            load_config(path)
    path.write_text('null')
    with pytest.raises(ValueError, match='JSON object'):
        load_config(path)


def test_snapshot_dict_matches_saved_schema(wired_scene, tmp_path):
    manifest, settings = wired_scene
    pipeline.reconstruct(manifest, tmp_path / 'run', settings)
    path = tmp_path / 'run' / 'model.json'
    assert reconstruction_to_dict(load_reconstruction(path)) == json.loads(path.read_text())


def test_pipeline_timings_cover_all_stages_and_matching_subtasks(wired_scene, tmp_path, monkeypatch):
    from test_timing import Clock
    clock = Clock()
    monkeypatch.setattr(pipeline, 'perf_counter', clock)
    for name, seconds in [('extract_features', 2), ('match_descriptors', .5),
                          ('ransac_fundamental', .25), ('initialize_map', 3),
                          ('register_next_image', 1), ('triangulate_new_tracks', .5)]:
        original = getattr(pipeline, name)

        def measured(*args, _original=original, _seconds=seconds, **kwargs):
            clock.advance(_seconds)
            return _original(*args, **kwargs)
        monkeypatch.setattr(pipeline, name, measured)
    manifest, settings = wired_scene
    output = tmp_path / 'timed'
    result = pipeline.reconstruct(manifest, output, settings)
    report = json.loads((output / 'timings.json').read_text())
    assert report == result['timings'] == json.loads((output / 'summary.json').read_text())['timings']
    assert set(report['stages_seconds']) == {'dataset_loading', 'features', 'pair_geometry',
                                            'tracks', 'initialization', 'incremental_mapping', 'export'}
    assert report['stages_seconds']['features'] == 6
    assert report['stages_seconds']['pair_geometry'] == 2.25
    assert report['stages_seconds']['initialization'] == 3
    assert report['stages_seconds']['incremental_mapping'] == 1.5
    assert report['details']['descriptor_matching'] == {'calls': 3, 'seconds': 1.5}
    assert report['details']['fundamental_ransac'] == {'calls': 3, 'seconds': .75}
    assert report['total_seconds'] == result['elapsed_seconds'] == sum(report['stages_seconds'].values())


def test_failed_pipeline_persists_elapsed_stage_and_rethrows(wired_scene, tmp_path, monkeypatch):
    from test_timing import Clock
    clock = Clock()
    monkeypatch.setattr(pipeline, 'perf_counter', clock)

    def fail(*args, **kwargs):
        clock.advance(4)
        raise ValueError('No suitable initial image pair found')
    monkeypatch.setattr(pipeline, 'initialize_map', fail)
    manifest, settings = wired_scene
    with pytest.raises(ValueError, match='No suitable'):
        pipeline.reconstruct(manifest, tmp_path / 'failed_timed', settings)
    report = json.loads((tmp_path / 'failed_timed' / 'timings.json').read_text())
    assert report['status'] == 'failed' and report['total_seconds'] == 4
    assert report['stages_seconds']['initialization'] == 4
    assert 'export' not in report['stages_seconds']


def test_ba_timing_is_present_only_when_executed(wired_scene, tmp_path, monkeypatch):
    from test_timing import Clock
    clock = Clock()
    monkeypatch.setattr(pipeline, 'perf_counter', clock)

    def ba(*args):
        clock.advance(7)
        return {'accepted': True, 'solver_success': True}
    monkeypatch.setattr(pipeline, 'refine_model', ba)
    manifest, settings = wired_scene
    settings['bundle_adjustment']['enabled'] = True
    result = pipeline.reconstruct(manifest, tmp_path / 'timed_ba', settings)
    assert result['timings']['stages_seconds']['bundle_adjustment'] == 7


def test_timing_counts_skipped_and_failed_pair_attempts(wired_scene, tmp_path, monkeypatch):
    manifest, settings = wired_scene
    matches, ransac = pipeline.match_descriptors, pipeline.ransac_fundamental
    calls = {'match': 0, 'ransac': 0}

    def match(*args):
        calls['match'] += 1
        return np.zeros((7, 2), int) if calls['match'] == 1 else matches(*args)

    def estimate(*args, **kwargs):
        calls['ransac'] += 1
        if calls['ransac'] == 1:
            raise ValueError('degenerate pair')
        return ransac(*args, **kwargs)
    monkeypatch.setattr(pipeline, 'match_descriptors', match)
    monkeypatch.setattr(pipeline, 'ransac_fundamental', estimate)
    result = pipeline.reconstruct(manifest, tmp_path / 'rejected_timed', settings)
    assert result['timings']['details']['descriptor_matching']['calls'] == 3
    assert result['timings']['details']['fundamental_ransac']['calls'] == 2


@pytest.mark.parametrize('legacy', [False, True])
def test_pipeline_passes_and_saves_adaptive_ransac_options(wired_scene, tmp_path, monkeypatch, legacy):
    manifest, settings = wired_scene
    settings['geometry'].update(confidence=.975, min_trials=37)
    if legacy:
        settings['geometry'].pop('confidence')
        settings['geometry'].pop('min_trials')
    calls = []

    def estimate(a, b, threshold_px, max_trials, seed, *, confidence, min_trials):
        calls.append((threshold_px, max_trials, seed, confidence, min_trials))
        return np.eye(3), np.ones(len(a), bool)
    monkeypatch.setattr(pipeline, 'ransac_fundamental', estimate)
    pipeline.reconstruct(manifest, tmp_path / 'adaptive_config', settings)
    assert calls == [(1.5, 2000, 0, .99 if legacy else .975, 20 if legacy else 37)] * 3
    saved = json.loads((tmp_path / 'adaptive_config' / 'config.json').read_text())
    if not legacy:
        assert saved['geometry']['confidence'] == .975
        assert saved['geometry']['min_trials'] == 37
