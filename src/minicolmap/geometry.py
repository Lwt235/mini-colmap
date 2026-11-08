"""Multi-view geometry algorithms (G1–G8).

Conventions: float64 arrays with observations in rows; world-to-camera
``X_c = R @ X_w + t``; ``x2_h.T @ F @ x1_h = 0``. Intrinsics describe
undistorted pinhole images. Do not feed distorted keypoints into these APIs.
Inputs are validated before geometric estimation.
"""

from __future__ import annotations

import math
import numpy as np
import einx
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]


def _array(value: ArrayLike, name: str, shape: tuple[int | None, ...]) -> FloatArray:
    """Convert input to float64 and reject an incorrect shape or nonfinite data."""
    try:
        result = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a numeric array") from exc
    if result.ndim != len(shape) or any(
        required is not None and actual != required
        for actual, required in zip(result.shape, shape)
    ):
        raise ValueError(f"{name} must have shape {shape}; got {result.shape}")
    if not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain only finite values")
    return result


def _intrinsics(value: ArrayLike, name: str) -> FloatArray:
    result = _array(value, name, (3, 3))
    if np.linalg.matrix_rank(result) < 3:
        raise ValueError(f"{name} must be invertible")
    if not np.allclose(result[2], [0.0, 0.0, 1.0], atol=1e-12):
        raise ValueError(f"{name} must be a pinhole K with last row [0, 0, 1]")
    return result


def _correspondences(
    x1: ArrayLike, x2: ArrayLike, minimum: int = 0
) -> tuple[FloatArray, FloatArray]:
    a = _array(x1, "x1", (None, 2))
    b = _array(x2, "x2", (None, 2))
    if a.shape != b.shape:
        raise ValueError("x1 and x2 must have the same shape")
    if len(a) < minimum:
        raise ValueError(f"at least {minimum} correspondences are required")
    return a, b


def project_points(
    X: ArrayLike, K: ArrayLike, R: ArrayLike, t: ArrayLike
) -> tuple[FloatArray, FloatArray]:
    """G1: Project world points, returning pixels (N, 2) and camera depth (N,).

    Use X_c = X @ R.T + t, then apply K and divide by camera z. Return
    negative depth too: cheirality filtering belongs to the caller. Reject
    any |z| < 1e-12 with ValueError. Empty (0, 3) input returns empty arrays.
    R is a world-to-camera rotation, t is not the camera's world position.

    Checkpoint: can you derive the camera center C = -R.T @ t?
    """
    X = _array(X, "X", (None, 3))
    K = _intrinsics(K, "K")
    R = _array(R, "R", (3, 3))
    t = _array(t, "t", (3,))
    # G1: camera coordinates -> homogeneous pixels -> Euclidean pixels.
    if X.shape[0] == 0:
        return np.empty((0, 2)), np.empty((0,))
    X_c = X @ R.T + t
    P = X_c @ K.T
    if (np.abs(P[:, 2]) < 1e-12).any():
        raise ValueError(f"found at least one of the |z| < 1e-12")
    pixels, z = P[:, :2] / P[:, 2:3], P[:, 2]
    return pixels, z


def normalize_points(points: ArrayLike) -> tuple[FloatArray, FloatArray]:
    """G2: Hartley normalize pixels, returning coordinates and transform T.

    Move the centroid to (0, 0), then scale so the mean Euclidean distance
    from the origin is sqrt(2). This is mean distance, not RMS distance.
    With row storage, normalized_h = points_h @ T.T. Reject empty input
    and a spread below 1e-12. Never silently return an identity transform
    for repeated points.

    Checkpoint: why does normalization improve the conditioning of A?
    """
    points = _array(points, "points", (None, 2))
    if len(points) == 0:
        raise ValueError("normalization requires at least one point")
    # G2: centroid, mean distance, similarity T, transformed coordinates.
    centroid = points.mean(axis=0)                          # (2,)
    shifted = points - centroid
    mean_dist = np.linalg.norm(shifted, axis=1).mean()
    if mean_dist < 1e-12:
        raise ValueError("all points nearly coincident")
    scale = np.sqrt(2) / mean_dist
    T = np.array([
        [scale, 0,     -scale * centroid[0]],
        [0,     scale, -scale * centroid[1]],
        [0,     0,      1],
    ])
    normalized_h = np.c_[points, np.ones(len(points))] @ T.T
    normalized = normalized_h[:, :2]    # 最后一列全为1
    return normalized, T


