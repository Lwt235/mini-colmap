"""Runtime contracts for model I/O, export, quality metrics and synthetic fixtures."""

from __future__ import annotations

import json

import numpy as np
import pytest

from minicolmap.export import _rotation_to_quaternion, write_colmap_text, write_ply
from minicolmap.io import load_dataset, load_reconstruction, save_reconstruction
from minicolmap.metrics import reconstruction_summary
from minicolmap.models import (
    Camera, ImageRecord, Point3D, Pose, Reconstruction, validate_reconstruction,
)
from minicolmap.synthetic import generate_scene, load_scene, save_scene


def rotation_y(angle):
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def projection(K, pose, point):
    h = K @ (pose.R @ point + pose.t)
    return h[:2] / h[2]


@pytest.fixture
def model():
    """Nonconsecutive IDs, shared intrinsics, unsorted keypoints and measured errors."""
    K = np.array([[800.0, 0, 320], [0, 790, 240], [0, 0, 1]])
    pose1 = Pose(np.eye(3), np.zeros(3))
    R2 = rotation_y(0.12)
    pose2 = Pose(R2, -R2 @ np.array([0.4, 0, 0.1]))
    A, B = np.array([0.1, -0.2, 5.0]), np.array([-0.25, 0.3, 6.0])
    keypoints1 = np.array([
        projection(K, pose1, A) + [3, 4], [12.0, 16.0], projection(K, pose1, B),
    ])
    keypoints2 = np.array([
        projection(K, pose2, B) + [0, 2], projection(K, pose2, A),
    ])
    return Reconstruction(
        cameras={7: Camera(7, 640, 480, K)},
        images={
            11: ImageRecord(11, "first.jpg", 7, keypoints1),
            29: ImageRecord(29, "second.jpg", 7, keypoints2),
            47: ImageRecord(47, "empty.jpg", 7),
            103: ImageRecord(103, "unregistered.jpg", 7),
        },
        poses={11: pose1, 29: pose2, 47: Pose(np.eye(3), [0.2, 0, 0])},
        points={
            101: Point3D(101, A, [255, 20, 40], ((11, 0), (29, 1))),
            905: Point3D(905, B, [5, 240, 60], ((11, 2), (29, 0))),
        },
    )


def test_pose_center_and_row_vector_convention():
    R = rotation_y(0.4)
    center = np.array([0.8, -0.3, 1.2])
    pose = Pose(R, -R @ center)
    np.testing.assert_allclose(pose.center, center)
    np.testing.assert_allclose(pose.R @ pose.center + pose.t, 0, atol=1e-12)
    points = np.array([[0.1, 0.2, 4], [0.4, -0.1, 8]])
    np.testing.assert_allclose(points @ R.T + pose.t, np.stack([R @ p + pose.t for p in points]))


def test_nonconsecutive_ids_valid(model):
    validate_reconstruction(model)
    assert len(model.images) == 4 and len(model.poses) == 3


@pytest.mark.parametrize("fault,match", [
    ("key_mismatch", "key must match"),
    ("missing_camera", "missing camera"),
    ("duplicate_name", "Duplicate image name"),
    ("unregistered_observation", "unregistered image"),
    ("out_of_bounds", "out of bounds"),
    ("duplicate_image_in_track", "multiple observations"),
    ("duplicate_observation", "belongs to points"),
    ("invalid_rotation", "orthogonal"),
    ("invalid_point", "must be finite"),
])
def test_topology_failures(model, fault, match):
    if fault == "key_mismatch":
        model.cameras = {8: model.cameras[7]}
    elif fault == "missing_camera":
        model.images[11].camera_id = 77
    elif fault == "duplicate_name":
        model.images[29].name = model.images[11].name
    elif fault == "unregistered_observation":
        model.points[101].observations = ((103, 0),)
    elif fault == "out_of_bounds":
        model.points[101].observations = ((11, 100),)
    elif fault == "duplicate_image_in_track":
        model.points[101].observations = ((11, 0), (11, 1))
    elif fault == "duplicate_observation":
        model.points[905].observations = ((11, 0),)
    elif fault == "invalid_rotation":
        model.poses[11].R[0, 0] = 2
    elif fault == "invalid_point":
        model.points[101].xyz[0] = np.nan
    with pytest.raises(ValueError, match=match):
        validate_reconstruction(model)


