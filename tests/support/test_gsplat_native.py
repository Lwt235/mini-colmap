"""Opt-in tests of the actual compiled CUDA backend (not protocol substitutes)."""
import os

import pytest

torch = pytest.importorskip('torch')
pytestmark = pytest.mark.skipif(os.environ.get('MINICOLMAP_TEST_GSPLAT') != '1',
    reason='Set MINICOLMAP_TEST_GSPLAT=1 after building the optional native extension')

from minicolmap.rendering.algorithms import initialize_gaussians
from minicolmap.rendering.config import resolve_render_config
from minicolmap.rendering.gaussian_backend import rasterize_gsplat
from minicolmap.rendering.pipeline import _render


def test_native_kernel_uses_the_same_integer_pixel_center_as_the_project():
    device = 'cuda'
    rgb = rasterize_gsplat(torch.tensor([[0., 0., 2.]], device=device),
        torch.eye(3, device=device)[None] * .04, torch.tensor([[.2, .4, .8]], device=device),
        torch.tensor([.5], device=device), torch.tensor([[4., 0., 3.], [0., 4., 3.], [0., 0., 1.]], device=device),
        torch.eye(3, device=device), torch.zeros(3, device=device), 7, 7, torch.zeros(3, device=device))
    # The central pixel has footprint exactly one, independent of the covariance.
    torch.testing.assert_close(rgb[3, 3], torch.tensor([.1, .2, .4], device=device), atol=1e-6, rtol=0)
    torch.testing.assert_close(rgb[2, 3], rgb[4, 3], atol=1e-6, rtol=0)
    torch.testing.assert_close(rgb[3, 2], rgb[3, 4], atol=1e-6, rtol=0)


def test_native_and_reference_agree_and_native_backpropagates_into_all_student_parameters():
    state = initialize_gaussians(torch.tensor([[.1, -.1, 2.], [-.2, .15, 2.5]], device='cuda'),
        torch.tensor([[.8, .2, .1], [.1, .5, .7]], device='cuda'), initial_scale=.1, initial_opacity=.3)
    frame = dict(K=[[6., 0., 4.], [0., 6., 3.], [0., 0., 1.]],
        R=[[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]], t=[0., 0., 0.], width=9, height=7)
    outputs = []
    for backend in ('reference', 'gsplat'):
        config = resolve_render_config({'method': '3dgs', 'device': 'cuda', 'background': 'black',
                                        'gaussian': {'backend': backend}})
        outputs.append(_render(None, state, frame, config, torch, 'cuda'))
    # gsplat omits alpha tails below 1/255; the reference intentionally keeps them.
    assert (outputs[0] - outputs[1]).abs().max() < .004
    outputs[1].square().mean().backward()
    for name, value in state.items():
        assert value.grad is not None and torch.isfinite(value.grad).all(), name
    assert state['opacity_logits'].grad.abs().sum() > 0


def test_native_image_plane_gradient_is_available_for_density_control():
    from minicolmap.rendering.gaussian_training import DensityStatistics
    state = initialize_gaussians(torch.tensor([[.1, -.1, 2.], [-.2, .15, 2.5]], device='cuda'),
        torch.tensor([[.8, .2, .1], [.1, .5, .7]], device='cuda'), initial_scale=.1, initial_opacity=.3)
    frame = dict(K=[[6., 0., 4.], [0., 6., 3.], [0., 0., 1.]], R=torch.eye(3),
                 t=[0., 0., 0.], width=9, height=7)
    config = resolve_render_config({'method': '3dgs', 'device': 'cuda', 'background': 'black',
                                    'gaussian': {'backend': 'gsplat', 'density_strategy': 'adaptive'}})
    info = {}
    rgb = _render(None, state, frame, config, torch, 'cuda', training_info=info)
    rgb.square().mean().backward()
    statistics = DensityStatistics(state)
    statistics.accumulate(info)
    assert statistics.count.sum() > 0
    assert statistics.gradient.sum() > 0 and torch.isfinite(statistics.gradient).all()