def eight_point(x1: ArrayLike, x2: ArrayLike) -> FloatArray:
    """G3: Estimate F with the normalized eight-point algorithm (N >= 8).

    Build A for x2_h.T @ F @ x1_h = 0 using row-major F coefficients.
    Estimate the null vector with SVD, enforce rank 2, then unnormalize
    F = T2.T @ F_normalized @ T1. Return finite (3, 3) float64, with
    Frobenius norm 1 (its sign is arbitrary). For exactly 8 rows use the
    *full* right singular-vector matrix; reduced SVD loses the null vector.
    Reject rank(A) < 8, repeated/collinear points, or zero-norm F with
    ValueError. Planar data can also be degenerate for this estimator.

    Checkpoint: rank enforcement happens before unnormalization; why?
    """
    x1, x2 = _correspondences(x1, x2, minimum=8)
    # G3: use G2; estimate F; enforce rank 2; unnormalize and normalize F.
    x1_norm, T1 = normalize_points(x1)
    x2_norm, T2 = normalize_points(x2)
    x1_h = np.c_[x1_norm, np.ones(len(x1_norm))]
    x2_h = np.c_[x2_norm, np.ones(len(x2_norm))]
    A = einx.multiply("N i, N j -> N (i j)", x2_h, x1_h)
    U, S, Vh = np.linalg.svd(A, full_matrices=True)
    tol = S.max() * max(A.shape) * np.finfo(S.dtype).eps
    if np.count_nonzero(S > tol) < 8:
        raise ValueError("found rank(A) < 8, repeated/collinear points")
    Fhat = Vh[-1, :].reshape(3, 3)
    U, S, Vh = np.linalg.svd(Fhat, full_matrices=True)
    S[-1] = 0
    Fhat = U @ np.diag(S) @ Vh
    F = T2.T @ Fhat @ T1
    norm = np.linalg.norm(F, ord='fro')
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("Invalid fundamental matrix")
    F = F / norm
    return F


def sampson_distance(F: ArrayLike, x1: ArrayLike, x2: ArrayLike) -> FloatArray:
    """G4: Return squared first-order epipolar error, shape (N,).

    d = (x2_h.T F x1_h)^2 / [(F x1_h)_x^2 + (F x1_h)_y^2
                             + (F.T x2_h)_x^2 + (F.T x2_h)_y^2].
    Pixel inputs yield squared-pixel distances. Return inf where the
    denominator is zero, including 0/0; do not accept those as inliers.
    The result must be invariant to any nonzero scaling/sign of F.

    Checkpoint: Sampson error is an approximation, not true reprojection error.
    """
    F = _array(F, "F", (3, 3))
    x1, x2 = _correspondences(x1, x2)
    # G4: compute both epipolar line arrays and safe squared distances.
    x1_h = np.c_[x1, np.ones(len(x1))]
    x2_h = np.c_[x2, np.ones(len(x2))]
    Fx1 = x1_h @ F.T
    Ftx2 = x2_h @ F
    e = einx.dot("N i, N i -> N", x2_h, Fx1)
    denom = (Fx1[:, :2] ** 2).sum(axis=1) + (Ftx2[:, :2] ** 2).sum(axis=1)
    d = np.divide(
        e ** 2, denom,
        out=np.full_like(e, np.inf, dtype=np.float64),
        where=denom > 0
    )
    return d


