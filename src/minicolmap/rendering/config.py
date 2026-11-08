"""Independent, strict rendering configuration; SfM configuration is unchanged."""
from __future__ import annotations

import copy
import math


MAX_GAUSSIANS = 5_000_000


def validate_gaussian_count(value: int) -> int:
    """Shared configuration/checkpoint count limit; does not allocate tensors."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_GAUSSIANS:
        raise ValueError(f"max_gaussians 必须是 1 至 {MAX_GAUSSIANS} 的整数")
    return value


DEFAULT = {
    "method": "nerf", "iterations": 1000, "learning_rate": .001,
    "image_downscale": 8, "rays_per_batch": 256, "samples_per_ray": 32,
    "near": .1, "far": 4., "holdout_every": 4, "seed": 0,
    "device": "cpu", "preview_every": 100,
    "background": "white", "stratified_sampling": False, "ray_sampling": "image",
    "nerf": {"levels_xyz": 6, "levels_dir": 4, "hidden_dim": 64},
    "gaussian": {"initial_scale": .01, "initial_opacity": .1,
                 "initial_scale_mode": "fixed", "scene_normalization": "sparse_points",
                 "max_gaussians": 512, "densify_every": 0,
                 "sh_degree": 3, "sh_degree_interval": 0, "backend": "reference",
                 "optimizer_state": "preserve", "lr_mode": "uniform", "position_lr_final_ratio": .01,
                 "adam_eps": 1e-8, "sh_rest_lr_ratio": 1., "position_lr_multiplier": .16,
                 "absgrad": False, "grow_screen_radius": 0.,
                 "split_mode": "deterministic",
                 "loss": "mse", "ssim_weight": .2, "density_strategy": "legacy",
                 "densify_start": 500, "densify_stop": 15000, "grow_grad2d": .0002,
                 "split_scale": .01, "prune_scale": .1, "prune_screen_radius": .15,
                 "min_opacity": .005, "opacity_reset_every": 3000},
}


def resolve_render_config(overrides: dict | None = None) -> dict:
    result = copy.deepcopy(DEFAULT)
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ValueError("渲染配置必须是 JSON 对象")
        for key, value in overrides.items():
            if key not in result:
                raise ValueError(f"未知渲染参数：{key}")
            if isinstance(result[key], dict):
                if not isinstance(value, dict) or value.keys() - result[key].keys():
                    raise ValueError(f"渲染参数分组无效：{key}")
                result[key].update(value)
            else:
                result[key] = value
    if result['method'] not in ('nerf', '3dgs'):
        raise ValueError("method 必须是 nerf 或 3dgs")
    if result['device'] not in ('cpu', 'cuda'):
        raise ValueError("device 必须是 cpu 或 cuda")
    if result['background'] not in ('white', 'black'):
        raise ValueError("background 必须是 white 或 black")
    if not isinstance(result['stratified_sampling'], bool):
        raise ValueError("stratified_sampling 必须是布尔值")
    if result['ray_sampling'] not in ('image', 'all_images'):
        raise ValueError("ray_sampling 必须是 image 或 all_images")

    def number(target, key, low, high, integer=False, open_low=False):
        value = target[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < low or value > high
                or (open_low and value == low) or (integer and not isinstance(value, int))):
            raise ValueError(f"渲染参数 {key} 超出有效范围 [{low}, {high}]")
    for key, low, high in (
        ('iterations', 1, 1000000), ('image_downscale', 1, 128),
        ('rays_per_batch', 1, 65536), ('samples_per_ray', 2, 1024),
        ('holdout_every', 2, 1000000), ('seed', 0, 2147483647),
        ('preview_every', 1, 1000000),
    ):
        number(result, key, low, high, True)
    number(result, 'learning_rate', 0, 1, open_low=True)
    number(result, 'near', 0, 1000000)
    number(result, 'far', 0, 1000000, open_low=True)
    if result['far'] <= result['near']:
        raise ValueError("far 必须大于 near；单位为归一化后的重建距离")
    for key in ('levels_xyz', 'levels_dir'):
        number(result['nerf'], key, 0, 16, True)
    number(result['nerf'], 'hidden_dim', 4, 1024, True)
    number(result['gaussian'], 'initial_scale', 0, 10, open_low=True)
    number(result['gaussian'], 'initial_opacity', 0, 1, open_low=True)
    if result['gaussian']['initial_opacity'] == 1:
        raise ValueError("initial_opacity 必须严格小于 1")
    validate_gaussian_count(result['gaussian']['max_gaussians'])
    number(result['gaussian'], 'densify_every', 0, 1000000, True)
    number(result['gaussian'], 'sh_degree', 0, 3, True)
    number(result['gaussian'], 'sh_degree_interval', 0, 1000000, True)
    number(result['gaussian'], 'adam_eps', 0, 1, open_low=True)
    number(result['gaussian'], 'sh_rest_lr_ratio', 0, 1, open_low=True)
    number(result['gaussian'], 'position_lr_multiplier', 0, 10, open_low=True)
    number(result['gaussian'], 'grow_screen_radius', 0, 1000000)
    if not isinstance(result['gaussian']['absgrad'], bool):
        raise ValueError('absgrad 必须是布尔值')
    if result['gaussian']['absgrad'] and result['gaussian']['backend'] != 'gsplat':
        raise ValueError('绝对屏幕梯度需要 gsplat 后端')
    for key, choices in {'initial_scale_mode': ('fixed', 'knn'),
                         'split_mode': ('deterministic', 'stochastic'),
                         'scene_normalization': ('sparse_points', 'camera_extent'),
                         'optimizer_state': ('preserve', 'reset'), 'lr_mode': ('uniform', 'grouped'),
                         'loss': ('mse', 'l1_ssim'), 'density_strategy': ('legacy', 'adaptive')}.items():
        if result['gaussian'][key] not in choices:
            raise ValueError(f'gaussian.{key} 必须为 {choices}')
    for key in ('position_lr_final_ratio', 'ssim_weight', 'min_opacity'):
        number(result['gaussian'], key, 0, 1, open_low=key == 'position_lr_final_ratio')
    for key in ('grow_grad2d', 'split_scale', 'prune_scale', 'prune_screen_radius'):
        number(result['gaussian'], key, 0, 1000000, open_low=True)
    for key in ('densify_start', 'densify_stop', 'opacity_reset_every'):
        number(result['gaussian'], key, 0, 1000000, True)
    if result['gaussian']['densify_stop'] <= result['gaussian']['densify_start']:
        raise ValueError('densify_stop 必须大于 densify_start')
    if result['gaussian']['backend'] not in ('reference', 'gsplat'):
        raise ValueError("gaussian.backend 必须是 reference 或 gsplat")
    if (result['method'] == '3dgs' and result['gaussian']['backend'] == 'gsplat'
            and result['device'] != 'cuda'):
        raise ValueError("gsplat 光栅后端需要 device=cuda；CPU 数学验收请选择 reference")
    return result


def capabilities() -> dict:
    """Discover an optional dependency without loading CUDA into the web server."""
    import importlib.util
    import importlib.metadata
    import sys
    from pathlib import Path
    from .gsplat_runtime import validated_library
    available = importlib.util.find_spec('torch') is not None
    gsplat_available = importlib.util.find_spec('gsplat') is not None
    native_available = False
    if available and gsplat_available:
        try:
            manifest = Path(__file__).resolve().parents[3] / '.cache/gsplat-runtime.json'
            native_available = validated_library(manifest, {
                'python_tag': sys.implementation.cache_tag,
                'torch_distribution_version': importlib.metadata.version('torch'),
                'gsplat_version': importlib.metadata.version('gsplat'),
            }) is not None
        except (OSError, ValueError, KeyError, RuntimeError, importlib.metadata.PackageNotFoundError):
            pass  # Runtime ABI/build diagnostics belong to the training process.
    return {"torch_available": available, "cuda_available": None,
            "gsplat_available": gsplat_available, "gsplat_native_available": native_available,
            "limits": {"max_gaussians": MAX_GAUSSIANS},
            "cuda_note": "CUDA 可用性在训练子进程中检查；此页不初始化 GPU",
            "methods": ['nerf', '3dgs'], "defaults": resolve_render_config()}
