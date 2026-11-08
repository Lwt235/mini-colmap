"""Trained float32 ellipsoids must remain usable in CPU checkpoint previews."""
import copy

import cv2
import numpy as np
import pytest

torch = pytest.importorskip('torch')

from minicolmap.rendering import algorithms as learner
from minicolmap.rendering.config import resolve_render_config
from minicolmap.rendering.pipeline import _render, _render_gaussian_preview, render_checkpoint


def ellipsoids(dtype=torch.float32):
    generator = torch.Generator().manual_seed(73)
    scales = (torch.randn((128, 3), generator=generator) * .8 - 2).to(dtype).requires_grad_()
    rotations = torch.randn((128, 4), generator=generator).to(dtype).requires_grad_()
    means = torch.randn((128, 3), generator=generator).to(dtype)
    means[:, 2] = torch.linspace(1, 3, 128, dtype=dtype)
    return means, scales, rotations


@pytest.mark.parametrize('dtype', [torch.float32, torch.float64])
def test_rotated_ellipsoids_and_large_pixel_covariances_remain_exactly_symmetric(dtype):
    means, scales, rotations = ellipsoids(dtype)
    covariance = learner.gaussian_covariance(scales, rotations)
    torch.testing.assert_close(covariance, covariance.transpose(-1, -2), rtol=0, atol=0)
    K = torch.tensor([[900., 13., 320.], [0., 850., 240.], [0., 0., 1.]], dtype=dtype)
    R = torch.tensor([[.8, -.6, 0.], [.6, .8, 0.], [0., 0., 1.]], dtype=dtype)
    projected = learner.project_gaussians(means, covariance, K, R, torch.tensor([.1, -.2, .3], dtype=dtype))
    cov2d = projected['cov2d']
    torch.testing.assert_close(cov2d, cov2d.transpose(-1, -2), rtol=0, atol=0)
    assert cov2d.abs().max() > 10000  # Pixel-space values where absolute-only tolerances used to fail.
    assert torch.all(torch.linalg.cholesky_ex(cov2d).info == 0)
    image = learner.splat_gaussians(projected['means2d'], cov2d, projected['depths'],
        torch.full((128, 3), .5, dtype=dtype), torch.full((128,), .1, dtype=dtype),
        3, 2, torch.zeros(3, dtype=dtype))['rgb']
    image.sum().backward()
    assert torch.isfinite(image).all()
    for parameter in (scales, rotations):
        assert parameter.grad is not None and torch.isfinite(parameter.grad).all()
        assert parameter.grad.abs().sum() > 0


@pytest.mark.parametrize('bad_covariance,reason', [
    ([[3., 1.], [.5, 2.]], 'symmetric'),
    ([[1., 2.], [2., 1.]], 'positive definite'),
])
def test_invalid_external_covariances_are_still_rejected(bad_covariance, reason):
    with pytest.raises(ValueError, match=reason):
        learner.splat_gaussians(torch.zeros((1, 2)), torch.tensor([bad_covariance]), torch.ones(1),
            torch.ones((1, 3)), torch.tensor([.5]), 1, 1, torch.zeros(3))


def state_and_frame():
    means, scales, rotations = ellipsoids()
    state = learner.initialize_gaussians(means, torch.full_like(means, .5))
    state['log_scales'] = scales
    state['quaternions'] = rotations
    with torch.no_grad():
        state['sh_coeffs'][:, 1:4] = .02
    frame = {'image_id': 3, 'width': 17, 'height': 13,
             'K': [[90., 1.3, 8.], [0., 85., 6.], [0., 0., 1.]],
             'R': [[.8, -.6, 0.], [.6, .8, 0.], [0., 0., 1.]], 't': [.1, -.2, .3]}
    return state, frame


@pytest.mark.parametrize('degree', [0, 3])
def test_bounded_cpu_preview_matches_full_reference_including_sh_and_skew(monkeypatch, degree):
    import minicolmap.rendering.pipeline as pipeline
    state, frame = state_and_frame()
    original_frame = copy.deepcopy(frame)
    config = resolve_render_config({'method': '3dgs', 'background': 'black'})
    rendered_sizes = []
    original_render = pipeline._render

    def render(*args, **kwargs):
        tile = args[2]
        rendered_sizes.append(tile['width'] * tile['height'])
        return original_render(*args, **kwargs)

    with torch.no_grad():
        expected = _render(None, state, frame, config, torch, 'cpu', sh_degree=degree)
        monkeypatch.setattr(pipeline, '_render', render)
        actual = _render_gaussian_preview(state, frame, config, torch, sh_degree=degree,
                                          max_gaussian_pixels=4096)
    assert len(rendered_sizes) > 1 and max(rendered_sizes) * len(state['means']) <= 4096
    torch.testing.assert_close(actual, expected, atol=2e-6, rtol=2e-5)
    assert frame == original_frame


def test_saved_float32_checkpoint_can_render_an_original_camera_on_cpu(tmp_path):
    state, frame = state_and_frame()
    config = resolve_render_config({'method': '3dgs', 'device': 'cuda', 'background': 'black',
                                    'gaussian': {'backend': 'gsplat'}})
    torch.save({'schema_version': 2, 'pipeline': '3dgs', 'config': config, 'step': 5000,
                'state': {key: value.detach() for key, value in state.items()}, 'frames': [frame],
                'normalization': {'center': [0, 0, 0], 'scale': 1}, 'active_sh_degree': 3},
               tmp_path / 'checkpoint.pt')
    before = (tmp_path / 'checkpoint.pt').read_bytes()
    preview = render_checkpoint(tmp_path, image_id=3)
    assert preview['image_id'] == 3
    pixels = cv2.imdecode(np.fromfile(tmp_path / preview['path'], dtype=np.uint8), cv2.IMREAD_COLOR)
    assert pixels.shape == (13, 17, 3) and pixels.max() > 0
    assert (tmp_path / 'checkpoint.pt').read_bytes() == before
