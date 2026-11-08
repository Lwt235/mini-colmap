"""Real CUDA numerical/gradient parity; skipped when the optional GPU is absent."""
import copy

import pytest

torch = pytest.importorskip('torch')
pytestmark = pytest.mark.skipif(not torch.cuda.is_available(), reason='CUDA GPU unavailable')

from minicolmap.rendering.algorithms import (
    build_nerf, generate_rays, positional_encoding, volume_render,
)


@pytest.mark.parametrize('dtype', [torch.float32, torch.float64])
def test_cuda_rotated_rays_and_encoding_gradients_match_cpu(dtype):
    results = []
    for device in ('cpu', 'cuda'):
        K = torch.tensor([[2., .3, 1.], [0., 3., 2.], [0., 0., 1.]], dtype=dtype, device=device)
        R = torch.tensor([[0., 0., 1.], [0., 1., 0.], [-1., 0., 0.]], dtype=dtype, device=device)
        t = torch.tensor([3., -2., 1.], dtype=dtype, device=device)
        pixels = torch.tensor([[1.4, 2.2], [.3, 1.1]], dtype=dtype, device=device, requires_grad=True)
        origins, directions = generate_rays(K, R, t, pixels)
        encoded = positional_encoding(directions, 4)
        (encoded * torch.linspace(.1, 1., encoded.numel(), dtype=dtype, device=device).reshape_as(encoded)).sum().backward()
        assert encoded.device.type == device
        results.append([v.detach().cpu() for v in (origins, directions, encoded, pixels.grad)])
    for cpu, gpu in zip(*results):
        torch.testing.assert_close(cpu, gpu, atol=2e-5, rtol=2e-4)


@pytest.mark.parametrize('dtype', [torch.float32, torch.float64])
def test_cuda_compositing_values_and_gradients_match_cpu(dtype):
    results = []
    for device in ('cpu', 'cuda'):
        rgb = torch.linspace(.05, .95, 18, dtype=dtype, device=device).reshape(2, 3, 3).requires_grad_()
        sigma = torch.tensor([[0., .7, 20.], [1e-6, 2., 3.]], dtype=dtype, device=device, requires_grad=True)
        times = torch.tensor([[.1, .4, .9], [.2, .2, .6]], dtype=dtype, device=device)
        far = torch.tensor([1.5, 1.], dtype=dtype, device=device)
        rendered = volume_render(rgb, sigma, times, far, torch.ones(3, dtype=dtype, device=device))
        (rendered['rgb'].square().sum() + .2 * rendered['depth'].sum()).backward()
        assert all(v.device.type == device for v in rendered.values())
        results.append([v.detach().cpu() for v in (*rendered.values(), rgb.grad, sigma.grad)])
    for cpu, gpu in zip(*results):
        torch.testing.assert_close(cpu, gpu, atol=2e-6, rtol=2e-5)


@pytest.mark.parametrize('dtype', [torch.float32, torch.float64])
def test_cuda_network_checkpoint_forward_and_backward_match_cpu(dtype):
    torch.manual_seed(19)
    cpu = build_nerf(3, 2, 16).to(dtype=dtype)
    gpu = copy.deepcopy(cpu).to('cuda')
    xyz = torch.tensor([[.12, -.2, .4], [.2, .1, -.1]], dtype=dtype)
    directions = torch.tensor([[0., 0., 1.], [0., .6, .8]], dtype=dtype)
    outputs, gradients = [], []
    for network, device in ((cpu, 'cpu'), (gpu, 'cuda')):
        positions = xyz.to(device).clone().requires_grad_()
        dirs = directions.to(device).clone().requires_grad_()
        rgb, density = network(positions, dirs)
        (rgb.square().sum() + .1 * density.square().sum()).backward()
        assert rgb.device.type == density.device.type == device
        outputs.append([rgb.detach().cpu(), density.detach().cpu()])
        values = [positions.grad, dirs.grad, *(p.grad for p in network.parameters())]
        assert all(v is not None and torch.isfinite(v).all() for v in values)
        gradients.append([v.detach().cpu() for v in values])
    for a, b in zip(outputs[0] + gradients[0], outputs[1] + gradients[1]):
        torch.testing.assert_close(a, b, atol=3e-6, rtol=3e-4)
