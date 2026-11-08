"""S6 independent real-SH acceptance: Legendre reference, axes and autograd.

The reference uses associated Legendre functions, not the implementation's
Cartesian polynomial expansion. It is test-only and never used by the runner.
"""
import math

import numpy as np
import pytest
import torch
from scipy.special import lpmv

from minicolmap.rendering.algorithms import evaluate_sh, initialize_gaussians, splat_gaussians


def real_basis(directions, degree):
    directions = np.asarray(directions, dtype=np.float64)
    directions = directions / np.linalg.norm(directions, axis=-1, keepdims=True)
    phi = np.arctan2(directions[:, 1], directions[:, 0])
    z = directions[:, 2]
    columns = []
    for ell in range(degree + 1):
        for m in range(-ell, ell + 1):
            k = abs(m)
            norm = math.sqrt((2 * ell + 1) / (4 * math.pi)
                             * math.factorial(ell-k) / math.factorial(ell+k))
            value = norm * lpmv(k, ell, z)
            if m < 0:
                value *= math.sqrt(2) * np.sin(k * phi)
            elif m > 0:
                value *= math.sqrt(2) * np.cos(k * phi)
            columns.append(value)
    return np.stack(columns, axis=-1)


@pytest.mark.parametrize('degree', range(4))
def test_s6_all_bands_match_independent_legendre_reference(degree):
    rng = np.random.default_rng(86)
    directions = rng.normal(size=(24, 3))
    coeff = rng.normal(scale=.06, size=(24, 16, 3))
    expected = np.maximum(.5 + np.einsum('gb,gbc->gc', real_basis(directions, degree),
                                       coeff[:, :(degree+1)**2]), 0)
    actual = evaluate_sh(torch.tensor(coeff), torch.tensor(directions), degree)
    np.testing.assert_allclose(actual.detach().numpy(), expected, atol=1e-12, rtol=1e-12)


@pytest.mark.parametrize('index,axis,sign', [(1, [0., 1., 0.], -1),
    (2, [0., 0., 1.], 1), (3, [1., 0., 0.], -1)])
def test_s6_first_order_axes_and_direction_reversal(index, axis, sign):
    coeff = torch.zeros((2, 16, 3), dtype=torch.float64)
    coeff[:, index, 0] = .2
    directions = torch.tensor([axis, [-v for v in axis]], dtype=torch.float64)
    result = evaluate_sh(coeff, directions, 1)
    expected = .5 + sign * .2 * math.sqrt(3 / (4 * math.pi))
    assert result[0, 0].item() == pytest.approx(expected)
    assert result[1, 0].item() == pytest.approx(1 - expected)
    torch.testing.assert_close(result[:, 1:], torch.full((2, 2), .5, dtype=torch.float64))


@pytest.mark.parametrize('degree', range(4))
def test_s1_allocates_dc_and_zero_rest_at_each_capacity(degree):
    rgb = torch.tensor([[0., .5, 1.]], dtype=torch.float64)
    state = initialize_gaussians(torch.tensor([[0., 0., 2.]], dtype=torch.float64), rgb,
                                 sh_degree=degree)
    coefficients = state['sh_coeffs']
    assert coefficients.shape == (1, (degree+1)**2, 3)
    assert coefficients.is_leaf and coefficients.requires_grad
    expected = torch.zeros_like(coefficients)
    expected[:, 0] = (rgb - .5) * math.sqrt(4 * math.pi)
    torch.testing.assert_close(coefficients, expected)
    # Mutating the state must not change the input RGB array.
    with torch.no_grad():
        coefficients[:, 0] = 4.
    torch.testing.assert_close(rgb, torch.tensor([[0., .5, 1.]], dtype=torch.float64))


@pytest.mark.parametrize('degree', [-1, 4, 1.5, True])
def test_s1_rejects_invalid_sh_degree(degree):
    with pytest.raises(ValueError):
        initialize_gaussians(torch.zeros((1, 3)), torch.full((1, 3), .5), sh_degree=degree)


def test_s6_dc_is_view_independent_and_normalizes_nonunit_directions():
    coeff = torch.zeros((2, 1, 3), dtype=torch.float64)
    coeff[:, 0] = torch.tensor([-.3, .1, .5])
    directions = torch.tensor([[3., 1., 2.], [-1., 7., 4.]], dtype=torch.float64)
    expected = .5 + coeff[:, 0] / math.sqrt(4 * math.pi)
    torch.testing.assert_close(evaluate_sh(coeff, directions, 0), expected)


