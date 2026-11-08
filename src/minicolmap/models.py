"""Shared records and invariants for the teaching SfM pipeline.

All poses map world coordinates to camera coordinates: ``Xc = R @ Xw + t``.
For arrays of row vectors, use ``Xc = Xw @ R.T + t``. Entity IDs are stable
positive integers, never positions in an array or dictionary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from numbers import Integral

import numpy as np


def _id(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
        raise ValueError(f"{label} must be a positive integer, got {value!r}")
    return int(value)


def _array(value: object, shape: tuple[int, ...], label: str) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if result.shape != shape or not np.isfinite(result).all():
        raise ValueError(f"{label} must be finite with shape {shape}")
    return result.copy()


def _check_intrinsics(K: np.ndarray) -> None:
    if K[0, 0] <= 0 or K[1, 1] <= 0:
        raise ValueError("Camera focal lengths must be positive")
    if not np.allclose(K[2], [0.0, 0.0, 1.0], atol=1e-12, rtol=0):
        raise ValueError("Camera K must have final row [0, 0, 1]")
    if not np.isclose(K[1, 0], 0.0, atol=1e-12, rtol=0):
        raise ValueError("Camera K[1, 0] must be zero")


def _check_rotation(R: np.ndarray) -> None:
    if not np.allclose(R @ R.T, np.eye(3), atol=1e-6, rtol=0):
        raise ValueError("Pose R must be orthogonal")
    if not np.isclose(np.linalg.det(R), 1.0, atol=1e-6, rtol=0):
        raise ValueError("Pose R must be a proper rotation (determinant +1)")


@dataclass
class Camera:
    """An undistorted pinhole camera; multiple images can share this camera."""

    id: int
    width: int
    height: int
    K: np.ndarray

    def __post_init__(self) -> None:
        self.id = _id(self.id, "Camera.id")
        self.width = _id(self.width, "Camera.width")
        self.height = _id(self.height, "Camera.height")
        self.K = _array(self.K, (3, 3), "Camera.K")
        _check_intrinsics(self.K)


@dataclass
class ImageRecord:
    """A dataset image and its pixel keypoints, indexed from zero."""

    id: int
    name: str
    camera_id: int
    keypoints: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))

    def __post_init__(self) -> None:
        self.id = _id(self.id, "ImageRecord.id")
        self.camera_id = _id(self.camera_id, "ImageRecord.camera_id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ImageRecord.name must be a nonempty string")
        if "\n" in self.name or "\r" in self.name or "\x00" in self.name:
            raise ValueError("ImageRecord.name must not contain line breaks or NUL")
        keypoints = np.asarray(self.keypoints, dtype=np.float64)
        if keypoints.ndim != 2 or keypoints.shape[1] != 2:
            raise ValueError("ImageRecord.keypoints must have shape (N, 2)")
        if not np.isfinite(keypoints).all():
            raise ValueError("ImageRecord.keypoints must be finite")
        self.keypoints = keypoints.copy()


@dataclass
class Pose:
    """World-to-camera extrinsics, with a length-three translation vector."""

    R: np.ndarray
    t: np.ndarray

    def __post_init__(self) -> None:
        self.R = _array(self.R, (3, 3), "Pose.R")
        self.t = _array(self.t, (3,), "Pose.t")
        _check_rotation(self.R)

    @property
    def center(self) -> np.ndarray:
        """Camera center in world coordinates: ``-R.T @ t``."""
        return -self.R.T @ self.t


@dataclass
class Point3D:
    """A world point and its track of ``(image_id, keypoint_index)`` pairs."""

    id: int
    xyz: np.ndarray
    color: np.ndarray
    observations: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        self.id = _id(self.id, "Point3D.id")
        self.xyz = _array(self.xyz, (3,), "Point3D.xyz")
        color = np.asarray(self.color)
        if color.shape != (3,) or not np.issubdtype(color.dtype, np.integer):
            raise ValueError("Point3D.color must contain three integer RGB values")
        if np.any(color < 0) or np.any(color > 255):
            raise ValueError("Point3D.color values must be in [0, 255]")
        self.color = color.astype(np.uint8, copy=True)
        observations = []
        for observation in self.observations:
            if len(observation) != 2:
                raise ValueError("Observations must be (image_id, keypoint_index) pairs")
            image_id, index = observation
            image_id = _id(image_id, "Observation.image_id")
            if isinstance(index, bool) or not isinstance(index, Integral) or index < 0:
                raise ValueError("Observation keypoint indices must be nonnegative integers")
            observations.append((image_id, int(index)))
        self.observations = tuple(observations)


@dataclass
class Reconstruction:
    """Registered images are exactly the IDs present in ``poses``.

    ``images`` may also contain unregistered dataset images. Tracks live in
    ``points``; image-to-point associations are derived, avoiding two mutable
    copies of the same topology.
    """

    cameras: dict[int, Camera] = field(default_factory=dict)
    images: dict[int, ImageRecord] = field(default_factory=dict)
    poses: dict[int, Pose] = field(default_factory=dict)
    points: dict[int, Point3D] = field(default_factory=dict)


def validate_reconstruction(model: Reconstruction) -> None:
    """Reject malformed records, missing references and ambiguous tracks.

    Revalidates mutable arrays as well as constructor invariants. Positive
    depth and reprojection error are geometry quality checks, not topology
    invariants, and are measured separately in :mod:`minicolmap.metrics`.
    """
    if not isinstance(model, Reconstruction):
        raise TypeError("Expected a Reconstruction")
    for label, records, kind in (
        ("camera", model.cameras, Camera),
        ("image", model.images, ImageRecord),
        ("point", model.points, Point3D),
    ):
        if not isinstance(records, dict):
            raise ValueError(f"{label} collection must be a dictionary")
        for key, record in records.items():
            _id(key, f"{label} dictionary key")
            if not isinstance(record, kind) or key != record.id:
                raise ValueError(f"{label} dictionary key must match its record ID")

    for camera in model.cameras.values():
        Camera(camera.id, camera.width, camera.height, camera.K)
    names = set()
    for image in model.images.values():
        ImageRecord(image.id, image.name, image.camera_id, image.keypoints)
        if image.camera_id not in model.cameras:
            raise ValueError(f"Image {image.id} references missing camera {image.camera_id}")
        if image.name in names:
            raise ValueError(f"Duplicate image name: {image.name!r}")
        names.add(image.name)
    if not isinstance(model.poses, dict):
        raise ValueError("Pose collection must be a dictionary")
    for image_id, pose in model.poses.items():
        _id(image_id, "Pose image ID")
        if image_id not in model.images or not isinstance(pose, Pose):
            raise ValueError(f"Pose {image_id} must reference an existing image")
        Pose(pose.R, pose.t)

    occupied: dict[tuple[int, int], int] = {}
    for point in model.points.values():
        Point3D(point.id, point.xyz, point.color, point.observations)
        image_ids = set()
        for image_id, keypoint_index in point.observations:
            if image_id not in model.images or image_id not in model.poses:
                raise ValueError(f"Point {point.id} observes unregistered image {image_id}")
            if keypoint_index >= len(model.images[image_id].keypoints):
                raise ValueError(f"Point {point.id} keypoint index is out of bounds")
            if image_id in image_ids:
                raise ValueError(f"Point {point.id} has multiple observations in image {image_id}")
            image_ids.add(image_id)
            observation = (image_id, keypoint_index)
            if observation in occupied:
                raise ValueError(
                    f"Observation {observation} belongs to points "
                    f"{occupied[observation]} and {point.id}"
                )
            occupied[observation] = point.id