@pytest.mark.parametrize("bad_R", [np.diag([1, 1, -1]), np.zeros((3, 3))])
def test_invalid_rotations_rejected(bad_R):
    with pytest.raises(ValueError):
        Pose(bad_R, np.zeros(3))


@pytest.mark.parametrize("color", [[256, 0, 0], [-1, 0, 0], [1.5, 0, 0]])
def test_colors_are_not_silently_wrapped_or_truncated(color):
    with pytest.raises(ValueError, match="color"):
        Point3D(1, np.ones(3), color)


def test_json_round_trip_and_stable_bytes(model, tmp_path):
    path = tmp_path / "model.json"
    save_reconstruction(model, path)
    loaded = load_reconstruction(path)
    assert set(loaded.images) == {11, 29, 47, 103}
    assert set(loaded.points) == {101, 905}
    assert loaded.images[47].keypoints.shape == (0, 2)
    assert loaded.points[101].color.dtype == np.uint8
    assert loaded.points[101].observations == ((11, 0), (29, 1))
    np.testing.assert_array_equal(loaded.poses[29].R, model.poses[29].R)
    np.testing.assert_array_equal(loaded.poses[29].t, model.poses[29].t)
    second = tmp_path / "copy.json"
    save_reconstruction(loaded, second)
    assert second.read_bytes() == path.read_bytes()


def test_json_rejects_duplicate_ids(model, tmp_path):
    path = tmp_path / "model.json"
    save_reconstruction(model, path)
    data = json.loads(path.read_text())
    data["cameras"].append(data["cameras"][0])
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Duplicate camera ID"):
        load_reconstruction(path)


def test_json_does_not_silently_reshape_malformed_keypoints(model, tmp_path):
    path = tmp_path / "model.json"
    save_reconstruction(model, path)
    data = json.loads(path.read_text())
    data["images"][0]["keypoints"] = [0, 1, 2, 3]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="keypoints must have shape"):
        load_reconstruction(path)


