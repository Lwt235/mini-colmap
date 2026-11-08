"""Nonlinear refinement of the initial pair and the incremental map."""
from typing import TypedDict

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .ba import bundle_residuals
from .metrics import reconstruction_summary
from .models import Pose, validate_reconstruction


class OptimizationReport(TypedDict):
    accepted: bool
    solver_success: bool
    nfev: int
    before_mean_px: float
    candidate_mean_px: float | None
    message: str
    rejection_reason: str | None


def refine_two_view(
    R: ArrayLike, t: ArrayLike, points: ArrayLike,
    K1: ArrayLike, K2: ArrayLike, pixels1: ArrayLike, pixels2: ArrayLike,
    max_nfev: int = 100,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], OptimizationReport]:
    """Refine a G8/DLT seed using only its verified pixel correspondences.

    Fix camera 1 to (I,0) and ||t2||=1. Optimize r2 (3), the direction of
    t2 in a local tangent chart (2), and all points (3N), with separate K1/K2.
    Thus the two-view pose can improve without the monocular scale gauge.
    Use squared pixel residuals on the upstream RANSAC inliers; M1 still
    enforces depth, angle, and per-observation error after refinement.
    Invalid/worsening candidates return copies of the original seed.
    """
    from scipy.optimize import least_squares
    from scipy.sparse import lil_matrix
    from scipy.spatial.transform import Rotation
    from .geometry import _array, _correspondences, _intrinsics

    R = _array(R, 'R', (3, 3)).copy()
    t = _array(t, 't', (3,)).copy()
    points = _array(points, 'points', (None, 3)).copy()
    K1, K2 = _intrinsics(K1, 'K1'), _intrinsics(K2, 'K2')
    pixels1, pixels2 = _correspondences(pixels1, pixels2, minimum=8)
    Pose(R, t)  # Check the rotation before Rotation.from_matrix can orthogonalize it.
    if len(points) != len(pixels1) or not np.isclose(np.linalg.norm(t), 1., atol=1e-8):
        raise ValueError('Two-view refinement needs aligned points and unit translation')
    if isinstance(max_nfev, bool) or not isinstance(max_nfev, int) or max_nfev < 1:
        raise ValueError('max_nfev must be a positive integer')

    # This chart has two direction DOFs and always preserves the unit baseline.
    tangent = np.linalg.svd(t[None, :], full_matrices=True)[2][1:].T
    x0 = np.r_[Rotation.from_matrix(R).as_rotvec(), 0., 0., points.ravel()]

    def unpack(x):
        rotation = Rotation.from_rotvec(x[:3]).as_matrix()
        direction = t + tangent @ x[3:5]
        return rotation, direction / np.linalg.norm(direction), x[5:].reshape(-1, 3)

    def residual(x):
        rotation, direction, xyz = unpack(x)
        camera2 = xyz @ rotation.T + direction
        if np.any(np.abs(xyz[:, 2]) < 1e-12) or np.any(np.abs(camera2[:, 2]) < 1e-12):
            raise ValueError('Two-view trial has near-zero depth')
        h1, h2 = xyz @ K1.T, camera2 @ K2.T
        values = np.column_stack((h1[:, :2] / h1[:, 2:] - pixels1,
                                  h2[:, :2] / h2[:, 2:] - pixels2)).ravel()
        if not np.isfinite(values).all():
            raise ValueError('Two-view trial has nonfinite projections')
        return values

    def positive(rotation, direction, xyz):
        return np.all(xyz[:, 2] > 1e-12) and np.all((xyz @ rotation.T + direction)[:, 2] > 1e-12)

    if not positive(R, t, points):
        raise ValueError('Two-view input needs positive depth in both cameras')
    before_values = residual(x0)
    before_mean = float(np.linalg.norm(before_values.reshape(-1, 2), axis=1).mean())
    report: OptimizationReport = {
        'accepted': False, 'solver_success': False, 'nfev': 0,
        'before_mean_px': before_mean, 'candidate_mean_px': None,
        'message': '', 'rejection_reason': None,
    }
    sparsity = lil_matrix((4 * len(points), len(x0)), dtype=np.int8)
    for index in range(len(points)):
        sparsity[4 * index:4 * index + 4, 5 + 3 * index:8 + 3 * index] = 1
        sparsity[4 * index + 2:4 * index + 4, :5] = 1
    try:
        result = least_squares(residual, x0, method='trf', loss='linear',
                               jac_sparsity=sparsity.tocsr(), x_scale='jac',
                               tr_options={'atol': 1e-12, 'btol': 1e-12},
                               max_nfev=max_nfev)
        report.update(solver_success=bool(result.success), nfev=int(result.nfev),
                      message=str(result.message))
        solution = np.asarray(result.x, dtype=np.float64)
        if solution.shape != x0.shape or not np.isfinite(solution).all():
            raise ValueError('Two-view optimizer returned invalid parameters')
        rotation, direction, xyz = unpack(solution)
        values = residual(solution)
        candidate_mean = float(np.linalg.norm(values.reshape(-1, 2), axis=1).mean())
        report['candidate_mean_px'] = candidate_mean
        if not positive(rotation, direction, xyz):
            raise ValueError('Two-view candidate has nonpositive depth')
        if candidate_mean > before_mean + 1e-9 or values @ values > before_values @ before_values + 1e-9:
            raise ValueError('Two-view candidate worsened reprojection error')
    except (ValueError, FloatingPointError, OverflowError) as exc:
        report['rejection_reason'] = str(exc)
        return R, t, points, report
    report['accepted'] = True
    return rotation, direction, xyz.copy(), report


