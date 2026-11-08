"""M1–M3 mapping contracts with independent projection fixtures.

Known geometry creates observations and the already-registered input map.
M1 is judged by its gauge and reprojections, never by copying a GT world frame.
"""

import numpy as np
import pytest

from minicolmap import backends
from minicolmap.config import load_config
from minicolmap.mapper import (
    VerifiedPair,
    initialize_map,
    register_next_image,
    triangulate_new_tracks,
)
from minicolmap.models import (
    Camera,
    ImageRecord,
    Point3D,
    Pose,
    Reconstruction,
    validate_reconstruction,
)
from minicolmap.synthetic import generate_scene


def _pixels(points, K, R, t):
    camera_points = points @ R.T + t
    homogeneous = camera_points @ K.T
    return homogeneous[:, :2] / homogeneous[:, 2, None]


def _skew(v):
    x, y, z = v
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])


def _fixture(n=36, cameras=4):
    scene = generate_scene(seed=32, num_points=n, num_cameras=cameras)
    ids = (3, 11, 27, 41)[:cameras]
    model = Reconstruction()
    colors = {}
    for i, image_id in enumerate(ids):
        camera_id = 100 + image_id
        model.cameras[camera_id] = Camera(camera_id, 640, 480, scene["K"])
        model.images[image_id] = ImageRecord(
            image_id, f"view_{image_id}.png", camera_id, scene["observations"][i]
        )
        colors[image_id] = scene["colors"].copy()
    tracks = [tuple((image_id, j) for image_id in ids) for j in range(n)]
    pairs = []
    inv_K = np.linalg.inv(scene["K"])
    for i, first in enumerate(ids):
        for j in range(i + 1, len(ids)):
            R = scene["rotations"][j] @ scene["rotations"][i].T
            t = scene["translations"][j] - R @ scene["translations"][i]
            F = inv_K.T @ _skew(t) @ R @ inv_K
            F /= np.linalg.norm(F)
            pairs.append(VerifiedPair(first, ids[j], np.column_stack((np.arange(n), np.arange(n))), F))
    return scene, ids, model, tracks, pairs, colors, load_config()


def _known_registered_input(scene, ids, model, point_count=None, camera_count=2):
    """Allowed fixture input for M2/M3: an existing map in a known world frame."""
    for i, image_id in enumerate(ids[:camera_count]):
        model.poses[image_id] = Pose(scene["rotations"][i], scene["translations"][i])
    if point_count is not None:
        for index in range(point_count):
            point_id = 101 + 4 * index  # IDs deliberately differ from array offsets.
            observations = tuple((image_id, index) for image_id in ids[:camera_count])
            model.points[point_id] = Point3D(
                point_id, scene["points"][index], scene["colors"][index], observations
            )
    validate_reconstruction(model)


def _signature(model):
    """Value snapshot: registration failures must not mutate any map records."""
    return (
        tuple((key, c.width, c.height, tuple(c.K.ravel())) for key, c in model.cameras.items()),
        tuple((key, image.name, image.camera_id, tuple(image.keypoints.ravel())) for key, image in model.images.items()),
        tuple((key, tuple(pose.R.ravel()), tuple(pose.t)) for key, pose in model.poses.items()),
        tuple((key, tuple(point.xyz), tuple(point.color), point.observations) for key, point in model.points.items()),
    )


def _assert_geometric_map(model, maximum_error=1e-4):
    validate_reconstruction(model)
    assert model.points
    for point in model.points.values():
        assert len(point.observations) >= 2
        for image_id, feature_index in point.observations:
            image = model.images[image_id]
            pose = model.poses[image_id]
            K = model.cameras[image.camera_id].K
            depth = (pose.R @ point.xyz + pose.t)[2]
            assert depth > 0.0
            projected = _pixels(point.xyz[None], K, pose.R, pose.t)[0]
            assert np.linalg.norm(projected - image.keypoints[feature_index]) <= maximum_error


def test_m1_initialization_gauge_tracks_and_measured_reprojection():
    _, ids, model, tracks, pairs, colors, settings = _fixture(cameras=2)
    result = initialize_map(model, pairs, tracks, settings, colors)
    assert result is None
    assert tuple(model.poses) == ids
    first, second = (model.poses[image_id] for image_id in ids)
    np.testing.assert_allclose(first.R, np.eye(3), atol=1e-10)
    np.testing.assert_allclose(first.t, np.zeros(3), atol=1e-10)
    np.testing.assert_allclose(np.linalg.norm(second.t), 1.0, atol=1e-8)
    assert len(model.points) >= settings["geometry"]["min_inliers"]
    _assert_geometric_map(model)
    for point in model.points.values():
        indices = {feature for _, feature in point.observations}
        assert len(indices) == 1
        index = next(iter(indices))
        np.testing.assert_array_equal(point.color, colors[ids[0]][index])


def test_m1_missing_pairs_are_rejected_without_mutation():
    _, _, model, tracks, _, colors, settings = _fixture(cameras=2)
    before = _signature(model)
    with pytest.raises(ValueError):
        initialize_map(model, [], tracks, settings, colors)
    assert _signature(model) == before


