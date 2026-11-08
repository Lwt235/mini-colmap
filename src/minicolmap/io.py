"""JSON interchange and local dataset manifest loading."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

import numpy as np

from .models import Camera, ImageRecord, Point3D, Pose, Reconstruction, validate_reconstruction


def _json_path(path: str | Path) -> Path:
    result = Path(path)
    if result.suffix.lower() != ".json":
        raise ValueError("Expected a .json path")
    return result


def _unique_records(items: list, make_record, label: str) -> dict:
    if not isinstance(items, list):
        raise ValueError(f"{label} must be a list")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"Each {label} entry must be an object")
        record = make_record(item)
        if record.id in result:
            raise ValueError(f"Duplicate {label} ID: {record.id}")
        result[record.id] = record
    return result


def _keypoint_array(values: object) -> np.ndarray:
    result = np.asarray(values, dtype=np.float64)
    # JSON cannot retain the second dimension of an empty list. Restore only
    # that special case; malformed nonempty arrays must keep their bad shape
    # so ImageRecord can reject them instead of silently reshaping the data.
    if result.shape == (0,):
        return np.empty((0, 2))
    return result


def reconstruction_to_dict(model: Reconstruction) -> dict:
    """Return an independent, JSON-ready version-one model in stable ID order.

    Array and observation values are copied to ordinary Python lists, so consumers
    of a progress snapshot cannot mutate the reconstruction being computed.
    """
    validate_reconstruction(model)
    return {
        "schema_version": 1,
        "pose_convention": "world_to_camera",
        "cameras": [
            {"id": c.id, "width": c.width, "height": c.height, "K": c.K.tolist()}
            for _, c in sorted(model.cameras.items())
        ],
        "images": [
            {"id": i.id, "name": i.name, "camera_id": i.camera_id,
             "keypoints": i.keypoints.tolist()}
            for _, i in sorted(model.images.items())
        ],
        "poses": [
            {"image_id": image_id, "R": pose.R.tolist(), "t": pose.t.tolist()}
            for image_id, pose in sorted(model.poses.items())
        ],
        "points": [
            {"id": p.id, "xyz": p.xyz.tolist(), "color": p.color.tolist(),
             "observations": [list(o) for o in p.observations]}
            for _, p in sorted(model.points.items())
        ],
    }


def save_reconstruction(model: Reconstruction, path: str | Path) -> None:
    """Save records in stable ID order; do not persist image pixels."""
    data = reconstruction_to_dict(model)
    path = _json_path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_reconstruction(path: str | Path) -> Reconstruction:
    """Load version-one JSON and validate topology and camera conventions."""
    data = json.loads(_json_path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported reconstruction schema_version")
    if data.get("pose_convention") != "world_to_camera":
        raise ValueError("Only world_to_camera poses are supported")
    cameras = _unique_records(data["cameras"], lambda c: Camera(**c), "camera")
    images = _unique_records(
        data["images"],
        lambda i: ImageRecord(
            i["id"], i["name"], i["camera_id"],
            _keypoint_array(i["keypoints"]),
        ),
        "image",
    )
    points = _unique_records(data["points"], lambda p: Point3D(**p), "point")
    poses = {}
    if not isinstance(data["poses"], list):
        raise ValueError("poses must be a list")
    for item in data["poses"]:
        image_id = item["image_id"]
        if image_id in poses:
            raise ValueError(f"Duplicate pose image ID: {image_id}")
        poses[image_id] = Pose(item["R"], item["t"])
    model = Reconstruction(cameras, images, poses, points)
    validate_reconstruction(model)
    return model


def _relative_image_path(root: Path, raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ValueError("Image path must be a nonempty relative string")
    windows = PureWindowsPath(raw_path)
    portable = Path(raw_path.replace("\\", "/"))
    if windows.is_absolute() or windows.drive or windows.root or portable.is_absolute():
        raise ValueError("Absolute image paths are not allowed")
    result = (root / portable).resolve()
    try:
        result.relative_to(root)
    except ValueError as exc:
        raise ValueError("Image path must stay within the manifest directory") from exc
    if not result.is_file():
        raise ValueError(f"Image file does not exist: {raw_path}")
    return result


def load_dataset(path: str | Path) -> dict:
    """Read calibrated cameras and image paths from a local manifest.

    Manifest keys are ``cameras`` and ``images`` lists. Each image supplies
    ``id``, ``name``, ``camera_id`` and ``path`` relative to the manifest.
    Absolute paths and paths escaping that directory (including symlinks)
    are rejected. Decoding image pixels belongs to the feature frontend.
    """
    path = _json_path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Dataset manifest must be an object")
    cameras = _unique_records(data["cameras"], lambda c: Camera(**c), "camera")
    images = _unique_records(
        data["images"],
        lambda i: ImageRecord(i["id"], i["name"], i["camera_id"]),
        "image",
    )
    validate_reconstruction(Reconstruction(cameras=cameras, images=images))
    image_paths = {
        entry["id"]: _relative_image_path(path.parent, entry["path"])
        for entry in data["images"]
    }
    return {"cameras": cameras, "images": images, "image_paths": image_paths}
