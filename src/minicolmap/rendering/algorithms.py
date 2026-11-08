"""Differentiable algorithms for the NeRF / SH-colored 3DGS pipelines.

This module intentionally imports without PyTorch. Install the optional
``render`` dependencies before running tensor functions. Differentiable tensors
stay in PyTorch without NumPy conversion or detaching inside rendering code.
Camera poses use the existing SfM convention ``Xc = R @ Xw + t``.

The application reports algorithm errors explicitly rather than substituting
another reconstruction or fabricating rendered output.
"""
from __future__ import annotations

import math
import einx
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    import torch
    from torch import Tensor
    from torch.nn import Module


# Graphdeco SH constants
C0 = 0.28209479177387814
C1 = 0.4886025119029199

C2 = (
    1.0925484305920792,
    -1.0925484305920792,
    0.31539156525252005,
    -1.0925484305920792,
    0.5462742152960396,
)

C3 = (
    -0.5900435899266435,
    2.890611442640554,
    -0.4570457994644658,
    0.3731763325901154,
    -0.4570457994644658,
    1.445305721320277,
    -0.5900435899266435,
)


class RayRender(TypedDict):
    rgb: Tensor
    depth: Tensor
    opacity: Tensor
    weights: Tensor


class GaussianState(TypedDict):
    means: Tensor
    log_scales: Tensor
    quaternions: Tensor
    sh_coeffs: Tensor
    opacity_logits: Tensor


class ProjectedGaussians(TypedDict):
    means2d: Tensor
    cov2d: Tensor
    depths: Tensor
    visible: Tensor


class GaussianRender(TypedDict):
    rgb: Tensor
    depth: Tensor
    opacity: Tensor


def require_torch():
    """Import the optional tensor backend only when rendering is requested."""
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            'NeRF / 3DGS requires PyTorch; install with python -m pip install -e ".[render]"'
        ) from exc
    return torch


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


def generate_rays(K: Tensor, R: Tensor, t: Tensor, pixels: Tensor) -> tuple[Tensor, Tensor]:
    """N1: return world ray origins / unit directions, each shaped ``(N, 3)``.

    Inputs are floating tensors on one device with one dtype: K/R (3,3), t (3,),
    pixels (N,2), ordered (u,v). Integer pixel locations mean their exact stored
    coordinates; do not add 0.5. Each origin is ``-R.T @ t``. Use the inverse of
    K, including skew, then rotate into world coordinates and normalize. Keep
    autograd, dtype and device; support N=0 without changing the input tensors.
    Invalid shapes, nonfinite values, nonpositive focal lengths, a singular K
    or an improper rotation raise ValueError.
    """
    torch = require_torch()

    K = _tensor(K, "K", (3, 3))
    R = _tensor(R, "R", (3, 3), reference=K)
    t = _tensor(t, "t", (3,), reference=K)
    pixels = _tensor(pixels, "pixels", (None, 2), reference=K)

    if (K[0, 0] <= 0).item() or (K[1, 1] <= 0).item():
        raise ValueError("Focal lengths must be positive")
    if torch.linalg.matrix_rank(K).item() < 3:
        raise ValueError("Camera intrinsics are singular")
    I = torch.eye(3, dtype=R.dtype, device=R.device)
    if not torch.allclose(R @ R.T, I, atol=1e-6, rtol=0):
        raise ValueError("R must be orthogonal")
    if not torch.isclose(torch.linalg.det(R), R.new_tensor(1.0), atol=1e-6, rtol=0):
        raise ValueError("R must be a proper rotation")

    N, _ = pixels.shape
    pixels_h = torch.cat([
        pixels,
        torch.ones(
            (N, 1),
            dtype=pixels.dtype,
            device=pixels.device
        ),
    ], dim=1)

    directions_camera = torch.linalg.solve(K, pixels_h.T).T
    directions_world = directions_camera @ R
    directions_world = torch.nn.functional.normalize(directions_world, p=2, dim=-1)

    origin = -(R.T @ t)
    origins = origin.unsqueeze(0).expand(N, 3)
    return origins, directions_world


