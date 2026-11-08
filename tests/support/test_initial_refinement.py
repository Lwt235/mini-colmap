"""Noisy two-view refinement: independent projections, gauge and rollback."""
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from minicolmap.optimization import refine_two_view


def fixture():
    rng = np.random.default_rng(44)
    xyz = rng.uniform([-1., -.8, 4.], [1., .8, 8.], (40, 3))
    K1 = np.array([[800., 0., 320.], [0., 790., 240.], [0., 0., 1.]])
    K2 = np.array([[920., 0., 310.], [0., 900., 250.], [0., 0., 1.]])
    true_R = Rotation.from_rotvec([.01, .08, -.02]).as_matrix()
    true_t = np.array([-1., .03, .02])
    true_t /= np.linalg.norm(true_t)
    h1, h2 = xyz @ K1.T, (xyz @ true_R.T + true_t) @ K2.T
    a = h1[:, :2] / h1[:, 2:] + rng.normal(0., .2, (40, 2))
    b = h2[:, :2] / h2[:, 2:] + rng.normal(0., .2, (40, 2))
    R = Rotation.from_rotvec([.022, .072, -.016]).as_matrix()
    t = true_t + [0., .02, -.01]
    t /= np.linalg.norm(t)
    return (R, t, xyz + [.02, -.01, .1], K1, K2, a, b), (true_R, true_t)


def test_two_view_reduces_noisy_pixel_error_with_distinct_intrinsics_and_unit_baseline():
    inputs, (true_R, true_t) = fixture()
    original = [value.copy() for value in inputs]
    R, t, xyz, report = refine_two_view(*inputs)
    assert report['accepted'] and report['solver_success']
    assert report['candidate_mean_px'] < .3
    assert report['candidate_mean_px'] < report['before_mean_px'] * .1
    assert np.linalg.norm(t) == pytest.approx(1., abs=1e-12)
    assert np.rad2deg(Rotation.from_matrix(R @ true_R.T).magnitude()) < 1.
    assert np.rad2deg(np.arccos(np.clip(t @ true_t, -1., 1.))) < 2.
    assert np.all(xyz[:, 2] > 0) and np.all((xyz @ R.T + t)[:, 2] > 0)
    for actual, expected in zip(inputs, original):
        np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize('fault', ['nonfinite', 'negative_depth', 'worse', 'exception'])
def test_two_view_invalid_candidates_preserve_seed(monkeypatch, fault):
    import scipy.optimize

    inputs, _ = fixture()
    original = [value.copy() for value in inputs]

    def solver(fun, x0, **kwargs):
        if fault == 'exception':
            raise ValueError('Invalid trial')
        x = x0.copy()
        if fault == 'nonfinite':
            x[0] = np.nan
        elif fault == 'negative_depth':
            x[7] = -5.
        else:
            x[5] += 10.
        return SimpleNamespace(x=x, success=True, nfev=1, message='Controlled invalid result')

    monkeypatch.setattr(scipy.optimize, 'least_squares', solver)
    R, t, xyz, report = refine_two_view(*inputs)
    assert not report['accepted'] and report['rejection_reason']
    for actual, expected in zip((R, t, xyz), inputs[:3]):
        np.testing.assert_array_equal(actual, expected)
    for actual, expected in zip(inputs, original):
        np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize('fault', ['negative_depth', 'nonunit_baseline'])
def test_two_view_rejects_invalid_seed(fault):
    inputs, _ = fixture()
    if fault == 'negative_depth':
        inputs[2][0, 2] = -5.
    else:
        inputs[1][:] *= 2.
    with pytest.raises(ValueError):
        refine_two_view(*inputs)
