"""SH configuration, backend computation graphs and rendering adapter protocols."""
import pytest

from minicolmap.rendering.config import resolve_render_config
from minicolmap.rendering.gaussian_backend import active_sh_degree, gsplat_arguments, rasterize_gsplat


@pytest.mark.parametrize('overrides', [
    {'sh_degree': -1}, {'sh_degree': 4}, {'sh_degree': True}, {'sh_degree': 1.5},
    {'sh_degree_interval': -1}, {'sh_degree_interval': 1.5}, {'sh_degree_interval': True},
    {'backend': 'unknown'},
])
def test_sh_configuration_rejects_invalid_parameters(overrides):
    with pytest.raises(ValueError):
        resolve_render_config({'gaussian': overrides})


def test_accelerated_backend_requires_cuda_only_for_gaussian_training():
    with pytest.raises(ValueError, match='device=cuda'):
        resolve_render_config({'method': '3dgs', 'gaussian': {'backend': 'gsplat'}})
    # Existing NeRF experiments retain their unrelated Gaussian parameter group.
    resolve_render_config({'method': 'nerf', 'gaussian': {'backend': 'gsplat'}})


@pytest.mark.parametrize('step,expected', [(0, 0), (1, 0), (999, 0), (1000, 1),
    (1999, 1), (2000, 2), (3000, 3), (10000, 3)])
def test_progressive_sh_degree_matches_saved_iteration(step, expected):
    config = resolve_render_config({'gaussian': {'sh_degree_interval': 1000}})
    assert active_sh_degree(config, step) == expected
    config['gaussian']['sh_degree_interval'] = 0
    assert active_sh_degree(config, step) == 3


def test_gsplat_adapter_preserves_covariance_and_color_graph_and_camera_conventions():
    torch = pytest.importorskip('torch')
    means = torch.tensor([[.2, .3, 2.]], requires_grad=True)
    covariance = torch.eye(3)[None].requires_grad_()
    colors = torch.tensor([[1.5, .2, .4]], requires_grad=True)
    opacity = torch.tensor([.6], requires_grad=True)
    K = torch.tensor([[10., 0., 3.], [0., 12., 4.], [0., 0., 1.]]) * 2
    R = torch.tensor([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    t = torch.tensor([1., 2., 3.])
    args = gsplat_arguments(means, covariance, colors, opacity, K, R, t, 8, 6, torch.zeros(3))
    torch.testing.assert_close(args['Ks'][0], torch.tensor([[10., 0., 3.5], [0., 12., 4.5], [0., 0., 1.]]))
    # An on-axis point has project pixel (3,4), which gsplat samples at (3.5,4.5).
    projected = args['Ks'][0] @ torch.tensor([0., 0., 2.])
    torch.testing.assert_close(projected[:2] / projected[2], torch.tensor([3.5, 4.5]))
    assert K[0, 2] == 6, 'The adapter must not modify source calibration'
    torch.testing.assert_close(args['viewmats'][0, :3, :3], R)
    torch.testing.assert_close(args['viewmats'][0, :3, 3], t)
    assert args['quats'] is args['scales'] is args['sh_degree'] is None
    assert args['colors'].max() > 1, 'Do not clamp SH before alpha compositing'
    assert args['render_mode'] == 'RGB' and args['rasterize_mode'] == 'classic'
    assert args['packed'] is False  # 1.5.3 single-camera background shape compatibility.
    assert args['eps2d'] == .3
    (args['means'].sum() + args['covars'].sum() + args['colors'].sum() + args['opacities'].sum()).backward()
    for tensor in (means, covariance, colors, opacity):
        assert tensor.grad is not None and torch.isfinite(tensor.grad).all()
    K[0, 1] = .2
    with pytest.raises(ValueError, match='skew'):
        gsplat_arguments(means, covariance, colors, opacity, K, R, t, 8, 6, torch.zeros(3))


def test_cuda_adapter_does_not_silently_fall_back_on_cpu():
    torch = pytest.importorskip('torch')
    with pytest.raises(ValueError, match='CUDA float32'):
        rasterize_gsplat(torch.zeros((1, 3)), None, None, None, None, None, None, 1, 1, None)


def test_pipeline_uses_world_camera_to_gaussian_direction_and_filters_before_sh(monkeypatch):
    torch = pytest.importorskip('torch')
    from minicolmap.rendering import algorithms as learner
    from minicolmap.rendering.pipeline import _render
    captured = {}
    center = torch.tensor([1., 2., 3.])
    R = torch.tensor([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    state = {'means': torch.tensor([[2., 3., 5.], [1., 2., 3.], [1., 2., 2.]]),
             'log_scales': torch.zeros((3, 3)), 'quaternions': torch.zeros((3, 4)),
             'sh_coeffs': torch.arange(3*16*3).float().reshape(3, 16, 3),
             'opacity_logits': torch.zeros(3)}
    def sh(coefficients, directions, degree):
        captured.update(coefficients=coefficients, directions=directions, degree=degree)
        return coefficients[:, 0] * 0 + .5
    monkeypatch.setattr(learner, 'evaluate_sh', sh)
    monkeypatch.setattr(learner, 'gaussian_covariance', lambda *args: torch.eye(3)[None])
    monkeypatch.setattr(learner, 'project_gaussians', lambda *args: {
        'visible': torch.tensor([True]), 'means2d': torch.zeros((1, 2)),
        'cov2d': torch.eye(2)[None], 'depths': torch.ones(1)})
    monkeypatch.setattr(learner, 'splat_gaussians', lambda *args: {'rgb': args[3][None]})
    frame = dict(K=torch.eye(3), R=R, t=-(R @ center), width=1, height=1)
    _render(None, state, frame, resolve_render_config({'method': '3dgs'}), torch, 'cpu', sh_degree=2)
    torch.testing.assert_close(captured['directions'], torch.tensor([[1., 1., 2.]]))
    torch.testing.assert_close(captured['coefficients'], state['sh_coeffs'][:1])
    assert captured['degree'] == 2