def positional_encoding(x: Tensor, levels: int, include_input: bool = True) -> Tensor:
    """N2: encode the last dimension while retaining all leading dimensions.

    Output order is ``[x, sin(pi*x), cos(pi*x), sin(2*pi*x), cos(2*pi*x), ...]``.
    Omit x only when include_input=False. Levels is a nonnegative integer; zero
    levels returns x (or an empty last dimension). Preserve dtype/device and
    gradients. Reject a negative/noninteger level count and nonfinite input.
    """
    torch = require_torch()

    x = _tensor(x, "x")

    if isinstance(levels, (bool, torch.Tensor)) or not isinstance(levels, int) or levels < 0:
        raise ValueError("levels must be a nonnegative integer")

    encoded = [x] if include_input else []
    for k in range(levels):
        frequency = (2 ** k) * torch.pi
        encoded.append(torch.sin(frequency * x))
        encoded.append(torch.cos(frequency * x))

    if not encoded:
        return x[..., :0]

    return torch.cat(encoded, dim=-1)


def volume_render(rgb: Tensor, sigma: Tensor, t_values: Tensor,
                  far: float | Tensor, background: Tensor) -> RayRender:
    """N3: differentiable emission-absorption compositing on unit-length rays.

    rgb is (N,S,3) in [0,1], sigma (N,S) >= 0, t_values (N,S) nondecreasing
    and >=0, S>=1. Far is a scalar or (N,) and must be >= each final sample.
    Last interval is far-last_t; it is never an artificial infinite interval.
    alpha_i=1-exp(-sigma_i*delta_i), weight_i=alpha_i*prod_{j<i}(1-alpha_j).
    Return rgb=(sum weight*color)+(1-sum weight)*background, unnormalized
    depth=sum weight*t, opacity=sum weight, and the original sample weights.
    Shapes: (N,3), (N,), (N,), (N,S). Background is (3,) in [0,1].
    Support an empty ray batch; validate shapes/finiteness and physical bounds
    with ValueError. Do not mutate inputs or sever autograd.
    """
    torch = require_torch()

    N, S, _ = rgb.shape
    if S < 1:
        raise ValueError("S must be >= 1")

    rgb = _tensor(rgb, "rgb", (None, None, 3))
    sigma = _tensor(sigma, "sigma", (N, S), reference=rgb)
    t_values = _tensor(t_values, "t_values", (N, S), reference=rgb)
    background = _tensor(background, "background", (3,), reference=rgb)
    if isinstance(far, torch.Tensor):
        far_tensor = _tensor(far, "far", () if far.ndim == 0 else (N,), reference=rgb)
    else:
        try:
            far_tensor = torch.as_tensor(far, dtype=rgb.dtype, device=rgb.device)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError("Invalid far") from exc
        if far_tensor.ndim != 0:
            raise ValueError("Non-tensor far must be scalar")
    if far_tensor.ndim == 0:
        far_tensor = far_tensor.expand(N)

    if not torch.isfinite(far_tensor).all().item():
        raise ValueError("far must be finite")
    if torch.any((rgb < 0) | (rgb > 1)).item():
        raise ValueError("RGB must be in [0, 1]")
    if torch.any((background < 0) | (background > 1)).item():
        raise ValueError("background must be in [0, 1]")
    if torch.any(sigma < 0).item():
        raise ValueError("sigma must be nonnegative")
    if torch.any(t_values < 0).item():
        raise ValueError("t_values must be nonnegative")
    if torch.any(t_values[:, 1:] < t_values[:, :-1]).item():
        raise ValueError("t_values must be non-decreasing")
    if torch.any(far_tensor < t_values[:, -1]).item():
        raise ValueError("far must be >= the last sample")

    delta = torch.cat([
        t_values[:, 1:] - t_values[:, :-1],
        far_tensor[:, None] - t_values[:, -1:],
    ], dim=1)

    alpha = -torch.expm1(-sigma * delta)
    transmittance = torch.cumprod(
        torch.cat([
            torch.ones_like(alpha[:, :1]),
            1.0 - alpha[:, :-1],
        ], dim=1),
        dim=1,
    )
    weights = transmittance * alpha
    opacity = weights.sum(dim=1)
    rendered_rgb = (
        (weights[..., None] * rgb).sum(dim=1)
        + (1.0 - opacity)[:, None] * background
    )
    depth = (weights * t_values).sum(dim=1)

    return {
        "rgb": rendered_rgb,
        "depth": depth,
        "opacity": opacity,
        "weights": weights,
    }


