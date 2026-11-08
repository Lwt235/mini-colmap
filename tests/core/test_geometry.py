"""Acceptance tests for the G1–G8 multi-view geometry algorithms.

The scene and expected projection below are independent of project_points:
using the function under test to generate its own fixtures hides mistakes.
"""

import numpy as np
import pytest

from minicolmap.geometry import (
    eight_point,
    essential_from_fundamental,
    normalize_points,
    project_points,
    ransac_fundamental,
    recover_relative_pose,
    sampson_distance,
    triangulate_points,
)


def _skew(v):
    x, y, z = v
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])


def _scene(n=40, seed=4):
    rng = np.random.default_rng(seed)
    X = rng.uniform([-1.5, -1.0, 4.0], [1.5, 1.0, 8.0], (n, 3))
    angle = 0.12
    c, s = np.cos(angle), np.sin(angle)
    R = np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])
    t = np.array([-1.0, 0.02, 0.04])
    t /= np.linalg.norm(t)
    K1 = np.array([[820.0, 1.5, 320.0], [0.0, 780.0, 240.0], [0.0, 0.0, 1.0]])
    K2 = np.array([[760.0, 0.0, 300.0], [0.0, 810.0, 250.0], [0.0, 0.0, 1.0]])
    p1 = X @ K1.T
    p2 = (X @ R.T + t) @ K2.T
    x1 = p1[:, :2] / p1[:, 2, None]
    x2 = p2[:, :2] / p2[:, 2, None]
    E = _skew(t) @ R
    F = np.linalg.inv(K2).T @ E @ np.linalg.inv(K1)
    P1 = K1 @ np.column_stack((np.eye(3), np.zeros(3)))
    P2 = K2 @ np.column_stack((R, t))
    return X, x1, x2, K1, K2, R, t, F, E, P1, P2


def _same_projective_matrix(actual, expected, atol=1e-6):
    actual = actual / np.linalg.norm(actual)
    expected = expected / np.linalg.norm(expected)
    assert min(np.linalg.norm(actual - expected), np.linalg.norm(actual + expected)) < atol


def test_g1_hand_calculation_and_world_to_camera_translation():
    K = np.array([[100.0, 0.0, 10.0], [0.0, 200.0, 20.0], [0.0, 0.0, 1.0]])
    X = np.array([[2, 1, 4], [-2, -1, -4]])
    uv, depth = project_points(X, K, np.eye(3), np.array([1.0, 0.0, 0.0]))
    np.testing.assert_allclose(uv, [[85.0, 70.0], [35.0, 70.0]])
    np.testing.assert_array_equal(depth, [4.0, -4.0])
    assert uv.dtype == np.float64 and depth.dtype == np.float64


def test_g1_nonidentity_rotation():
    X, x1, x2, K1, K2, R, t, *_ = _scene()
    uv, depth = project_points(X, K2, R, t)
    np.testing.assert_allclose(uv, x2, atol=1e-10)
    np.testing.assert_allclose(depth, (X @ R.T + t)[:, 2])


def test_g1_rejects_near_zero_depth():
    with pytest.raises(ValueError):
        project_points([[0.0, 0.0, 5e-13]], np.eye(3), np.eye(3), np.zeros(3))


def test_g1_empty_batch():
    uv, depth = project_points(np.empty((0, 3)), np.eye(3), np.eye(3), np.zeros(3))
    assert uv.shape == (0, 2) and depth.shape == (0,)


@pytest.mark.parametrize("bad", [np.zeros((3, 2)), [[np.nan, 0.0, 1.0]]])
def test_geometry_rejects_invalid_points(bad):
    with pytest.raises(ValueError):
        project_points(bad, np.eye(3), np.eye(3), np.zeros(3))


def test_g2_mean_distance_and_transform():
    # Unequal radii distinguish mean distance from RMS distance.
    original = np.array([[9.0, 20.0], [11.0, 20.0], [10.0, 23.0], [10.0, 17.0]])
    normalized, T = normalize_points(original)
    np.testing.assert_allclose(normalized.mean(axis=0), np.zeros(2), atol=1e-12)
    np.testing.assert_allclose(np.linalg.norm(normalized, axis=1).mean(), np.sqrt(2), atol=1e-12)
    h = np.column_stack((original, np.ones(len(original)))) @ T.T
    np.testing.assert_allclose(normalized, h[:, :2] / h[:, 2, None], atol=1e-12)
    np.testing.assert_allclose(T[2], [0.0, 0.0, 1.0])
    assert normalized.dtype == np.float64 and T.dtype == np.float64


