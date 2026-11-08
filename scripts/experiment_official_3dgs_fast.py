"""Same ablation loop, with gsplat's fused SH/covariance path for throughput.

Use --parity-only to measure images and gradients against the learner adapter
before using this runner. Production remains unchanged. The imported trainer
is fingerprinted in an extra sidecar and is checked on every worker startup.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

import experiment_official_3dgs as base

ROOT = base.ROOT
SCENE_SCALE = json.loads((base.ORIGINAL / 'training_scene.json').read_text())['normalization']['scale']
base.POLICIES['opacity_reset_rewrap'] = {'topology_before_adam': True, 'opacity_reset_rewrap': True}
base.POLICIES['window_capacity'] = {'window': 15000, 'capacity': 100_000_000}
base.POLICIES['statistics_grow_prune'] = {'statistics': True, 'grow_then_prune': True}
base.POLICIES['epsilon_sh'] = {'adam_eps': 1e-15, 'sh_rest_lr_ratio': .05}
POLICY = {}
for i, value in enumerate(sys.argv[:-1]):
    if value == '--variant':
        POLICY = base.POLICIES[sys.argv[i+1]]


def fast_render(network, state, frame, config, torch, device, *, sh_degree=None, training_info=None):
    from gsplat import rasterization
    from minicolmap.rendering.gsplat_runtime import load_project_extension
    load_project_extension()
    K = torch.as_tensor(frame['K'], device=device, dtype=torch.float32).clone()
    K[0, 2] += .5
    K[1, 2] += .5
    R, t = [torch.as_tensor(frame[key], device=device, dtype=torch.float32) for key in ('R', 't')]
    view = torch.cat((torch.cat((R, t[:, None]), 1), R.new_tensor([[0, 0, 0, 1]])))
    near = POLICY.get('near_plane', 1e-6)
    if 'near_plane' in POLICY and not POLICY.get('official_model'):
        near /= SCENE_SCALE
    width, height = frame['width'], frame['height']
    background = torch.ones(1, 3, device=device) if config['background'] == 'white' else torch.zeros(1, 3, device=device)
    rgb, _, info = rasterization(
        means=state['means'], quats=state['quaternions'], scales=state['log_scales'].exp(),
        opacities=state['opacity_logits'].sigmoid().reshape(-1), colors=state['sh_coeffs'],
        viewmats=view[None].contiguous(), Ks=K[None].contiguous(), width=width, height=height,
        sh_degree=config['gaussian']['sh_degree'] if sh_degree is None else sh_degree,
        packed=False, rasterize_mode='classic', render_mode='RGB', eps2d=.3,
        near_plane=near, far_plane=1e10, backgrounds=background,
        absgrad=config['gaussian']['absgrad'])
    if training_info is not None:
        info['means2d'].retain_grad()
        training_info.update(means2d=info['means2d'], radii=info['radii'],
                             indices=torch.arange(len(state['means']), device=device),
                             width=width, height=height, absgrad=config['gaussian']['absgrad'])
    return rgb[0]


def parity():
    import numpy as np
    import torch
    from minicolmap.rendering import pipeline as pipe
    from minicolmap.rendering.data import load_training_scene
    config = base.config_for({}, 0, 1)
    scene = load_training_scene(base.ORIGINAL / 'source_model.json', base.DATASET, config)
    checkpoint = torch.load(base.ORIGINAL / 'checkpoint.pt', map_location='cuda', weights_only=True)
    state = {key: value.requires_grad_() for key, value in checkpoint['state'].items()}
    rows = []
    with torch.no_grad():
        for image_id in scene.validation_ids:
            frame = pipe._frame_record(scene.frames[image_id])
            legacy = pipe._render(None, state, frame, config, torch, 'cuda', sh_degree=3)
            fused = fast_render(None, state, frame, config, torch, 'cuda', sh_degree=3)
            target = torch.as_tensor(scene.frames[image_id].rgb, device='cuda')
            rows.append({'image_id': image_id, 'image_max_abs': float((legacy-fused).abs().max()),
                         'image_mean_abs': float((legacy-fused).abs().mean()),
                         'legacy_mse': float((legacy-target).square().mean()),
                         'fused_mse': float((fused-target).square().mean())})
    # Full parameter-gradient comparison on seeds, using the same loss and view.
    state = base.initial_state(scene, config, {})
    frame = pipe._frame_record(scene.frames[scene.train_ids[0]])
    target = torch.as_tensor(scene.frames[scene.train_ids[0]].rgb, device='cuda')
    gradients = []
    images = []
    from minicolmap.rendering.gaussian_training import photometric_loss
    for renderer in (pipe._render, fast_render):
        info = {}
        rgb = renderer(None, state, frame, config, torch, 'cuda', sh_degree=3, training_info=info)
        images.append(rgb.detach())
        photometric_loss(rgb, target, config).backward()
        gradients.append({key: value.grad.detach().clone() for key, value in state.items()})
        for value in state.values():
            value.grad = None
    grad_rows = {}
    for key in state:
        a, b = gradients[0][key], gradients[1][key]
        grad_rows[key] = {'max_abs': float((a-b).abs().max()),
                         'relative_l2': float((a-b).norm()/a.norm().clamp_min(1e-30))}
    reference_psnr = -10*math_log10(float(np.mean([row['legacy_mse'] for row in rows])))
    fused_psnr = -10*math_log10(float(np.mean([row['fused_mse'] for row in rows])))
    result = {'legacy_psnr': reference_psnr, 'fused_psnr': fused_psnr,
              'delta_db': fused_psnr-reference_psnr, 'views': rows, 'seed_gradients': grad_rows,
              'seed_image_max_abs': float((images[0]-images[1]).abs().max()),
              'scope': 'Fused CUDA mathematics is a transport control; training topology may diverge from tiny floating-point differences.'}
    base.write(base.REPORT / 'fused-parity.json', result)
    print(json.dumps(result, indent=2), flush=True)
    if abs(result['delta_db']) > .002 or any(row['relative_l2'] > .005 for row in grad_rows.values()):
        raise RuntimeError('Fused transport did not pass parity; do not use in ablations')


def math_log10(value):
    import math
    return math.log10(value)


def rewrap_opacity_parameter(state, optimizer):
    """Upstream replaces this Parameter, so a pending old gradient is dropped."""
    import torch
    old = state['opacity_logits']
    replacement = torch.nn.Parameter(old.detach().clone())
    moments = optimizer.state.pop(old, {})
    state['opacity_logits'] = replacement
    for group in optimizer.param_groups:
        if group['name'] == 'opacity_logits':
            group['params'] = [replacement]
    if moments:
        optimizer.state[replacement] = moments


def main():
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '7':
        raise RuntimeError('Only GPU 7 is authorized')
    if '--parity-only' in sys.argv:
        parity()
        return
    expected = base.REPORT / 'fused-transport-source.json'
    actual = base.sha(ROOT / 'scripts/experiment_official_3dgs.py')
    if expected.exists() and json.loads(expected.read_text())['trainer_sha256'] != actual:
        raise RuntimeError('Underlying experiment trainer changed')
    if not expected.exists():
        base.write(expected, {'trainer_sha256': actual, 'adapter': str(Path(__file__).resolve())})
        saved = base.REPORT / 'source' / actual[:12] / 'experiment_official_3dgs.py'
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes((ROOT / 'scripts/experiment_official_3dgs.py').read_bytes())
    from minicolmap.rendering import pipeline as pipe
    from minicolmap.rendering import gaussian_training
    original_reset = gaussian_training.reset_opacity
    def reset_with_parameter_identity(state, optimizer, maximum=.01):
        original_reset(state, optimizer, maximum)
        if POLICY.get('opacity_reset_rewrap'):
            rewrap_opacity_parameter(state, optimizer)
    gaussian_training.reset_opacity = reset_with_parameter_identity
    pipe._render = fast_render
    # Base suite derives child commands and snapshots from this entry path.
    base.__file__ = __file__
    base.main()


if __name__ == '__main__':
    main()
