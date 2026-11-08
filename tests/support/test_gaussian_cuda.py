"""CUDA regressions for Gaussian state and the reference renderer."""
import pytest

torch = pytest.importorskip('torch')
pytestmark = pytest.mark.skipif(not torch.cuda.is_available(), reason='CUDA GPU unavailable')

from minicolmap.rendering.algorithms import (
    densify_and_prune, evaluate_sh, initialize_gaussians,
)
from minicolmap.rendering.config import resolve_render_config
from minicolmap.rendering.pipeline import _render


@pytest.mark.parametrize('dtype', [torch.float32, torch.float64])
@pytest.mark.parametrize('count', [0, 2])
def test_cuda_initialization_keeps_every_parameter_on_input_device(dtype, count):
    points = torch.tensor([[.1, .2, 2.], [-.2, .1, 3.]], dtype=dtype)[:count]
    colors = torch.tensor([[.2, .4, .6], [.8, .3, .1]], dtype=dtype)[:count]
    cpu = initialize_gaussians(points, colors)
    gpu_points, gpu_colors = points.cuda(), colors.cuda()
    gpu = initialize_gaussians(gpu_points, gpu_colors)
    for key, parameter in gpu.items():
        assert parameter.device == gpu_points.device, f'{key} was initialized on {parameter.device}'
        assert parameter.dtype == dtype and parameter.is_leaf and parameter.requires_grad
        torch.testing.assert_close(parameter.cpu(), cpu[key])
    # This is the exact mask/index combination that failed in real training.
    gpu['opacity_logits'].sigmoid()[gpu['means'][:, 2] > 0]


def test_cuda_reference_pipeline_forward_backward_and_adam_match_cpu():
    dtype = torch.float32  # The pipeline deliberately prepares float32 images/cameras.
    config = resolve_render_config({'method': '3dgs', 'background': 'black'})
    frame = dict(width=5, height=4, K=[[5., .1, 2.], [0., 6., 1.5], [0., 0., 1.]],
                 R=[[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]], t=[-.1, .1, .2])
    outputs, gradients, updated = [], [], []
    for device in ('cpu', 'cuda'):
        points = torch.tensor([[.1, .2, 2.], [-.2, .1, 2.8], [0., 0., -2.]], dtype=dtype, device=device)
        colors = torch.tensor([[.2, .4, .6], [.7, .3, .1], [.5, .5, .5]], dtype=dtype, device=device)
        state = initialize_gaussians(points, colors, initial_scale=.15, initial_opacity=.3)
        with torch.no_grad():
            # Nonzero higher bands exercise S6 direction gradients as well as DC.
            state['sh_coeffs'][:, 1:4] = .02
            state['log_scales'][:, 1] += .3
            state['quaternions'][:, 3] = .15
        optimizer = torch.optim.Adam(list(state.values()), lr=1e-3)
        prediction = _render(None, state, frame, config, torch, device)
        assert prediction.device.type == device and torch.isfinite(prediction).all()
        (prediction - .1).square().mean().backward()
        assert all(v.grad is not None and torch.isfinite(v.grad).all() for v in state.values())
        assert state['opacity_logits'].grad.abs().sum() > 0
        outputs.append(prediction.detach().cpu())
        gradients.append([v.grad.detach().cpu() for v in state.values()])
        optimizer.step()
        updated.append([v.detach().cpu() for v in state.values()])
    for a, b in zip([outputs[0], *gradients[0], *updated[0]], [outputs[1], *gradients[1], *updated[1]]):
        torch.testing.assert_close(a, b, atol=3e-6, rtol=3e-4)


@pytest.mark.parametrize('degree', [0, 1, 2, 3])
def test_cuda_sh_color_and_both_gradients_match_cpu(degree):
    results = []
    for device in ('cpu', 'cuda'):
        coefficients = (torch.arange(96, dtype=torch.float64, device=device).reshape(2, 16, 3)
                        / 3000).requires_grad_()
        directions = torch.tensor([[.2, -.3, 1.], [.7, .1, .4]], dtype=torch.float64,
                                  device=device, requires_grad=True)
        rgb = evaluate_sh(coefficients, directions, degree)
        rgb.square().sum().backward()
        direction_grad = directions.grad if directions.grad is not None else torch.zeros_like(directions)
        if degree > 0:
            assert direction_grad.abs().sum() > 0
        results.append([v.detach().cpu() for v in (rgb, coefficients.grad, direction_grad)])
    for a, b in zip(*results):
        torch.testing.assert_close(a, b, atol=1e-12, rtol=1e-10)


def test_cuda_topology_update_preserves_devices_and_all_sh_bands():
    points = torch.tensor([[0., 0., 2.], [.2, 0., 3.]], device='cuda')
    state = initialize_gaussians(points, torch.full_like(points, .4))
    revised = densify_and_prune(state, torch.tensor([.8, .1], device='cuda'), .3, .01, 3)
    assert len(revised['means']) == 3
    for key, value in revised.items():
        assert value.device == points.device and value.is_leaf and value.requires_grad
        if key == 'sh_coeffs':
            torch.testing.assert_close(value[-1], value[0])


def test_gsplat_missing_extension_is_an_actionable_error(monkeypatch):
    import sys
    from types import ModuleType
    from minicolmap.rendering.gaussian_backend import rasterize_gsplat
    monkeypatch.setattr('minicolmap.rendering.gsplat_runtime.load_project_extension', lambda: None)
    package, cuda, backend = (ModuleType(name) for name in ('gsplat', 'gsplat.cuda', 'gsplat.cuda._backend'))
    package.rasterization = lambda **kwargs: pytest.fail('An unavailable extension must not be called')
    backend._C = None
    for name, module in (('gsplat', package), ('gsplat.cuda', cuda), ('gsplat.cuda._backend', backend)):
        monkeypatch.setitem(sys.modules, name, module)
    with pytest.raises(RuntimeError, match='CUDA 光栅扩展未加载'):
        rasterize_gsplat(torch.zeros((1, 3), device='cuda'), None, None, None,
                         None, None, None, 1, 1, None)
