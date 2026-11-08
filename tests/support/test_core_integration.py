"""Integration checks for geometry, tracks, mapping and nonlinear bundle adjustment."""
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    'core_integration_probe', Path(__file__).resolve().parents[2] / 'scripts/check_core_integration.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize('noise', [0., .05, .2])
def test_complete_noisy_reconstruction_and_converged_ba(noise):
    result = probe.check(noise, require_ba_convergence=True)
    assert result['status'] == 'passed', result
    assert result['warnings'] == []
    assert result['after_ba']['registered_images'] == 8
    assert result['after_ba']['point_count'] == 120
    assert result['after_ba']['positive_depth_ratio'] == 1.
    assert result['after_ba']['mean_reprojection_error_px'] < max(1e-6, noise * 2)
    assert result['after_ba']['mean_reprojection_error_px'] <= result['before_ba']['mean_reprojection_error_px'] + 1e-9
    assert result['stages'][0]['points'] == 60
    assert sum(stage.get('new_points', 0) for stage in result['stages']) == 60
    initialization = result['initialization']
    assert initialization['gauge']['second_translation_norm'] == pytest.approx(1.)
    selected = [item for item in initialization['candidates'] if item['selected']]
    assert len(selected) == 1
    assert selected[0]['accepted_tracks'] == 60
    assert initialization['ground_truth_errors']['rotation_deg'] < 2.
    assert initialization['ground_truth_errors']['translation_direction_deg'] < 2.
    if noise == .2:
        # The former regression: constrained E had a large pixel bias even
        # though F's epipolar residual was small. Do not loosen the 3px gate.
        assert selected[0]['refinement']['before_mean_px'] > 3.
        assert selected[0]['refinement']['candidate_mean_px'] < .3


def test_budget_exhaustion_is_visible_and_strict_mode_rejects_it():
    result = probe.check(.2, ba_max_nfev=1, require_ba_convergence=True)
    assert result['ba']['accepted']
    assert not result['ba']['solver_success']
    assert result['warnings']
    assert result['status'] == 'failed' and result['stage'] == 'bundle_adjustment'
