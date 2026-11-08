"""B1 public residual tests; skip only if the actual SciPy dependency is absent."""

import numpy as np
import pytest

pytest.importorskip("scipy", reason="B1 needs scipy.spatial.transform.Rotation")

from minicolmap.ba import bundle_residuals


def test_b1_residual_sign_shape_camera_point_indexing_and_rotation():
    # Camera 1 rotates 90 degrees around z: (x,y,z)->(-y,x,z).
    rotvecs = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, np.pi / 2]])
    translations = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    points = np.array([[2.0, 1.0, 4.0], [1.0, -1.0, 2.0]])
    K = np.array([[100.0, 0.0, 10.0], [0.0, 100.0, 20.0], [0.0, 0.0, 1.0]])
    camera_ids = np.array([1, 0, 1])
    point_ids = np.array([1, 0, 0])
    # Expected pixels: (110,70), (60,45), (10,70), then p - observation.
    observed = np.array([[109.0, 72.0], [63.0, 41.0], [10.0, 70.0]])
    residual = bundle_residuals(rotvecs, translations, points, K, camera_ids, point_ids, observed)
    np.testing.assert_allclose(residual, [1.0, -2.0, -3.0, 4.0, 0.0, 0.0], atol=1e-10)
    assert residual.shape == (6,) and residual.dtype == np.float64


def test_b1_negative_depth_is_projected_but_near_zero_is_rejected():
    common = (np.zeros((1, 3)), np.zeros((1, 3)))
    residual = bundle_residuals(*common, [[2.0, 1.0, -4.0]], np.eye(3), np.array([0]), np.array([0]), [[0.0, 0.0]])
    np.testing.assert_allclose(residual, [-0.5, -0.25])
    with pytest.raises(ValueError):
        bundle_residuals(*common, [[1.0, 1.0, 0.0]], np.eye(3), np.array([0]), np.array([0]), [[0.0, 0.0]])


def test_b1_empty_observation_batch():
    residual = bundle_residuals(np.zeros((1, 3)), np.zeros((1, 3)), np.zeros((1, 3)), np.eye(3), np.array([], dtype=int), np.array([], dtype=int), np.empty((0, 2)))
    assert residual.shape == (0,)


@pytest.mark.parametrize("camera_ids, point_ids", [(np.array([1]), np.array([0])), (np.array([0]), np.array([-1])), (np.array([0.0]), np.array([0]))])
def test_b1_rejects_invalid_observation_indices(camera_ids, point_ids):
    with pytest.raises(ValueError):
        bundle_residuals(np.zeros((1, 3)), np.zeros((1, 3)), np.ones((1, 3)), np.eye(3), camera_ids, point_ids, np.zeros((1, 2)))
