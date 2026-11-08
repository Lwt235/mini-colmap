"""Deterministic synthetic observations for geometry validation.

This fixture generator uses ground truth to create observations. Production
reconstruction must estimate poses and points from those observations; it must
never take ``rotations``, ``translations`` or ``points`` as estimated results.
"""

from __future__ import annotations

from pathlib import Path
from numbers import Integral

import numpy as np

from .models import Camera, Pose


SCENE_KEYS = (
    "K", "rotations", "translations", "points", "observations", "visible", "colors", "outliers"
)


def generate_scene(
    seed: int = 0,
    num_points: int = 80,
    num_cameras: int = 4,
    noise_px: float = 0.0,
    outlier_ratio: float = 0.0,
) -> dict[str, np.ndarray]:
    """Generate a 640x480 pinhole scene with noncoplanar points at depth 4–8.

    Camera centers span a 0.8-unit horizontal baseline; rotations are identity.
    Gaussian noise is added in pixels before replacing a controlled number
    (rounded ``ratio * visible observations``) with uniform image outliers.
    Visibility is computed from clean projections, not noisy measurements.
    """
    for name, value in (("num_points", num_points), ("num_cameras", num_cameras)):
        if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not np.isfinite(noise_px) or noise_px < 0:
        raise ValueError("noise_px must be finite and nonnegative")
    if not np.isfinite(outlier_ratio) or not 0 <= outlier_ratio <= 1:
        raise ValueError("outlier_ratio must be in [0, 1]")
    rng = np.random.default_rng(seed)
    K = np.array([[800.0, 0.0, 320.0], [0.0, 800.0, 240.0], [0.0, 0.0, 1.0]])
    points = rng.uniform([-0.8, -0.65, 4.0], [0.8, 0.65, 8.0], size=(num_points, 3))
    centers = np.zeros((num_cameras, 3))
    if num_cameras > 1:
        centers[:, 0] = np.linspace(-0.4, 0.4, num_cameras)
    rotations = np.broadcast_to(np.eye(3), (num_cameras, 3, 3)).copy()
    translations = -centers
    camera_points = points[None, :, :] + translations[:, None, :]
    homogeneous = camera_points @ K.T
    clean = homogeneous[..., :2] / homogeneous[..., 2:3]
    visible = (
        (camera_points[..., 2] > 0)
        & (clean[..., 0] >= 0) & (clean[..., 0] < 640)
        & (clean[..., 1] >= 0) & (clean[..., 1] < 480)
    )
    observations = clean + rng.normal(0.0, noise_px, clean.shape)
    outliers = np.zeros_like(visible)
    candidates = np.flatnonzero(visible)
    count = round(outlier_ratio * len(candidates))
    if count:
        chosen = rng.choice(candidates, count, replace=False)
        outliers.flat[chosen] = True
        observations[outliers] = rng.uniform([0.0, 0.0], [640.0, 480.0], (count, 2))
    colors = rng.integers(32, 256, (num_points, 3), dtype=np.uint8)
    scene = {
        "K": K,
        "rotations": rotations,
        "translations": translations,
        "points": points,
        "observations": observations,
        "visible": visible,
        "colors": colors,
        "outliers": outliers,
    }
    _validate_scene(scene)
    return scene


def _validate_scene(scene: dict[str, np.ndarray]) -> None:
    missing = set(SCENE_KEYS) - set(scene)
    if missing:
        raise ValueError(f"Scene is missing keys: {sorted(missing)}")
    points = np.asarray(scene["points"])
    rotations = np.asarray(scene["rotations"])
    if points.ndim != 2 or points.shape[1] != 3 or len(points) < 1:
        raise ValueError("Scene points must have shape (N, 3), N >= 1")
    if rotations.ndim != 3 or rotations.shape[1:] != (3, 3) or len(rotations) < 1:
        raise ValueError("Scene rotations must have shape (C, 3, 3), C >= 1")
    n, c = len(points), len(rotations)
    expected = {
        "K": (3, 3), "rotations": (c, 3, 3), "translations": (c, 3),
        "points": (n, 3), "observations": (c, n, 2), "visible": (c, n),
        "colors": (n, 3), "outliers": (c, n),
    }
    for key, shape in expected.items():
        value = np.asarray(scene[key])
        if value.shape != shape:
            raise ValueError(f"Scene {key} must have shape {shape}")
        if not np.issubdtype(value.dtype, np.number) and value.dtype != np.bool_:
            raise ValueError(f"Scene {key} must be numeric")
        if not np.isfinite(value).all():
            raise ValueError(f"Scene {key} must be finite")
    for key in ("visible", "outliers"):
        if np.asarray(scene[key]).dtype != np.bool_:
            raise ValueError(f"Scene {key} must have boolean dtype")
    if np.asarray(scene["colors"]).dtype != np.uint8:
        raise ValueError("Scene colors must have uint8 dtype")
    if np.any(np.asarray(scene["outliers"]) & ~np.asarray(scene["visible"])):
        raise ValueError("Outliers must be visible observations")
    Camera(1, 640, 480, scene["K"])
    for R, t in zip(scene["rotations"], scene["translations"]):
        Pose(R, t)


def _npz_path(path: str | Path) -> Path:
    result = Path(path)
    if result.suffix.lower() != ".npz":
        raise ValueError("Expected a .npz path")
    return result


def save_scene(scene: dict[str, np.ndarray], path: str | Path) -> None:
    """Save fixture arrays in a compressed archive without object arrays."""
    _validate_scene(scene)
    path = _npz_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **{key: scene[key] for key in SCENE_KEYS})


def load_scene(path: str | Path) -> dict[str, np.ndarray]:
    """Load fixtures with pickle disabled, then validate all required shapes."""
    with np.load(_npz_path(path), allow_pickle=False) as archive:
        missing = set(SCENE_KEYS) - set(archive.files)
        if missing:
            raise ValueError(f"Scene is missing keys: {sorted(missing)}")
        scene = {key: archive[key].copy() for key in SCENE_KEYS}
    _validate_scene(scene)
    return scene
