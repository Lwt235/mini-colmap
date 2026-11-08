"""Independent analytic / gradient acceptance tests for S1-S5."""
from __future__ import annotations

import math

import numpy as np
import pytest
import torch

from minicolmap.rendering.algorithms import (
    densify_and_prune, gaussian_covariance, initialize_gaussians,
    project_gaussians, splat_gaussians,
)


def T(value, *, grad=False):
    return torch.tensor(value, dtype=torch.float64, requires_grad=grad)


def state():
    """An independent state fixture: S2-S5 do not depend on a completed S1."""
    return {
        'means': T([[0., 0., 2.], [1., 0., 3.], [2., 0., 4.]]),
        'log_scales': torch.log(T([[.2, .4, .8], [.3, .6, .9], [.4, .8, 1.2]])),
        'quaternions': T([[1., 0., 0., 0.]] * 3),
        'sh_coeffs': torch.arange(3 * 16 * 3, dtype=torch.float64).reshape(3, 16, 3) / 100,
        'opacity_logits': torch.logit(T([.8, .01, .5])),
    }


def test_s1_sparse_point_and_color_initialization():
    points = T([[1., 2., 3.], [-1., 0., 5.]])
    colors = T([[0., .5, 1.], [.1, .3, .7]])
    result = initialize_gaussians(points, colors, initial_scale=.2, initial_opacity=.3)
    assert set(result) == {'means', 'log_scales', 'quaternions', 'sh_coeffs', 'opacity_logits'}
    torch.testing.assert_close(result['means'], points)
    torch.testing.assert_close(result['log_scales'].exp(), torch.full_like(points, .2))
    torch.testing.assert_close(result['quaternions'], T([[1., 0., 0., 0.], [1., 0., 0., 0.]]))
    assert result['sh_coeffs'].shape == (2, 16, 3)
    torch.testing.assert_close(result['sh_coeffs'][:, 0] / math.sqrt(4 * math.pi) + .5, colors)
    assert torch.count_nonzero(result['sh_coeffs'][:, 1:]) == 0
    torch.testing.assert_close(result['opacity_logits'].sigmoid(), T([.3, .3]))
    for value in result.values():
        assert value.is_leaf and value.requires_grad and value.dtype == points.dtype
        assert value.device == points.device and torch.isfinite(value).all()
    with torch.no_grad():
        result['means'][0, 0] = 9.
    assert float(points[0, 0]) == 1., 'State must not alias the SfM point tensor'


def test_s1_empty_sparse_cloud():
    result = initialize_gaussians(torch.empty((0, 3), dtype=torch.float64),
                                  torch.empty((0, 3), dtype=torch.float64))
    assert result['means'].shape == result['log_scales'].shape == (0, 3)
    assert result['sh_coeffs'].shape == (0, 16, 3)
    assert result['quaternions'].shape == (0, 4) and result['opacity_logits'].shape == (0,)


@pytest.mark.parametrize('scale,opacity', [(0., .1), (-1., .1), (.1, 0.), (.1, 1.), (float('nan'), .1)])
def test_s1_invalid_initialization_parameters(scale, opacity):
    with pytest.raises(ValueError):
        initialize_gaussians(T([[0., 0., 1.]]), T([[.2, .3, .4]]), scale, opacity)


def test_s2_identity_anisotropic_covariance():
    out = gaussian_covariance(torch.log(T([[1., 2., 3.]])), T([[1., 0., 0., 0.]]))
    torch.testing.assert_close(out, torch.diag(T([1., 4., 9.])).unsqueeze(0))


def test_s2_quaternion_rotation_normalization_and_double_cover():
    # 45 degrees about z creates nonzero off-diagonal covariance; a diagonal
    # shortcut or an xyzw convention cannot satisfy this independent answer.
    q = T([[2 * math.cos(math.pi / 8), 0., 0., 2 * math.sin(math.pi / 8)]])
    scales = torch.log(T([[1., 3., 2.]]))
    expected = T([[[5., -4., 0.], [-4., 5., 0.], [0., 0., 4.]]])
    torch.testing.assert_close(gaussian_covariance(scales, q), expected)
    torch.testing.assert_close(gaussian_covariance(scales, -q), expected)


def test_s2_log_scale_and_quaternion_gradients():
    logs, q = T([[-.2, .3, .5]], grad=True), T([[1., .1, -.2, .3]], grad=True)
    assert torch.autograd.gradcheck(gaussian_covariance, (logs, q))


def test_s2_zero_quaternion_rejected():
    with pytest.raises(ValueError):
        gaussian_covariance(T([[0., 0., 0.]]), T([[0., 0., 0., 0.]]))


