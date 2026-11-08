"""Behavior checks for the OpenCV front end, CLI and pipeline error protocol."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from minicolmap.backends import solve_pnp
from minicolmap.cli import main
from minicolmap.config import DEFAULT, load_config
from minicolmap.frontend import extract_features, load_rgb, match_descriptors, save_match_preview
from minicolmap.io import load_reconstruction, save_reconstruction
from minicolmap.models import Camera, ImageRecord, Point3D, Pose, Reconstruction
from minicolmap.synthetic import generate_scene, load_scene


def save_rgb(path, rgb):
    """Use encoded bytes so Chinese Windows paths never rely on cv2.imread."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    assert ok
    encoded.tofile(path)
    return path


def texture():
    return np.random.default_rng(123).integers(0, 256, (240, 320, 3), dtype=np.uint8)


def test_load_rgb_preserves_channels_and_chinese_path(tmp_path):
    rgb = np.zeros((48, 64, 3), dtype=np.uint8)
    rgb[:] = [19, 101, 231]
    rgb[10:20, 12:30] = [240, 7, 49]
    path = save_rgb(tmp_path / "中文目录" / "相机图像.png", rgb)
    decoded = load_rgb(path)
    assert decoded.dtype == np.uint8
    np.testing.assert_array_equal(decoded, rgb)


def test_blank_image_returns_well_shaped_empty_features(tmp_path):
    rgb = np.full((128, 160, 3), 127, dtype=np.uint8)
    path = save_rgb(tmp_path / "blank.png", rgb)
    keypoints, descriptors, decoded = extract_features(path)
    assert keypoints.shape == (0, 2)
    assert descriptors.shape == (0, 128)
    assert descriptors.dtype == np.float32
    assert match_descriptors(descriptors, descriptors).shape == (0, 2)
    np.testing.assert_array_equal(decoded, rgb)


def test_actual_sift_matching_and_unicode_preview(tmp_path):
    rgb = texture()
    path = save_rgb(tmp_path / "纹理.png", rgb)
    keypoints, descriptors, decoded = extract_features(path, max_features=100)
    assert len(keypoints) >= 8
    assert descriptors.shape == (len(keypoints), 128)
    assert np.isfinite(descriptors).all()
    assert np.all((keypoints >= [0, 0]) & (keypoints < [320, 240]))
    indices = match_descriptors(descriptors, descriptors)
    assert len(indices) >= 8
    np.testing.assert_array_equal(indices[:, 0], indices[:, 1])
    preview = tmp_path / "可视化" / "匹配.png"
    save_match_preview(preview, decoded, decoded, keypoints, keypoints, indices, limit=12)
    assert load_rgb(preview).shape == (240, 640, 3)


def test_mutual_matching_rejects_competing_and_ambiguous_descriptors():
    # a[1] also passes the forward ratio against b[0], but b[0] prefers a[0].
    # a[3] is near the midpoint of b[0] and b[1] and fails the ratio test.
    a, b = np.zeros((4, 128), np.float32), np.zeros((3, 128), np.float32)
    a[:, 0], b[:, 0] = [0, 0.1, 20.7, 10], [0.001, 20.001, 40]
    np.testing.assert_array_equal(match_descriptors(a, b, ratio=0.75), [[0, 0], [2, 1]])
    np.testing.assert_array_equal(match_descriptors(a, b, ratio=0.03), [[0, 0]])
    for kwargs in ({"ratio": 1}, {"ratio": 0}, {"ratio": np.nan}):
        with pytest.raises(ValueError):
            match_descriptors(a, b, **kwargs)


