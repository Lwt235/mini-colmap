"""Explicit 3DGS optimization policies and topology-aware Adam state transport.

Legacy rendering/SH/covariance maths remains in algorithms.py. No CUDA imports
occur until these functions are called in a training process.
"""
from __future__ import annotations

import math


FIELDS = ('means', 'log_scales', 'quaternions', 'sh_coeffs', 'opacity_logits')
LR_MULTIPLIERS = {'means': .16, 'log_scales': 5., 'quaternions': 1.,
                  'sh_coeffs': 2.5, 'opacity_logits': 25.}


def make_optimizer(state, config):
    import torch
    grouped = config['gaussian']['lr_mode'] == 'grouped'
    return torch.optim.Adam([
        {'params': [state[key]], 'name': key,
         'lr': config['learning_rate'] * ((config['gaussian']['position_lr_multiplier'] if key == 'means'
                                          else LR_MULTIPLIERS[key]) if grouped else 1.)}
        for key in FIELDS], eps=config['gaussian']['adam_eps'])


def update_learning_rates(optimizer, config, step):
    if config['gaussian']['lr_mode'] == 'grouped':
        fraction = min(1., step / config['iterations'])
        for group in optimizer.param_groups:
            if group['name'] == 'means':
                group['lr'] = config['learning_rate'] * config['gaussian']['position_lr_multiplier'] * (
                    config['gaussian']['position_lr_final_ratio'] ** fraction)


def optimizer_step(optimizer, state, config):
    """Apply a separate higher-order SH learning rate, preserving one SH tensor.

    Adam moments are elementwise: scaling its completed update is equivalent
    to separate DC/rest parameter groups. Scaling gradients would cancel in
    Adam's normalization and would not implement the requested learning rate.
    """
    ratio = config['gaussian']['sh_rest_lr_ratio']
    coefficients = state['sh_coeffs'] if state is not None else None
    previous = coefficients[:, 1:].detach().clone() if coefficients is not None and ratio != 1 else None
    optimizer.step()
    if previous is not None:
        import torch
        with torch.no_grad():
            coefficients[:, 1:].copy_(previous + ratio * (coefficients[:, 1:] - previous))


def replace_topology(state, revised, optimizer, source_indices, is_new, config):
    """Keep Adam's scalar step and retained rows; initialize only new row moments.

    source_indices maps EVERY output Gaussian to its old source. is_new marks
    children (including split children); changed parent values retain history.
    reset is an explicit historical control, including no-op topology changes.
    """
    import torch
    if source_indices.shape != is_new.shape or len(source_indices) != len(revised['means']):
        raise ValueError('Topology lineage must align with revised Gaussian rows')
    new_state = {key: torch.nn.Parameter(revised[key].detach().clone()) for key in FIELDS}
    if config['gaussian']['optimizer_state'] == 'reset':
        return new_state, make_optimizer(new_state, config)
    for group in optimizer.param_groups:
        key = group['name']
        old = state[key]
        moments = optimizer.state.pop(old, {})
        transported = {}
        for name, value in moments.items():
            if isinstance(value, torch.Tensor) and value.shape == old.shape:
                value = value.index_select(0, source_indices).clone()
                value[is_new] = 0
            elif isinstance(value, torch.Tensor):
                value = value.clone()
            transported[name] = value
        group['params'] = [new_state[key]]
        if transported:
            optimizer.state[new_state[key]] = transported
    return new_state, optimizer


def ssim_image(prediction, target):
    """Differentiable RGB SSIM: Gaussian valid window, sigma=1.5, data_range=1.

    No display clipping: identical float RGB is used by MSE and SSIM. Tiny
    protocol images use the largest odd window <= min(H,W,11).
    """
    import torch
    import torch.nn.functional as F
    if prediction.shape != target.shape or prediction.ndim != 3 or prediction.shape[-1] != 3:
        raise ValueError('SSIM expects matching HxWx3 images')
    size = min(11, prediction.shape[0], prediction.shape[1])
    size -= 1 - size % 2
    x = torch.arange(size, dtype=prediction.dtype, device=prediction.device) - size // 2
    kernel = torch.exp(-x.square() / (2 * 1.5 ** 2))
    kernel = kernel / kernel.sum()
    window = (kernel[:, None] * kernel[None, :]).expand(3, 1, size, size)
    a, b = (value.permute(2, 0, 1)[None] for value in (prediction, target))
    def average(value):
        return F.conv2d(value, window, groups=3)
    ma, mb = average(a), average(b)
    va = (average(a * a) - ma * ma).clamp_min(0)
    vb = (average(b * b) - mb * mb).clamp_min(0)
    covariance = average(a * b) - ma * mb
    return (((2 * ma * mb + .01 ** 2) * (2 * covariance + .03 ** 2)) /
            ((ma.square() + mb.square() + .01 ** 2) * (va + vb + .03 ** 2))).mean()


