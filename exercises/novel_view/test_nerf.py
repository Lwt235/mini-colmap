"""Public numerical acceptance tests for N1-N4; no datasets or mock renderer."""
from __future__ import annotations

import math

import pytest
import torch

from minicolmap.rendering.algorithms import (
    build_nerf, generate_rays, positional_encoding, volume_render,
)


def T(value, *, grad=False):
    return torch.tensor(value, dtype=torch.float64, requires_grad=grad)


def camera():
    return T([[2., .3, 1.], [0., 3., 2.], [0., 0., 1.]]), torch.eye(3, dtype=torch.float64), T([0., 0., 0.])


def test_n4_default_deep_initialization_can_learn_density_with_six_encoding_levels():
    # This real training configuration previously returned zero density for
    # every sample under seed=0, permanently cutting the density gradient.
    torch.manual_seed(0)
    model = build_nerf(6, 4, 256)
    points = torch.linspace(-1, 1, 192).reshape(64, 3)
    directions = torch.nn.functional.normalize(torch.ones_like(points), dim=-1)
    _, density = model(points, directions)
    assert torch.count_nonzero(density) > 0
    (density - .5).square().mean().backward()
    assert model.density_head.weight.grad is not None
    assert torch.count_nonzero(model.density_head.weight.grad) > 0


def test_n1_principal_ray_has_no_half_pixel_shift():
    K, R, t = camera()
    origins, directions = generate_rays(K, R, t, T([[1., 2.]]))
    torch.testing.assert_close(origins, T([[0., 0., 0.]]))
    torch.testing.assert_close(directions, T([[0., 0., 1.]]))


def test_n1_rotated_translated_camera_and_skew():
    K, _, _ = camera()
    # world x becomes camera -z; principal viewing direction is world -x.
    R = T([[0., 0., 1.], [0., 1., 0.], [-1., 0., 0.]])
    t = T([3., -2., 1.])
    origins, directions = generate_rays(K, R, t, T([[1., 2.], [3.3, 5.]]))
    expected_origin = T([[1., 2., -3.], [1., 2., -3.]])
    expected_dirs = T([[-1., 0., 0.], [-1., 1., 1.]])
    expected_dirs = expected_dirs / torch.linalg.vector_norm(expected_dirs, dim=-1, keepdim=True)
    torch.testing.assert_close(origins, expected_origin)
    torch.testing.assert_close(directions, expected_dirs)


@pytest.mark.parametrize('dtype', [torch.float32, torch.float64])
def test_n1_batch_unit_vectors_dtype_and_input_unchanged(dtype):
    K, R, t = [v.to(dtype=dtype) for v in camera()]
    pixels = T([[0., 0.], [3., 4.], [2., 7.]]).to(dtype=dtype)
    inputs = [v.clone() for v in (K, R, t, pixels)]
    origins, dirs = generate_rays(K, R, t, pixels)
    assert origins.shape == dirs.shape == (3, 3)
    assert origins.dtype == dirs.dtype == dtype
    assert origins.device == dirs.device == pixels.device
    torch.testing.assert_close(torch.linalg.vector_norm(dirs, dim=-1), torch.ones(3, dtype=dtype))
    for after, before in zip((K, R, t, pixels), inputs):
        torch.testing.assert_close(after, before)


def test_n1_empty_batch():
    K, R, t = camera()
    origins, dirs = generate_rays(K, R, t, torch.empty((0, 2), dtype=torch.float64))
    assert origins.shape == dirs.shape == (0, 3)


def test_n1_pixel_gradients_match_finite_difference():
    K, R, t = camera()
    pixels = T([[1.4, 2.2], [.3, 1.1]], grad=True)
    assert torch.autograd.gradcheck(lambda x: generate_rays(K, R, t, x)[1], (pixels,))


@pytest.mark.parametrize('bad', ['focal', 'rotation', 'pixels', 'nan'])
def test_n1_invalid_camera_or_pixels(bad):
    K, R, t = camera()
    pixels = T([[1., 2.]])
    if bad == 'focal':
        K[0, 0] = 0.
    elif bad == 'rotation':
        R[0, 0] = 2.
    elif bad == 'pixels':
        pixels = torch.zeros((2, 3), dtype=torch.float64)
    else:
        pixels[0, 0] = float('nan')
    with pytest.raises(ValueError):
        generate_rays(K, R, t, pixels)


def test_n2_frequency_order_and_pi():
    encoded = positional_encoding(T([[0., .5]]), levels=2)
    # Hand-computed order: x, sin(pi*x), cos(pi*x), sin(2*pi*x), cos(2*pi*x).
    expected = T([[0., .5, 0., 1., 1., 0., 0., 0., 1., -1.]])
    torch.testing.assert_close(encoded, expected, atol=1e-12, rtol=1e-12)


