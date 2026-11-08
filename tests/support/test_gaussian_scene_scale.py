"""Scale adaptation must preserve projection and resist unrelated outliers."""
import numpy as np

from minicolmap.rendering.data import gaussian_initial_scales


def test_knn_initial_scales_follow_local_spacing_and_respect_ceiling():
    points = np.zeros((8, 3))
    points[:, 0] = [0, 1, 2, 3, 100, 110, 120, 130]
    options = {'initial_scale_mode': 'knn', 'initial_scale': 50.}
    scales = gaussian_initial_scales(points, options)
    np.testing.assert_allclose(scales[[0, 4]], np.sqrt(14 / 3) * np.array([1, 10]))
    options['initial_scale'] = 5.
    capped = gaussian_initial_scales(points, options)
    np.testing.assert_allclose(capped[:4], scales[:4])
    np.testing.assert_allclose(capped[4:], 5.)


def test_knn_scales_change_with_units_and_handle_duplicates():
    points = np.array([[0., 0., 0.], [1., 2., 3.], [4., 0., 0.]])
    scales = gaussian_initial_scales(points, dict(initial_scale_mode='knn', initial_scale=100.))
    changed = gaussian_initial_scales(points * 7, dict(initial_scale_mode='knn', initial_scale=700.))
    np.testing.assert_allclose(changed, scales * 7)
    duplicate_scales = gaussian_initial_scales(np.zeros((5, 3)), dict(initial_scale_mode='knn', initial_scale=.01))
    assert np.isfinite(np.log(duplicate_scales)).all() and np.all(duplicate_scales > 0)


def test_legacy_fixed_initialization_ignores_spacing():
    points = np.array([[0., 0., 0.], [1e6, 0., 0.]])
    np.testing.assert_array_equal(gaussian_initial_scales(points, {'initial_scale': .01}), [.01, .01])
    np.testing.assert_array_equal(gaussian_initial_scales(points[:1], dict(initial_scale_mode='knn', initial_scale=.01)), [.01])