def ransac_fundamental(
    x1: ArrayLike, x2: ArrayLike,
    threshold_px: float = 1.5, max_trials: int = 2000, seed: int = 0,
    *, confidence: float = 0.99, min_trials: int = 20
) -> tuple[FloatArray, BoolArray]:
    """G5: Robust F from 8-point samples; return F and Boolean inlier mask.

    Use np.random.default_rng(seed), sample 8 unique rows, skip degenerate
    samples, and compare G4 to threshold_px**2. Favor greater inlier count;
    ties may favor lower inlier median error. Refit with all best inliers
    and recompute the mask from that returned F. If no model with >= 8
    inliers survives, raise ValueError. A hard max_trials bound is required;
    adaptive trial reduction is an optional later optimization.

    Checkpoint: an initial model's mask may change after final refitting.
    """
    x1, x2 = _correspondences(x1, x2, minimum=8)
    if not np.isscalar(threshold_px) or not np.isfinite(threshold_px) or threshold_px <= 0:
        raise ValueError("threshold_px must be a finite positive number")
    if isinstance(max_trials, (bool, np.bool_)) or not isinstance(max_trials, (int, np.integer)) or max_trials < 1:
        raise ValueError("max_trials must be a positive integer")
    if isinstance(seed, (bool, np.bool_)) or not isinstance(seed, (int, np.integer)) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if isinstance(confidence, (bool, np.bool_)) or not isinstance(confidence, (int, float, np.integer, np.floating)) or not np.isfinite(confidence) or not 0 < confidence < 1:
        raise ValueError("confidence must be finite and in (0, 1)")
    if isinstance(min_trials, (bool, np.bool_)) or not isinstance(min_trials, (int, np.integer)) or min_trials < 1:
        raise ValueError("min_trials must be a positive integer")
    # G5: sample -> G3 -> G4 -> consensus -> refit -> fresh mask.
    rng = np.random.default_rng(seed)
    N = len(x1)
    best_mask = None
    best_count = 0
    best_median = np.inf

    trial = 0
    trial_limit = max_trials
    while trial < trial_limit:
        trial += 1
        indices = rng.choice(N, size=8, replace=False)

        try:
            F_candidate = eight_point(x1[indices], x2[indices])
        except ValueError:
            continue

        errors = sampson_distance(F_candidate, x1, x2)

        mask = errors < threshold_px ** 2
        count = np.count_nonzero(mask)

        if count < 8:
             continue

        median = np.median(errors[mask])
        if count > best_count or (
            count == best_count and median < best_median
        ):
            best_count = count
            best_median = median
            best_mask = mask.copy()

            # Estimate required total trial count
            w = best_count / N
            p_good = w ** 8
            if p_good >= 1.0:
                required = 1
            else:
                required = math.ceil(math.log1p(-confidence) / math.log1p(-p_good))

            trial_limit = min(trial_limit, max(min_trials, required))

    if best_mask is None:
        raise ValueError("RANSAC failed to find a valid model")

    F = eight_point(x1[best_mask], x2[best_mask])
    final_errors = sampson_distance(F, x1, x2)
    final_mask = final_errors < threshold_px ** 2
    if np.count_nonzero(final_mask) < 8:
        raise ValueError("final valid counts less than 8")
    return F, final_mask


def essential_from_fundamental(F: ArrayLike, K1: ArrayLike, K2: ArrayLike) -> FloatArray:
    """G6: Convert F to E = K2.T @ F @ K1 and enforce (s, s, 0).

    Project E onto the essential-matrix singular-value constraint using
    s = (s1 + s2) / 2; return a Frobenius-normalized matrix. Reject a
    zero or numerically zero result. E is defined up to scale and sign.

    Checkpoint: explain why F operates on pixels but E on calibrated rays.
    """
    F = _array(F, "F", (3, 3))
    K1 = _intrinsics(K1, "K1")
    K2 = _intrinsics(K2, "K2")
    # G6: calibrate, SVD projection, normalize E.
    E = K2.T @ F @ K1
    U, S, Vh = np.linalg.svd(E)

    # Enforce essential matrix singular values
    s = (S[0] + S[1]) / 2
    E = U @ np.diag([s, s, 0.0]) @ Vh

    norm = np.linalg.norm(E, ord='fro')
    if not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("Zero or numerically zero essential matrix")
    return E / norm


def triangulate_points(
    P1: ArrayLike, P2: ArrayLike, x1: ArrayLike, x2: ArrayLike
) -> FloatArray:
    """G7: Linear DLT triangulation; return world points (N, 3).

    P1/P2 and observations must use the same coordinates: either both
    pixel (P = K [R|t]) or both normalized (P = [R|t]). Solve a 4 x 4
    homogeneous system per match. A point at infinity (|w| < 1e-12), a
    rank-deficient/ambiguous system, or a nonfinite result gives a full
    NaN row. Empty observations return shape (0, 3). Negative-depth
    points are returned for the caller's cheirality filter.

    Checkpoint: why is a small algebraic DLT residual not sufficient quality?
    """
    P1 = _array(P1, "P1", (3, 4))
    P2 = _array(P2, "P2", (3, 4))
    x1, x2 = _correspondences(x1, x2)
    # G7: two cross-product constraints per view, SVD, divide by w.
    N = len(x1)
    points = np.full((N, 3), np.nan, dtype=np.float64)
    for i in range(N):
        u1, v1 = x1[i]
        u2, v2 = x2[i]

        A = np.stack([
            u1 * P1[2] - P1[0],
            v1 * P1[2] - P1[1],
            u2 * P2[2] - P2[0],
            v2 * P2[2] - P2[1],
        ])

        _, S, Vh = np.linalg.svd(A)

        # Reject ambiguous or rank-deficient systems
        if S[-2] <= 1e-12 * S[0]:
            continue

        Xh = Vh[-1, :]
        # Reject points at infinity
        w = Xh[3]
        if np.abs(w) < 1e-12:
            continue

        X = Xh[:3] / w
        if not np.all(np.isfinite(X)):
            continue
        points[i] = X

    return points