def build_nerf(xyz_levels: int, dir_levels: int, hidden_dim: int) -> Module:
    """N4: construct a trainable torch.nn.Module; import torch inside this body.

    forward(xyz,directions) accepts (N,3) floating tensors and returns rgb
    (N,3) in [0,1] and nonnegative density (N,). Position drives the shared
    features/density; direction affects only the color head. Use N2 encodings.
    All trainable tensors must be registered Parameters/submodules so .to(),
    .state_dict(), and Adam work. A small MLP with at least two hidden layers
    suffices; the API does not prescribe exact parameter names or widths
    for the heads. Levels >=0 and hidden_dim >=4; invalid arguments raise
    ValueError. Preserve batched autograd in both inputs and parameters.
    """
    torch = require_torch()
    nn = torch.nn

    for name, value, minimum in (
        ("xyz_levels", xyz_levels, 0),
        ("dir_levels", dir_levels, 0),
        ("hidden_dim", hidden_dim, 4),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")

    xyz_dim = 3 * (2 * xyz_levels + 1)
    dir_dim = 3 * (2 * dir_levels + 1)

    class NeRF(nn.Module):
        def __init__(self):
            super().__init__()

            self.position_layers = nn.ModuleList()
            for i in range(8):
                if i == 0:
                    input_dim = xyz_dim
                elif i == 4:
                    input_dim = hidden_dim + xyz_dim
                else:
                    input_dim = hidden_dim
                self.position_layers.append(
                    nn.Linear(input_dim, hidden_dim)
                )

            self.density_head = nn.Linear(hidden_dim, 1)
            # Keep the initial ReLU density branch active on the normalized
            # scene. A negative random bias can make every density/gradient zero.
            nn.init.constant_(self.density_head.bias, .1)
            self.feature_head = nn.Linear(hidden_dim, hidden_dim)
            self.color_hidden = nn.Linear(hidden_dim + dir_dim, hidden_dim // 2)
            self.color_output = nn.Linear(hidden_dim // 2, 3)

        def forward(self, xyz: Tensor, directions: Tensor) -> tuple[Tensor, Tensor]:
            xyz = _tensor(xyz, "xyz", (None, 3))
            directions = _tensor(directions, "directions", (xyz.shape[0], 3), reference=xyz)

            parameter = next(self.parameters())
            if xyz.dtype != parameter.dtype or xyz.device != parameter.device:
                raise ValueError("Input dtype/device must match model")

            xyz_encoded = positional_encoding(xyz, xyz_levels)
            dir_encoded = positional_encoding(directions, dir_levels)

            h = xyz_encoded
            for i, layer in enumerate(self.position_layers):
                if i == 4:
                    h = torch.cat([h, xyz_encoded], dim=-1)
                h = torch.relu(layer(h))

            features = self.feature_head(h)
            color_input = torch.cat([features, dir_encoded], dim=-1)
            color_features = torch.relu(self.color_hidden(color_input))

            sigma = torch.relu(self.density_head(h)).squeeze(-1)
            rgb = torch.sigmoid(self.color_output(color_features))
            return rgb, sigma

    return NeRF()


def initialize_gaussians(points: Tensor, colors: Tensor, initial_scale: float = 0.01,
                         initial_opacity: float = 0.1, sh_degree: int = 3) -> GaussianState:
    """S1: initialize one Gaussian per sparse SfM point without changing SfM.

    points/colors (G,3) float, colors in [0,1]. Return means=points.clone(),
    log_scales (G,3)=log(initial_scale), identity wxyz quaternions (G,4),
    sh_coeffs (G,(sh_degree+1)**2,3), with DC=(colors-0.5)/C0,
    C0=1/sqrt(4*pi), all higher coefficients zero. No RGB logit/sigmoid.
    sh_degree is an integer in [0,3], including zero; bool is invalid.
    opacity_logits (G,) equal
    logit(initial_opacity). Return independent leaf tensors with requires_grad
    so the runner can optimize/checkpoint all five fields. Initial scale must
    be finite >0; opacity finite and strictly between 0 and 1. Preserve dtype
    and device; support G=0; invalid input raises ValueError.
    """
    torch = require_torch()

    G, _ = points.shape
    points = _tensor(points, "points", (G, 3))
    colors = _tensor(colors, "colors", (G, 3), reference=points)
    if torch.any((colors < 0) | (colors > 1)).item():
        raise ValueError("colors must be in [0, 1]")
    if isinstance(initial_scale, (bool, torch.Tensor)) or not isinstance(initial_scale, (int, float)) or not math.isfinite(initial_scale) or initial_scale <= 0:
        raise ValueError("initial_scale must be finite and positive")
    if isinstance(initial_opacity, (bool, torch.Tensor)) or not isinstance(initial_opacity, (int, float)) or not math.isfinite(initial_opacity) or not 0 < initial_opacity < 1:
        raise ValueError("initial_opacity must be in (0,1)")
    if isinstance(sh_degree, bool) or not isinstance(sh_degree, int) or not 0 <= sh_degree <= 3:
        raise ValueError("sh_degree must be an integer in [0,3]")

    means = points.detach().clone()
    log_scales = torch.full((G, 3), math.log(initial_scale), dtype=points.dtype, device=points.device)
    quaternions = torch.zeros((G, 4), dtype=points.dtype, device=points.device)
    quaternions[:, 0] = 1.0
    num_sh = (sh_degree + 1) ** 2
    sh_coeffs = torch.zeros((G, num_sh, 3), dtype=points.dtype, device=points.device)
    sh_coeffs[:, 0, :] = (colors.detach() - 0.5) / C0
    opacity_logit = math.log(initial_opacity / (1.0 - initial_opacity))
    opacity_logits = torch.full((G,), opacity_logit, dtype=points.dtype, device=points.device)

    state = {
        "means": means,
        "log_scales": log_scales,
        "quaternions": quaternions,
        "sh_coeffs": sh_coeffs,
        "opacity_logits": opacity_logits,
    }

    return {
        key: value.detach().requires_grad_(True)
        for key, value in state.items()
    }


def evaluate_sh(sh_coeffs: Tensor, directions: Tensor, degree: int = 3) -> Tensor:
    """S6 (required): evaluate original-3DGS real spherical-harmonic RGB.

    sh_coeffs (G,B,3), B in {1,4,9,16}; directions (G,3) are WORLD vectors
    from camera center to Gaussian center (means-C), NOT their negatives or
    camera-local directions. Normalize nonzero directions inside this function.
    degree is a non-bool integer 0..3 with (degree+1)**2 <= B. Use the real-SH
    ordering/signs specified in docs/architecture.md (Graphdeco order).
    Return (G,3) = clamp_min(0.5 + sum(coeff*basis), 0). Do not sigmoid or
    clamp the upper bound: linear radiance may exceed 1; PNG export clips it.
    Only the first (degree+1)**2 coefficients participate. Preserve gradients
    through coefficients AND directions, including normalization; inactive
    coefficients have zero gradient. Preserve floating dtype/device, support
    G=0, do not mutate inputs. Reject shape/device/dtype mismatch, nonfinite
    inputs, zero direction, insufficient bases or invalid degree with ValueError.
    The caller filters invisible Gaussians before evaluating viewing directions.
    """
    torch = require_torch()

    G, B, _ = sh_coeffs.shape
    if isinstance(degree, bool) or not isinstance(degree, int) or not 0 <= degree <= 3:
        raise ValueError("degree must be an integer in [0, 3]")
    if B not in (1, 4, 9, 16):
        raise ValueError("Invalid number of SH bases")
    if (degree + 1) ** 2 > B:
        raise ValueError("Insufficient SH bases")

    sh_coeffs = _tensor(sh_coeffs, "sh_coeffs", (G, B, 3))
    directions = _tensor(directions, "directions", (G, 3), reference=sh_coeffs)
    lengths = torch.linalg.vector_norm(directions, dim=-1, keepdim=True)

    if torch.any(lengths == 0).item():
        raise ValueError("Zero direction vector")

    d = directions / lengths
    x, y, z = d.unbind(dim=-1)

    result = C0 * sh_coeffs[:, 0, :]
    if degree >= 1:
        result = (
            result
            - C1 * y[:, None] * sh_coeffs[:, 1, :]
            + C1 * z[:, None] * sh_coeffs[:, 2, :]
            - C1 * x[:, None] * sh_coeffs[:, 3, :]
        )
    if degree >= 2:
        xx, yy, zz = x*x, y*y, z*z
        xy, yz, xz = x*y, y*z, x*z
        result = (
            result
            + C2[0] * xy[:, None] * sh_coeffs[:, 4, :]
            + C2[1] * yz[:, None] * sh_coeffs[:, 5, :]
            + C2[2] * (2*zz - xx - yy)[:, None] * sh_coeffs[:, 6, :]
            + C2[3] * xz[:, None] * sh_coeffs[:, 7, :]
            + C2[4] * (xx- yy)[:, None] * sh_coeffs[:, 8, :]
        )
    if degree >= 3:
        result = (
            result
            + C3[0] * (y * (3 * xx - yy))[:, None] * sh_coeffs[:, 9, :]
            + C3[1] * (x * y * z)[:, None] * sh_coeffs[:, 10, :]
            + C3[2] * (y * (4 * zz - xx - yy))[:, None] * sh_coeffs[:, 11, :]
            + C3[3] * (z * (2 * zz - 3 * xx - 3 * yy))[:, None] * sh_coeffs[:, 12, :]
            + C3[4] * (x * (4 * zz - xx - yy))[:, None] * sh_coeffs[:, 13, :]
            + C3[5] * (z * (xx - yy))[:, None] * sh_coeffs[:, 14, :]
            + C3[6] * (x * (xx - 3 * yy))[:, None] * sh_coeffs[:, 15, :]
        )

    return torch.clamp_min(result + 0.5, 0.0)


def gaussian_covariance(log_scales: Tensor, quaternions: Tensor) -> Tensor:
    """S2: return full world covariance (G,3,3) from (G,3)/(G,4) parameters.

    Normalize wxyz quaternions, convert to proper rotation Q, and form
    Q @ diag(exp(log_scales)**2) @ Q.T. q and -q encode the same covariance.
    Reject zero-norm quaternions, shape mismatch, and nonfinite inputs with
    ValueError. Preserve gradients/device/dtype and support G=0. No isotropic
    replacement or diagonal-only shortcut: rotated ellipsoids must remain
    anisotropic.
    """
    torch = require_torch()

    G, _ = log_scales.shape
    log_scales = _tensor(log_scales, "log_scales", (G, 3))
    quaternions = _tensor(quaternions, "quaternions", (G, 4), reference=log_scales)

    norms = torch.linalg.vector_norm(quaternions, dim=-1, keepdim=True)
    if torch.any(norms == 0).item():
        raise ValueError("Zero-norm quaternion")
    q = quaternions / norms

    w, x, y, z = q.unbind(dim=-1)
    Q = torch.stack([
        torch.stack([1-2*(y*y + z*z), 2 * (x*y - w*z), 2 * (x*z + w*y)], dim=-1),
        torch.stack([2 * (x*y + w*z), 1-2*(x*x + z*z), 2 * (y*z - w*x)], dim=-1),
        torch.stack([2 * (x*z - w*y), 2 * (y*z + w*x), 1-2*(x*x + y*y)], dim=-1),
    ], dim=-2)

    scales = torch.exp(log_scales)
    covariance = (Q @ torch.diag_embed(scales.square()) @ Q.transpose(-1, -2))
    # The analytic result is symmetric; independent float32 dot products can
    # round differently. Preserve that invariant without detaching gradients.
    return 0.5 * covariance + 0.5 * covariance.transpose(-1, -2)


def project_gaussians(means: Tensor, covariances: Tensor, K: Tensor, R: Tensor,
                      t: Tensor, min_variance: float = 0.3) -> ProjectedGaussians:
    """S3: project centers / covariances using the pinhole projection Jacobian.

    Inputs means (G,3), covariances (G,3,3), K/R (3,3), t (3,). Transform
    covariance with R, then use the Jacobian of the full K pinhole projection,
    including skew. cov2d=J @ camera_cov @ J.T + min_variance*I; min_variance
    is a positive pixel-squared variance, not its standard deviation.
    Return means2d (G,2), cov2d (G,2,2), depths (G,) and boolean visible (G,).
    Visible means finite and z>0; image-bound clipping belongs to rasterization.
    For z<=0, return means2d=0 and cov2d=min_variance*I while retaining signed
    depth. This avoids divisions by zero and NaNs in unused branches. Support
    G=0, retain gradients of visible entries, never mutate the source model.
    Invalid shapes/nonfinite input, nonpositive min_variance, invalid camera
    or a non-symmetric/non-positive-definite covariance raises ValueError.
    """
    torch = require_torch()

    G = means.shape[0]
    means = _tensor(means, "means", (G, 3))
    covariances = _tensor(covariances, "covariances", (G, 3, 3), reference=means,)
    K = _tensor(K, "K", (3, 3), reference=means)
    R = _tensor(R, "R", (3, 3), reference=means)
    t = _tensor(t, "t", (3,), reference=means)

    if isinstance(min_variance, bool) or not isinstance(min_variance, (int, float)) or not math.isfinite(min_variance) or min_variance <= 0:
        raise ValueError("min_variance must be finite and positive")
    if torch.abs(K[2, 2]).item() < 1e-12:
        raise ValueError("Invalid K")
    Kn = K / K[2, 2]
    if not torch.allclose(Kn[2], Kn.new_tensor([0., 0., 1.]), atol=1e-6, rtol=0):
        raise ValueError("K must be a pinhole intrinsic matrix")
    if (Kn[0, 0] <= 0).item() or (Kn[1, 1] <= 0).item():
        raise ValueError("Focal lengths must be positive")

    I3 = torch.eye(3, dtype=R.dtype, device=R.device)
    if not torch.allclose(R.T @ R, I3, atol=1e-6, rtol=0):
        raise ValueError("R must be orthogonal")
    if not torch.isclose(torch.linalg.det(R), R.new_tensor(1.0), atol=1e-6, rtol=0):
        raise ValueError("R must be a proper rotation")

    if not torch.allclose(covariances, covariances.transpose(-1, -2), atol=1e-6, rtol=0):
        raise ValueError("Covariances must be symmetric")
    _, info = torch.linalg.cholesky_ex(covariances)
    if torch.any(info != 0).item():
        raise ValueError("Covariances must be positive definite")

    camera_means = means @ R.T + t
    camera_cov = R @ covariances @ R.T

    depths = camera_means[:, 2]
    visible = depths > 0
    safe_depths = torch.where(visible, depths, torch.ones_like(depths))

    X, Y, Z = camera_means.unbind(dim=-1)
    Zsafe = safe_depths

    fx, skew, fy = Kn[0, 0], Kn[0, 1], Kn[1, 1]
    projected_h = camera_means @ Kn.T
    means2d_raw = projected_h[:, :2] / Zsafe[:, None]

    J = torch.stack([
        torch.stack([fx / Zsafe, skew / Zsafe, -(fx * X + skew * Y) / (Zsafe ** 2)], dim=-1),
        torch.stack([torch.zeros_like(Zsafe), fy / Zsafe, -(fy * Y) / (Zsafe ** 2)], dim=-1)
    ], dim=-2)
    I2 = torch.eye(2, dtype=means.dtype, device=means.device)
    cov2d_raw = (J @ camera_cov @ J.transpose(-1, -2) + min_variance * I2)
    # Projection amplifies a few ulps into >1e-6 pixel^2 for trained ellipsoids.
    # Restore exact symmetry here instead of weakening S4's input validation.
    cov2d_raw = 0.5 * cov2d_raw + 0.5 * cov2d_raw.transpose(-1, -2)

    means2d = torch.where(visible[:, None], means2d_raw, torch.zeros_like(means2d_raw))
    cov2d = torch.where(visible[:, None, None], cov2d_raw, min_variance * I2.expand(G, 2, 2))

    return {
        "means2d": means2d,
        "cov2d": cov2d,
        "depths": depths,
        "visible": visible,
    }


def splat_gaussians(means2d: Tensor, cov2d: Tensor, depths: Tensor,
                    colors: Tensor, opacities: Tensor, width: int, height: int,
                    background: Tensor) -> GaussianRender:
    """S4: differentiable CPU reference splatting with front-to-back blending.

    Inputs (G,2),(G,2,2),(G,),(G,3),(G,), positive integer dimensions, (3,)
    background. Positive-depth Gaussians are sorted by depth (stable for ties).
    On integer pixel centers (u,v), evaluate exp(-0.5*delta.T@inv(cov)@delta).
    alpha=clamp(opacity*footprint,0,0.99), exclusive transmittance as in N3.
    Return rgb (H,W,3) including remaining background, unnormalized alpha-
    weighted depth (H,W), opacity (H,W). Zero/negative-depth inputs contribute
    nothing; do not normalize accumulated depth. Preserve gradients/device/
    dtype. Support G=0. Validate finite inputs, shapes, positive-definite
    symmetric covariances, nonnegative colors (SH may exceed 1),
    opacities/background in [0,1] with ValueError. Do not clamp rendered RGB.
    This broadcast/pixel implementation is a reference for small crops and
    numerical checks, not the real-time CUDA renderer of the original 3DGS paper.
    """
    torch = require_torch()

    G = means2d.shape[0]
    means2d = _tensor(means2d, "means2d", (None, 2))
    cov2d = _tensor(cov2d, "cov2d", (G, 2, 2), reference=means2d)
    depths = _tensor(depths, "depths", (G,), reference=means2d)
    colors = _tensor(colors, "colors", (G, 3), reference=means2d)
    opacities = _tensor(opacities, "opacities", (G,), reference=means2d)
    background = _tensor(background, "background", (3,), reference=means2d)

    if isinstance(width, bool) or not isinstance(width, int) or width <= 0 or isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise ValueError("width/height must be positive integers")
    if torch.any(colors < 0).item():
        raise ValueError("colors must be nonnegative")
    if torch.any((opacities < 0) | (opacities > 1)).item():
        raise ValueError("opacities must be in [0,1]")
    if torch.any((background < 0) | (background > 1)).item():
        raise ValueError("background must be in [0,1]")
    if not torch.allclose(cov2d, cov2d.transpose(-1, -2), atol=1e-6, rtol=0):
        raise ValueError("cov2d must be symmetric")
    if G > 0:
        _, info = torch.linalg.cholesky_ex(cov2d)
        if torch.any(info != 0).item():
            raise ValueError("cov2d must be positive definite")

    H, W = height, width
    valid = depths > 0
    valid_means = means2d[valid]
    valid_cov = cov2d[valid]
    valid_depths = depths[valid]
    valid_colors = colors[valid]
    valid_opacities = opacities[valid]

    order = torch.argsort(valid_depths, stable=True)
    valid_means = valid_means[order]
    valid_cov = valid_cov[order]
    valid_depths = valid_depths[order]
    valid_colors = valid_colors[order]
    valid_opacities = valid_opacities[order]

    M = valid_depths.shape[0]
    if M == 0:
        return {
            "rgb": background.view(1, 1, 3).expand(H, W, 3).clone(),
            "depth": means2d.new_zeros((H, W)),
            "opacity": means2d.new_zeros((H, W)),
        }

    ys = torch.arange(H, dtype=means2d.dtype, device=means2d.device)
    xs = torch.arange(W, dtype=means2d.dtype, device=means2d.device)
    grid_y, grid_x = torch.meshgrid(ys, xs, indexing="ij")
    pixels = torch.stack((grid_x, grid_y), dim=-1)
    delta = (pixels[None, :, :, :] - valid_means[:, None, None, :])
    inv_cov = torch.linalg.inv(valid_cov)
    mahalanobis = einx.dot("g h w [i], g [i j], g h w [j] -> g h w", delta, inv_cov, delta)

    footprint = torch.exp(-0.5 * mahalanobis)
    alpha = torch.clamp(valid_opacities[:, None, None] * footprint, min=0.0, max=0.99)
    survival = 1.0 - alpha
    transmittance = torch.cumprod(
        torch.cat([
            torch.ones_like(survival[:1]),
            survival[:-1],
        ], dim=0),
        dim=0,
    )
    weights = transmittance * alpha
    opacity = weights.sum(dim=0)
    rendered_rgb = (
        (weights[..., None] * valid_colors[:, None, None, :]).sum(dim=0)
        + (1.0 - opacity)[..., None] * background
    )
    depth = (weights * valid_depths[:, None, None]).sum(dim=0)

    return {
        "rgb": rendered_rgb,
        "depth": depth,
        "opacity": opacity
    }


def densify_and_prune(state: GaussianState, gradient_norm: Tensor,
                      threshold: float, min_opacity: float,
                      max_gaussians: int, *, return_lineage: bool = False) -> GaussianState:
    """S5 (optional): deterministic topology change, separate from Adam updates.

    gradient_norm (G,) finite >=0; threshold finite >=0, min_opacity in [0,1],
    max_gaussians positive integer. First retain sigmoid(opacity_logits) >=
    min_opacity in original order, capped to max_gaussians. In remaining slots,
    append one child for each retained source with gradient_norm>threshold,
    again in source order. Only sources actually receiving a child have all
    three scales halved (subtract log(2) from log_scales); children share those
    halved scales. Child center is source + Q[:,0]*min(original_scale), using
    its normalized wxyz rotation. Original means stay fixed. Copy all other
    fields unchanged; no randomness or hidden gradient-based displacement.
    Return independent detached leaf tensors requiring gradients. Do not
    mutate input state. Optional return_lineage adds old source row indices and
    an is_new mask, allowing the caller to migrate Adam state without resets.
    """
    torch = require_torch()

    required_keys = ("means", "log_scales", "quaternions", "sh_coeffs", "opacity_logits")
    if not isinstance(state, dict) or any(key not in state for key in required_keys):
        raise ValueError("Invalid GaussianState")
    G = state["means"].shape[0]
    means = _tensor(state["means"], "means", (None, 3))
    log_scales = _tensor(state["log_scales"], "log_scales", (G, 3), reference=means)
    quaternions = _tensor(state["quaternions"], "quaternions",(G, 4), reference=means)
    sh_coeffs = _tensor(state["sh_coeffs"], "sh_coeffs", (G, None, 3), reference=means)
    opacity_logits = _tensor(state["opacity_logits"], "opacity_logits", (G,), reference=means)
    gradient_norm = _tensor(gradient_norm, "gradient_norm", (G,), reference=means)

    if sh_coeffs.shape[1] not in (1, 4, 9, 16):
        raise ValueError("Invalid SH coefficient count")
    if torch.any(gradient_norm < 0).item():
        raise ValueError("gradient_norm must be nonnegative")
    if isinstance(threshold, (bool, torch.Tensor)) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or threshold < 0:
        raise ValueError("threshold must be finite and >= 0")
    if isinstance(min_opacity, (bool, torch.Tensor)) or not isinstance(min_opacity, (int, float)) or not math.isfinite(min_opacity) or not 0 <= min_opacity <= 1:
        raise ValueError("min_opacity must be in [0,1]")
    if isinstance(max_gaussians, bool) or not isinstance(max_gaussians, int) or max_gaussians <= 0:
        raise ValueError("max_gaussians must be a positive integer")

    q_norms = torch.linalg.vector_norm(quaternions, dim=-1, keepdim=True)
    if torch.any(q_norms == 0).item():
        raise ValueError("Zero-norm quaternion")

    with torch.no_grad():
        opacities = torch.sigmoid(opacity_logits)
        keep_indices = torch.nonzero(
            opacities >= min_opacity,
            as_tuple=True
        )[0][:max_gaussians]
        n_keep = keep_indices.numel()
        retained = {
            key: state[key][keep_indices].clone()
            for key in required_keys
        }

        capacity = max_gaussians - n_keep
        selected = keep_indices[gradient_norm[keep_indices] > threshold]
        child_indices = selected[:capacity]
        n_children = child_indices.numel()

        if n_children > 0:
            q = quaternions[child_indices] / q_norms[child_indices]
            w, x, y, z = q.unbind(dim=-1)
            local_x_world = torch.stack([
                1-2*(y*y + z*z),
                2 * (x*y + w*z),
                2 * (x*z - w*y),
            ], dim=-1)

            original_scales = torch.exp(log_scales[child_indices])
            offset_length = original_scales.min(dim=-1, keepdim=True).values
            child_means = (means[child_indices] + local_x_world * offset_length)
            child_log_scales = (log_scales[child_indices] - math.log(2.0))
            parent_mask = torch.isin(keep_indices, child_indices)
            retained["log_scales"][parent_mask] -= math.log(2.0)

            children = {
                "means": child_means,
                "log_scales": child_log_scales,
                "quaternions": quaternions[child_indices].clone(),
                "sh_coeffs": sh_coeffs[child_indices].clone(),
                "opacity_logits": opacity_logits[child_indices].clone(),
            }

            result = {
                key: torch.cat([retained[key], children[key]], dim=0)
                for key in required_keys
            }
        else:
            result = retained

    revised = {
        key: value.detach().clone().requires_grad_(True)
        for key, value in result.items()
    }
    if return_lineage:
        sources = torch.cat((keep_indices, child_indices))
        is_new = torch.arange(len(sources), device=sources.device) >= n_keep
        return revised, sources, is_new
    return revised
