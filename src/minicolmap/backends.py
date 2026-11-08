"""Library-backed pose estimation; no ground-truth input is accepted here."""
import numpy as np

from .frontend import _cv2


def solve_pnp(points, pixels, K, threshold_px: float = 3.0, seed: int = 0):
    """PnP-RANSAC + refinement. Return world-to-camera R,t and a recomputed bool mask."""
    points, pixels, K = [np.asarray(a, dtype=np.float64) for a in (points, pixels, K)]
    if points.ndim != 2 or points.shape[1] != 3 or pixels.shape != (len(points), 2) or len(points) < 6:
        raise ValueError("PnP needs at least six 3D-2D pairs")
    if K.shape != (3, 3) or not all(np.isfinite(a).all() for a in (points, pixels, K)):
        raise ValueError("PnP inputs must be finite with K(3,3)")
    if threshold_px <= 0 or np.linalg.matrix_rank(points - points.mean(0)) < 2:
        raise ValueError("Invalid PnP threshold or collinear point cloud")
    cv2 = _cv2()
    cv2.setRNGSeed(int(seed) % (2**31 - 1))
    ok, rvec, tvec, inliers = cv2.solvePnPRansac(
        np.ascontiguousarray(points), np.ascontiguousarray(pixels), K, None,
        iterationsCount=2000, reprojectionError=threshold_px, confidence=0.999,
        flags=cv2.SOLVEPNP_EPNP)
    if not ok or inliers is None or len(inliers) < 6:
        raise ValueError("PnP-RANSAC could not register this image")
    selected = inliers.reshape(-1)
    ok, rvec, tvec = cv2.solvePnP(points[selected], pixels[selected], K, None, rvec, tvec,
                                 useExtrinsicGuess=True, flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        raise ValueError("PnP refinement failed")
    R, _ = cv2.Rodrigues(rvec)
    t = tvec.reshape(3)
    projected, _ = cv2.projectPoints(points, rvec, tvec, K, None)
    errors = np.linalg.norm(projected.reshape(-1, 2) - pixels, axis=1)
    depth = (points @ R.T + t)[:, 2]
    mask = (depth > 1e-8) & np.isfinite(errors) & (errors <= threshold_px)
    if np.count_nonzero(mask) < 6:
        raise ValueError("Refined PnP has too few geometrically valid correspondences")
    return R, t, mask