def test_s6_gradients_include_direction_normalization_and_exclude_inactive_bands():
    generator = torch.Generator().manual_seed(28)
    coeff = (torch.randn((2, 16, 3), generator=generator, dtype=torch.float64) * .03).requires_grad_()
    directions = torch.tensor([[.3, -.4, 1.1], [-.6, .2, .8]], dtype=torch.float64, requires_grad=True)
    assert torch.autograd.gradcheck(lambda c, d: evaluate_sh(c, d, 2), (coeff, directions))
    evaluate_sh(coeff, directions, 2).sum().backward()
    assert coeff.grad[:, :9].abs().sum() > 0
    assert torch.count_nonzero(coeff.grad[:, 9:]) == 0
    assert directions.grad.abs().sum() > 0
    # Normalization makes radial scaling invisible: gradient is tangent to sphere.
    torch.testing.assert_close((directions.grad * directions).sum(-1), torch.zeros(2, dtype=torch.float64),
                               atol=1e-10, rtol=0)


def test_s6_original_color_activation_clamps_only_negative_values():
    coeff = torch.tensor([[[-4., 0., 5.]]], dtype=torch.float64, requires_grad=True)
    result = evaluate_sh(coeff, torch.tensor([[1., 0., 0.]], dtype=torch.float64), 0)
    expected = torch.tensor([[0., .5, .5 + 5/math.sqrt(4*math.pi)]], dtype=torch.float64)
    torch.testing.assert_close(result, expected)
    result.sum().backward()
    assert coeff.grad[0, 0, 0] == 0
    assert coeff.grad[0, 0, 2] > 0


def test_s4_accepts_unclipped_sh_radiance_above_one():
    out = splat_gaussians(torch.zeros((1, 2), dtype=torch.float64),
        torch.eye(2, dtype=torch.float64)[None], torch.tensor([2.], dtype=torch.float64),
        torch.tensor([[2., .5, 0.]], dtype=torch.float64), torch.tensor([.8], dtype=torch.float64),
        1, 1, torch.zeros(3, dtype=torch.float64))
    torch.testing.assert_close(out['rgb'], torch.tensor([[[1.6, .4, 0.]]], dtype=torch.float64))


def test_s6_empty_batch_preserves_device_and_dtype():
    result = evaluate_sh(torch.empty((0, 16, 3), dtype=torch.float64),
                         torch.empty((0, 3), dtype=torch.float64), 3)
    assert result.shape == (0, 3) and result.dtype == torch.float64 and result.device.type == 'cpu'


def test_s6_adam_fits_direction_dependent_colors_and_generalizes_to_held_out_directions():
    rng = np.random.default_rng(27)
    directions = rng.normal(size=(30, 3))
    basis = real_basis(directions, 1)
    truth = np.array([[.1, -.2, .3], [.2, .1, -.3], [-.2, .3, .1], [.3, -.1, .2]])
    target = torch.tensor(.5 + basis @ truth, dtype=torch.float64)
    coefficients = torch.zeros((1, 16, 3), dtype=torch.float64, requires_grad=True)
    optimizer = torch.optim.Adam([coefficients], lr=.04)
    directions = torch.tensor(directions, dtype=torch.float64)
    errors = []
    for _ in range(160):
        optimizer.zero_grad()
        predicted = evaluate_sh(coefficients.expand(24, -1, -1), directions[:24], 1)
        loss = (predicted - target[:24]).square().mean()
        errors.append(loss.detach().item())
        loss.backward()
        optimizer.step()
    assert errors[-1] < errors[0] * 1e-4
    held_out = evaluate_sh(coefficients.expand(6, -1, -1), directions[24:], 1)
    torch.testing.assert_close(held_out, target[24:], atol=1e-3, rtol=0)
    assert torch.count_nonzero(coefficients[:, 4:]) == 0


@pytest.mark.parametrize('bad', ['negative_degree', 'float_degree', 'bool_degree', 'excess_degree',
    'insufficient_bases', 'bad_capacity', 'wrong_rgb_axis', 'batch_mismatch', 'dtype',
    'zero_direction', 'nan_coeff', 'inf_direction'])
def test_s6_rejects_invalid_inputs(bad):
    coeff, directions, degree = torch.zeros((1, 16, 3)), torch.tensor([[1., 0., 0.]]), 3
    if bad == 'negative_degree': degree = -1
    elif bad == 'float_degree': degree = 1.5
    elif bad == 'bool_degree': degree = True
    elif bad == 'excess_degree': degree = 4
    elif bad == 'insufficient_bases': coeff = coeff[:, :9]
    elif bad == 'bad_capacity': coeff = coeff[:, :5]
    elif bad == 'wrong_rgb_axis': coeff = coeff.transpose(1, 2)
    elif bad == 'batch_mismatch': directions = directions.expand(2, 3)
    elif bad == 'dtype': directions = directions.double()
    elif bad == 'zero_direction': directions.zero_()
    elif bad == 'nan_coeff': coeff[0, 0, 0] = float('nan')
    elif bad == 'inf_direction': directions[0, 0] = float('inf')
    with pytest.raises(ValueError):
        evaluate_sh(coeff, directions, degree)