@pytest.mark.parametrize("points", [np.empty((0, 2)), np.full((8, 2), 12.0)])
def test_g2_rejects_no_spread(points):
    with pytest.raises(ValueError):
        normalize_points(points)


@pytest.mark.parametrize("n", [8, 40])
def test_g3_exact_nonplanar_scene_including_minimal_eight(n):
    _, x1, x2, _, _, _, _, true_F, *_ = _scene(n=n)
    F = eight_point(x1, x2)
    assert F.shape == (3, 3) and F.dtype == np.float64
    np.testing.assert_allclose(np.linalg.norm(F), 1.0, atol=1e-12)
    singular = np.linalg.svd(F, compute_uv=False)
    assert singular[2] < 1e-10 * singular[0]
    _same_projective_matrix(F, true_F)
    h1 = np.column_stack((x1, np.ones(n)))
    h2 = np.column_stack((x2, np.ones(n)))
    np.testing.assert_allclose(np.sum((h1 @ F.T) * h2, axis=1), 0.0, atol=1e-8)


def test_g3_rejects_insufficient_and_degenerate_correspondences():
    with pytest.raises(ValueError):
        eight_point(np.zeros((7, 2)), np.zeros((7, 2)))
    line = np.column_stack((np.arange(10.0), np.arange(10.0) * 2))
    with pytest.raises(ValueError):
        eight_point(line, line + 3.0)


def test_g4_hand_squared_error_and_scale_invariance():
    F = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
    x1 = np.array([[4.0, 2.0], [3.0, 5.0]])
    x2 = np.array([[9.0, 6.0], [8.0, 5.0]])
    distance = sampson_distance(F, x1, x2)
    np.testing.assert_allclose(distance, [8.0, 0.0])
    np.testing.assert_allclose(sampson_distance(-3.5 * F, x1, x2), distance)
    assert distance.shape == (2,) and distance.dtype == np.float64


def test_g4_zero_denominator_is_infinite_even_for_zero_numerator():
    d = sampson_distance(np.zeros((3, 3)), [[1.0, 2.0]], [[3.0, 4.0]])
    assert np.isinf(d[0])


def test_g4_known_scene_has_zero_error():
    _, x1, x2, _, _, _, _, F, *_ = _scene()
    np.testing.assert_allclose(sampson_distance(F, x1, x2), 0.0, atol=1e-18)


def test_g5_outliers_reproducibility_and_refit_mask():
    _, x1, x2, *_ = _scene(n=100, seed=17)
    rng = np.random.default_rng(22)
    x1 = x1 + rng.normal(0.0, 0.2, x1.shape)
    x2 = x2 + rng.normal(0.0, 0.2, x2.shape)
    outliers = np.zeros(100, dtype=bool)
    outliers[rng.choice(100, 25, replace=False)] = True
    x2[outliers] = rng.uniform([0.0, 0.0], [640.0, 480.0], (25, 2))
    F, mask = ransac_fundamental(x1, x2, threshold_px=1.2, max_trials=1200, seed=6)
    F_again, mask_again = ransac_fundamental(x1, x2, threshold_px=1.2, max_trials=1200, seed=6)
    assert mask.shape == (100,) and mask.dtype == np.bool_
    assert np.count_nonzero(mask & ~outliers) >= 65
    assert np.count_nonzero(mask & outliers) <= 4
    np.testing.assert_allclose(F_again, F, atol=1e-12)
    np.testing.assert_array_equal(mask_again, mask)
    # A stale pre-refit mask would fail this public invariant.
    np.testing.assert_array_equal(mask, sampson_distance(F, x1, x2) <= 1.2**2)


@pytest.mark.parametrize("kwargs", [{"threshold_px": 0}, {"threshold_px": np.inf}, {"max_trials": 0}, {"max_trials": 1.2}, {"seed": -1}])
def test_g5_invalid_options(kwargs):
    _, x1, x2, *_ = _scene(n=8)
    with pytest.raises(ValueError):
        ransac_fundamental(x1, x2, **kwargs)


def test_g5_all_degenerate_samples_raise():
    with pytest.raises(ValueError):
        ransac_fundamental(np.zeros((12, 2)), np.zeros((12, 2)), max_trials=20)


def test_g6_calibration_with_different_intrinsics():
    _, _, _, K1, K2, _, _, F, true_E, *_ = _scene()
    E = essential_from_fundamental(F, K1, K2)
    singular = np.linalg.svd(E, compute_uv=False)
    np.testing.assert_allclose(singular[0], singular[1], atol=1e-12)
    assert singular[2] < 1e-12
    np.testing.assert_allclose(np.linalg.norm(E), 1.0, atol=1e-12)
    _same_projective_matrix(E, true_E)


