"""M1 upper-bound pruning acceptance: unchanged selection, less actual work.

Control cases replace geometric subroutines, not initialize_map. Real geometry
cases use independent projections and the actual G6/G7/G8/refinement code.
No datasets, saved user outputs, elapsed-time assertions, skips or xfails.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from minicolmap import mapper
from minicolmap.config import resolve_config
from minicolmap.io import reconstruction_to_dict
from minicolmap.models import Camera, ImageRecord, Reconstruction, validate_reconstruction


@dataclass
class Case:
    count: int
    depth: float = 6.
    tracked: int | None = None
    bad_pixels: int = 0
    recover_error: bool = False


def _pixels(xyz, K, R, t):
    camera = xyz @ R.T + t
    homogeneous = camera @ K.T
    return homogeneous[:, :2] / homogeneous[:, 2:]


def _settings():
    return resolve_config({'geometry': {'min_inliers': 8},
                           'mapping': {'min_angle_deg': 1., 'max_reprojection_px': .01}})


def _controlled(monkeypatch, cases):
    """Make real record topology, but prescribe the geometric outcomes of M1."""
    model, pairs, tracks, colors = Reconstruction(), [], [], {}
    K = np.array([[500., 0, 320], [0, 510., 240], [0, 0, 1.]])
    t = np.array([-1., 0, 0])
    clouds, case_ids = {}, []
    for index, case in enumerate(cases):
        # Non-contiguous IDs, with first > second. IDs are not list offsets.
        i, j = 101 + 20 * index, 7 + 20 * index
        case_ids.append((i, j))
        row = np.arange(case.count)
        xyz = np.column_stack(((row % 5) * .07 - .14,
                               (row // 5) * .05 - .1,
                               case.depth + (row % 3) * .02))
        clouds[index + 1] = xyz
        first = _pixels(xyz, K, np.eye(3), np.zeros(3))
        second = _pixels(xyz, K, np.eye(3), t)
        if case.bad_pixels:
            second[-case.bad_pixels:, 0] += 35.
        for image_id, pixels in ((i, first), (j, second)):
            camera_id = 1000 + image_id
            model.cameras[camera_id] = Camera(camera_id, 640, 480, K.copy())
            model.images[image_id] = ImageRecord(image_id, f'{image_id}.png', camera_id, pixels)
            colors[image_id] = np.column_stack(((row * 7) % 256, (row * 11) % 256,
                                                (row * 13) % 256)).astype(np.uint8)
        # A marker only: G6/G8 are controlled in this group, never in real_geometry.
        F = np.array([[float(index + 1), 0, 0], [0, 0, -1.], [0, 1., 0]])
        pairs.append(mapper.VerifiedPair(i, j, np.column_stack((row, row)), F))
        tracked = case.count if case.tracked is None else case.tracked
        tracks.extend(((i, feature), (j, feature)) for feature in range(tracked))

    calls = {name: [] for name in ('essential', 'pose', 'triangulate', 'refine')}
    current = [None]

    def record(stage, token):
        assert not model.poses and not model.points, 'A tentative candidate must not mutate the map'
        calls[stage].append(case_ids[token - 1])

    def essential(F, K1, K2):
        record('essential', int(F[0, 0]))
        return F.copy()

    def recover(E, pixels1, pixels2, K1, K2):
        token = int(E[0, 0])
        current[0] = token
        record('pose', token)
        if cases[token - 1].recover_error:
            raise ValueError('Controlled unusable pose')
        return np.eye(3), t.copy(), np.ones(len(pixels1), dtype=bool)

    def triangulate(P1, P2, pixels1, pixels2):
        token = current[0]
        record('triangulate', token)
        assert len(pixels1) == len(clouds[token])
        return clouds[token].copy()

    def refine(R, translation, xyz, K1, K2, pixels1, pixels2, **kwargs):
        record('refine', current[0])
        report = {'accepted': False, 'solver_success': False, 'nfev': 1,
                  'before_mean_px': 0., 'candidate_mean_px': None,
                  'message': 'Controlled unchanged seed', 'rejection_reason': 'Controlled rejection'}
        # Returning an unchanged seed on rejection is the optimizer's contract.
        return R.copy(), translation.copy(), xyz.copy(), report

    monkeypatch.setattr(mapper, 'essential_from_fundamental', essential)
    monkeypatch.setattr(mapper, 'recover_relative_pose', recover)
    monkeypatch.setattr(mapper, 'triangulate_points', triangulate)
    monkeypatch.setattr(mapper, 'refine_two_view', refine)
    monkeypatch.setattr(mapper, 'sampson_distance', lambda F, a, b: np.zeros(len(a)))
    return SimpleNamespace(model=model, pairs=pairs, tracks=tracks, colors=colors,
                           settings=_settings(), calls=calls, ids=case_ids)


def _run(harness, diagnostics=None):
    args = (harness.model, harness.pairs, harness.tracks, harness.settings, harness.colors)
    if diagnostics is None:
        return mapper.initialize_map(*args)
    return mapper.initialize_map(*args, diagnostics=diagnostics)


def _assert_winner(harness, index, count):
    model = harness.model
    assert tuple(model.poses) == harness.ids[index]
    assert len(model.points) == count
    validate_reconstruction(model)
    first, second = harness.ids[index]
    np.testing.assert_allclose(model.poses[first].R, np.eye(3), atol=1e-12)
    np.testing.assert_allclose(model.poses[first].t, 0., atol=1e-12)
    assert np.linalg.norm(model.poses[second].t) == pytest.approx(1.)
    for point in model.points.values():
        assert tuple(image_id for image_id, _ in point.observations) == (first, second)
        for image_id, feature in point.observations:
            image, pose = model.images[image_id], model.poses[image_id]
            K = model.cameras[image.camera_id].K
            assert (pose.R @ point.xyz + pose.t)[2] > 0
            np.testing.assert_allclose(_pixels(point.xyz[None], K, pose.R, pose.t)[0],
                                       image.keypoints[feature], atol=1e-8)
        np.testing.assert_array_equal(point.color, harness.colors[first][point.observations[0][1]])


@pytest.mark.parametrize('diagnostics_enabled', [False, True])
@pytest.mark.parametrize('stage', ['essential', 'pose', 'triangulate', 'refine'])
def test_prunes_before_expensive_work(monkeypatch, diagnostics_enabled, stage):
    h = _controlled(monkeypatch, [Case(40), Case(24), Case(16), Case(12), Case(8)])
    diagnostics = [] if diagnostics_enabled else None
    assert _run(h, diagnostics) is None
    _assert_winner(h, 0, 40)
    assert h.calls[stage] == [h.ids[0]], (
        f'{stage}: candidates with upper bound below 40 should not reach expensive geometry; '
        f'actual calls: {h.calls[stage]}'
    )


def test_pruned_diagnostics_record_bound_without_fabricated_geometry(monkeypatch):
    h = _controlled(monkeypatch, [Case(24), Case(12), Case(8)])
    diagnostics = []
    _run(h, diagnostics)
    assert len(diagnostics) == len(h.pairs)
    by_pair = {tuple(row['images']): row for row in diagnostics}
    assert len(by_pair) == len(h.pairs)
    assert sum(bool(row['selected']) for row in diagnostics) == 1
    assert by_pair[h.ids[0]]['selected'] is True
    for index in (1, 2):
        row = by_pair[h.ids[index]]
        assert row.get('skipped_by_upper_bound') is True, 'Use a structured skip flag, not a fake geometry failure'
        assert row['upper_bound_points'] == len(h.pairs[index].indices)
        assert row['best_points_at_prune'] == 24
        assert row['upper_bound_points'] < row['best_points_at_prune']
        assert row['selected'] is False and row.get('rejection_reason')
        assert 'refinement' not in row
        assert 'seed_positive_points' not in row
    json.dumps(diagnostics, allow_nan=False)  # Must remain writable by the existing pipeline/UI.


@pytest.mark.parametrize('depths,expected', [((8., 4.), 1), ((4., 8.), 0)])
def test_equal_upper_bound_still_compares_parallax(monkeypatch, depths, expected):
    h = _controlled(monkeypatch, [Case(12, depth=depths[0]), Case(12, depth=depths[1])])
    _run(h)
    _assert_winner(h, expected, 12)
    assert set(h.calls['refine']) == set(h.ids), 'M == B can still win on the angle tie-break'


@pytest.mark.parametrize('counts', [(12, 20), (20, 12)])
def test_exact_score_tie_uses_original_input_order_even_if_sorted(monkeypatch, counts):
    # First 10 points/rays are bit-identical; extra untracked matches change M,
    # but do not change the final score (10, median_angle).
    h = _controlled(monkeypatch, [Case(counts[0], tracked=10), Case(counts[1], tracked=10)])
    _run(h)
    _assert_winner(h, 0, 10)


def test_incumbent_is_final_unique_track_count_not_raw_match_count(monkeypatch):
    h = _controlled(monkeypatch, [Case(24, tracked=10), Case(14), Case(12)])
    _run(h)
    _assert_winner(h, 1, 14)


def test_incumbent_is_after_final_reprojection_filter(monkeypatch):
    h = _controlled(monkeypatch, [Case(24, bad_pixels=14), Case(14), Case(12)])
    _run(h)
    _assert_winner(h, 1, 14)


def test_best_bound_never_decreases_after_a_worse_candidate(monkeypatch):
    h = _controlled(monkeypatch, [Case(20), Case(24, tracked=8), Case(16)])
    _run(h)
    _assert_winner(h, 0, 20)
    assert h.ids[2] not in h.calls['essential'], (
        'The valid 20-point incumbent must survive an evaluated candidate with only 8 tracks'
    )


@pytest.mark.parametrize('failure', ['recover', 'parallax', 'track_gate'])
def test_failed_large_candidate_cannot_establish_incumbent(monkeypatch, failure):
    first = {'recover': Case(32, recover_error=True),
             'parallax': Case(32, depth=1000.),
             'track_gate': Case(32, tracked=7)}[failure]
    h = _controlled(monkeypatch, [first, Case(12)])
    _run(h)
    _assert_winner(h, 1, 12)


def test_late_valid_candidate_is_not_lost_to_fixed_top_k(monkeypatch):
    # More than 10 larger candidates fail. No valid incumbent exists yet.
    cases = [Case(40 + i, recover_error=True) for i in range(14)] + [Case(8)]
    h = _controlled(monkeypatch, cases)
    _run(h)
    _assert_winner(h, 14, 8)
    assert h.ids[-1] in h.calls['refine']


def test_all_failures_preserve_empty_map_and_diagnostics(monkeypatch):
    h = _controlled(monkeypatch, [Case(24, recover_error=True), Case(16, recover_error=True),
                                Case(12, recover_error=True)])
    before = reconstruction_to_dict(h.model)
    diagnostics = []
    with pytest.raises(ValueError, match='No suitable initial image pair'):
        _run(h, diagnostics)
    assert reconstruction_to_dict(h.model) == before
    assert len(diagnostics) == 3 and not any(row['selected'] for row in diagnostics)
    assert not any(row.get('skipped_by_upper_bound', False) for row in diagnostics)


def test_caller_inputs_are_not_sorted_or_modified_in_place(monkeypatch):
    h = _controlled(monkeypatch, [Case(12), Case(20, tracked=8)])
    original_pairs = tuple(h.pairs)
    pair_arrays = [(pair.indices.copy(), pair.F.copy()) for pair in h.pairs]
    tracks, colors, settings = copy.deepcopy((h.tracks, h.colors, h.settings))
    images = {image_id: image.keypoints.copy() for image_id, image in h.model.images.items()}
    intrinsics = {camera_id: camera.K.copy() for camera_id, camera in h.model.cameras.items()}
    _run(h)
    assert len(h.pairs) == len(original_pairs)
    assert all(actual is expected for actual, expected in zip(h.pairs, original_pairs))
    for pair, (indices, F) in zip(h.pairs, pair_arrays):
        np.testing.assert_array_equal(pair.indices, indices)
        np.testing.assert_array_equal(pair.F, F)
    assert h.tracks == tracks and h.settings == settings
    for image_id, image in h.model.images.items():
        np.testing.assert_array_equal(image.keypoints, images[image_id])
        np.testing.assert_array_equal(h.colors[image_id], colors[image_id])
    for camera_id, camera in h.model.cameras.items():
        np.testing.assert_array_equal(camera.K, intrinsics[camera_id])


def _real_fixture(seed):
    rng = np.random.default_rng(seed)
    model, tracks, pairs, colors = Reconstruction(), [], [], {}
    K1 = np.array([[800., 0, 320], [0, 810., 240], [0, 0, 1.]])
    K2 = np.array([[760., 0, 310], [0, 780., 245], [0, 0, 1.]])
    theta = .04
    R = np.array([[np.cos(theta), 0, np.sin(theta)], [0, 1., 0],
                  [-np.sin(theta), 0, np.cos(theta)]])
    t = np.array([-1., .02, .01])
    t /= np.linalg.norm(t)
    x, y, z = t
    skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    F = np.linalg.inv(K2).T @ skew @ R @ np.linalg.inv(K1)
    F /= np.linalg.norm(F)
    expected_points = None
    for (i, j), count in zip(((101, 3), (42, 7), (88, 29)), (24, 16, 12)):
        xyz = rng.uniform([-.5, -.4, 4.], [.5, .4, 7.], (count, 3))
        if expected_points is None:
            expected_points = xyz.copy()
        pixels1 = _pixels(xyz, K1, np.eye(3), np.zeros(3))
        pixels2 = _pixels(xyz, K2, R, t)
        for image_id, K, pixels in ((i, K1, pixels1), (j, K2, pixels2)):
            camera_id = 1000 + image_id
            model.cameras[camera_id] = Camera(camera_id, 640, 480, K.copy())
            model.images[image_id] = ImageRecord(image_id, f'{image_id}.png', camera_id, pixels)
            colors[image_id] = rng.integers(0, 256, (count, 3), dtype=np.uint8)
        row = np.arange(count)
        pairs.append(mapper.VerifiedPair(i, j, np.column_stack((row, row)), F.copy()))
        tracks.extend(((i, feature), (j, feature)) for feature in row)
    return model, pairs, tracks, colors, _settings(), expected_points


@pytest.mark.parametrize('seed', [3, 17, 73])
def test_real_geometry_matches_single_winner_reference_and_independent_projection(seed):
    model, pairs, tracks, colors, settings, expected = _real_fixture(seed)
    reference = copy.deepcopy(model)
    # No alternate implementation of pruning: this reference has only one
    # candidate, so candidate scheduling cannot change its result.
    mapper.initialize_map(reference, pairs[:1], tracks, settings, colors)
    diagnostics = []
    mapper.initialize_map(model, pairs, tracks, settings, colors, diagnostics=diagnostics)
    assert reconstruction_to_dict(model) == reconstruction_to_dict(reference)
    assert tuple(model.poses) == (101, 3)
    assert len(model.points) == 24
    validate_reconstruction(model)
    for point in model.points.values():
        feature = point.observations[0][1]
        np.testing.assert_allclose(point.xyz, expected[feature], atol=1e-6)
        np.testing.assert_array_equal(point.color, colors[101][feature])
        for image_id, index in point.observations:
            image, pose = model.images[image_id], model.poses[image_id]
            assert (pose.R @ point.xyz + pose.t)[2] > 0
            pixel = _pixels(point.xyz[None], model.cameras[image.camera_id].K, pose.R, pose.t)[0]
            np.testing.assert_allclose(pixel, image.keypoints[index], atol=1e-6)
    assert sum(bool(row['selected']) for row in diagnostics) == 1