def test_n2_leading_dimensions_and_exclude_input():
    x = torch.arange(12, dtype=torch.float64).reshape(2, 2, 3) / 7.
    actual = positional_encoding(x, 2, include_input=False)
    assert actual.shape == (2, 2, 12)
    torch.testing.assert_close(actual[..., :3], torch.sin(math.pi * x))
    torch.testing.assert_close(actual[..., -3:], torch.cos(2 * math.pi * x))


@pytest.mark.parametrize('include_input', [True, False])
def test_n2_zero_levels(include_input):
    x = T([[.2, .7, .9]])
    out = positional_encoding(x, 0, include_input)
    assert out.shape == (1, 3 if include_input else 0)
    if include_input:
        torch.testing.assert_close(out, x)


def test_n2_gradients():
    x = T([[.13, .27, -.44]], grad=True)
    assert torch.autograd.gradcheck(lambda v: positional_encoding(v, 3), (x,))


@pytest.mark.parametrize('levels', [-1, 1.5, True])
def test_n2_invalid_levels(levels):
    with pytest.raises(ValueError):
        positional_encoding(T([[0., 0., 0.]]), levels)


def test_n3_constant_medium_matches_closed_form():
    rgb = T([[[.2, .4, .8]] * 3])
    sigma = T([[.7, .7, .7]])
    out = volume_render(rgb, sigma, T([[1., 2., 4.]]), 6., T([1., 1., 1.]))
    trans = math.exp(-.7 * 5.)
    expected_color = T([[.2, .4, .8]]) * (1 - trans) + trans
    torch.testing.assert_close(out['rgb'], expected_color)
    torch.testing.assert_close(out['opacity'], T([1 - trans]))
    torch.testing.assert_close(out['weights'].sum(-1), out['opacity'])


def test_n3_two_samples_exclusive_transmittance_and_depth():
    # Each segment alpha is 0.5. The second weight must be 0.25, not 0.5.
    out = volume_render(T([[[1., 0., 0.], [0., 1., 0.]]]),
                        T([[math.log(2), math.log(2)]]), T([[1., 2.]]), 3., T([0., 0., 1.]))
    torch.testing.assert_close(out['weights'], T([[.5, .25]]))
    torch.testing.assert_close(out['rgb'], T([[.5, .25, .25]]))
    torch.testing.assert_close(out['depth'], T([1.]))
    torch.testing.assert_close(out['opacity'], T([.75]))


def test_n3_zero_density_and_zero_last_interval():
    out = volume_render(torch.ones((2, 2, 3), dtype=torch.float64),
                        T([[0., 0.], [0., 100.]]), T([[1., 3.], [1., 3.]]),
                        T([4., 3.]), T([.1, .2, .3]))
    torch.testing.assert_close(out['rgb'], T([[.1, .2, .3], [.1, .2, .3]]))
    torch.testing.assert_close(out['depth'], T([0., 0.]))
    torch.testing.assert_close(out['opacity'], T([0., 0.]))


def test_n3_single_sample_and_per_ray_far():
    out = volume_render(T([[[1., 0., 0.]], [[0., 1., 0.]]]),
                        T([[math.log(2)], [math.log(2)]]), T([[1.], [2.]]),
                        T([2., 4.]), T([0., 0., 0.]))
    torch.testing.assert_close(out['opacity'], T([.5, .75]))
    torch.testing.assert_close(out['depth'], T([.5, 1.5]))
    assert out['rgb'].shape == (2, 3)
    assert out['weights'].shape == (2, 1)


def test_n3_empty_ray_batch():
    out = volume_render(torch.empty((0, 2, 3), dtype=torch.float64),
                        torch.empty((0, 2), dtype=torch.float64),
                        torch.empty((0, 2), dtype=torch.float64), 5., T([0., 0., 0.]))
    assert out['rgb'].shape == (0, 3)
    assert out['weights'].shape == (0, 2)
    assert out['depth'].shape == out['opacity'].shape == (0,)


def test_n3_large_optical_density_stays_finite():
    out = volume_render(T([[[.2, .4, .6], [1., 1., 1.]]]),
                        T([[1e6, 1e6]]), T([[1., 2.]]), 3., T([1., 1., 1.]))
    for value in out.values():
        assert torch.isfinite(value).all()
    torch.testing.assert_close(out['rgb'], T([[.2, .4, .6]]))
    torch.testing.assert_close(out['weights'], T([[1., 0.]]))


