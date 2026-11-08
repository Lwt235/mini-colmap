"""Optional CUDA transport preserving the S1/S2/S6 Gaussian computations.

The reference S3/S4 implementations remain numerical controls. gsplat replaces
projection and rasterization only when explicitly selected. Importing SfM/UI
never loads it.
"""
from __future__ import annotations


def active_sh_degree(config: dict, step: int) -> int:
    maximum = config['gaussian']['sh_degree']
    interval = config['gaussian']['sh_degree_interval']
    return maximum if interval == 0 else min(maximum, step // interval)


def gsplat_arguments(means, covariances, colors, opacities, K, R, t,
                     width, height, background):
    """Prepare the pinned gsplat 1.5.3 API; also testable without CUDA/gsplat.

    Supplying covars and pre-evaluated RGB preserves the S2/S6 computation graph.
    The pinhole kernel ignores skew; reject unsupported calibration explicitly.
    """
    import torch
    K = K / K[2, 2]
    standard = torch.stack((
        torch.stack((K[0, 0], K.new_zeros(()), K[0, 2])),
        torch.stack((K.new_zeros(()), K[1, 1], K[1, 2])),
        K.new_tensor([0., 0., 1.]),
    ))
    if not torch.allclose(K, standard, atol=1e-8, rtol=0):
        raise ValueError('gsplat 仅支持标准无 skew 内参；请使用 reference 后端保留完整 K')
    # Project coordinates address integer pixel centers in Mini-COLMAP; gsplat
    # evaluates pixels at (j+0.5,i+0.5). Shift only the rasterizer's principal point.
    K = K.clone()
    K[0, 2] += .5
    K[1, 2] += .5
    view = torch.cat((torch.cat((R, t[:, None]), dim=1), R.new_tensor([[0., 0., 0., 1.]])))
    return dict(means=means.contiguous(), covars=covariances.contiguous(), quats=None, scales=None,
                colors=colors.contiguous(), opacities=opacities.contiguous(),
                viewmats=view[None].contiguous(), Ks=K[None].contiguous(),
                width=width, height=height, backgrounds=background[None].contiguous(),
                # 1.5.3's packed wrapper loses the image dimension when validating
                # backgrounds. The dense single-camera path avoids that API bug.
                sh_degree=None, packed=False, rasterize_mode='classic', render_mode='RGB',
                eps2d=.3, near_plane=1e-6, far_plane=1e10)


def rasterize_gsplat(means, covariances, colors, opacities, K, R, t,
                     width, height, background, *, training_info=None, absgrad=False):
    if not means.is_cuda or str(means.dtype) != 'torch.float32':
        raise ValueError('gsplat 后端需要 CUDA float32 张量')
    if len(means) == 0:
        return background.expand(height, width, 3)
    try:
        from gsplat import rasterization
        from .gsplat_runtime import load_project_extension
        load_project_extension()
        # gsplat 1.5.3 can import successfully while its lazily loaded extension
        # is None. Detect that before the wrapper raises an opaque AttributeError.
        from gsplat.cuda._backend import _C
    except ImportError as exc:
        raise RuntimeError('未安装 gsplat CUDA 后端；见 docs/usage.md') from exc
    if _C is None:
        raise RuntimeError('gsplat Python 包已安装，但 CUDA 光栅扩展未加载；请配置 CUDA Toolkit/nvcc '
                           '与 MSVC C++ 编译环境后重启工作台。现在可选择 reference + CUDA 训练；'
                           '见 docs/usage.md')
    arguments = gsplat_arguments(means, covariances, colors, opacities, K, R, t,
                                width, height, background)
    arguments['absgrad'] = absgrad
    rgb, _, info = rasterization(**arguments)
    if training_info is not None:
        means2d = info['means2d']
        means2d.retain_grad()
        training_info.update(means2d=means2d, radii=info['radii'], absgrad=absgrad)
    return rgb[0]
