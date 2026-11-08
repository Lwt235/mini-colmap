"""ASCII PLY and COLMAP text export of estimated reconstructions."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np

from .metrics import _observation_measurements
from .models import Reconstruction, validate_reconstruction


def _rotation_to_quaternion(R: np.ndarray) -> np.ndarray:
    """Return the Hamilton quaternion [qw, qx, qy, qz] of world-to-camera R."""
    trace = np.trace(R)
    if trace > 0:
        s = 2.0 * np.sqrt(trace + 1.0)
        q = np.array([0.25 * s, (R[2, 1] - R[1, 2]) / s,
                      (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s])
    else:
        axis = int(np.argmax(np.diag(R)))
        if axis == 0:
            s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
            q = np.array([(R[2, 1] - R[1, 2]) / s, 0.25 * s,
                          (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s])
        elif axis == 1:
            s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
            q = np.array([(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s,
                          0.25 * s, (R[1, 2] + R[2, 1]) / s])
        else:
            s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
            q = np.array([(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s,
                          (R[1, 2] + R[2, 1]) / s, 0.25 * s])
    q /= np.linalg.norm(q)
    # q and -q represent the same rotation; fix a deterministic sign.
    nonzero = np.flatnonzero(np.abs(q) > 1e-15)
    if len(nonzero) and q[nonzero[0]] < 0:
        q = -q
    return q


def _numbers(values) -> str:
    return " ".join(format(float(value), ".17g") for value in values)


def write_ply(model: Reconstruction, path: str | Path) -> None:
    """Write an ASCII colored point cloud; PLY does not store image tracks."""
    validate_reconstruction(model)
    lines = [
        "ply", "format ascii 1.0", f"element vertex {len(model.points)}",
        "property double x", "property double y", "property double z",
        "property uchar red", "property uchar green", "property uchar blue", "end_header",
    ]
    for _, point in sorted(model.points.items()):
        lines.append(_numbers(point.xyz) + " " + " ".join(str(int(c)) for c in point.color))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_colmap_text(model: Reconstruction, directory: str | Path) -> None:
    """Write PINHOLE cameras.txt, images.txt and points3D.txt.

    Only registered images are exported. POINT2D_IDX is a zero-based keypoint
    index, while image/point/camera IDs retain their original values. ERROR is
    the actual mean Euclidean pixel error of that point's track. Invalid or
    nonpositive-depth projections are rejected before any files are written.
    Exporting text alone does not guarantee a 3DGS trainer's input contract.
    """
    validate_reconstruction(model)
    for camera in model.cameras.values():
        if not np.isclose(camera.K[0, 1], 0.0, atol=1e-12, rtol=0):
            raise ValueError("COLMAP PINHOLE does not support nonzero camera skew")
    errors: dict[int, list[float]] = defaultdict(list)
    for point_id, error, depth in _observation_measurements(model):
        if depth <= 0:
            raise ValueError(f"Point {point_id} has a nonpositive-depth projection")
        errors[point_id].append(error)
    for point_id in model.points:
        if not errors[point_id]:
            raise ValueError(f"Point {point_id} has no observations to compute its error")

    cameras = ["# CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]"]
    for camera_id, camera in sorted(model.cameras.items()):
        parameters = [camera.K[0, 0], camera.K[1, 1], camera.K[0, 2], camera.K[1, 2]]
        cameras.append(f"{camera_id} PINHOLE {camera.width} {camera.height} {_numbers(parameters)}")

    association = {
        (image_id, index): point_id
        for point_id, point in model.points.items()
        for image_id, index in point.observations
    }
    images = [
        "# IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME",
        "# POINTS2D[] as (X, Y, POINT3D_ID); one second line per image, including empty lines",
    ]
    for image_id, pose in sorted(model.poses.items()):
        image = model.images[image_id]
        q = _rotation_to_quaternion(pose.R)
        images.append(f"{image_id} {_numbers(q)} {_numbers(pose.t)} {image.camera_id} {image.name}")
        fields = [
            f"{_numbers(keypoint)} {association.get((image_id, index), -1)}"
            for index, keypoint in enumerate(image.keypoints)
        ]
        images.append(" ".join(fields))

    points = ["# POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)"]
    for point_id, point in sorted(model.points.items()):
        rgb = " ".join(str(int(channel)) for channel in point.color)
        error = format(float(np.mean(errors[point_id])), ".17g")
        track = " ".join(f"{image_id} {index}" for image_id, index in sorted(point.observations))
        points.append(f"{point_id} {_numbers(point.xyz)} {rgb} {error} {track}")

    # Construct and validate every payload before making the output directory.
    payloads = {"cameras.txt": cameras, "images.txt": images, "points3D.txt": points}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for filename, lines in payloads.items():
        (directory / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")
