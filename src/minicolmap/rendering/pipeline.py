"""Training and I/O orchestration around the differentiable rendering algorithms.

The reference renderer is intentionally small: one coarse NeRF and SH-colored
Gaussian splats. The optional gsplat adapter accelerates projection/rasterization;
it is not a complete paper reproduction. SfM inputs remain read-only.
"""
from __future__ import annotations

import json
import hashlib
import math
import shutil
import time
import uuid
from contextlib import suppress
from pathlib import Path

import cv2
import numpy as np

from . import algorithms as learner
from .config import resolve_render_config
from .data import load_training_scene, load_posed_dataset, gaussian_initial_scales, scaled_intrinsics
from .gaussian_backend import active_sh_degree, rasterize_gsplat
from .gaussian_training import (DensityStatistics, adaptive_topology, make_optimizer,
                               photometric_loss, replace_topology, reset_opacity,
                               ssim_image, update_learning_rates, optimizer_step)


def _torch(device='cpu'):
    try:
        import torch
    except ImportError as exc:
        raise ImportError('NeRF/3DGS 需要可选依赖：pip install -e ".[render]"') from exc
    if device == 'cuda' and not torch.cuda.is_available():
        raise ValueError('当前 PyTorch 无可用 CUDA；请选择 cpu 或按官方说明安装匹配的 CUDA 版本')
    return torch


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    _replace(temporary, path)


def _replace(temporary, target):
    # Readers of previews/checkpoints can briefly hold Windows sharing locks.
    for attempt in range(21):
        try:
            temporary.replace(target)
            return
        except PermissionError:
            if attempt == 20:
                raise
            time.sleep(.01)