def test_g6_projects_noisy_matrix_to_essential_constraint():
    E = essential_from_fundamental(np.diag([4.0, 2.0, 0.5]), np.eye(3), np.eye(3))
    np.testing.assert_allclose(np.linalg.svd(E, compute_uv=False), [1 / np.sqrt(2), 1 / np.sqrt(2), 0.0], atol=1e-12)


def test_g6_rejects_zero_and_singular_calibration():
    with pytest.raises(ValueError):
        essential_from_fundamental(np.zeros((3, 3)), np.eye(3), np.eye(3))
    with pytest.raises(ValueError):
        essential_from_fundamental(np.eye(3), np.zeros((3, 3)), np.eye(3))


def test_g7_recovers_known_points_in_pixel_and_normalized_coordinates():
    X, x1, x2, K1, K2, R, t, _, _, P1, P2 = _scene()
    actual = triangulate_points(P1, P2, x1, x2)
    np.testing.assert_allclose(actual, X, atol=1e-8)
    h1 = np.column_stack((x1, np.ones(len(x1)))) @ np.linalg.inv(K1).T
    h2 = np.column_stack((x2, np.ones(len(x2)))) @ np.linalg.inv(K2).T
    actual_normalized = triangulate_points(
        np.column_stack((np.eye(3), np.zeros(3))), np.column_stack((R, t)),
        h1[:, :2] / h1[:, 2, None], h2[:, :2] / h2[:, 2, None],
    )
    np.testing.assert_allclose(actual_normalized, X, atol=1e-8)
    assert actual.dtype == np.float64


def test_g7_infinite_and_ambiguous_points_are_nan_rows():
    P1 = np.column_stack((np.eye(3), np.zeros(3)))
    P2 = np.column_stack((np.eye(3), [-1.0, 0.0, 0.0]))
    infinity = triangulate_points(P1, P2, [[0.0, 0.0]], [[0.0, 0.0]])
    ambiguous = triangulate_points(P1, P1, [[0.2, 0.1]], [[0.2, 0.1]])
    assert np.isnan(infinity).all() and np.isnan(ambiguous).all()


def test_g7_empty_batch():
    P = np.column_stack((np.eye(3), np.zeros(3)))
    assert triangulate_points(P, P, np.empty((0, 2)), np.empty((0, 2))).shape == (0, 3)


def test_g8_recovers_correct_sign_with_nonidentity_rotation():
    _, x1, x2, K1, K2, true_R, true_t, _, E, *_ = _scene()
    for sign in [1, -1]:
        R, t, mask = recover_relative_pose(sign * E, x1, x2, K1, K2)
        np.testing.assert_allclose(R, true_R, atol=1e-8)
        np.testing.assert_allclose(t, true_t, atol=1e-8)
        np.testing.assert_allclose(R.T @ R, np.eye(3), atol=1e-10)
        np.testing.assert_allclose(np.linalg.det(R), 1.0, atol=1e-10)
        np.testing.assert_allclose(np.linalg.norm(t), 1.0, atol=1e-12)
        assert mask.dtype == np.bool_ and mask.shape == (40,) and mask.all()


def test_g8_mask_rejects_negative_depth_and_low_parallax():
    X, _, _, K1, K2, true_R, true_t, _, E, *_ = _scene(n=32)
    X = np.vstack((X, [[0.2, -0.1, -6.0], [-0.3, 0.4, -7.0], [0.0, 0.0, 1e7], [1.0, 0.5, 1e7]]))
    p1 = X @ K1.T
    p2 = (X @ true_R.T + true_t) @ K2.T
    x1 = p1[:, :2] / p1[:, 2, None]
    x2 = p2[:, :2] / p2[:, 2, None]
    R, t, mask = recover_relative_pose(E, x1, x2, K1, K2)
    np.testing.assert_allclose(R, true_R, atol=1e-8)
    np.testing.assert_allclose(t, true_t, atol=1e-8)
    assert mask[:32].all() and not mask[32:].any()


def test_g8_rejects_zero_essential_matrix():
    _, x1, x2, K1, K2, *_ = _scene()
    with pytest.raises(ValueError):
        recover_relative_pose(np.zeros((3, 3)), x1, x2, K1, K2)


def test_correspondence_shape_mismatch_is_rejected():
    with pytest.raises(ValueError):
        sampson_distance(np.eye(3), np.zeros((3, 2)), np.zeros((4, 2)))
