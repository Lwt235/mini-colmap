"""Language-field algorithms for regions, feature compression and fusion (L1–L7).

Tensor functions use PyTorch, preserve gradients/device/dtype and do not mutate
inputs. The pipeline, pretrained encoders and rasterizer adapter live next to
this file. Algorithm acceptance tests are under tests/semantic; integration and
support checks are separate.
"""
from __future__ import annotations

import math
import numpy as np
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import torch
    from torch import Tensor
    from torch.nn import Module


def require_torch():
    """Import the optional tensor backend only when rendering is requested."""
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            'NeRF / 3DGS requires PyTorch; install with python -m pip install -e ".[render]"'
        ) from exc
    return torch


def _validate_array(
    value,
    name: str,
    shape: tuple[int | None, ...],
    *,
    dtype=None,
    check_finite: bool = True,
) -> np.ndarray:
    if not isinstance(value, np.ndarray):
        raise ValueError(f"{name} must be a NumPy array")

    if value.ndim != len(shape) or any(
        expected is not None and actual != expected
        for actual, expected in zip(value.shape, shape)
    ):
        raise ValueError(
            f"{name} must have shape {shape}; got {value.shape}"
        )

    if dtype is not None and value.dtype != np.dtype(dtype):
        raise ValueError(f"{name} must have dtype {dtype}")

    if check_finite:
        if not np.issubdtype(value.dtype, np.number):
            raise ValueError(f"{name} must be numeric")
        if not np.isrealobj(value):
            raise ValueError(f"{name} must contain real numbers")
        if not np.all(np.isfinite(value)):
            raise ValueError(f"{name} must contain finite values")

    return value


def _tensor(
    value: Tensor,
    name: str,
    shape: tuple[int | None, ...] | None = None,
    *,
    reference: Tensor | None = None,
    check_finite: bool = True,
) -> Tensor:
    torch = require_torch()
    if not isinstance(value, torch.Tensor):
        raise ValueError(f"{name} must be a torch.Tensor")
    if not value.is_floating_point():
        raise ValueError(f"{name} must be floating point")
    if shape is not None:
        if value.ndim != len(shape) or any(
            expected is not None and actual != expected
            for actual, expected in zip(value.shape, shape)
        ):
            raise ValueError(f"{name} must have shape {shape}; got {tuple(value.shape)}")
    if reference is not None:
        if value.dtype != reference.dtype:
            raise ValueError(f"{name} must have dtype {reference.dtype}")
        if value.device != reference.device:
            raise ValueError(f"{name} must be on device {reference.device}")
    if check_finite and not torch.isfinite(value).all().item():
        raise ValueError(f"{name} must contain only finite values")
    return value


def masks_to_regions(
    masks,
    quality,
    *,
    min_area=100,
    min_quality=0.7,
):
    """L1: Filter and resolve overlapping SAM masks."""

    M, H, W = masks.shape
    masks = _validate_array(masks, "masks", (M, H, W), dtype=np.bool_, check_finite=False)
    quality = _validate_array(quality, "quality", (M,))
    if not np.issubdtype(quality.dtype, np.floating):
        raise ValueError("quality must be floating point")

    if isinstance(min_area, (bool, np.bool_)) or not isinstance(min_area, (int, np.integer)) or min_area <= 0:
        raise ValueError("min_area must be a positive integer")
    if isinstance(min_quality, (bool, np.bool_)) or not isinstance(min_quality, (int, float, np.number)) or not math.isfinite(float(min_quality)) or not 0 <= min_quality <= 1:
        raise ValueError("min_quality must be in [0,1]")

    areas = masks.sum(axis=(1, 2), dtype=np.int64)
    valid = (
        (areas >= min_area)
        & (areas > 0)
        & (quality >= min_quality)
    )
    indices = np.flatnonzero(valid)

    order = sorted(
        indices.tolist(),
        key=lambda i: (
            int(areas[i]),
            -float(quality[i]),
            i,
        ),
    )

    regions = np.full(
        (H, W),
        -1,
        dtype=np.int32,
    )
    for i in order:
        claim = masks[i] & (regions == -1)
        regions[claim] = i

    return regions