def test_m1_existing_map_is_rejected_without_mutation():
    scene, ids, model, tracks, pairs, colors, settings = _fixture(cameras=2)
    _known_registered_input(scene, ids, model, point_count=1)
    before = _signature(model)
    with pytest.raises(ValueError):
        initialize_map(model, pairs, tracks, settings, colors)
    assert _signature(model) == before


def test_m1_unusable_baseline_is_rejected_without_partial_map():
    scene, ids, model, tracks, pairs, colors, settings = _fixture(cameras=2)
    # Same-direction translation is tiny but nonzero: many exact matches,
    # insufficient parallax. Feature count alone must not initialize the map.
    scene["translations"][1] = scene["translations"][0] + [1e-7, 0.0, 0.0]
    for i, image_id in enumerate(ids):
        model.images[image_id].keypoints = _pixels(
            scene["points"], scene["K"], scene["rotations"][i], scene["translations"][i]
        )
    before = _signature(model)
    with pytest.raises(ValueError):
        initialize_map(model, pairs, tracks, settings, colors)
    assert _signature(model) == before


def test_m2_failed_best_candidate_does_not_prevent_next_registration(monkeypatch):
    scene, ids, model, tracks, _, _, settings = _fixture()
    _known_registered_input(scene, ids, model, point_count=36)
    # Image 27 has 36 mapped tracks; image 41 has 32. Force the first PnP
    # attempt to fail, then use the actual library PnP for the second image.
    tracks = [track if i >= 4 else track[:-1] for i, track in enumerate(tracks)]
    original_pnp = backends.solve_pnp
    attempts = []

    def one_failed_pnp(points, pixels, K, *args, **kwargs):
        attempts.append(len(points))
        if len(points) == 36:
            raise ValueError("Controlled first candidate failure")
        return original_pnp(points, pixels, K, *args, **kwargs)

    monkeypatch.setattr(backends, "solve_pnp", one_failed_pnp)
    old_points = {key: point.xyz.copy() for key, point in model.points.items()}
    assert register_next_image(model, tracks, settings) is True
    assert attempts == [36, 32]
    assert set(model.poses) == {ids[0], ids[1], ids[3]}
    assert len(model.points) == 36
    assert sum((ids[3], i) in model.points[101 + 4 * i].observations for i in range(36)) == 32
    assert not any(image_id == ids[2] for point in model.points.values() for image_id, _ in point.observations)
    for key, xyz in old_points.items():
        np.testing.assert_array_equal(model.points[key].xyz, xyz)
    _assert_geometric_map(model, maximum_error=settings["mapping"]["pnp_threshold_px"])


def test_m2_no_usable_2d3d_support_returns_false_and_preserves_map():
    scene, ids, model, tracks, _, _, settings = _fixture()
    _known_registered_input(scene, ids, model, point_count=36)
    tracks = [track[:2] for track in tracks]
    before = _signature(model)
    assert register_next_image(model, tracks, settings) is False
    assert _signature(model) == before


def test_m3_checks_third_view_and_does_not_duplicate_existing_tracks():
    scene, ids, model, tracks, _, colors, settings = _fixture(n=3, cameras=3)
    _known_registered_input(scene, ids, model, camera_count=3)
    model.images[ids[2]].keypoints[1] += [60.0, 0.0]
    # Track 2 already owns point 17; track 1 has a bad third observation.
    model.points[17] = Point3D(17, scene["points"][2], scene["colors"][2], tracks[2])
    original_xyz = model.points[17].xyz.copy()
    original_observations = model.points[17].observations
    assert triangulate_new_tracks(model, tracks, settings, colors) == 1
    assert len(model.points) == 2 and 17 in model.points
    new_point = next(point for key, point in model.points.items() if key != 17)
    assert set(new_point.observations) == set(tracks[0])
    np.testing.assert_array_equal(new_point.color, scene["colors"][0])
    np.testing.assert_array_equal(model.points[17].xyz, original_xyz)
    assert model.points[17].observations == original_observations
    _assert_geometric_map(model)
    before = _signature(model)
    assert triangulate_new_tracks(model, tracks, settings, colors) == 0
    assert _signature(model) == before


def test_m3_rejects_negative_depth_and_low_parallax():
    scene, ids, model, tracks, _, colors, settings = _fixture(n=3, cameras=2)
    _known_registered_input(scene, ids, model, camera_count=2)
    scene["points"][1] = [0.0, 0.1, -6.0]
    scene["points"][2] = [0.0, 0.0, 1e7]
    for i, image_id in enumerate(ids):
        pose = model.poses[image_id]
        model.images[image_id].keypoints = _pixels(scene["points"], scene["K"], pose.R, pose.t)
    assert triangulate_new_tracks(model, tracks, settings, colors) == 1
    assert len(model.points) == 1
    point = next(iter(model.points.values()))
    assert set(point.observations) == set(tracks[0])
    _assert_geometric_map(model)
