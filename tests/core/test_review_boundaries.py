"""Review regressions not covered by the original fill-in acceptance suite."""
import numpy as np
import pytest

from minicolmap.geometry import project_points, ransac_fundamental


def test_zero_depth_is_rejected_before_division():
    with np.errstate(divide='raise', invalid='raise'):
        with pytest.raises(ValueError):
            project_points([[0., 0., 0.]], np.eye(3), np.eye(3), np.zeros(3))


def test_ransac_refit_never_returns_fewer_than_eight_inliers():
    rng = np.random.default_rng(15)
    K = np.array([[800., 0., 320.], [0., 800., 240.], [0., 0., 1.]])
    X = rng.uniform([-1., -.7, 4.], [1., .7, 8.], (14, 3))
    a, b = X @ K.T, (X + [-.7, .03, 0.]) @ K.T
    x1 = a[:, :2] / a[:, 2:] + rng.normal(0., 1., (14, 2))
    x2 = b[:, :2] / b[:, 2:] + rng.normal(0., 1., (14, 2))
    try:
        _, mask = ransac_fundamental(x1, x2, threshold_px=.5, max_trials=25, seed=15)
    except ValueError:
        return  # Explicit failure or a valid >=8 consensus are both acceptable.
    assert np.count_nonzero(mask) >= 8, 'Refitting invalidated the consensus; reject or retain a valid model'