def _write_image(path, rgb):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = np.asarray(rgb)
    if values.ndim != 3 or values.shape[-1] != 3 or not np.isfinite(values).all():
        raise ValueError('渲染图像必须是有限 H×W×3 RGB')
    bgr = cv2.cvtColor(np.round(np.clip(values, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    ok, encoded = cv2.imencode('.png', bgr)
    if not ok:
        raise OSError('无法编码训练预览')
    # Independent offline requests must not share the same temporary filename.
    temporary = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_bytes(encoded.tobytes())
        _replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _write_reference_image(path, rgb):
    """Reference photos are immutable within a run; never replace a reader's file."""
    path = Path(path)
    if not path.is_file():
        _write_image(path, rgb)


def _tensor(value, torch, device):
    return torch.as_tensor(value, dtype=torch.float32, device=device)


def _pixels(width, height, torch, device):
    y, x = torch.meshgrid(torch.arange(height, device=device),
                          torch.arange(width, device=device), indexing='ij')
    return torch.stack([x, y], dim=-1).reshape(-1, 2).float()


def _background(config, torch, device, dtype=None):
    return torch.full((3,), 0. if config.get('background', 'white') == 'black' else 1.,
                      device=device, dtype=dtype or torch.float32)


def _nerf_ray_bundle(network, origins, directions, config, torch, *, training=False):
    """Sample a world-ray batch using the network and volume-compositing algorithms."""
    count = config['samples_per_ray']
    # Left-edge samples cover [near, far] with equal finite intervals. No infinite
    # terminal delta or hierarchical sampling is hidden in the compositing contract.
    positions = config['near'] + torch.arange(count, device=origins.device, dtype=origins.dtype) * (
        config['far'] - config['near']) / count
    positions = positions.expand(len(origins), -1)
    if training and config.get('stratified_sampling', False):
        # Jitter independently inside ordered bins. Anchor the first sample at
        # near so finite-interval compositing still integrates the whole range.
        jitter = torch.rand_like(positions)
        jitter[:, 0] = 0
        positions = positions + jitter * ((config['far'] - config['near']) / count)
    xyz = origins[:, None, :] + directions[:, None, :] * positions[..., None]
    dirs = directions[:, None, :].expand_as(xyz)
    rgb, sigma = network(xyz.reshape(-1, 3), dirs.reshape(-1, 3))
    return learner.volume_render(rgb.reshape(-1, count, 3), sigma.reshape(-1, count),
                                 positions, config['far'], _background(config, torch, origins.device, origins.dtype))


def _nerf_rays(network, K, R, t, pixels, config, torch, *, training=False):
    origins, directions = learner.generate_rays(K, R, t, pixels)
    return _nerf_ray_bundle(network, origins, directions, config, torch, training=training)


def _training_ray_pool(scene, torch, device):
    """Only training photos enter the pool; no validation colors or rays."""
    origins, directions, targets = [], [], []
    with torch.no_grad():
        for image_id in scene.train_ids:
            frame = scene.frames[image_id]
            ray_o, ray_d = learner.generate_rays(*[_tensor(getattr(frame, k), torch, device)
                for k in ('K', 'R', 't')], _pixels(frame.width, frame.height, torch, device))
            origins.append(ray_o)
            directions.append(ray_d)
            targets.append(_tensor(frame.rgb, torch, device).reshape(-1, 3))
    return torch.cat(origins), torch.cat(directions), torch.cat(targets)


def _render(network, state, frame, config, torch, device, *, sh_degree=None, training_info=None):
    K, R, t = [_tensor(frame[key], torch, device) for key in ('K', 'R', 't')]
    width, height = frame['width'], frame['height']
    if config['method'] == 'nerf':
        pixels = _pixels(width, height, torch, device)
        chunks = []
        for chunk in pixels.split(config['rays_per_batch']):
            chunks.append(_nerf_rays(network, K, R, t, chunk, config, torch)['rgb'])
        return torch.cat(chunks).reshape(height, width, 3)
    means = state['means']
    # Evaluate SH only on positive-depth entries. A center at the observer is
    # invisible and must not generate an invalid zero viewing direction.
    front = ((means @ R.T + t)[:, 2] > 0)
    means = means[front]
    covariance = learner.gaussian_covariance(state['log_scales'][front], state['quaternions'][front])
    center = -(R.T @ t)
    colors = learner.evaluate_sh(state['sh_coeffs'][front], means - center,
        config['gaussian']['sh_degree'] if sh_degree is None else sh_degree)
    opacities = state['opacity_logits'].sigmoid().reshape(-1)[front]
    background = _background(config, torch, device)
    if training_info is not None:
        training_info.update(indices=front.nonzero(as_tuple=True)[0], width=width, height=height)
    if config['gaussian']['backend'] == 'gsplat':
        if training_info is not None:
            return rasterize_gsplat(means, covariance, colors, opacities, K, R, t, width, height, background,
                                    training_info=training_info, absgrad=config['gaussian']['absgrad'])
        return rasterize_gsplat(means, covariance, colors, opacities, K, R, t, width, height, background)
    projected = learner.project_gaussians(means, covariance, K, R, t)
    visible = projected['visible']
    if training_info is not None:
        projected['means2d'].retain_grad()
        radius = torch.linalg.eigvalsh(projected['cov2d']).amax(-1).clamp_min(0).sqrt() * 3
        training_info.update(means2d=projected['means2d'], radii=radius * visible)
    return learner.splat_gaussians(
        projected['means2d'][visible], projected['cov2d'][visible], projected['depths'][visible],
        colors[visible], opacities[visible], width, height, background)['rgb']


def _frame_record(frame):
    return {'image_id': frame.image_id, 'K': frame.K.tolist(), 'R': frame.R.tolist(),
            't': frame.t.tolist(), 'width': frame.width, 'height': frame.height}


def _save_checkpoint(output, config, scene, network, state, step, torch, warm_start=None):
    checkpoint = {'schema_version': 1, 'pipeline': config['method'], 'config': config, 'step': step,
                  'normalization': {'center': scene.center.tolist(), 'scale': scene.scale,
                                    'source': scene.normalization_source},
                  'frames': [_frame_record(f) for f in scene.frames.values()],
                  'train_ids': scene.train_ids, 'validation_ids': scene.validation_ids,
                  'state': {k: v.detach().cpu() for k, v in (
                      network.state_dict() if network is not None else state).items()}}
    if state is not None:
        checkpoint.update(schema_version=2, color_model='real_sh',
                          active_sh_degree=active_sh_degree(config, step))
    if warm_start is not None:
        checkpoint['warm_start'] = warm_start
    temporary = output / 'checkpoint.pt.tmp'
    torch.save(checkpoint, temporary)
    _replace(temporary, output / 'checkpoint.pt')
    if state is not None:
        # Coordinates in this archive are ORIGINAL SfM world units; the tensor
        # checkpoint remains normalized. The manifest makes both conventions explicit.
        means = state['means'].detach().cpu().numpy() * scene.scale + scene.center
        covariance = learner.gaussian_covariance(state['log_scales'], state['quaternions']).detach().cpu().numpy()
        temporary = output / 'gaussians.npz.tmp'
        with temporary.open('wb') as stream:
            np.savez_compressed(stream, means=means, covariances=covariance * scene.scale ** 2,
                                scales=state['log_scales'].exp().detach().cpu().numpy() * scene.scale,
                                quaternions=state['quaternions'].detach().cpu().numpy(),
                                sh_coeffs=state['sh_coeffs'].detach().cpu().numpy(),
                                sh_degree=config['gaussian']['sh_degree'],
                                active_sh_degree=active_sh_degree(config, step), schema_version=2,
                                sh_direction='camera_to_gaussian_world',
                                opacities=state['opacity_logits'].sigmoid().detach().cpu().numpy())
        _replace(temporary, output / 'gaussians.npz')


def train(source_model_path, dataset_path, output, config=None, *, on_event=None, initial_checkpoint=None):
    """Train NeRF or SH-colored Gaussian models with the configured renderer.

    Orchestrates loss, Adam, deterministic batches, evaluation, persistence and
    notifications around the network and rendering algorithms.
    """
    config = resolve_render_config(config)
    if initial_checkpoint is not None and config['method'] != '3dgs':
        raise ValueError('检查点微调目前仅支持 3DGS')
    if source_model_path is None and config['method'] == '3dgs':
        raise ValueError('3DGS 需要已有点云模型，请先构建或加载 model.json')
    torch = _torch(config['device'])
    if config['device'] == 'cuda':
        torch.cuda.reset_peak_memory_stats()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ('checkpoint.pt', 'summary.json', 'training.json')):
        raise ValueError('训练输出已存在；请选择独立的新目录')
    started = time.monotonic()
    records, previews = [], []
    metrics = {}
    status = {'status': 'running', 'stage': 'preparing', 'pipeline': config['method'],
              'source_model': str(Path(source_model_path).resolve()) if source_model_path is not None else None,
              'source_kind': 'posed_dataset' if source_model_path is None else 'model',
              'dataset': str(Path(dataset_path).resolve())}

    def event(stage, **value):
        status['stage'] = stage
        status['elapsed_seconds'] = time.monotonic() - started
        _write_json(output / 'run_status.json', status)
        if on_event:
            on_event(dict(stage=stage, **value))

    _write_json(output / 'config.json', config)
    try:
        event('preparing', kind='stage', message='读取照片、内参与相机位姿' if config['method'] == 'nerf'
              else '读取照片、相机位姿与初始化点云')
        frozen = output / 'source_model.json'
        if source_model_path is None:
            from ..io import save_reconstruction
            save_reconstruction(load_posed_dataset(dataset_path), frozen)
        elif Path(source_model_path).resolve() != frozen.resolve():
            shutil.copy2(source_model_path, frozen)
        source_model_path = frozen
        status['source_model'] = str(frozen.resolve())
        scene = load_training_scene(source_model_path, dataset_path, config)
        prepared = {'normalization': {'center': scene.center.tolist(), 'scale': scene.scale,
                                     'source': scene.normalization_source},
                    'pose_convention': 'world_to_camera', 'pixel_convention': 'integer pixel centers',
                    'train_ids': scene.train_ids, 'validation_ids': scene.validation_ids,
                    'source_points': len(scene.points), 'frames': [_frame_record(f) for f in scene.frames.values()],
                    'notes': '验证视图只留出外观，位姿由输入提供；若来自全图 SfM，不是严格全流程未见视角。RGB 沿用 sRGB。'}
        _write_json(output / 'training_scene.json', prepared)
        torch.manual_seed(config['seed'])
        if config['device'] == 'cuda':
            torch.cuda.manual_seed_all(config['seed'])
        rng = np.random.default_rng(config['seed'])
        device = config['device']
        network, state, warm_start = None, None, None
        if config['method'] == 'nerf':
            network = learner.build_nerf(config['nerf']['levels_xyz'], config['nerf']['levels_dir'],
                                         config['nerf']['hidden_dim']).to(device)
            parameters = list(network.parameters())
        elif initial_checkpoint is None:
            count = min(len(scene.points), config['gaussian']['max_gaussians'])
            indices = np.linspace(0, len(scene.points) - 1, count, dtype=int)
            initial = learner.initialize_gaussians(_tensor(scene.points[indices], torch, device),
                _tensor(scene.colors[indices], torch, device),
                config['gaussian']['initial_scale'], config['gaussian']['initial_opacity'],
                config['gaussian']['sh_degree'])
            scales = gaussian_initial_scales(scene.points[indices], config['gaussian'])
            if config['gaussian']['initial_scale_mode'] == 'knn':
                with torch.no_grad():
                    initial['log_scales'].copy_(_tensor(np.repeat(np.log(scales)[:, None], 3, axis=1), torch, device))
            _write_json(output / 'initialization.json', {
                'scale_mode': config['gaussian']['initial_scale_mode'],
                'normalization_source': scene.normalization_source,
                'scale_quantiles': np.quantile(scales, [0, .1, .5, .9, 1]).tolist()})
            if config['gaussian']['initial_scale_mode'] == 'fixed':
                neighbors = gaussian_initial_scales(scene.points[indices],
                    {**config['gaussian'], 'initial_scale_mode': 'knn'})
                ratio = float(np.median(scales) / np.median(neighbors))
                if ratio > 10:
                    event('preparing', kind='warning', message=(
                        f'固定初始高斯尺度约为局部点间距的 {ratio:.1f} 倍，'
                        '可能在屏幕尺寸裁剪时大量丢点；LERF 建议导入 configs/3dgs_lerf.json'))
            state = {k: torch.nn.Parameter(v.detach().clone()) for k, v in initial.items()}
            parameters = list(state.values())
        if initial_checkpoint is not None:
            initial_checkpoint = Path(initial_checkpoint).resolve()
            previous = torch.load(initial_checkpoint, map_location='cpu', weights_only=True)
            original_source = initial_checkpoint.parent / 'source_model.json'
            def digest(path):
                with path.open('rb') as stream:
                    result = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        result.update(chunk)
                    return result.hexdigest()
            if (previous.get('schema_version') != 2 or previous.get('pipeline') != '3dgs'
                    or previous['config']['gaussian']['sh_degree'] != config['gaussian']['sh_degree']):
                raise ValueError('微调需要同球谐阶数的 3DGS 检查点')
            if not original_source.is_file() or digest(original_source) != digest(source_model_path):
                raise ValueError('微调源 SfM 模型与检查点不匹配')
            old_normalization = previous['normalization']
            if (not np.allclose(old_normalization['center'], scene.center, rtol=0, atol=1e-10)
                    or not np.isclose(old_normalization['scale'], scene.scale, rtol=0, atol=1e-10)
                    or previous['train_ids'] != scene.train_ids or previous['validation_ids'] != scene.validation_ids):
                raise ValueError('微调必须沿用检查点的归一化与训练/验证划分')
            if len(previous['state']['means']) > config['gaussian']['max_gaussians']:
                raise ValueError('微调容量小于原检查点的高斯数量')
            for key, value in previous['state'].items():
                if not torch.isfinite(value).all():
                    raise ValueError(f'微调检查点包含非有限值：{key}')
            state = {key: torch.nn.Parameter(value.to(device=device, dtype=torch.float32).detach().clone())
                     for key, value in previous['state'].items()}
            parameters = list(state.values())
            warm_start = {'checkpoint': str(initial_checkpoint), 'source_step': previous['step'],
                          'optimizer': 'fresh Adam moments; iterations are additional fine-tuning steps'}
            _write_json(output / 'warm_start.json', warm_start)
            _write_json(output / 'initialization.json', {
                'scale_mode': 'checkpoint', 'gaussian_count': len(state['means']),
                'normalization_source': scene.normalization_source, 'warm_start': warm_start})
            del previous
        optimizer = (make_optimizer(state, config) if state is not None else
                     torch.optim.Adam(parameters, lr=config['learning_rate']))
        density = DensityStatistics(state) if state is not None and config['gaussian']['density_strategy'] == 'adaptive' else None
        density_records = []
        ray_pool = (_training_ray_pool(scene, torch, device)
                    if network is not None and config.get('ray_sampling') == 'all_images' else None)
        warned_empty_views = set()
        for step in range(1, config['iterations'] + 1):
            if ray_pool is not None:
                frame = None
            elif state is None:
                frame = scene.frames[int(rng.choice(scene.train_ids))]
            else:
                # A legal empty splat returns a constant background, so there
                # is no parameter graph to backpropagate. Select a supported
                # training view instead; never substitute a validation view.
                frame = None
                for image_id in rng.permutation(scene.train_ids):
                    candidate = scene.frames[int(image_id)]
                    with torch.no_grad():
                        camera_points = state['means'] @ _tensor(candidate.R.T, torch, device) + _tensor(candidate.t, torch, device)
                        supported = bool(torch.any(camera_points[:, 2] > 0))
                    if supported:
                        frame = candidate
                        break
                    if candidate.image_id not in warned_empty_views:
                        warned_empty_views.add(candidate.image_id)
                        event('training', kind='warning', message=f'训练相机 #{candidate.image_id} 没有正深度高斯，本轮跳过')
                if frame is None:
                    raise ValueError('所有训练视角都没有正深度高斯；检查位姿、点云与初始化')
            target = None if frame is None else _tensor(frame.rgb, torch, device)
            if state is not None:
                update_learning_rates(optimizer, config, step)
            optimizer.zero_grad(set_to_none=True)
            training_info = {} if (density is not None and config['gaussian']['densify_every']
                                  and config['gaussian']['densify_start'] <= step < config['gaussian']['densify_stop']) else None
            if ray_pool is not None:
                indices = torch.as_tensor(rng.integers(0, len(ray_pool[2]),
                    size=config['rays_per_batch']), device=device)
                prediction = _nerf_ray_bundle(network, ray_pool[0][indices], ray_pool[1][indices],
                    config, torch, training=True)['rgb']
                loss = (prediction - ray_pool[2][indices]).square().mean()
            elif network is not None:
                indices = torch.as_tensor(rng.integers(0, frame.width * frame.height,
                    size=config['rays_per_batch']), device=device)
                pixels = torch.stack([indices % frame.width, indices // frame.width], dim=-1).float()
                prediction = _nerf_rays(network, _tensor(frame.K, torch, device),
                    _tensor(frame.R, torch, device), _tensor(frame.t, torch, device), pixels, config, torch,
                    training=True)['rgb']
                loss = (prediction - target.reshape(-1, 3)[indices]).square().mean()
            else:
                render_options = {'sh_degree': active_sh_degree(config, step)}
                if training_info is not None:
                    render_options['training_info'] = training_info
                prediction = _render(None, state, _frame_record(frame), config, torch, device, **render_options)
                loss = photometric_loss(prediction, target, config)
            if not torch.isfinite(loss):
                raise ValueError('训练损失不是有限数；检查渲染算法的数值稳定性')
            loss.backward()
            if training_info is not None:
                density.accumulate(training_info)
            if not any(p.grad is not None for p in parameters):
                raise ValueError('渲染计算图未连接到可训练参数')
            if any(p.grad is not None and not torch.isfinite(p.grad).all() for p in parameters):
                raise ValueError('训练梯度出现 NaN/Inf')
            optimizer_step(optimizer, state, config)
            if state is not None and config['gaussian']['densify_every'] and step % config['gaussian']['densify_every'] == 0:
                options = config['gaussian']
                before_count = len(state['means'])
                topology = None
                if density is None:
                    gradients = state['means'].grad.norm(dim=-1) if state['means'].grad is not None else torch.zeros(len(state['means']), device=device)
                    topology = learner.densify_and_prune(state, gradients, 1e-3, options['min_opacity'],
                                                        options['max_gaussians'], return_lineage=True)
                elif (options['densify_start'] <= step < options['densify_stop'] and
                      (not options['opacity_reset_every'] or step % options['opacity_reset_every'] >= len(scene.train_ids))):
                    topology = adaptive_topology(state, density, options)
                if topology is not None:
                    revised, sources, is_new = topology
                    if not len(revised['means']):
                        diagnosis = {'step': step, 'before': before_count,
                            'opacity_rejected': int((state['opacity_logits'].sigmoid() < options['min_opacity']).sum()),
                            'normalization_source': scene.normalization_source}
                        if density is not None:
                            diagnosis.update(
                                world_scale_rejected=int((state['log_scales'].exp().amax(-1) > options['prune_scale']).sum()),
                                screen_radius_rejected=int((density.radius > options['prune_screen_radius']).sum()))
                        _write_json(output / 'density_failure.json', diagnosis)
                        raise ValueError('密度控制将移除全部高斯，已停止且未应用该次裁剪；'
                                         '检查场景归一化、初始尺度和裁剪阈值。LERF 请使用 '
                                         'configs/3dgs_lerf.json（相机范围归一化与近邻尺度初始化）')
                    state, optimizer = replace_topology(state, revised, optimizer, sources, is_new, config)
                    parameters = list(state.values())
                    density_records.append({'step': step, 'before': before_count, 'after': len(state['means']),
                                            'new': int(is_new.sum()), 'optimizer_state': options['optimizer_state']})
                if density is not None:
                    density.reset(state)
            if (density is not None and config['gaussian']['opacity_reset_every']
                and step % config['gaussian']['opacity_reset_every'] == 0
                and step + len(scene.train_ids) < min(config['gaussian']['densify_stop'], config['iterations'])):
                reset_opacity(state, optimizer)
            metrics.update(step=step, total=config['iterations'], loss=float(loss.detach()))
            records.append(dict(metrics))
            event('training', kind='progress', completed=step, total=config['iterations'], metrics=dict(metrics),
                  message=f"训练 {step}/{config['iterations']}，loss {metrics['loss']:.6g}")
            if step % config['preview_every'] == 0 or step == config['iterations']:
                validation = scene.frames[scene.validation_ids[0]]
                with torch.no_grad():
                    rgb = _render(network, state, _frame_record(validation), config, torch, device,
                                  sh_degree=active_sh_degree(config, step))
                    mse = float((rgb - _tensor(validation.rgb, torch, device)).square().mean())
                if not math.isfinite(mse):
                    raise ValueError('验证图像含 NaN/Inf')
                metrics.update(validation_mse=mse, psnr=-10 * math.log10(max(mse, 1e-12)),
                               psnr_scope='first_validation_view')
                relative = f'previews/step-{step:06d}.png'
                reference = f'previews/reference-{validation.image_id}.png'
                _write_image(output / relative, rgb.detach().cpu().numpy())
                _write_reference_image(output / reference, validation.rgb)
                preview = {'path': relative, 'reference_path': reference, 'image_id': validation.image_id, 'step': step}
                previews.append(preview)
                _save_checkpoint(output, config, scene, network, state, step, torch, warm_start)
                _write_json(output / 'training.json', {'metrics': records, 'previews': previews})
                event('validation', kind='preview', metrics=dict(metrics), preview=preview,
                      message='保存真实验证视图与权重')
        errors, per_view = [], []
        with torch.no_grad():
            for image_id in scene.validation_ids:
                frame = scene.frames[image_id]
                result = _render(network, state, _frame_record(frame), config, torch, device,
                                 sh_degree=active_sh_degree(config, config['iterations']))
                target = _tensor(frame.rgb, torch, device)
                error = float((result - target).square().mean())
                errors.append(error)
                per_view.append({'image_id': image_id, 'mse': error,
                                 'psnr': -10 * math.log10(max(error, 1e-12)),
                                 'ssim': float(ssim_image(result, target))})
                _write_image(output / f'validation/image-{image_id}.png', result.detach().cpu().numpy())
                _write_reference_image(output / f'previews/reference-{image_id}.png', frame.rgb)
        validation_mse = float(np.mean(errors))
        if not math.isfinite(validation_mse):
            raise ValueError('最终验证误差不是有限数')
        metrics.update(validation_mse=validation_mse, psnr=-10 * math.log10(max(validation_mse, 1e-12)),
                       ssim=float(np.mean([item['ssim'] for item in per_view])),
                       psnr_scope='mean_validation_view_mse')
        summary = {'pipeline': config['method'], 'step': config['iterations'],
                   'image_count': len(scene.frames), 'registered_images': len(scene.frames),
                   'point_count': len(state['means']) if state is not None else len(scene.points),
                   'metrics': metrics, 'previews': previews, 'checkpoint': 'checkpoint.pt',
                   'elapsed_seconds': time.monotonic() - started,
                   'train_ids': scene.train_ids, 'validation_ids': scene.validation_ids}
        summary['per_view_metrics'] = per_view
        summary['peak_cuda_allocated_mb'] = torch.cuda.max_memory_allocated() / 2**20 if device == 'cuda' else None
        summary['peak_cuda_reserved_mb'] = torch.cuda.max_memory_reserved() / 2**20 if device == 'cuda' else None
        summary['photometric_loss'] = config['gaussian']['loss'] if state is not None else 'mse'
        if warm_start is not None:
            summary['warm_start'] = warm_start
        _write_json(output / 'density.json', density_records)
        _write_json(output / 'summary.json', summary)
        status['status'] = 'completed'
        event('done', kind='complete', summary=summary)
        return summary
    except Exception as exc:
        status.update(status='todo' if isinstance(exc, NotImplementedError) else 'failed', error=str(exc))
        event(status['stage'], kind='error', message=str(exc))
        raise


def _render_gaussian_preview(state, frame, config, torch, *, sh_degree=None, max_gaussian_pixels=4_000_000):
    """Bound CPU reference intermediates without dropping or changing Gaussians."""
    count = len(state['means'])
    width, height = frame['width'], frame['height']
    if count * width * height <= max_gaussian_pixels:
        return _render(None, state, frame, config, torch, 'cpu', sh_degree=sh_degree)
    edge = max(1, min(64, math.isqrt(max(1, max_gaussian_pixels // max(count, 1)))))
    rows = []
    for top in range(0, height, edge):
        tiles = []
        for left in range(0, width, edge):
            K = np.array(frame['K'], dtype=np.float64, copy=True)
            K[0] -= left * K[2]
            K[1] -= top * K[2]
            tile = {**frame, 'width': min(edge, width - left), 'height': min(edge, height - top),
                    'K': K.tolist()}
            tiles.append(_render(None, state, tile, config, torch, 'cpu', sh_degree=sh_degree))
        rows.append(torch.cat(tiles, dim=1))
    return torch.cat(rows, dim=0)


def render_checkpoint(output, *, camera=None, image_id=None, device='cpu', max_edge=512):
    """Render a saved checkpoint; camera extrinsics use ORIGINAL SfM units."""
    if isinstance(max_edge, bool) or not isinstance(max_edge, int) or not 1 <= max_edge <= 1920:
        raise ValueError('预览边长必须是 1 至 1920 的整数')
    output = Path(output)
    torch = _torch(device)
    checkpoint = torch.load(output / 'checkpoint.pt', map_location='cpu', weights_only=True)
    if checkpoint.get('schema_version') not in (1, 2):
        raise ValueError('不支持的训练权重格式')
    if checkpoint.get('pipeline') == '3dgs' and 'sh_coeffs' not in checkpoint['state']:
        raise ValueError('旧 RGB-only 3DGS 权重不能当作球谐模型；请用新接口重新训练')
    config = resolve_render_config(checkpoint['config'])
    config['device'] = device
    config['gaussian']['backend'] = 'gsplat' if device == 'cuda' else 'reference'
    if (camera is None) == (image_id is None):
        raise ValueError('请只提供 image_id 或 camera 之一')
    if camera is not None:
        from ..models import Camera, Pose
        if not isinstance(camera, dict):
            raise ValueError('camera 必须是对象')
        c = Camera(1, camera['width'], camera['height'], camera['K'])
        pose = Pose(camera['R'], camera['t'])
        if c.width > max_edge or c.height > max_edge:
            raise ValueError(f'自定义渲染预览每边最多 {max_edge} 像素')
        normalization = checkpoint['normalization']
        frame = {'K': c.K.tolist(), 'R': pose.R.tolist(),
                 't': ((pose.R @ np.array(normalization['center']) + pose.t) / normalization['scale']).tolist(),
                 'width': c.width, 'height': c.height}
    else:
        if isinstance(image_id, bool) or not isinstance(image_id, int):
            raise ValueError('image_id 必须是整数')
        frame = next((f for f in checkpoint['frames'] if f['image_id'] == image_id), None)
        if frame is None:
            raise ValueError('该相机没有参与本次训练/验证数据准备')
        frame = dict(frame)
        if max(frame['width'], frame['height']) > max_edge:
            old_size = (frame['width'], frame['height'])
            ratio = max_edge / max(old_size)
            size = (max(1, round(old_size[0] * ratio)), max(1, round(old_size[1] * ratio)))
            frame.update(width=size[0], height=size[1],
                         K=scaled_intrinsics(np.asarray(frame['K']), old_size, size).tolist())
    network, state = None, None
    if config['method'] == 'nerf':
        network = learner.build_nerf(config['nerf']['levels_xyz'], config['nerf']['levels_dir'], config['nerf']['hidden_dim'])
        network.load_state_dict(checkpoint['state'])
        network.eval()
    else:
        state = {key: value.to(device) for key, value in checkpoint['state'].items()}
    with torch.no_grad():
        if state is not None and device == 'cuda':
            rgb = _render(None, state, frame, config, torch, device,
                          sh_degree=checkpoint.get('active_sh_degree'))
        elif state is not None:
            rgb = _render_gaussian_preview(state, frame, config, torch,
                                           sh_degree=checkpoint.get('active_sh_degree'))
        else:
            rgb = _render(network, state, frame, config, torch, 'cpu',
                          sh_degree=checkpoint.get('active_sh_degree'))
    relative = f'previews/render-{uuid.uuid4().hex[:12]}.png'
    _write_image(output / relative, rgb.cpu().numpy())
    preview = {'path': relative, 'step': checkpoint['step'], 'width': frame['width'], 'height': frame['height']}
    if image_id is not None:
        preview['image_id'] = image_id
        reference = f'previews/reference-{image_id}.png'
        if not (output / reference).exists():
            # A manual camera can be a training view, whereas periodic previews
            # only saved the first validation reference. Recover its actual
            # resized photo when source data is still available. A portable
            # checkpoint remains renderable after the source photos are moved.
            try:
                status = json.loads((output / 'run_status.json').read_text(encoding='utf-8'))
                scene = load_training_scene(output / 'source_model.json', status['dataset'], config)
                _write_reference_image(output / reference, scene.frames[image_id].rgb)
            except (OSError, ValueError, KeyError):
                pass
        if (output / reference).exists():
            # Keep comparison pixels on the same grid after preview resizing;
            # never overwrite references belonging to training/evaluation.
            pixels = cv2.imread(str(output / reference), cv2.IMREAD_COLOR)
            if pixels is not None and pixels.shape[:2] != (frame['height'], frame['width']):
                reference = relative.replace('render-', 'reference-')
                rgb_reference = cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
                rgb_reference = cv2.resize(rgb_reference, (frame['width'], frame['height']), interpolation=cv2.INTER_AREA)
                _write_reference_image(output / reference, rgb_reference)
            preview['reference_path'] = reference
    return preview