@pytest.mark.parametrize('bad', ['negative_density', 'descending_t', 'far_before_last', 'nan', 'shape'])
def test_n3_rejects_invalid_samples(bad):
    rgb, sigma, ts, far = T([[[.1, .2, .3], [.3, .2, .1]]]), T([[1., 1.]]), T([[1., 2.]]), 3.
    if bad == 'negative_density':
        sigma[0, 0] = -1.
    elif bad == 'descending_t':
        ts = T([[2., 1.]])
    elif bad == 'far_before_last':
        far = 1.5
    elif bad == 'nan':
        rgb[0, 0, 0] = float('nan')
    else:
        sigma = T([1., 1.])
    with pytest.raises(ValueError):
        volume_render(rgb, sigma, ts, far, T([0., 0., 0.]))


def test_n3_color_density_and_position_gradients():
    rgb = T([[[.2, .5, .7], [.6, .4, .3]]], grad=True)
    sigma, ts = T([[.4, .8]], grad=True), T([[1., 2.]], grad=True)
    assert torch.autograd.gradcheck(
        lambda c, s, t: volume_render(c, s, t, 4., T([.1, .2, .3]))['rgb'], (rgb, sigma, ts))


def test_n3_real_gradient_descent_reduces_rendering_loss():
    # Optimize actual composited color through logits, not an analytic surrogate.
    logits = torch.nn.Parameter(T([[[0., 0., 0.], [0., 0., 0.]]]))
    optimizer = torch.optim.Adam([logits], lr=.15)
    target = T([[.8, .1, .2]])
    history = []
    for _ in range(30):
        optimizer.zero_grad()
        out = volume_render(logits.sigmoid(), T([[4., 4.]]), T([[1., 2.]]), 3., T([0., 0., 0.]))
        loss = (out['rgb'] - target).square().mean()
        history.append(float(loss.detach()))
        loss.backward()
        optimizer.step()
    assert history[-1] < history[0] * .15


def test_n4_module_shapes_bounds_and_registered_parameters():
    torch.manual_seed(13)
    model = build_nerf(2, 1, 24).to(dtype=torch.float64)
    assert isinstance(model, torch.nn.Module)
    params = list(model.parameters())
    assert params and all(p.requires_grad for p in params)
    rgb, sigma = model(T([[.1, .2, .3], [.3, .4, -.5]]), T([[0., 0., 1.], [1., 0., 0.]]))
    assert rgb.shape == (2, 3) and sigma.shape == (2,)
    assert rgb.dtype == sigma.dtype == torch.float64
    assert torch.isfinite(rgb).all() and torch.isfinite(sigma).all()
    assert ((rgb >= 0) & (rgb <= 1)).all() and (sigma >= 0).all()
    assert model.state_dict()


def test_n4_density_independent_of_direction_but_color_uses_direction():
    torch.manual_seed(11)
    model = build_nerf(2, 2, 32).double().eval()
    xyz = T([[.1, .2, .3]]).repeat(4, 1)
    dirs = T([[0., 0., 1.], [0., 1., 0.], [1., 0., 0.], [-1., 0., 0.]])
    rgb, sigma = model(xyz, dirs)
    torch.testing.assert_close(sigma, sigma[:1].expand_as(sigma), rtol=0, atol=1e-12)
    assert torch.max(torch.abs(rgb - rgb[:1])) > 1e-8, 'The color head must consume direction encoding'


def test_n4_autograd_reaches_positions_directions_and_parameters():
    torch.manual_seed(17)
    model = build_nerf(2, 2, 32).double()
    xyz = T([[.1, .2, .3], [.4, -.1, .5]], grad=True)
    dirs = T([[0., 0., 1.], [1., 0., 0.]], grad=True)
    rgb, sigma = model(xyz, dirs)
    (rgb.square().sum() + sigma.sum()).backward()
    for value in (xyz, dirs):
        assert value.grad is not None and torch.isfinite(value.grad).all()
        assert value.grad.abs().sum() > 0
    assert any(p.grad is not None and p.grad.abs().sum() > 0 for p in model.parameters())


def test_n4_state_dict_roundtrip():
    torch.manual_seed(3)
    model = build_nerf(1, 1, 16).double().eval()
    other = build_nerf(1, 1, 16).double().eval()
    other.load_state_dict(model.state_dict())
    xyz, dirs = T([[.1, -.2, .3]]), T([[0., 0., 1.]])
    for expected, actual in zip(model(xyz, dirs), other(xyz, dirs)):
        torch.testing.assert_close(actual, expected)


@pytest.mark.parametrize('args', [(-1, 1, 16), (1, -1, 16), (1, 1, 0), (1, 1, 3)])
def test_n4_invalid_architecture(args):
    with pytest.raises(ValueError):
        build_nerf(*args)