def build_autoencoder(input_dim=512, latent_dim=3):
    """L2: return an nn.Module with .encode(x), .decode(z), forward(x).

    encode maps (...,input_dim) -> (...,latent_dim); decode reverses dimensions;
    forward equals decode(encode(x)). Use trainable nonlinear MLP layers, no
    output sigmoid, preserve arbitrary leading dimensions, and initialize with
    the caller's torch seed. Moving module with .to() must work. Do not normalize
    latent codes: encode/decode must describe the same numeric latent space.
    Raise ValueError for invalid dimensions (bool/non-int, <=0 or latent>=input).
    Suggested widths: encoder 512→256→128→64→latent, decoder reversed.
    """
    torch = require_torch()
    nn = torch.nn

    for name, value in (
        ("input_dim", input_dim),
        ("latent_dim", latent_dim),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if latent_dim >= input_dim:
        raise ValueError("latent_dim must be smaller than input_dim")

    class Autoencoder(nn.Module):
        def __init__(self):
            super().__init__()

            self.encoder = nn.Sequential(
                nn.Linear(input_dim, 256),
                nn.ReLU(),
                nn.Linear(256, 128),
                nn.ReLU(),
                nn.Linear(128, 64),
                nn.ReLU(),
                nn.Linear(64, latent_dim),
            )

            self.decoder = nn.Sequential(
                nn.Linear(latent_dim, 64),
                nn.ReLU(),
                nn.Linear(64, 128),
                nn.ReLU(),
                nn.Linear(128, 256),
                nn.ReLU(),
                nn.Linear(256, input_dim),
            )

        def encode(self, x):
            if x.shape[-1] != input_dim:
                raise ValueError(
                    f"Expected last dimension {input_dim}"
                )
            return self.encoder(x)

        def decode(self, z):
            if z.shape[-1] != latent_dim:
                raise ValueError(
                    f"Expected last dimension {latent_dim}"
                )
            return self.decoder(z)

        def forward(self, x):
            return self.decode(self.encode(x))

    return Autoencoder()


def reconstruction_loss(prediction, target):
    """L3: mean squared error + mean (1-cosine), scalar differentiable tensor.

    Inputs (N,D) same shape/device/dtype, finite, N,D>0. Cosine uses L2-normalized
    vectors with eps=1e-8. Do not normalize inputs to the MSE term. Reject invalid
    shapes or nonfinite data with ValueError. Keep gradients to prediction.
    """
    torch = require_torch()

    prediction = _tensor(prediction, "prediction", (None, None))
    N, D = prediction.shape
    target = _tensor(target, "target", (N, D), reference=prediction)

    if N == 0 or D == 0:
        raise ValueError("N and D must be positive")

    mse = torch.mean((prediction - target).square())
    eps = 1e-8
    pred_norm = torch.linalg.vector_norm(prediction, dim=-1, keepdim=True)
    target_norm = torch.linalg.vector_norm(target, dim=-1, keepdim=True)

    pred_unit = prediction / pred_norm.clamp_min(eps)
    target_unit = target / target_norm.clamp_min(eps)

    cosine = (pred_unit * target_unit).sum(dim=-1)
    cosine_loss = (1.0 - cosine).mean()

    return mse + cosine_loss


def normalize_feature_render(weighted_features, opacity, *, eps=1e-6):
    """L4: alpha-normalize splatted features without treating background as a feature.

    weighted_features is floating (H,W,D), opacity floating (H,W,1), same dtype
    and device. Return weighted_features / opacity.clamp_min(eps), but EXACT zero
    where opacity<=eps. Leave latent vector lengths unconstrained. The caller
    supplies premultiplied sums from the existing gsplat CUDA renderer. Preserve
    autograd to weighted_features. Validate shape/finiteness, opacity in [0,1],
    eps finite>0. For opacity=1, return the original feature values.
    """
    torch = require_torch()

    weighted_features = _tensor(weighted_features, "weighted_features", (None, None, None),)
    H, W, D = weighted_features.shape

    opacity = _tensor(opacity, "opacity", (H, W, 1), reference=weighted_features)
    if torch.any((opacity < 0) | (opacity > 1)).item():
        raise ValueError("opacity must be in [0,1]")
    if isinstance(eps, bool) or not isinstance(eps, (int, float)) or not math.isfinite(eps) or eps <= 0:
        raise ValueError("eps must be finite and positive")

    normalized = weighted_features / opacity.clamp_min(eps)
    normalized = torch.where(
        opacity > eps,
        normalized,
        torch.zeros_like(normalized),
    )
    return normalized


def distillation_loss(prediction, target, valid):
    """L5: masked L1 mean over VALID PIXELS and all feature channels.

    prediction/target floating (H,W,D), valid bool (H,W). Invalid target entries
    may be NaN and must not enter computation. Validate finite prediction and
    finite target at valid locations; reject wrong shapes/dtypes. If no valid
    pixels, return prediction.sum()*0 (not a detached constant). No RGB or
    geometry gradients are involved; the caller freezes those parameters.
    """
    torch = require_torch()

    prediction = _tensor(prediction, "prediction", (None, None, None))
    H, W, D = prediction.shape

    target = _tensor(target, "target", (H, W, D), reference=prediction, check_finite=False)

    if not isinstance(valid, torch.Tensor):
        raise ValueError("valid must be a torch.Tensor")
    if valid.shape != (H, W):
        raise ValueError("valid must have shape (H,W)")
    if valid.dtype != torch.bool:
        raise ValueError("valid must have bool dtype")
    if valid.device != prediction.device:
        raise ValueError("valid must share device with prediction")
    if not torch.isfinite(target[valid]).all().item():
        raise ValueError("target must be finite at valid pixels")
    if not torch.any(valid).item():
        return prediction.sum() * 0.0

    differences = torch.abs(prediction[valid] - target[valid])
    return differences.mean()


def text_relevancy(features, positive, negatives, *, temperature=10.0):
    """L6: worst-negative pairwise CLIP relevancy, output shaped features.shape[:-1].

    features (...,D), positive (D,), negatives (M,D), M>=1, same floating dtype
    and device, all finite. L2-normalize embeddings (eps=1e-8), compute cosine
    scores. For each negative n compare [positive,n] with a temperature-scaled
    2-way softmax; return the minimum positive probability over negatives.
    Avoid softmax across all prompts together (that changes this score).
    Reject bad shape, empty negatives or temperature<=0/nonfinite. A feature
    with norm<=1e-8 must return 0, so empty pixels do not become false positives.
    """
    """L6: Worst-negative pairwise CLIP relevancy."""

    torch = require_torch()

    if not isinstance(features, torch.Tensor):
        raise ValueError("features must be a Tensor")
    if features.ndim < 1 or features.shape[-1] <= 0 or not features.is_floating_point() or not torch.isfinite(features).all().item():
        raise ValueError("Invalid features")

    D = features.shape[-1]

    for name, value, shape in (
        ("positive", positive, (D,)),
        ("negatives", negatives, (None, D)),
    ):
        if not isinstance(value, torch.Tensor):
            raise ValueError(f"{name} must be a Tensor")

        if (
            value.ndim != len(shape)
            or any(
                expected is not None and actual != expected
                for actual, expected in zip(value.shape, shape)
            )
        ):
            raise ValueError(f"Invalid {name} shape")

        if not value.is_floating_point() or value.dtype != features.dtype or value.device != features.device or not torch.isfinite(value).all().item():
            raise ValueError(f"Invalid {name}")

    if negatives.shape[0] < 1:
        raise ValueError("negatives must be nonempty")

    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("Invalid temperature")

    eps = 1e-8
    feature_norms = torch.linalg.vector_norm(features, dim=-1, keepdim=True)
    f = features / feature_norms.clamp_min(eps)
    p = positive / torch.linalg.vector_norm(positive).clamp_min(eps)
    n = negatives / torch.linalg.vector_norm(negatives, dim=-1, keepdim=True).clamp_min(eps)

    positive_scores = (f * p).sum(dim=-1)
    negative_scores = f @ n.T

    logits = temperature * (positive_scores.unsqueeze(-1) - negative_scores)
    probabilities = torch.sigmoid(logits)

    relevancy = probabilities.min(dim=-1).values
    zero_features = feature_norms.squeeze(-1) <= eps
    relevancy = torch.where(
        zero_features,
        torch.zeros_like(relevancy),
        relevancy,
    )

    return relevancy


def fuse_observations(indices, features, weights, gaussian_count):
    """L7: weighted feature-fusion baseline, with repeated Gaussian indices.

    indices int64 (K,), features float (K,D), weights float (K,), same device.
    Return (values,coverage): values float (N,D), coverage float (N,). Each value
    is sum(weight*feature)/sum(weight) for its Gaussian, zero if uncovered.
    coverage is the sum of weights. Preserve gradients to features/weights and
    support K=0. Reject negative/nonfinite weights, nonfinite features, invalid
    index bounds or N<1. Use scatter/index accumulation rather than K Python
    loops. The pipeline supplies view observations gated by rendered depth;
    this simple baseline is approximate and is not full visibility integration.
    """
    torch = require_torch()

    if isinstance(gaussian_count, bool) or not isinstance(gaussian_count, int) or gaussian_count < 1:
        raise ValueError("gaussian_count must be a positive integer")

    N = gaussian_count
    features = _tensor(features, "features", (None, None))
    K, D = features.shape

    if not isinstance(indices, torch.Tensor):
        raise ValueError("indices must be a Tensor")
    if indices.dtype != torch.int64:
        raise ValueError("indices must have dtype int64")
    if indices.shape != (K,):
        raise ValueError("indices must have shape (K,)")
    if indices.device != features.device:
        raise ValueError("indices must share device with features")
    if torch.any((indices < 0) | (indices >= N)).item():
        raise ValueError("Gaussian index out of range")

    weights = _tensor(weights, "weights", (K,), reference=features)

    if torch.any(weights < 0).item():
        raise ValueError("weights must be nonnegative")

    coverage = features.new_zeros((N,))
    coverage = coverage.index_add(
        dim=0,
        index=indices,
        source=weights,
    )

    weighted_features = features * weights.unsqueeze(-1)
    feature_sum = features.new_zeros((N, D))
    feature_sum = feature_sum.index_add(
        dim=0,
        index=indices,
        source=weighted_features,
    )
    safe_coverage = torch.where(
        coverage > 0,
        coverage,
        torch.ones_like(coverage),
    )

    values = feature_sum / safe_coverage.unsqueeze(-1)
    return values, coverage