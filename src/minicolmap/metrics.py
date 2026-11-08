"""Measured reconstruction quality without requiring ground truth."""

from __future__ import annotations

import numpy as np

from .models import Reconstruction, validate_reconstruction


def _observation_measurements(model: Reconstruction) -> list[tuple[int, float, float]]:
    """Return (point_id, pixel error, camera depth) for every tracked observation."""
    measurements = []
    for point_id, point in sorted(model.points.items()):
        for image_id, keypoint_index in point.observations:
            image = model.images[image_id]
            pose = model.poses[image_id]
            camera = model.cameras[image.camera_id]
            camera_point = pose.R @ point.xyz + pose.t
            homogeneous = camera.K @ camera_point
            if abs(homogeneous[2]) <= 1e-12:
                raise ValueError(f"Point {point_id} has undefined projection in image {image_id}")
            projected = homogeneous[:2] / homogeneous[2]
            error = np.linalg.norm(projected - image.keypoints[keypoint_index])
            if not np.isfinite(error):
                raise ValueError(f"Point {point_id} has nonfinite reprojection error")
            measurements.append((point_id, float(error), float(camera_point[2])))
    return measurements


def reconstruction_summary(model: Reconstruction) -> dict[str, int | float | None]:
    """Report Euclidean pixel errors over tracked observations.

    Negative-depth projections contribute their mathematical pixel error and
    lower ``positive_depth_ratio``. Zero-depth projections raise an error.
    Empty observation sets produce ``None`` for all quality metrics.
    This is an internal consistency measure, not ground-truth pose accuracy.
    """
    validate_reconstruction(model)
    measurements = _observation_measurements(model)
    errors = np.array([item[1] for item in measurements])
    depths = np.array([item[2] for item in measurements])
    return {
        "image_count": len(model.images),
        "registered_images": len(model.poses),
        "point_count": len(model.points),
        "observation_count": len(measurements),
        "mean_reprojection_error_px": float(np.mean(errors)) if len(errors) else None,
        "median_reprojection_error_px": float(np.median(errors)) if len(errors) else None,
        "p95_reprojection_error_px": float(np.percentile(errors, 95)) if len(errors) else None,
        "positive_depth_ratio": float(np.mean(depths > 0)) if len(depths) else None,
    }
