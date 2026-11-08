"""Controlled Graphdeco ablations; production and semantic learner stay untouched.

Cold starts only. --steps is the actual budget; position LR always uses the
original 30k schedule. Suite workers run sequentially on physical GPU 7.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / '.cache/gaussian-upstream-audit'
REPORT = ROOT / 'reports/3dgs-official-ablation-20261020'
ORIGINAL = ROOT / 'outputs/ui_3dgs_20261019_230921_797f2a'
DATASET = ROOT / 'data/lerf_ovs/waldo_kitchen/dataset_all.json'
POLICIES = {
    'baseline': {},
    'statistics': {'statistics': True},
    'grow_then_prune': {'grow_then_prune': True},
    'window15000': {'window': 15000},
    'no_capacity': {'capacity': 100_000_000},
    'stochastic_split': {'split_mode': 'stochastic'},
    'adam_epsilon': {'adam_eps': 1e-15},
    'sh_rest_lr': {'sh_rest_lr_ratio': .05},
    'knn_unclipped': {'knn_unclipped': True},
    'knn_floor': {'knn_floor': True},
    'epoch_sampling': {'epoch_sampling': True},
    'white_reset': {'white_reset': True},
    'no_reset_pause': {'no_reset_pause': True},
    'ssim_same': {'ssim_same': True},
    'ssim_fused': {'ssim_fused': True},
    'rgb_clamp': {'rgb_clamp': True},
    'topology_before_adam': {'topology_before_adam': True},
    'prune_schedule': {'prune_schedule': True},
    'near_plane': {'near_plane': .2},
    'rasterizer_prerequisites': {'near_plane': .2, 'rgb_clamp': True},
    'official_rasterizer': {'official_rasterizer': True, 'near_plane': .2, 'rgb_clamp': True},
    'official_policy_gsplat': {'official_model': True, 'near_plane': .2},
    'official_reference': {'official_model': True, 'official_rasterizer': True},
    'alpha99': {'kernel_control': 'alpha99'},
    'alpha99_ste': {'kernel_control': 'alpha99_ste'},
    'circular_footprint': {'kernel_control': 'circular_footprint'},
}


def write(path, value):
    from minicolmap.ui.worker import atomic_json
    atomic_json(Path(path), value)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def config_for(policy, seed, downscale):
    from minicolmap.rendering.config import resolve_render_config
    config = resolve_render_config(json.loads((ORIGINAL / 'config.json').read_text()))
    config.update(seed=seed, iterations=30000, image_downscale=downscale)
    options = config['gaussian']
    for key in ('adam_eps', 'sh_rest_lr_ratio', 'split_mode'):
        if key in policy:
            options[key] = policy[key]
    options['densify_stop'] = policy.get('window', options['densify_stop'])
    options['max_gaussians'] = policy.get('capacity', options['max_gaussians'])
    return config


def upstream_import():
    """Import exact cached files, avoiding scene.__init__ dataset side effects."""
    sys.path.insert(0, str(UPSTREAM / 'site'))
    sys.path.insert(0, str(UPSTREAM))
    scene_package = types.ModuleType('scene')
    scene_package.__path__ = [str(UPSTREAM / 'scene')]
    sys.modules['scene'] = scene_package
    from scene.gaussian_model import GaussianModel
    from gaussian_renderer import render
    from utils.graphics_utils import BasicPointCloud, getProjectionMatrix
    from arguments import OptimizationParams
    parser = argparse.ArgumentParser()
    OptimizationParams(parser)
    return GaussianModel, render, BasicPointCloud, getProjectionMatrix, parser.parse_args([])


def camera_for(frame, projection):
    import torch
    from types import SimpleNamespace
    width, height = frame.width, frame.height
    fx, fy = frame.K[0, 0], frame.K[1, 1]
    # getProjectionMatrix/official viewport centers at (W-1)/2,(H-1)/2.
    # Current Waldo has exactly this principal point, also after resize.
    if not (abs(frame.K[0, 2] - (width - 1) / 2) < 1e-5 and
            abs(frame.K[1, 2] - (height - 1) / 2) < 1e-5):
        raise ValueError('Official FOV camera cannot silently discard off-center principal points')
    world_view = torch.eye(4, device='cuda')
    world_view[:3, :3] = torch.as_tensor(frame.R, device='cuda', dtype=torch.float32)
    world_view[:3, 3] = torch.as_tensor(frame.t, device='cuda', dtype=torch.float32)
    world_view = world_view.T.contiguous()
    fovx, fovy = 2 * math.atan(width / (2 * fx)), 2 * math.atan(height / (2 * fy))
    proj = projection(.01, 100., fovx, fovy).T.cuda()
    return SimpleNamespace(FoVx=fovx, FoVy=fovy, image_height=height, image_width=width,
                           world_view_transform=world_view,
                           full_proj_transform=world_view @ proj,
                           camera_center=torch.linalg.inv(world_view)[3, :3], image_name=frame.name)


def official_proxy(state, degree, center=None, scale=1.):
    from types import SimpleNamespace
    means = state['means'] * scale + center if center is not None else state['means']
    return SimpleNamespace(get_xyz=means, get_features=state['sh_coeffs'],
                           get_scaling=state['log_scales'].exp() * scale,
                           get_rotation=state['quaternions'] / state['quaternions'].norm(dim=-1, keepdim=True),
                           get_opacity=state['opacity_logits'].sigmoid().reshape(-1, 1),
                           active_sh_degree=degree, max_sh_degree=3)


def export_state(model):
    import torch
    return {'means': model._xyz, 'log_scales': model._scaling, 'quaternions': model._rotation,
            'sh_coeffs': torch.cat((model._features_dc, model._features_rest), 1),
            'opacity_logits': model._opacity.reshape(-1)}


def topology(state, stats, options, policy, step):
    """Use production verbatim except for the explicitly named topology factor."""
    import torch
    from minicolmap.rendering.gaussian_training import adaptive_topology
    opts = dict(options)
    if policy.get('prune_schedule'):
        # Upstream resets max_radii2D in densification_postfix BEFORE pruning.
        # Accordingly the effective screen predicate at this revision is false.
        opts['prune_screen_radius'] = float('inf')
        opts['prune_scale'] = .1 if step > 3000 else float('inf')
    if not policy.get('grow_then_prune'):
        return adaptive_topology(state, stats, opts)
    # Change only ordering. Keep the existing capacity, stochastic/deterministic
    # split, gradient ranking, moment lineage and pruning thresholds unchanged.
    relaxed = {**opts, 'min_opacity': 0., 'prune_scale': float('inf'),
               'prune_screen_radius': float('inf')}
    revised, sources, is_new = adaptive_topology(state, stats, relaxed)
    keep = ((revised['opacity_logits'].sigmoid() >= opts['min_opacity']) &
            (revised['log_scales'].exp().amax(-1) <= opts['prune_scale']) &
            (stats.radius[sources] <= opts['prune_screen_radius']))
    return {key: value[keep] for key, value in revised.items()}, sources[keep], is_new[keep]


def initial_state(scene, config, policy):
    import numpy as np
    import torch
    from minicolmap.rendering import algorithms as learner
    from minicolmap.rendering.data import gaussian_initial_scales
    options = config['gaussian']
    points = torch.as_tensor(scene.points, dtype=torch.float32, device='cuda')
    colors = torch.as_tensor(scene.colors, dtype=torch.float32, device='cuda')
    initial = learner.initialize_gaussians(points, colors, options['initial_scale'],
                                           options['initial_opacity'], options['sh_degree'])
    if policy.get('knn_unclipped') or policy.get('knn_floor'):
        from scipy.spatial import cKDTree
        distances = cKDTree(scene.points).query(scene.points, k=4)[0][:, 1:]
        scales = np.sqrt(np.mean(distances ** 2, axis=1))
        if not policy.get('knn_unclipped'):
            scales = np.minimum(scales, options['initial_scale'])
        scales = np.maximum(scales, math.sqrt(1e-7)/scene.scale if policy.get('knn_floor') else 1e-7)
    else:
        scales = gaussian_initial_scales(scene.points, options)
    initial['log_scales'] = torch.as_tensor(np.repeat(np.log(scales)[:, None], 3, axis=1),
                                           dtype=torch.float32, device='cuda')
    return {key: torch.nn.Parameter(value.detach().clone()) for key, value in initial.items()}


def train(args):
    import numpy as np
    import torch
    from minicolmap.rendering import pipeline as pipe
    from minicolmap.rendering.data import load_training_scene
    from minicolmap.rendering.gaussian_backend import active_sh_degree
    from minicolmap.rendering.gaussian_training import (DensityStatistics, make_optimizer, optimizer_step,
        update_learning_rates, replace_topology, reset_opacity, photometric_loss, ssim_image)
    from minicolmap.ui.runtime import configure_worker_runtime
    configure_worker_runtime()
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '7' or torch.cuda.device_count() != 1:
        raise ValueError('Experiments require only physical GPU 7 (logical cuda:0)')
    policy = copy.deepcopy(POLICIES[args.variant])
    if policy.get('kernel_control'):
        import gsplat
        control = UPSTREAM / 'kernel-controls' / policy['kernel_control']
        library = control / 'gsplat_cuda.so'
        expected = json.loads((control.parent / 'manifest.json').read_text())[policy['kernel_control']]['library_sha256']
        if sha(library) != expected:
            raise ValueError('Kernel control library fingerprint mismatch')
        spec = importlib.util.spec_from_file_location('gsplat_cuda', library)
        native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native)
        sys.modules['gsplat.csrc'] = native
        gsplat.csrc = native
        backend = sys.modules.get('gsplat.cuda._backend')
        if backend is not None:
            backend._C = native
    config = config_for(policy, args.seed, args.downscale)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    write(output / 'config.json', {'config': config, 'policy': policy, 'steps': args.steps})
    scene = load_training_scene(ORIGINAL / 'source_model.json', DATASET, config)
    normalized_scene = copy.deepcopy(scene)
    if policy.get('official_model'):
        # Stock upstream operates in original world coordinates. Its KNN floor,
        # hard-coded z>.2 culling and position LR must keep those units.
        scene.points = scene.points * scene.scale + scene.center
        for frame in scene.frames.values():
            frame.t = frame.t * scene.scale - frame.R @ scene.center
    write(output / 'protocol.json', {'train_ids': scene.train_ids, 'development_ids': scene.validation_ids,
          'normalization': {'center': scene.center.tolist(), 'scale': scene.scale},
          'resolution': [next(iter(scene.frames.values())).width, next(iter(scene.frames.values())).height],
          'note': 'These held-out appearance views have already been used for development; not a fresh test.'})
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    random.seed(args.seed)
    rng = np.random.default_rng(args.seed)
    frames = {i: {key: torch.as_tensor(getattr(frame, key), device='cuda', dtype=torch.float32)
                  for key in ('rgb', 'K', 'R', 't')} for i, frame in scene.frames.items()}
    records = {i: {'image_id': i, 'width': frame.width, 'height': frame.height,
                   **{key: frames[i][key] for key in ('K', 'R', 't')}} for i, frame in scene.frames.items()}
    model, official_render, cameras, pipeline_options = None, None, None, None
    upstream_ssim = None
    if (policy.get('official_model') or policy.get('official_rasterizer') or
            policy.get('ssim_same') or policy.get('ssim_fused')):
        GaussianModel, official_render, BasicPointCloud, projection, opt = upstream_import()
        from utils.loss_utils import ssim as upstream_ssim
        camera_frames = copy.deepcopy(scene.frames)
        if not policy.get('official_model'):
            for frame in camera_frames.values():
                frame.t = frame.t * scene.scale - frame.R @ scene.center
        cameras = {i: camera_for(frame, projection) for i, frame in camera_frames.items()}
        pipeline_options = types.SimpleNamespace(debug=False, antialiasing=False,
                                                compute_cov3D_python=False, convert_SHs_python=False)
    if policy.get('official_model'):
        opt.iterations = 30000
        model = GaussianModel(3)
        model.create_from_pcd(BasicPointCloud(scene.points, scene.colors, np.zeros_like(scene.points)),
                              list(cameras.values()), scene.scale)
        model.training_setup(opt)
        state = export_state(model)
        optimizer, density = model.optimizer, None
    else:
        state = initial_state(scene, config, policy)
        optimizer = make_optimizer(state, config)
        density = DensityStatistics(state)
    # An identical renderer wrapper is used for training, eval and timing.
    background = torch.ones(3, device='cuda')
    def render(image_id, degree, info=None):
        current = export_state(model) if model is not None else state
        if policy.get('official_rasterizer'):
            center = torch.as_tensor(scene.center, device='cuda', dtype=torch.float32)
            pc = model if model is not None else official_proxy(current, degree, center, scene.scale)
            if model is not None:
                model.active_sh_degree = degree
            packet = official_render(cameras[image_id], pc, pipeline_options, background)
            rgb = packet['render'].permute(1, 2, 0)
            if info is not None:
                info.update(official_packet=packet)
            return rgb
        rgb = pipe._render(None, current, records[image_id], config, torch, 'cuda',
                           sh_degree=degree, training_info=info)
        return rgb.clamp(0, 1) if policy.get('rgb_clamp') or model is not None else rgb
    if policy.get('near_plane'):
        import minicolmap.rendering.gaussian_backend as backend
        original_arguments = backend.gsplat_arguments
        def arguments(*values, **kwargs):
            result = original_arguments(*values, **kwargs)
            result['near_plane'] = policy['near_plane'] / (1. if model is not None else scene.scale)
            return result
        backend.gsplat_arguments = arguments
    history, curve, epochs, stack = [], [], 0, []
    options = config['gaussian']
    torch.cuda.reset_peak_memory_stats()
    began = time.perf_counter()
    train_seconds, eval_seconds = 0., 0.
    def evaluate(step, save_images=False):
        nonlocal eval_seconds
        started = time.perf_counter()
        rows = []
        with torch.no_grad():
            for image_id in scene.validation_ids:
                rgb = render(image_id, active_sh_degree(config, step))
                target = frames[image_id]['rgb']
                mse = float((rgb - target).square().mean())
                clipped_mse = float((rgb.clamp(0, 1) - target).square().mean())
                row = {'image_id': image_id, 'mse': mse, 'psnr': -10*math.log10(max(mse, 1e-12)),
                       'ssim_valid': float(ssim_image(rgb, target)), 'clipped_mse': clipped_mse}
                rows.append(row)
                if save_images:
                    pipe._write_image(output / f'validation/{image_id}.png', rgb.cpu().numpy())
                    pipe._write_image(output / f'validation/{image_id}-reference.png', scene.frames[image_id].rgb)
        result = {'step': step, 'gaussians': len(state['means']),
                  'psnr': -10 * math.log10(max(float(np.mean([row['mse'] for row in rows])), 1e-12)),
                  'mean_view_psnr': float(np.mean([row['psnr'] for row in rows])),
                  'ssim_valid': float(np.mean([row['ssim_valid'] for row in rows])), 'per_view': rows,
                  'clipped_psnr': -10 * math.log10(max(float(np.mean([row['clipped_mse'] for row in rows])), 1e-12))}
        eval_seconds += time.perf_counter() - started
        curve.append(result)
        write(output / 'curve.json', curve)
        print(json.dumps({'evaluation': result['step'], 'psnr': result['psnr'], 'gaussians': result['gaussians']}), flush=True)
        return result
    for step in range(1, args.steps + 1):
        if model is not None:
            state = export_state(model)
            model.update_learning_rate(step)
        else:
            update_learning_rates(optimizer, config, step)
        if policy.get('epoch_sampling') or model is not None:
            if not stack:
                stack = list(scene.train_ids)
                epochs += 1
            image_id = stack.pop(random.randint(0, len(stack)-1))
        else:
            image_id = int(rng.permutation(scene.train_ids)[0])
        # All current Waldo views contain positive-depth seeds. The production
        # retry is retained whenever this assumption ceases to hold.
        if step == 1 or step % 100 == 0:
            with torch.no_grad():
                f = frames[image_id]
                if not torch.any((state['means'] @ f['R'].T + f['t'])[:, 2] > 0):
                    raise ValueError('Unsupported view: controlled camera sequence cannot be silently changed')
        optimizer.zero_grad(set_to_none=True)
        stop = 15000 if model is not None else options['densify_stop']
        collect = step < stop and (model is not None or policy.get('statistics') or step >= options['densify_start'])
        info = {} if collect else None
        rgb = render(image_id, active_sh_degree(config, step), info)
        target = frames[image_id]['rgb']
        if policy.get('ssim_fused') or model is not None:
            from fused_ssim import fused_ssim
            loss = .8 * (rgb - target).abs().mean() + .2 * (1-fused_ssim(
                rgb.permute(2, 0, 1).unsqueeze(0).contiguous(),
                target.permute(2, 0, 1).unsqueeze(0).contiguous()))
        elif policy.get('ssim_same'):
            if upstream_ssim is None:
                from utils.loss_utils import ssim as upstream_ssim
            # Exact upstream fallback; compared separately with fused SSIM.
            loss = .8 * (rgb - target).abs().mean() + .2 * (1-upstream_ssim(
                rgb.permute(2, 0, 1), target.permute(2, 0, 1)))
        else:
            loss = photometric_loss(rgb, target, config)
        loss.backward()
        before = len(state['means'])
        if collect:
            if model is not None:
                if policy.get('official_rasterizer'):
                    packet = info['official_packet']
                    visible, radii = packet['visibility_filter'], packet['radii']
                    model.max_radii2D[visible] = torch.maximum(model.max_radii2D[visible], radii[visible])
                    model.add_densification_stats(packet['viewspace_points'], visible)
                else:
                    # gsplat screen grad is pixel-valued. Transform to NDC
                    # before feeding the exact official accumulator.
                    grad = info['means2d'].grad.detach().reshape(-1, 2).clone()
                    grad *= grad.new_tensor([info['width']/2, info['height']/2])
                    radii = info['radii'].detach()
                    if radii.numel() != len(grad):
                        radii = radii.reshape(len(grad), -1).amax(-1)
                    radii = radii.reshape(-1)
                    full_radii = torch.zeros(len(state['means']), device='cuda')
                    full_radii[info['indices']] = radii.float()
                    visible = full_radii > 0
                    screen = types.SimpleNamespace(grad=torch.zeros((len(state['means']), 3), device='cuda'))
                    screen.grad[info['indices'], :2] = grad
                    model.max_radii2D[visible] = torch.maximum(model.max_radii2D[visible], full_radii[visible])
                    model.add_densification_stats(screen, visible)
                    radii = full_radii
            elif policy.get('official_rasterizer'):
                packet = info['official_packet']
                grad = packet['viewspace_points'].grad[:, :2]
                visible = packet['radii'] > 0
                density.gradient[visible] += grad[visible].norm(dim=-1)
                density.count[visible] += 1
                density.radius[visible] = torch.maximum(density.radius[visible], packet['radii'][visible] / max(rgb.shape[:2]))
            else:
                density.accumulate(info)
        official_order = model is not None or policy.get('topology_before_adam')
        if not official_order:
            optimizer_step(optimizer, state, config)
        changed = False
        if model is not None:
            with torch.no_grad():
                if step < 15000:
                    if step > 500 and step % 100 == 0:
                        model.densify_and_prune(.0002, .005, scene.scale, 20 if step > 3000 else None, radii)
                        changed = True
                    if step % 3000 == 0 or step == 500:
                        model.reset_opacity()
                if step < 30000:
                    model.optimizer.step()
                state = export_state(model)
            if changed:
                history.append({'step': step, 'before': before, 'after': len(state['means'])})
        else:
            if step % options['densify_every'] == 0:
                eligible = ((step > options['densify_start']) if policy.get('statistics') else
                            (step >= options['densify_start'])) and step < stop
                paused = (options['opacity_reset_every'] and step % options['opacity_reset_every'] < len(scene.train_ids))
                if eligible and (not paused or policy.get('no_reset_pause')):
                    counts = density.count
                    revised, sources, is_new = topology(state, density, options, policy, step)
                    if not len(sources):
                        raise ValueError(f'All Gaussians pruned at step {step}')
                    history.append({'step': step, 'before': before, 'after': len(sources),
                                    'new': int(is_new.sum()),
                                    'observations_max': float(counts.max()),
                                    'observations_mean': float(counts.mean()),
                                    'growth_candidates': int(((density.gradient / counts.clamp_min(1)) >= options['grow_grad2d']).sum())})
                    state, optimizer = replace_topology(state, revised, optimizer, sources, is_new, config)
                    changed = True
                if changed or not policy.get('statistics'):
                    density.reset(state)
            reset = (step % options['opacity_reset_every'] == 0 and
                     step + len(scene.train_ids) < min(stop, config['iterations']))
            reset |= bool(policy.get('white_reset') and step == options['densify_start'] and step < stop)
            if reset:
                reset_opacity(state, optimizer)
            if official_order and step < 30000:
                optimizer_step(optimizer, state, config)
        if step % 100 == 0 or step == 1:
            value = float(loss.detach())
            if not math.isfinite(value) or any(not bool(torch.isfinite(p).all()) for p in state.values()):
                raise ValueError(f'Non-finite loss/parameters at {step}')
            progress = {'variant': args.variant, 'step': step, 'budget': args.steps, 'loss': value,
                        'gaussians': len(state['means']), 'wall_seconds': time.perf_counter()-began,
                        'peak_allocated_mb': torch.cuda.max_memory_allocated()/2**20}
            write(output / 'progress.json', progress)
            if step % 500 == 0:
                print(json.dumps(progress), flush=True)
        if step in (1000, 3000, 6666, 15000, 30000) or step == args.steps:
            evaluate(step, save_images=step == args.steps)
            write(output / 'density.json', history)
    torch.cuda.synchronize()
    train_seconds = time.perf_counter() - began - eval_seconds
    state = export_state(model) if model is not None else state
    # Save a standard project checkpoint for independent rendering. These
    # artifacts never replace the workbench's frozen source model/checkpoint.
    checkpoint_state = dict(state)
    if model is not None:
        center = torch.as_tensor(scene.center, device='cuda', dtype=torch.float32)
        checkpoint_state['means'] = (state['means'] - center) / scene.scale
        checkpoint_state['log_scales'] = state['log_scales'] - math.log(scene.scale)
    pipe._save_checkpoint(output, config, normalized_scene, None, checkpoint_state, args.steps, torch)
    render_times = []
    with torch.no_grad():
        for repeat in range(4):
            for image_id in scene.validation_ids:
                a, b = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                a.record()
                render(image_id, active_sh_degree(config, args.steps))
                b.record()
                b.synchronize()
                if repeat:
                    render_times.append(a.elapsed_time(b))
    result = {'variant': args.variant, 'policy': policy, 'seed': args.seed, 'steps': args.steps,
              'downscale': args.downscale, 'train_seconds': train_seconds,
              'eval_seconds': eval_seconds, 'peak_allocated_mb': torch.cuda.max_memory_allocated()/2**20,
              'peak_reserved_mb': torch.cuda.max_memory_reserved()/2**20,
              'render_gpu_ms': float(np.mean(render_times)), 'render_gpu_p95_ms': float(np.percentile(render_times, 95)),
              'metrics': curve[-1], 'gpu': torch.cuda.get_device_name(), 'torch': str(torch.__version__),
              'status': 'complete'}
    write(output / 'result.json', result)
    print(json.dumps({'complete': args.variant, 'psnr': result['metrics']['psnr'], 'train_seconds': train_seconds}), flush=True)
    return result


def guard_idle():
    import urllib.request
    with urllib.request.urlopen('http://127.0.0.1:8765/api/semantic/status', timeout=10) as stream:
        status = json.load(stream)
    if status.get('gpu_busy') or status.get('offline_gpu_busy'):
        raise RuntimeError('GPU is busy; no experiments started')


def suite(args):
    from minicolmap.ui import gpu_lease
    variants = args.variants.split(',')
    if any(variant not in POLICIES for variant in variants):
        raise ValueError('Unknown variant')
    args.report.mkdir(parents=True, exist_ok=True)
    manifest_path = args.report / f'manifest-d{args.downscale}-t{args.steps}-s{args.seed}.json'
    code_paths = sorted((ROOT / 'src/minicolmap/rendering').glob('*.py')) + [Path(__file__)]
    frozen_code = {str(path.relative_to(ROOT)): sha(path) for path in code_paths}
    from minicolmap.io import load_dataset
    dataset = load_dataset(DATASET)
    images = {str(path): sha(path) for path in dataset['image_paths'].values()}
    protocol = {'code': frozen_code, 'source': sha(ORIGINAL / 'source_model.json'), 'images': images,
                'original_checkpoint': sha(ORIGINAL / 'checkpoint.pt'),
                'official_revision': '54c035f7834b564019656c3e3fcc3646292f727d',
                'policies': {name: POLICIES[name] for name in variants},
                'steps': args.steps, 'downscale': args.downscale, 'seed': args.seed}
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if previous != protocol:
            raise ValueError('Frozen manifest differs; use a new report directory')
    else:
        write(manifest_path, protocol)
    for name, digest in frozen_code.items():
        saved = args.report / 'source' / digest[:12] / Path(name).name
        saved.parent.mkdir(parents=True, exist_ok=True)
        if not saved.exists():
            saved.write_bytes((ROOT / name).read_bytes())
    guard_idle()
    token = gpu_lease.acquire(ROOT, args.report)
    try:
        results = []
        for variant in variants:
            output = args.report / 'runs' / f'{variant}-s{args.seed}-d{args.downscale}-t{args.steps}'
            result_path = output / 'result.json'
            if result_path.exists():
                result = json.loads(result_path.read_text())
                results.append(result)
                continue
            if any(sha(ROOT / name) != digest for name, digest in frozen_code.items()):
                raise RuntimeError('Source changed during suite')
            output.mkdir(parents=True, exist_ok=True)
            write(args.report / 'active.json', {'variant': variant, 'output': str(output), 'started': time.time()})
            command = [sys.executable, __file__, '--worker', '--variant', variant,
                       '--steps', str(args.steps), '--downscale', str(args.downscale),
                       '--seed', str(args.seed), '--output', str(output)]
            print(json.dumps({'start': variant, 'output': str(output)}), flush=True)
            with (output / 'worker.log').open('w') as stream:
                completed = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
            if completed.returncode or not result_path.exists():
                result = {'variant': variant, 'status': 'failed', 'returncode': completed.returncode,
                          'log': str(output / 'worker.log')}
                write(output / 'failure.json', result)
            else:
                result = json.loads(result_path.read_text())
            results.append(result)
            write(args.report / f'results-d{args.downscale}-t{args.steps}-s{args.seed}.json', results)
            print(json.dumps({'finished': variant, 'status': result['status'],
                              'psnr': result.get('metrics', {}).get('psnr')}), flush=True)
    finally:
        gpu_lease.release(ROOT, token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--variant', choices=POLICIES, default='baseline')
    parser.add_argument('--variants', default=','.join(POLICIES))
    parser.add_argument('--steps', type=int, default=30000)
    parser.add_argument('--downscale', type=int, default=4)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--report', type=Path, default=REPORT)
    parser.add_argument('--output', type=Path, default=REPORT / 'smoke')
    args = parser.parse_args()
    if not 1 <= args.steps <= 30000 or args.downscale < 1:
        parser.error('steps must be 1..30000; downscale must be >=1')
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '7':
        parser.error('Set CUDA_VISIBLE_DEVICES=7')
    if args.worker:
        train(args)
    else:
        suite(args)


if __name__ == '__main__':
    main()