def refine_model(model, anchor_image_ids, settings):
    """Teaching BA: freeze two initial poses, optimize other poses and all points, fixed K.

    Freezing both poses is an extra constraint, not merely a full BA gauge fix.
    Requires all cameras to share K. A sparse numerical Jacobian connects each
    observation only to its variable camera and its point.
    Reject a candidate if any stored observation has non-positive depth or nonfinite projection.
    Accept only finite, non-worsening mean pixel error; retain the original model otherwise.
    """
    from scipy.optimize import least_squares
    from scipy.sparse import lil_matrix
    from scipy.spatial.transform import Rotation
    import copy

    validate_reconstruction(model)
    ids, point_ids = sorted(model.poses), sorted(model.points)
    anchors = set(anchor_image_ids)
    if len(anchors) != 2 or not anchors <= set(ids) or not point_ids:
        raise ValueError("Teaching BA needs two anchor images and nonempty points")
    if any(len(model.points[i].observations) < 2 for i in point_ids):
        raise ValueError("BA input points need at least two registered observations")
    before = reconstruction_summary(model)
    if before['positive_depth_ratio'] != 1.0:
        raise ValueError("BA input must contain only positive-depth observations")
    matrices = [model.cameras[model.images[i].camera_id].K for i in ids]
    K = matrices[0]
    if not all(np.allclose(k, K) for k in matrices):
        raise ValueError("Teaching BA currently requires shared intrinsics")
    camera_lookup = {image_id: idx for idx, image_id in enumerate(ids)}
    rotvecs = Rotation.from_matrix(np.stack([model.poses[i].R for i in ids])).as_rotvec()
    translations = np.stack([model.poses[i].t for i in ids])
    points = np.stack([model.points[i].xyz for i in point_ids])
    variable = np.asarray([j for j, i in enumerate(ids) if i not in anchors], dtype=int)
    camera_indices, point_indices, pixels = [], [], []
    for pidx, point_id in enumerate(point_ids):
        for image_id, keypoint_idx in model.points[point_id].observations:
            camera_indices.append(camera_lookup[image_id])
            point_indices.append(pidx)
            pixels.append(model.images[image_id].keypoints[keypoint_idx])
    camera_indices = np.asarray(camera_indices, dtype=np.int64)
    point_indices = np.asarray(point_indices, dtype=np.int64)
    pixels = np.asarray(pixels)
    x0 = np.concatenate([np.column_stack([rotvecs[variable], translations[variable]]).ravel(), points.ravel()])
    pose_size = 6 * len(variable)
    # Frozen cameras have no columns. Each du/dv pair depends on one point
    # (three columns), and possibly one variable camera (six columns).
    sparsity = lil_matrix((2 * len(pixels), len(x0)), dtype=np.int8)
    camera_offsets = {int(camera_idx): 6 * idx for idx, camera_idx in enumerate(variable)}
    for obs_idx, (camera_idx, point_idx) in enumerate(zip(camera_indices, point_indices)):
        rows = slice(2 * obs_idx, 2 * obs_idx + 2)
        camera_offset = camera_offsets.get(int(camera_idx))
        if camera_offset is not None:
            sparsity[rows, camera_offset:camera_offset + 6] = 1
        point_offset = pose_size + 3 * int(point_idx)
        sparsity[rows, point_offset:point_offset + 3] = 1
    sparsity = sparsity.tocsr()

    def unpack(x):
        r, t = rotvecs.copy(), translations.copy()
        poses = x[:pose_size].reshape(-1, 6)
        r[variable], t[variable] = poses[:, :3], poses[:, 3:]
        return r, t, x[pose_size:].reshape(-1, 3)

    def residual(x):
        r, t, p = unpack(x)
        values = np.asarray(bundle_residuals(r, t, p, K, camera_indices, point_indices, pixels),
                            dtype=np.float64)
        if values.shape != (2 * len(pixels),) or not np.isfinite(values).all():
            raise ValueError("B1 must return a finite vector with exactly two residuals per observation")
        return values

    def report(result, accepted=False, candidate_mean=None, rejection_reason=None):
        return {'accepted': bool(accepted),
                'solver_success': bool(result.success) if result is not None else False,
                'nfev': int(result.nfev) if result is not None else 0,
                'before_mean_px': before['mean_reprojection_error_px'],
                'candidate_mean_px': candidate_mean,
                'message': str(result.message) if result is not None else str(rejection_reason),
                'rejection_reason': rejection_reason}

    try:
        result = least_squares(residual, x0, method='trf', jac_sparsity=sparsity, x_scale='jac',
                               tr_options={'atol': 1e-10, 'btol': 1e-10},
                               loss=settings['loss'], f_scale=settings['f_scale'],
                               max_nfev=settings['max_nfev'])
    except (ValueError, FloatingPointError, OverflowError) as exc:
        # B1 may reject a zero-depth trial point. No caller model was mutated.
        # Let NotImplementedError propagate for the caller's explicit todo protocol.
        return report(None, rejection_reason=f'Optimization rejected a trial: {exc}')
    try:
        solution = np.asarray(result.x, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        return report(result, rejection_reason=f'Optimizer returned invalid parameters: {exc}')
    if solution.shape != x0.shape or not np.isfinite(solution).all():
        return report(result, rejection_reason='Optimizer returned invalid or nonfinite parameters')
    try:
        candidate = copy.deepcopy(model)
        r, t, p = unpack(solution)
        for j in variable:
            image_id = ids[j]
            candidate.poses[image_id] = Pose(Rotation.from_rotvec(r[j]).as_matrix(), t[j])
        for j, point_id in enumerate(point_ids):
            candidate.points[point_id].xyz = p[j].copy()
        validate_reconstruction(candidate)
        after = reconstruction_summary(candidate)
    except (ValueError, FloatingPointError, OverflowError) as exc:
        return report(result, rejection_reason=f'Invalid optimized candidate: {exc}')
    if after['positive_depth_ratio'] != 1.0:
        return report(result, candidate_mean=after['mean_reprojection_error_px'],
                      rejection_reason='Optimized candidate has nonpositive-depth observations')
    accepted = after['mean_reprojection_error_px'] <= before['mean_reprojection_error_px'] + 1e-9
    if accepted:
        model.poses, model.points = candidate.poses, candidate.points
    return report(result, accepted, after['mean_reprojection_error_px'],
                  None if accepted else 'Optimized candidate worsened mean pixel error')