def test_actual_pnp_recovers_nonidentity_world_to_camera_pose_with_outliers():
    scene = generate_scene(seed=7, num_points=100)
    points, K = scene["points"], scene["K"]
    truth_R, _ = cv2.Rodrigues(np.array([0.08, -0.12, 0.04]))
    truth_t = np.array([0.15, -0.1, 0.25])
    h = (points @ truth_R.T + truth_t) @ K.T
    rng = np.random.default_rng(22)
    pixels = h[:, :2] / h[:, 2:3] + rng.normal(0, 0.15, (100, 2))
    outliers = np.zeros(100, dtype=bool)
    outliers[rng.choice(100, 25, replace=False)] = True
    pixels[outliers] += [170.0, -120.0]
    R, t, inliers = solve_pnp(points, pixels, K, threshold_px=1.5, seed=11)
    np.testing.assert_allclose(R, truth_R, atol=0.001)
    np.testing.assert_allclose(t, truth_t, atol=0.008)
    assert inliers.shape == (100,) and inliers.dtype == np.bool_
    assert np.count_nonzero(inliers & ~outliers) >= 73
    assert not np.any(inliers & outliers)
    estimated = points @ R.T + t
    projected = estimated @ K.T
    errors = np.linalg.norm(projected[:, :2] / projected[:, 2:3] - pixels, axis=1)
    np.testing.assert_array_equal(inliers, (estimated[:, 2] > 1e-8) & (errors <= 1.5))


def test_pnp_rejects_insufficient_and_collinear_support():
    K = np.array([[800, 0, 320], [0, 800, 240], [0, 0, 1]])
    with pytest.raises(ValueError, match="at least six"):
        solve_pnp(np.zeros((5, 3)), np.zeros((5, 2)), K)
    points = np.column_stack([np.arange(8), np.zeros(8), np.full(8, 5)])
    with pytest.raises(ValueError, match="collinear"):
        solve_pnp(points, np.zeros((8, 2)), K)


def test_configuration_defaults_match_saved_json_and_are_independent():
    root = Path(__file__).resolve().parents[2]
    saved = json.loads((root / "configs" / "default.json").read_text(encoding="utf-8"))
    assert load_config() == load_config(root / "configs" / "default.json") == saved == DEFAULT
    changed = load_config()
    changed["features"]["ratio"] = 0.2
    assert load_config()["features"]["ratio"] == 0.75


@pytest.mark.parametrize('overrides', [
    {'confidence': 0}, {'confidence': 1}, {'confidence': float('nan')},
    {'confidence': float('inf')}, {'confidence': True},
    {'min_trials': 0}, {'min_trials': -1}, {'min_trials': 1.5}, {'min_trials': True},
])
def test_configuration_rejects_invalid_adaptive_options(overrides, tmp_path):
    path = tmp_path / 'adaptive.json'
    path.write_text(json.dumps({'geometry': overrides}))
    with pytest.raises(ValueError):
        load_config(path)


def test_configuration_fills_legacy_defaults_and_preserves_hard_cap_policy(tmp_path):
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps({'geometry': {'max_trials': 4}}))
    config = load_config(path)
    assert config['geometry']['confidence'] == .99
    assert config['geometry']['min_trials'] == 20
    path.write_text(json.dumps({'geometry': {'confidence': 1e-15, 'min_trials': 37, 'max_trials': 4}}))
    config = load_config(path)
    assert config['geometry']['confidence'] == 1e-15
    assert config['geometry']['min_trials'] == 37  # The estimator enforces the hard cap.


def test_configuration_accepts_partial_override_and_rejects_invalid_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"features": {"ratio": 0.6}}))
    settings = load_config(path)
    assert settings["features"]["ratio"] == 0.6
    assert settings["features"]["max_features"] == DEFAULT["features"]["max_features"]
    for invalid in (
        {"unknown": {}}, {"features": {"typo": 100}}, {"features": {"ratio": 1}},
        {"geometry": {"min_inliers": 7}}, {"geometry": {"max_trials": True}},
        {"mapping": {"min_angle_deg": 180}}, {"mapping": {"pnp_threshold_px": -1}},
        {"bundle_adjustment": {"enabled": 1}}, {"bundle_adjustment": {"loss": "invalid"}},
        {"seed": -1},
    ):
        path.write_text(json.dumps(invalid))
        with pytest.raises(ValueError):
            load_config(path)


def test_cli_doctor_checks_installed_dependencies(capsys):
    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    for dependency in ("numpy", "cv2", "scipy", "pytest"):
        assert f"{dependency}:" in output
    assert "MISSING" not in output
    assert "Dependency checks do not certify reconstruction or rendering quality" in output