def test_s3_analytic_perspective_projection():
    result = project_gaussians(T([[0., 0., 2.]]), torch.eye(3, dtype=torch.float64).unsqueeze(0),
                               T([[2., 0., 1.], [0., 4., 2.], [0., 0., 1.]]),
                               torch.eye(3, dtype=torch.float64), T([0., 0., 0.]), .3)
    torch.testing.assert_close(result['means2d'], T([[1., 2.]]))
    torch.testing.assert_close(result['cov2d'], T([[[1.3, 0.], [0., 4.3]]]))
    torch.testing.assert_close(result['depths'], T([2.]))
    assert result['visible'].dtype == torch.bool and result['visible'].tolist() == [True]


def test_s3_nonidentity_pose_skew_and_jacobian_finite_difference():
    means = T([[1., 2., 4.]])
    covariance = T([[[.4, .1, 0.], [.1, .7, .2], [0., .2, .9]]])
    R = T([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    t = T([.4, -.2, 1.])
    K = T([[5., .6, 3.], [0., 7., 2.], [0., 0., 1.]])
    out = project_gaussians(means, covariance, K, R, t, .2)
    # Independently differentiate the full world->pixel mapping numerically.
    rn, tn, kn, p = R.numpy(), t.numpy(), K.numpy(), means.numpy()[0]
    def project(v):
        q = kn @ (rn @ v + tn)
        return q[:2] / q[2]
    h = 1e-5
    jac = np.column_stack([(project(p + h * axis) - project(p - h * axis)) / (2 * h)
                           for axis in np.eye(3)])
    expected_cov = jac @ covariance.numpy()[0] @ jac.T + .2 * np.eye(2)
    torch.testing.assert_close(out['means2d'], T([project(p).tolist()]))
    torch.testing.assert_close(out['cov2d'], T([expected_cov.tolist()]), atol=1e-8, rtol=1e-8)
    torch.testing.assert_close(out['depths'], T([5.]))


def test_s3_nonpositive_depth_has_finite_placeholder_not_mirrored_projection():
    means = T([[1., 2., -1.], [1., 2., 0.], [100., 100., 2.]])
    cov = torch.eye(3, dtype=torch.float64).repeat(3, 1, 1)
    out = project_gaussians(means, cov, torch.eye(3, dtype=torch.float64),
                            torch.eye(3, dtype=torch.float64), T([0., 0., 0.]), .3)
    assert out['visible'].tolist() == [False, False, True], 'Visibility is depth-based, not image-bound clipping'
    torch.testing.assert_close(out['means2d'][:2], torch.zeros((2, 2), dtype=torch.float64))
    torch.testing.assert_close(out['cov2d'][:2], .3 * torch.eye(2, dtype=torch.float64).repeat(2, 1, 1))
    assert all(torch.isfinite(out[key]).all() for key in ('means2d', 'cov2d', 'depths'))


def test_s3_position_gradients():
    means = T([[.2, .3, 2.]], grad=True)
    cov = torch.eye(3, dtype=torch.float64).unsqueeze(0)
    eye = torch.eye(3, dtype=torch.float64)
    def render(x):
        result = project_gaussians(x, cov, eye, eye, T([0., 0., 0.]))
        return torch.cat((result['means2d'].flatten(), result['cov2d'].flatten()))
    assert torch.autograd.gradcheck(render, (means,))


def test_s3_covariance_gradients_are_not_detached():
    variances = T([[.4, .7, .9]], grad=True)
    eye = torch.eye(3, dtype=torch.float64)
    assert torch.autograd.gradcheck(
        lambda v: project_gaussians(T([[.2, .3, 2.]]), torch.diag_embed(v), eye, eye,
                                    T([0., 0., 0.]))['cov2d'], (variances,))


def test_s3_empty_batch():
    eye = torch.eye(3, dtype=torch.float64)
    out = project_gaussians(torch.empty((0, 3), dtype=torch.float64),
                            torch.empty((0, 3, 3), dtype=torch.float64), eye, eye, T([0., 0., 0.]))
    assert out['means2d'].shape == (0, 2) and out['cov2d'].shape == (0, 2, 2)
    assert out['depths'].shape == out['visible'].shape == (0,)


@pytest.mark.parametrize('variance,bad_cov', [(0., False), (-.1, False), (.3, True)])
def test_s3_invalid_variance_or_covariance(variance, bad_cov):
    eye = torch.eye(3, dtype=torch.float64)
    cov = eye.unsqueeze(0).clone()
    if bad_cov:
        cov[0, 0, 0] = -1.
    with pytest.raises(ValueError):
        project_gaussians(T([[0., 0., 2.]]), cov, eye, eye, T([0., 0., 0.]), variance)


def test_s4_front_to_back_occlusion_and_depth_sort():
    # Input far first. Both means coincide with the integer pixel (0,0).
    result = splat_gaussians(T([[0., 0.], [0., 0.]]),
                             torch.eye(2, dtype=torch.float64).repeat(2, 1, 1), T([4., 2.]),
                             T([[0., 0., 1.], [1., 0., 0.]]), T([.5, .5]), 1, 1, T([0., 1., 0.]))
    torch.testing.assert_close(result['rgb'], T([[[.5, .25, .25]]]))
    torch.testing.assert_close(result['depth'], T([[2.]]))
    torch.testing.assert_close(result['opacity'], T([[.75]]))


def test_s4_equal_depth_ties_preserve_input_order():
    result = splat_gaussians(T([[0., 0.], [0., 0.]]),
                             torch.eye(2, dtype=torch.float64).repeat(2, 1, 1), T([2., 2.]),
                             T([[1., 0., 0.], [0., 0., 1.]]), T([.5, .5]), 1, 1, T([0., 0., 0.]))
    torch.testing.assert_close(result['rgb'], T([[[.5, 0., .25]]]))


def test_s4_anisotropic_offcenter_footprint_and_rgb_channel_layout():
    result = splat_gaussians(T([[0., 0.]]), T([[[4., 0.], [0., 1.]]]), T([2.]),
                             T([[.2, .4, .8]]), T([.8]), 3, 2, T([0., 0., 0.]))
    assert result['rgb'].shape == (2, 3, 3)
    assert result['opacity'].shape == result['depth'].shape == (2, 3)
    torch.testing.assert_close(result['opacity'][0, 2], T(.8 * math.exp(-.5)))
    torch.testing.assert_close(result['opacity'][1, 0], T(.8 * math.exp(-.5)))
    torch.testing.assert_close(result['rgb'][0, 0], T([.16, .32, .64]))


def test_s4_opaque_alpha_is_clamped_to_point_99():
    out = splat_gaussians(T([[0., 0.]]), torch.eye(2, dtype=torch.float64).unsqueeze(0),
                         T([2.]), T([[1., 0., 0.]]), T([1.]), 1, 1, T([0., 0., 1.]))
    torch.testing.assert_close(out['rgb'], T([[[.99, 0., .01]]]))
    torch.testing.assert_close(out['opacity'], T([[.99]]))


def test_s4_nonpositive_depth_does_not_occlude():
    out = splat_gaussians(T([[0., 0.], [0., 0.]]), torch.eye(2, dtype=torch.float64).repeat(2, 1, 1),
                         T([-2., 0.]), T([[1., 0., 0.], [1., 0., 0.]]), T([.9, .9]),
                         2, 1, T([.1, .2, .3]))
    torch.testing.assert_close(out['rgb'], T([[[.1, .2, .3], [.1, .2, .3]]]))
    torch.testing.assert_close(out['depth'], T([[0., 0.]]))


def test_s4_empty_scene_is_background():
    out = splat_gaussians(torch.empty((0, 2), dtype=torch.float64),
                         torch.empty((0, 2, 2), dtype=torch.float64),
                         torch.empty((0,), dtype=torch.float64), torch.empty((0, 3), dtype=torch.float64),
                         torch.empty((0,), dtype=torch.float64), 2, 3, T([.1, .2, .3]))
    torch.testing.assert_close(out['rgb'], T([.1, .2, .3]).expand(3, 2, 3))
    torch.testing.assert_close(out['depth'], torch.zeros((3, 2), dtype=torch.float64))


def test_s4_projection_color_and_opacity_gradients():
    means = T([[.2, .3]], grad=True)
    colors, opacities = T([[.2, .5, .7]], grad=True), T([.6], grad=True)
    cov = T([[[1.2, .1], [.1, .8]]])
    assert torch.autograd.gradcheck(
        lambda m, c, a: splat_gaussians(m, cov, T([2.]), c, a, 2, 2, T([.1, .1, .1]))['rgb'],
        (means, colors, opacities))


def test_s4_covariance_gradients_are_not_detached():
    # Symmetric positive covariance under every finite-difference perturbation.
    params = T([1.2, .1, .8], grad=True)
    def render(v):
        cov = torch.stack((v[0], v[1], v[1], v[2])).reshape(1, 2, 2)
        return splat_gaussians(T([[.2, .3]]), cov, T([2.]), T([[.2, .5, .7]]), T([.6]),
                               2, 2, T([.1, .1, .1]))['rgb']
    assert torch.autograd.gradcheck(render, (params,))


def test_s4_real_gradient_descent_reduces_splatting_loss():
    logits = torch.nn.Parameter(T([[0., 0., 0.]]))
    optimizer = torch.optim.Adam([logits], lr=.15)
    history = []
    for _ in range(30):
        optimizer.zero_grad()
        out = splat_gaussians(T([[0., 0.]]), torch.eye(2, dtype=torch.float64).unsqueeze(0),
                             T([2.]), logits.sigmoid(), T([.95]), 1, 1, T([0., 0., 0.]))
        loss = (out['rgb'] - T([[[.8, .1, .2]]])).square().mean()
        history.append(float(loss.detach()))
        loss.backward()
        optimizer.step()
    assert history[-1] < history[0] * .15


@pytest.mark.parametrize('bad', ['size', 'opacity', 'singular_covariance'])
def test_s4_invalid_raster_parameters(bad):
    width, opacity = 2, T([.5])
    cov = torch.eye(2, dtype=torch.float64).unsqueeze(0)
    if bad == 'size':
        width = 0
    elif bad == 'opacity':
        opacity = T([1.1])
    else:
        cov[0, 0, 0] = 0.
    with pytest.raises(ValueError):
        splat_gaussians(T([[0., 0.]]), cov, T([2.]), T([[.2, .3, .4]]), opacity,
                        width, 1, T([0., 0., 0.]))


def test_s5_prunes_low_opacity_and_appends_deterministic_children():
    original = state()
    before = {k: v.clone() for k, v in original.items()}
    out = densify_and_prune(original, T([.4, .8, .5]), .3, .1, 10)
    torch.testing.assert_close(out['means'], T([[0., 0., 2.], [2., 0., 4.], [.2, 0., 2.], [2.4, 0., 4.]]))
    expected_scales = T([[.1, .2, .4], [.2, .4, .6], [.1, .2, .4], [.2, .4, .6]])
    torch.testing.assert_close(out['log_scales'].exp(), expected_scales)
    torch.testing.assert_close(out['sh_coeffs'], before['sh_coeffs'][[0, 2, 0, 2]])
    torch.testing.assert_close(out['opacity_logits'], before['opacity_logits'][[0, 2, 0, 2]])
    for key in original:
        torch.testing.assert_close(original[key], before[key])
        assert out[key].is_leaf and out[key].requires_grad


def test_s5_rotated_child_offset_uses_world_rotation():
    original = {k: v[:1].clone() for k, v in state().items()}
    original['quaternions'] = T([[math.sqrt(.5), 0., 0., math.sqrt(.5)]])
    out = densify_and_prune(original, T([1.]), .1, 0., 2)
    torch.testing.assert_close(out['means'], T([[0., 0., 2.], [0., .2, 2.]]), atol=1e-12, rtol=1e-12)


def test_s5_threshold_equality_retains_opacity_but_does_not_split_gradient():
    original = {k: v[:1].clone() for k, v in state().items()}
    # Exactly representable opacity avoids an accidental rounding-boundary test.
    original['opacity_logits'] = T([0.])
    out = densify_and_prune(original, T([.3]), .3, .5, 10)
    assert len(out['means']) == 1
    torch.testing.assert_close(out['log_scales'], original['log_scales'])


def test_s5_cap_prioritizes_originals_and_only_shrinks_actual_split_sources():
    original = state()
    out = densify_and_prune(original, T([.9, .8, .7]), .1, .1, 3)
    assert len(out['means']) == 3
    torch.testing.assert_close(out['means'], T([[0., 0., 2.], [2., 0., 4.], [.2, 0., 2.]]))
    torch.testing.assert_close(out['log_scales'][0], original['log_scales'][0] - math.log(2))
    torch.testing.assert_close(out['log_scales'][1], original['log_scales'][2])


def test_s5_all_pruned_returns_well_shaped_empty_state():
    out = densify_and_prune(state(), T([0., 0., 0.]), .1, .99, 10)
    assert out['means'].shape == out['log_scales'].shape == (0, 3)
    assert out['sh_coeffs'].shape == (0, 16, 3)
    assert out['quaternions'].shape == (0, 4) and out['opacity_logits'].shape == (0,)


@pytest.mark.parametrize('bad', ['negative_gradient', 'negative_threshold', 'bad_cap'])
def test_s5_invalid_densification_parameters(bad):
    gradient, threshold, cap = T([.1, .2, .3]), .2, 10
    if bad == 'negative_gradient':
        gradient[0] = -1.
    elif bad == 'negative_threshold':
        threshold = -.1
    else:
        cap = 0
    with pytest.raises(ValueError):
        densify_and_prune(state(), gradient, threshold, .1, cap)
