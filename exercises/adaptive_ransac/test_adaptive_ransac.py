"""Opt-in acceptance for the learner's adaptive G5 optimization. No implementation here.

Run explicitly: python -m pytest exercises/adaptive_ransac -q
The learner's 2026-10-17 adaptive implementation passes these contracts.
"""
import math

import numpy as np
import pytest

from minicolmap import geometry as g


def budget(support, total=100, confidence=.99):
    """Independent test oracle for eight-point uniform RANSAC, not production code."""
    if support == total:
        return 1
    return math.ceil(math.log1p(-confidence) / math.log1p(-(support / total) ** 8))


def count_draws(monkeypatch, cap):
    original = np.random.default_rng
    counter = {'draws': 0}

    class CountedRng:
        def __init__(self, seed):
            self.generator = original(seed)

        def choice(self, *args, **kwargs):
            counter['draws'] += 1
            assert counter['draws'] <= cap, 'Sampling exceeded max_trials (including degenerate attempts)'
            sample = self.generator.choice(*args, **kwargs)
            assert sample.shape == (8,) and len(np.unique(sample)) == 8
            return sample

        def __getattr__(self, key):
            return getattr(self.generator, key)
    monkeypatch.setattr(g.np.random, 'default_rng', CountedRng)
    return counter


def scripted(monkeypatch, support=80, cap=1000, degenerate=(), refit_support=None):
    """Isolate stopping policy from geometry quality; scripted valid/invalid samples."""
    counter = count_draws(monkeypatch, cap)
    points = np.column_stack((np.arange(100.), np.arange(100.) ** 2))

    def fit(a, b):
        if len(a) != 8:
            F = np.diag([100000., 1., 0.])  # Final non-minimal refit.
            return F / np.linalg.norm(F)
        if counter['draws'] in degenerate:
            raise ValueError('scripted degenerate sample')
        F = np.diag([float(counter['draws']), 1., 0.])
        return F / np.linalg.norm(F)

    def distances(F, a, b):
        size = support(counter['draws']) if callable(support) else support
        if F[0, 0] / F[1, 1] > 99999 and refit_support is not None:
            size = refit_support
        return np.where(np.arange(len(a)) < size, .1, 100.)
    monkeypatch.setattr(g, 'eight_point', fit)
    monkeypatch.setattr(g, 'sampson_distance', distances)
    return points, points.copy(), counter


def run(a, b, **kwargs):
    options = dict(threshold_px=1.2, max_trials=1000, seed=7, confidence=.99, min_trials=3)
    options.update(kwargs)
    return g.ransac_fundamental(a, b, **options)


@pytest.mark.parametrize('cap,minimum', [(2000, 20), (4, 20), (1, 20)])
def test_all_inliers_stop_at_minimum_clamped_by_hard_cap(monkeypatch, cap, minimum):
    a, b, counter = scripted(monkeypatch, support=100, cap=cap)
    F, mask = run(a, b, max_trials=cap, min_trials=minimum)
    assert counter['draws'] == min(cap, minimum)
    assert mask.all() and np.isfinite(F).all()


@pytest.mark.parametrize('support,confidence', [(80, .99), (70, .99), (75, .9), (75, .999)])
def test_budget_uses_eight_point_probability_ceiling_and_requested_confidence(monkeypatch, support, confidence):
    a, b, counter = scripted(monkeypatch, support=support)
    run(a, b, confidence=confidence)
    assert counter['draws'] == max(3, budget(support, confidence=confidence))


def test_weak_consensus_cannot_stop_at_the_minimum(monkeypatch):
    a, b, counter = scripted(monkeypatch, support=10, cap=60)
    run(a, b, max_trials=60)
    assert counter['draws'] == 60


def test_improved_consensus_shrinks_remaining_budget(monkeypatch):
    a, b, counter = scripted(monkeypatch, support=lambda draw: 30 if draw < 10 else 80)
    run(a, b)
    assert counter['draws'] == budget(80)


def test_late_improvement_uses_total_attempts_not_a_fresh_budget(monkeypatch):
    a, b, counter = scripted(monkeypatch, support=lambda draw: 10 if draw < 30 else 90, cap=60)
    run(a, b, max_trials=60)
    assert counter['draws'] == 30