def test_cli_synthetic_generates_observation_fixture_without_claiming_sfm(tmp_path, capsys):
    path = tmp_path / "fixtures" / "scene.npz"
    assert main([
        "synthetic", "--output", str(path), "--seed", "15", "--points", "24",
        "--cameras", "3", "--noise-px", "0.2", "--outlier-ratio", "0.125",
    ]) == 0
    output = capsys.readouterr().out
    assert "Geometry fixture saved" in output
    assert "not a SIFT or SfM result" in output
    scene = load_scene(path)
    assert scene["observations"].shape == (3, 24, 2)
    assert scene["outliers"].sum() == 9
    expected = generate_scene(seed=15, num_points=24, num_cameras=3, noise_px=0.2, outlier_ratio=0.125)
    np.testing.assert_array_equal(scene["observations"], expected["observations"])
    contents = path.read_bytes()
    assert main(["synthetic", "--output", str(path)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert path.read_bytes() == contents


def small_model():
    """A stored reconstruction fixture, solely for testing inspect/export plumbing."""
    K = np.array([[800, 0, 320], [0, 800, 240], [0, 0, 1]])
    points = np.array([[0.1, 0.2, 5], [-0.2, -0.1, 6]])
    R, _ = cv2.Rodrigues(np.array([0.03, -0.05, 0.01]))
    poses = {11: Pose(np.eye(3), np.zeros(3)), 29: Pose(R, np.array([-0.4, 0.02, 0]))}
    images = {}
    for image_id, pose in poses.items():
        h = (points @ pose.R.T + pose.t) @ K.T
        images[image_id] = ImageRecord(image_id, f"{image_id}.png", 7, h[:, :2] / h[:, 2:3])
    cloud = {
        point_id: Point3D(point_id, xyz, [50 + index, 100, 200], ((11, index), (29, index)))
        for index, (point_id, xyz) in enumerate(zip((101, 905), points))
    }
    return Reconstruction({7: Camera(7, 640, 480, K)}, images, poses, cloud)


def test_cli_inspect_and_export_saved_model(tmp_path, capsys):
    path = tmp_path / "model.json"
    save_reconstruction(small_model(), path)
    assert main(["inspect", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["registered_images"] == 2
    assert summary["point_count"] == 2 and summary["observation_count"] == 4
    assert summary["mean_reprojection_error_px"] < 1e-10
    output = tmp_path / "exported"
    assert main(["export", str(path), "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out) == summary
    for filename in ("cameras.txt", "images.txt", "points3D.txt"):
        assert (output / "sparse" / "0" / filename).is_file()
    assert (output / "points.ply").is_file()
    assert set(load_reconstruction(path).points) == {101, 905}
    assert main(["export", str(path), "--output", str(output)]) == 1
    assert "must be empty" in capsys.readouterr().err


def test_pipeline_records_todo_after_real_features_and_matching(tmp_path, monkeypatch):
    from minicolmap import pipeline

    rgb = texture()
    images = []
    for image_id in (11, 29):
        save_rgb(tmp_path / "images" / f"{image_id}.png", rgb)
        images.append({"id": image_id, "name": f"{image_id}.png", "camera_id": 7,
                       "path": f"images/{image_id}.png"})
    manifest = {
        "cameras": [{"id": 7, "width": 320, "height": 240,
                     "K": [[400, 0, 160], [0, 400, 120], [0, 0, 1]]}],
        "images": images,
    }
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(manifest))

    # Deliberate error-protocol fixture; the production geometry is implemented.
    def unfinished_geometry(*args, **kwargs):
        raise NotImplementedError("TODO G5: geometry is deliberately unfinished")

    monkeypatch.setattr(pipeline, "ransac_fundamental", unfinished_geometry)
    settings = load_config()
    settings["features"]["max_features"] = 100
    output = tmp_path / "run"
    with pytest.raises(NotImplementedError, match="TODO G5"):
        pipeline.reconstruct(dataset, output, settings)
    status = json.loads((output / "run_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "todo" and status["stage"] == "pair_geometry"
    assert "TODO G5" in status["error"]
    for image_id in (11, 29):
        with np.load(output / "features" / f"{image_id}.npz", allow_pickle=False) as features:
            assert len(features["keypoints"]) >= 8
    assert not (output / "model.json").exists()
    assert not (output / "summary.json").exists()