def manifest_file(tmp_path, raw_path="images/first.jpg"):
    (tmp_path / "images").mkdir(exist_ok=True)
    (tmp_path / "images" / "first.jpg").write_bytes(b"test fixture")
    data = {
        "cameras": [{"id": 7, "width": 640, "height": 480,
                     "K": [[800, 0, 320], [0, 800, 240], [0, 0, 1]]}],
        "images": [{"id": 29, "name": "first.jpg", "camera_id": 7, "path": raw_path}],
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_dataset_resolves_manifest_relative_paths(tmp_path, monkeypatch):
    path = manifest_file(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    dataset = load_dataset(path)
    assert dataset["image_paths"][29] == (tmp_path / "images" / "first.jpg").resolve()
    assert dataset["images"][29].keypoints.shape == (0, 2)
    assert dataset["images"][29].camera_id == 7


@pytest.mark.parametrize("raw_path", [
    "../outside.jpg", "..\\outside.jpg", "/absolute.jpg", "C:\\absolute.jpg",
    "C:relative-drive.jpg", "\\\\server\\share\\image.jpg", "images/missing.jpg",
])
def test_dataset_rejects_unsafe_or_missing_paths(tmp_path, raw_path):
    with pytest.raises(ValueError):
        load_dataset(manifest_file(tmp_path, raw_path))


def test_dataset_rejects_duplicate_names(tmp_path):
    path = manifest_file(tmp_path)
    data = json.loads(path.read_text())
    data["images"].append(dict(data["images"][0], id=101))
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Duplicate image name"):
        load_dataset(path)


def test_synthetic_reproducible_with_noise_and_outliers():
    first = generate_scene(seed=42, noise_px=0.4, outlier_ratio=0.15)
    second = generate_scene(seed=42, noise_px=0.4, outlier_ratio=0.15)
    for key in first:
        np.testing.assert_array_equal(first[key], second[key])
    assert not np.array_equal(first["points"], generate_scene(seed=43)["points"])
    assert first["outliers"].sum() == round(0.15 * first["visible"].sum())
    assert not np.any(first["outliers"] & ~first["visible"])
    assert first["colors"].dtype == np.uint8


def test_synthetic_clean_projection_and_nonplanarity():
    scene = generate_scene(seed=3)
    points = scene["points"]
    assert np.linalg.matrix_rank(points - points.mean(axis=0)) == 3
    assert np.all((points[:, 2] >= 4) & (points[:, 2] <= 8))
    for i, (R, t) in enumerate(zip(scene["rotations"], scene["translations"])):
        h = (points @ R.T + t) @ scene["K"].T
        np.testing.assert_allclose(scene["observations"][i], h[:, :2] / h[:, 2:3], atol=1e-12)
    assert scene["visible"].all()
    centers = -np.einsum("cij,cj->ci", scene["rotations"].transpose(0, 2, 1), scene["translations"])
    assert np.ptp(centers[:, 0]) == pytest.approx(0.8)


def test_synthetic_outlier_mask_identifies_corruption():
    scene = generate_scene(seed=9, outlier_ratio=0.2)
    camera_points = scene["points"][None] + scene["translations"][:, None]
    h = camera_points @ scene["K"].T
    clean = h[..., :2] / h[..., 2:3]
    np.testing.assert_array_equal(scene["observations"][~scene["outliers"]], clean[~scene["outliers"]])
    assert np.all(np.linalg.norm(scene["observations"][scene["outliers"]] - clean[scene["outliers"]], axis=1) > 0)


def test_synthetic_noise_pixel_scale():
    scene = generate_scene(seed=2, num_points=2000, noise_px=1.5)
    h = (scene["points"][None] + scene["translations"][:, None]) @ scene["K"].T
    residual = scene["observations"] - h[..., :2] / h[..., 2:3]
    assert residual.std() == pytest.approx(1.5, rel=0.04)
    assert abs(residual.mean()) < 0.04


def test_scene_npz_round_trip_and_validation(tmp_path):
    scene = generate_scene(seed=10)
    path = tmp_path / "scene.npz"
    save_scene(scene, path)
    loaded = load_scene(path)
    for key in scene:
        np.testing.assert_array_equal(scene[key], loaded[key])
    np.savez(tmp_path / "bad.npz", K=scene["K"])
    with pytest.raises(ValueError, match="missing keys"):
        load_scene(tmp_path / "bad.npz")
    scene["observations"] = scene["observations"][:, :, :1]
    with pytest.raises(ValueError, match="observations must have shape"):
        save_scene(scene, tmp_path / "invalid.npz")
    assert not (tmp_path / "invalid.npz").exists()


@pytest.mark.parametrize("kwargs", [
    {"num_points": 0}, {"num_cameras": 0}, {"noise_px": -1},
    {"noise_px": np.nan}, {"outlier_ratio": 1.1},
])
def test_synthetic_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        generate_scene(**kwargs)


def quaternion_rotation(q):
    """Independent Hamilton quaternion-to-matrix formula for export checking."""
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
        [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
        [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)],
    ])


@pytest.mark.parametrize("axis", [np.array([1, 0, 0]), np.array([0, 1, 0]), np.array([0, 0, 1]), np.array([1, 2, -3])])
@pytest.mark.parametrize("angle", [0, np.pi / 2, np.pi, np.pi + 0.1, -0.7])
def test_export_quaternion_hamilton_branches(axis, angle):
    axis = axis / np.linalg.norm(axis)
    cross = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    R = np.cos(angle) * np.eye(3) + (1 - np.cos(angle)) * np.outer(axis, axis) + np.sin(angle) * cross
    q = _rotation_to_quaternion(R)
    assert np.linalg.norm(q) == pytest.approx(1)
    np.testing.assert_allclose(quaternion_rotation(q), R, atol=1e-12)