def test_degenerate_draws_count_toward_total_budget(monkeypatch):
    a, b, counter = scripted(monkeypatch, degenerate=set(range(1, 8)))
    run(a, b)
    assert counter['draws'] == budget(80)


@pytest.mark.parametrize('all_degenerate', [False, True])
def test_no_valid_consensus_uses_hard_cap_then_raises(monkeypatch, all_degenerate):
    a, b, counter = scripted(monkeypatch, support=0, cap=15,
                             degenerate=set(range(1, 16)) if all_degenerate else ())
    with pytest.raises(ValueError):
        run(a, b, max_trials=15)
    assert counter['draws'] == 15


def test_final_refit_recomputes_the_mask_instead_of_returning_stale_consensus(monkeypatch):
    a, b, counter = scripted(monkeypatch, refit_support=70)
    F, mask = run(a, b)
    assert counter['draws'] == budget(80)
    assert F[0, 0] / F[1, 1] == pytest.approx(100000)
    assert mask.dtype == np.bool_ and mask.shape == (100,)
    np.testing.assert_array_equal(mask, np.arange(100) < 70)


@pytest.mark.parametrize('confidence', [0, 1, -.1, 1.1, np.nan, np.inf, True, None])
def test_invalid_confidence_rejected_before_sampling(monkeypatch, confidence):
    a, b, counter = scripted(monkeypatch)
    with pytest.raises(ValueError):
        run(a, b, confidence=confidence)
    assert counter['draws'] == 0


@pytest.mark.parametrize('minimum', [0, -1, 1.5, True, np.inf])
def test_invalid_min_trials_rejected_before_sampling(monkeypatch, minimum):
    a, b, counter = scripted(monkeypatch)
    with pytest.raises(ValueError):
        run(a, b, min_trials=minimum)
    assert counter['draws'] == 0


def scene():
    """Independent known-correspondence scene; no call to student project_points."""
    rng = np.random.default_rng(73)
    xyz = rng.uniform([-1.5, -1., 4.], [1.5, 1., 8.], (120, 3))
    angle = .15
    R = np.array([[np.cos(angle), 0., np.sin(angle)], [0., 1., 0.],
                  [-np.sin(angle), 0., np.cos(angle)]])
    K = np.array([[800., 0., 320.], [0., 810., 240.], [0., 0., 1.]])
    h1 = xyz @ K.T
    h2 = (xyz @ R.T + [-1., .02, .05]) @ K.T
    a = h1[:, :2] / h1[:, 2:] + rng.normal(0, .1, (120, 2))
    b = h2[:, :2] / h2[:, 2:] + rng.normal(0, .1, (120, 2))
    outliers = np.zeros(120, bool)
    outliers[rng.choice(120, 12, replace=False)] = True
    b[outliers] = rng.uniform([0, 0], [640, 480], (12, 2))
    return a, b, outliers


@pytest.mark.parametrize('seed', [0, 7, 19])
def test_real_geometry_remains_accurate_repeatable_and_reduces_sampling(monkeypatch, seed):
    a, b, outliers = scene()
    original_a, original_b = a.copy(), b.copy()
    counter = count_draws(monkeypatch, cap=800)
    F, mask = run(a, b, max_trials=800, seed=seed, min_trials=20)
    first_draws = counter['draws']
    assert 20 <= first_draws < 400, 'Easy high-inlier data should not use most of the hard cap'
    assert np.count_nonzero(mask & ~outliers) >= 100
    assert np.count_nonzero(mask & outliers) <= 3
    np.testing.assert_array_equal(mask, g.sampson_distance(F, a, b) < 1.2 ** 2)
    assert np.linalg.svd(F, compute_uv=False)[-1] < 1e-8
    counter['draws'] = 0
    repeated_F, repeated_mask = run(a, b, max_trials=800, seed=seed, min_trials=20)
    assert counter['draws'] == first_draws
    np.testing.assert_allclose(repeated_F, F, atol=1e-12)
    np.testing.assert_array_equal(repeated_mask, mask)
    np.testing.assert_array_equal(a, original_a)
    np.testing.assert_array_equal(b, original_b)


def test_existing_positional_call_and_two_value_return_still_work(monkeypatch):
    a, b, _ = scripted(monkeypatch, support=100, cap=40)
    F, mask = g.ransac_fundamental(a, b, 1.2, 40, 7)
    assert F.shape == (3, 3) and mask.shape == (100,)