def recover_relative_pose(
    E: ArrayLike, x1_px: ArrayLike, x2_px: ArrayLike, K1: ArrayLike, K2: ArrayLike
) -> tuple[FloatArray, FloatArray, BoolArray]:
    """G8: Recover camera 2 world-to-camera R, unit t, and usable-point mask.

    The first camera defines the world: R1=I, t1=0. Calibrate observations
    with K1^-1/K2^-1; decompose E into four (R, +/-t) candidates, force
    det(R)=+1, triangulate with G7 and choose maximal positive depth in
    both views. Reject nonfinite points, depth <= 1e-12, and triangulation
    angle <= 1e-3 rad. Rays for that angle must be in the same world frame
    (camera-2 ray transforms by R.T). Return that filtered Boolean mask;
    require at least 8 usable points, otherwise ValueError. Reject zero E.
    Monocular translation has direction but no recoverable metric scale.

    Checkpoint: camera 2 center is -R.T @ t, not t.
    """
    E = _array(E, "E", (3, 3))
    x1_px, x2_px = _correspondences(x1_px, x2_px, minimum=8)
    K1 = _intrinsics(K1, "K1")
    K2 = _intrinsics(K2, "K2")
    # G8: calibrated rays, four candidates, cheirality and angle selection.

    norm = np.linalg.norm(E, ord='fro')
    if not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("Zero essential matrix")

    E = E / norm
    N = len(x1_px)

    x1_h = np.c_[x1_px, np.ones(N)]
    x2_h = np.c_[x2_px, np.ones(N)]

    rays1 = np.linalg.solve(K1, x1_h.T).T
    rays2 = np.linalg.solve(K2, x2_h.T).T

    x1 = rays1[:, :2] / rays1[:, 2:3]
    x2 = rays2[:, :2] / rays2[:, 2:3]

    U, S, Vh = np.linalg.svd(E)
    W = np.array([
        [0, -1, 0],
        [1,  0, 0],
        [0,  0, 1],
    ])

    Ra = U @ W @ Vh
    Rb = U @ W.T @ Vh

    Ra = -Ra if np.linalg.det(Ra) < 0 else Ra
    Rb = -Rb if np.linalg.det(Rb) < 0 else Rb

    t = U[:, -1]
    t /= np.linalg.norm(t)

    candiates = [
        (Ra, t),
        (Ra, -t),
        (Rb, t),
        (Rb, -t),
    ]

    P1 = np.hstack([np.eye(3), np.zeros((3, 1))])
    # normalized for theta
    rays1 = rays1 / np.linalg.norm(rays1, axis=1, keepdims=True)
    rays2 = rays2 / np.linalg.norm(rays2, axis=1, keepdims=True)

    best_count = 0
    best_R = None
    best_t = None
    best_mask = None

    for R, t_candidate in candiates:

        P2 = np.column_stack([R, t_candidate])
        X = triangulate_points(P1, P2, x1, x2)
        depth1 = X[:, 2]

        X_cam2 = X @ R.T + t_candidate
        depth2 = X_cam2[:, 2]
        rays2_world = rays2 @ R # R^T = R^{-1}

        cos_angle = einx.dot("N [i], N [i] -> N", rays1, rays2_world)
        cos_angle = np.clip(cos_angle, -1.0, 1.0)
        angles = np.arccos(cos_angle)

        mask = (
            np.all(np.isfinite(X), axis=1)
            & (depth1 > 1e-12)
            & (depth2 > 1e-12)
            & (angles > 1e-3)
        )
        count = np.count_nonzero(mask)

        if count > best_count:
            best_count = count
            best_R = R.copy()
            best_t = t_candidate.copy()
            best_mask = mask.copy()

    if best_count < 8:
        raise ValueError("Insufficient usable triangulated points")

    return best_R, best_t, best_mask