def photometric_loss(prediction, target, config):
    if config['gaussian']['loss'] == 'mse':
        return (prediction - target).square().mean()
    weight = config['gaussian']['ssim_weight']
    return (1 - weight) * (prediction - target).abs().mean() + weight * (1 - ssim_image(prediction, target))


class DensityStatistics:
    """Accumulate normalized image-plane gradient over visible training views."""
    def __init__(self, state):
        self.reset(state)

    def reset(self, state):
        import torch
        self.gradient = torch.zeros_like(state['means'][:, 0])
        self.count = torch.zeros_like(self.gradient)
        self.radius = torch.zeros_like(self.gradient)

    def accumulate(self, info):
        import torch
        means2d = info['means2d']
        screen_gradient = getattr(means2d, 'absgrad', None) if info.get('absgrad') else means2d.grad
        if screen_gradient is None:
            raise ValueError('Adaptive density requires retained image-plane gradients')
        gradient = screen_gradient.detach().reshape(-1, 2).clone()
        gradient *= gradient.new_tensor([info['width'] / 2, info['height'] / 2])
        radii = info['radii'].detach()
        if radii.numel() != len(gradient):
            radii = radii.reshape(len(gradient), -1).amax(-1)
        radii = radii.reshape(-1)
        visible = radii > 0
        indices = info['indices'][visible]
        self.gradient[indices] += gradient[visible].norm(dim=-1)
        self.count[indices] += 1
        self.radius[indices] = torch.maximum(self.radius[indices], radii[visible] / max(info['width'], info['height']))


def adaptive_topology(state, statistics, options):
    """Visibility-averaged gradient, small clones and large two-child splits.

    Deterministic +/- offsets along the largest principal axis make the ablation
    repeatable. This is a documented variant, not the original stochastic split.
    Retained rows keep order; growth is ranked by gradient, with stable ties.
    """
    import torch
    with torch.no_grad():
        scale = state['log_scales'].exp().amax(-1)
        opacity = state['opacity_logits'].sigmoid()
        keep = torch.nonzero((opacity >= options['min_opacity']) &
                             (scale <= options['prune_scale']) &
                             (statistics.radius <= options['prune_screen_radius']), as_tuple=True)[0]
        keep = keep[:options['max_gaussians']]
        score = statistics.gradient / statistics.count.clamp_min(1)
        grow = keep[(score[keep] >= options['grow_grad2d']) & (statistics.count[keep] > 0)]
        grow = grow[torch.argsort(score[grow], descending=True, stable=True)]
        # Clone adds one row; replacing one parent by two split children also adds one.
        grow = grow[:max(0, options['max_gaussians'] - len(keep))]
        large = scale[grow] > options['split_scale']
        if options.get('grow_screen_radius', 0) > 0:
            large |= statistics.radius[grow] > options['grow_screen_radius']
        split = grow[large]
        clone = grow[~large]
        retained = keep[~torch.isin(keep, split)]
        sources = torch.cat((retained, clone, split.repeat_interleave(2)))
        is_new = torch.arange(len(sources), device=sources.device) >= len(retained)
        revised = {key: value[sources].detach().clone() for key, value in state.items()}
        if len(split):
            logs = state['log_scales'][split]
            q = state['quaternions'][split]
            q = q / q.norm(dim=-1, keepdim=True)
            w, x, y, z = q.unbind(-1)
            rotation = torch.stack((1-2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y),
                                    2*(x*y+w*z), 1-2*(x*x+z*z), 2*(y*z-w*x),
                                    2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)), -1).reshape(-1, 3, 3)
            first = len(retained) + len(clone)
            if options.get('split_mode', 'deterministic') == 'stochastic':
                samples = torch.randn((len(split), 2, 3), dtype=logs.dtype, device=logs.device) * logs.exp()[:, None, :]
                offsets = torch.einsum('nij,nkj->nki', rotation, samples)
            else:
                axis = logs.argmax(-1)
                direction = rotation.gather(2, axis[:, None, None].expand(-1, 3, 1)).squeeze(-1)
                offset = .5 * direction * logs.exp().amax(-1, keepdim=True)
                offsets = torch.stack((-offset, offset), 1)
            revised['means'][first:] += offsets.reshape(-1, 3)
            revised['log_scales'][first:] -= math.log(1.6)
        return revised, sources, is_new


def reset_opacity(state, optimizer, maximum=.01):
    import torch
    with torch.no_grad():
        parameter = state['opacity_logits']
        parameter.copy_(torch.logit(parameter.sigmoid().clamp_max(maximum)))
        for key in ('exp_avg', 'exp_avg_sq', 'max_exp_avg_sq'):
            if key in optimizer.state.get(parameter, {}):
                optimizer.state[parameter][key].zero_()