def test_colmap_export_tracks_both_directions_and_real_errors(model, tmp_path):
    output = tmp_path / "colmap"
    write_colmap_text(model, output)
    camera_lines = [l for l in (output / "cameras.txt").read_text().splitlines() if not l.startswith("#")]
    assert camera_lines == ["7 PINHOLE 640 480 800 790 320 240"]
    image_lines = [l for l in (output / "images.txt").read_text().splitlines() if not l.startswith("#")]
    assert len(image_lines) == 6  # Empty second lines must survive.
    image_associations = {}
    for first, second in zip(image_lines[::2], image_lines[1::2]):
        fields = first.split()
        image_id = int(fields[0])
        assert image_id in {11, 29, 47}
        np.testing.assert_allclose(quaternion_rotation(np.array(fields[1:5], float)), model.poses[image_id].R, atol=1e-12)
        np.testing.assert_allclose(np.array(fields[5:8], float), model.poses[image_id].t)
        triples = second.split()
        for idx in range(len(triples) // 3):
            x, y, point_id = triples[idx*3:idx*3+3]
            np.testing.assert_allclose([float(x), float(y)], model.images[image_id].keypoints[idx])
            image_associations[(image_id, idx)] = int(point_id)
    assert image_lines[-1] == ""
    assert image_associations[(11, 1)] == -1
    point_lines = [l for l in (output / "points3D.txt").read_text().splitlines() if not l.startswith("#")]
    exported_errors = {}
    for line in point_lines:
        fields = line.split()
        point_id = int(fields[0])
        exported_errors[point_id] = float(fields[7])
        track = list(zip(map(int, fields[8::2]), map(int, fields[9::2])))
        assert track == sorted(model.points[point_id].observations)
        for observation in track:
            assert image_associations[observation] == point_id
    assert exported_errors == pytest.approx({101: 2.5, 905: 1.0})


@pytest.mark.parametrize("fault", ["negative_depth", "zero_depth", "missing_track", "skew"])
def test_colmap_preflight_does_not_create_partial_directory(model, tmp_path, fault):
    if fault == "negative_depth":
        model.points[101].xyz[2] = -5
    elif fault == "zero_depth":
        model.points[101].xyz[2] = 0
    elif fault == "missing_track":
        model.points[101].observations = ()
    else:
        model.cameras[7].K[0, 1] = 0.4
    output = tmp_path / "bad"
    with pytest.raises(ValueError):
        write_colmap_text(model, output)
    assert not output.exists()


def test_ply_contains_sorted_coordinates_and_rgb(model, tmp_path):
    path = tmp_path / "cloud.ply"
    write_ply(model, path)
    lines = path.read_text().splitlines()
    assert "element vertex 2" in lines
    header_end = lines.index("end_header")
    vertices = np.array([list(map(float, l.split())) for l in lines[header_end+1:]])
    np.testing.assert_allclose(vertices[:, :3], [model.points[101].xyz, model.points[905].xyz])
    np.testing.assert_array_equal(vertices[:, 3:], [model.points[101].color, model.points[905].color])


def test_summary_measures_observations_and_registration(model):
    summary = reconstruction_summary(model)
    assert summary["image_count"] == 4
    assert summary["registered_images"] == 3
    assert summary["point_count"] == 2
    assert summary["observation_count"] == 4
    assert summary["mean_reprojection_error_px"] == pytest.approx(1.75)
    assert summary["median_reprojection_error_px"] == pytest.approx(1)
    # The four measured errors are [0, 0, 2, 5]; linear p95 is 2 + 0.85*(5-2).
    assert summary["p95_reprojection_error_px"] == pytest.approx(4.55)
    assert summary["positive_depth_ratio"] == 1
    model.points[101].xyz[2] = -5
    assert reconstruction_summary(model)["positive_depth_ratio"] == 0.5


def test_empty_reconstruction_summary_and_export(tmp_path):
    model = Reconstruction()
    summary = reconstruction_summary(model)
    assert summary["point_count"] == summary["observation_count"] == 0
    assert summary["mean_reprojection_error_px"] is None
    assert summary["median_reprojection_error_px"] is None
    assert summary["p95_reprojection_error_px"] is None
    assert summary["positive_depth_ratio"] is None
    write_colmap_text(model, tmp_path / "empty")
    write_ply(model, tmp_path / "empty.ply")
