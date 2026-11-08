"""Bundle-adjustment residuals (B1), independent of the optimizer wrapper."""

from __future__ import annotations

import einx
import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.spatial.transform import Rotation

from .geometry import _array, _intrinsics


def bundle_residuals(
    rotvecs: ArrayLike,
    translations: ArrayLike,
    points: ArrayLike,
    K: ArrayLike,
    camera_indices: ArrayLike,
    point_indices: ArrayLike,
    observed_xy: ArrayLike,
) -> NDArray[np.float64]:
    """B1: Return 2O residuals ordered [du_0, dv_0, du_1, dv_1, ...].

    Shapes: rotvecs/translations (C, 3), points (N, 3), K (3, 3),
    camera_indices/point_indices (O,), observed_xy (O, 2). Rodrigues
    rotation vectors are radians and world-to-camera rotations. Transform
    each selected point by its selected camera, project with K, then
    return (predicted_xy - observed_xy).reshape(-1), float64.

    Rotations are evaluated with scipy.spatial.transform.Rotation.
    Reject any |camera_z| < 1e-12. Negative depth is mathematically
    projected, but BA input preparation must first remove behind-camera
    points; reprojection residuals alone do not enforce cheirality.

    Checkpoint: the caller must fix the first pose and one scale degree
    of freedom; otherwise the optimizer has a seven-DOF gauge freedom.
    """
    rotvecs = _array(rotvecs, "rotvecs", (None, 3))
    translations = _array(translations, "translations", (None, 3))
    points = _array(points, "points", (None, 3))
    K = _intrinsics(K, "K")
    observed_xy = _array(observed_xy, "observed_xy", (None, 2))
    if rotvecs.shape != translations.shape:
        raise ValueError("rotvecs and translations must have the same shape")
    indices = []
    for values, name, upper in (
        (camera_indices, "camera_indices", len(rotvecs)),
        (point_indices, "point_indices", len(points)),
    ):
        array = np.asarray(values)
        if array.ndim != 1 or len(array) != len(observed_xy):
            raise ValueError(f"{name} must have shape (O,)")
        if not np.issubdtype(array.dtype, np.integer):
            raise ValueError(f"{name} must contain integers")
        if np.any(array < 0) or np.any(array >= upper):
            raise ValueError(f"{name} contains an out-of-range index")
        indices.append(array)
    camera_indices, point_indices = indices
    # B1: observation-indexed projection with Rotation and flat residuals.
    if len(observed_xy) == 0:
        return np.empty(0, dtype=np.float64)
    R = Rotation.from_rotvec(rotvecs).as_matrix()

    R_obs = R[camera_indices]
    t_obs = translations[camera_indices]
    X_obs = points[point_indices]
    Xc = einx.dot("N i j, N j -> N i", R_obs, X_obs) + t_obs
    if np.any(np.abs(Xc[:, 2]) < 1e-12):
        raise ValueError("Camera depth is too close to zero")

    projected_h = Xc @ K.T
    predicted_xy = projected_h[:, :2] / projected_h[:, 2:3]
    residuals = predicted_xy - observed_xy

    return residuals.reshape(-1).astype(np.float64)
