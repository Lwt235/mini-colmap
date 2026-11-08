"""Frozen geometry adapter with alpha-normalized semantic feature rendering."""
from pathlib import Path

from . import algorithms


class FrozenGaussians:
    def __init__(self, config, prepared):
        import torch
        from ..rendering.algorithms import gaussian_covariance
        from ..rendering.gsplat_runtime import load_project_extension
        load_project_extension()
        self.torch = torch
        self.semantic_coverage = None
        checkpoint = torch.load(Path(config['rgb_run']) / 'checkpoint.pt', map_location='cpu', weights_only=True)
        self.normalization = checkpoint['normalization']
        self.state = {key: value.detach().cuda().requires_grad_(False) for key, value in checkpoint['state'].items()}
        if len(self.state['means']) != prepared['gaussian_count']:
            raise ValueError('Gaussian indexing differs from the prepared project')
        with torch.no_grad():
            self.covariances = gaussian_covariance(self.state['log_scales'], self.state['quaternions'])
            self.opacities = self.state['opacity_logits'].sigmoid()

    def camera(self, frame):
        from ..rendering.live import normalize_camera
        camera = normalize_camera(frame, self.normalization)
        return [self.torch.tensor(camera[key], device='cuda', dtype=self.state['means'].dtype) for key in ('K', 'R', 't')]

    def splat_raw(self, latents, frame, *, depth=False):
        from gsplat import rasterization
        from ..rendering.gaussian_backend import gsplat_arguments
        torch = self.torch
        if latents.shape[0] != len(self.state['means']):
            raise ValueError('Features must keep the original checkpoint Gaussian order')
        K, R, t = self.camera(frame)
        opacity = self.opacities if self.semantic_coverage is None else self.opacities * self.semantic_coverage
        arguments = gsplat_arguments(self.state['means'], self.covariances, latents,
                                     opacity, K, R, t, frame['width'], frame['height'],
                                     torch.zeros(latents.shape[-1], device='cuda'))
        if depth:
            arguments['render_mode'] = 'RGB+ED'
        weighted, alpha, _ = rasterization(**arguments)
        return weighted[0], alpha[0]

    def splat(self, latents, frame, *, depth=False):
        weighted, alpha = self.splat_raw(latents, frame, depth=depth)
        if depth:
            return weighted[..., -1], alpha
        return algorithms.normalize_feature_render(weighted, alpha), alpha

    def observations(self, target, valid, frame):
        """Approximate center projection + expected-depth gate for fusion baseline."""
        torch = self.torch
        K, R, t = self.camera(frame)
        points = self.state['means'] @ R.T + t
        projected = points @ K.T
        uv = (projected[:, :2] / projected[:, 2:].clamp_min(1e-8)).round().long()
        x, y = uv[:, 0], uv[:, 1]
        inside = (points[:, 2] > 0) & (x >= 0) & (x < frame['width']) & (y >= 0) & (y < frame['height'])
        indices = torch.where(inside)[0]
        x, y = x[indices], y[indices]
        depth, alpha = self.splat(torch.zeros((len(points), 3), device='cuda'), frame, depth=True)
        z = points[indices, 2]
        visible = valid[y, x] & (alpha[y, x, 0] > .1) & ((z - depth[y, x]).abs() < .05 * z.clamp_min(1e-3))
        return indices[visible], target[y[visible], x[visible]], alpha[y[visible], x[visible], 0]
