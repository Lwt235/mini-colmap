"""Incremental map management; algorithm flow is documented in docs/architecture.md.

All config arguments take the FULL PipelineConfig returned by load_config().
Read config['mapping'] for local thresholds, config['seed'] for PnP, and
config['geometry']['min_inliers'] for M1's minimum initial points.
Do not pass only config['mapping']. Full schema: docs/configuration.md.
"""
from dataclasses import dataclass
from typing import TypeAlias, TypedDict, NotRequired

import numpy as np
import einx
from numpy.typing import NDArray

from .models import (
    Reconstruction,
    Pose, Point3D,
    validate_reconstruction
)
from . import backends
from .config import PipelineConfig
from .optimization import OptimizationReport, refine_two_view
from .tracks import Track
from .geometry import (
    essential_from_fundamental,
    triangulate_points,
    recover_relative_pose,
    sampson_distance,
)


class InitialPairReport(TypedDict, total=False):
    """One attempted pair; counts are cumulative after the named quality gate."""

    images: list[int]
    matches: int
    seed_positive_points: int
    seed_angle_points: int
    fundamental_sampson_median_px: float
    constrained_essential_sampson_median_px: float
    refinement: OptimizationReport
    positive_points: int
    angle_points: int
    reprojection_points: int
    accepted_tracks: int
    selected: bool
    rejection_reason: str | None

    # Not Required
    skipped_by_upper_bound: bool
    upper_bound_points: int
    best_points_at_prune: int


# Each array has shape (N_i, 3), uint8 RGB in [0,255]. Its row k corresponds
# exactly to model.images[image_id].keypoints[k]; image IDs need not be contiguous.
FeatureColors: TypeAlias = dict[int, NDArray[np.uint8]]


@dataclass
class VerifiedPair:
    """Verified pixel matches: indices(M,2) stores keypoint indices, not coordinates.

    Column 0 indexes image_id1.keypoints; column 1 indexes image_id2.keypoints.
    F is float64(3,3), with x2.T @ F @ x1 == 0 in pixel coordinates.
    """

    image_id1: int
    image_id2: int
    indices: NDArray[np.int64]  # inlier keypoint indices(M,2), NOT xy coordinates
    F: NDArray[np.float64]      # x2.T F x1 = 0, pixel coordinates


def initialize_map(
    model: Reconstruction,
    pairs: list[VerifiedPair],
    tracks: list[Track],
    config: PipelineConfig,
    feature_colors: FeatureColors,
    *,
    diagnostics: list[InitialPairReport] | None = None,
) -> None:
    """M1: select a geometrically sound initial pair and populate two poses + Point3D objects.

    Inspect candidate pairs with G6/G8, fix the first pose to (I,0), use ||t2||=1.
    Refine the G8/DLT seed with two-view BA before final quality filtering.
    Retain positive-depth, sufficient-angle, low-reprojection-error tracks only.
    Evaluate enough candidates: most matches alone do not imply usable baseline.
    Every stored point needs >=2 observations from registered images and a unique track.
    Set model.poses in initial-pair order (the BA runner freezes these two anchors).
    tracks is a list of tuples of (image_id, zero-based keypoint_idx), one
    scene-point correspondence group per track, at most one entry per image.
    Track-list offsets are temporary track IDs, not model.points IDs.
    feature_colors[image_id] is uint8(N_i,3) RGB, sampled in keypoint order;
    feature_colors[image_id][keypoint_idx] is that observation's RGB triplet.
    Config: geometry.min_inliers is the minimum AFTER all initialization filters;
    mapping.min_angle_deg is in degrees; mapping.max_reprojection_px bounds each
    retained observation's Euclidean pixel error. F/E estimation is upstream.
    Return None; mutate model only after initialization passes all validity checks.
    Optional diagnostics receives one record per attempted pair, including failures.
    """
    if model.poses or model.points:
        raise ValueError("Initialization requires an empty map")
    if not pairs or not tracks:
        raise ValueError("No verified pairs/tracks; check overlap and feature diagnostics")

    min_points = config["geometry"]["min_inliers"]
    min_angle = np.deg2rad(config["mapping"]["min_angle_deg"])
    max_error = config["mapping"]["max_reprojection_px"]

    track_lookup = {}
    for track_id, track in enumerate(tracks):
        obs = list(track)
        for a in range(len(obs)):
            for b in range(a + 1, len(obs)):
                key = frozenset((obs[a], obs[b]))
                track_lookup[key] = track_id

    P1 = np.column_stack([
        np.eye(3), np.zeros(3)
    ])
    best = None
    best_score = (-1, -np.inf)
    best_original_idx = len(pairs)

    ordered_pairs = sorted(
        enumerate(pairs),
        key=lambda item: -len(item[1].indices),
    )

    for original_idx, pair in ordered_pairs:
        i, j = pair.image_id1, pair.image_id2
        upper_bound = len(pair.indices)
        item: InitialPairReport = {
            'images': [i, j], 'matches': len(pair.indices),
            'selected': False, 'rejection_reason': None,
        }
        if diagnostics is not None:
            diagnostics.append(item)

        if best is not None:
            best_points = best_score[0]

            if upper_bound < best_points:
                item.update({
                    "skipped_by_upper_bound": True,
                    "upper_bound_points": int(upper_bound),
                    "best_points_at_prune": int(best_points),
                    "rejection_reason": "Upper bound below best accepted point count",
                })
                continue

        image1 = model.images[i]
        image2 = model.images[j]

        K1 = model.cameras[image1.camera_id].K
        K2 = model.cameras[image2.camera_id].K

        indices = np.asarray(pair.indices)
        if len(indices) < 8:
            item['rejection_reason'] = 'Fewer than eight verified matches'
            continue
        x1_px = image1.keypoints[indices[:, 0]]
        x2_px = image2.keypoints[indices[:, 1]]

        try:
            E = essential_from_fundamental(pair.F, K1, K2)
            R, t, pose_mask = recover_relative_pose(
                E, x1_px, x2_px, K1, K2
            )
        except ValueError as exc:
            item['rejection_reason'] = str(exc)
            continue

        if diagnostics is not None:
            constrained_F = np.linalg.solve(K2.T, E) @ np.linalg.inv(K1)
            item['fundamental_sampson_median_px'] = float(np.median(
                np.sqrt(sampson_distance(pair.F, x1_px, x2_px))))
            item['constrained_essential_sampson_median_px'] = float(np.median(
                np.sqrt(sampson_distance(constrained_F, x1_px, x2_px))))

        t = t / np.linalg.norm(t)

        # because P1 is [I 0] and P2 is [R t], use normalized position
        x1_h = np.c_[x1_px, np.ones(len(x1_px))]
        x2_h = np.c_[x2_px, np.ones(len(x2_px))]
        r1 = np.linalg.solve(K1, x1_h.T).T
        r2 = np.linalg.solve(K2, x2_h.T).T
        x1_norm = r1[:, :2] / r1[:, 2:3]
        x2_norm = r2[:, :2] / r2[:, 2:3]
        P2 = np.column_stack([R, t])
        X = triangulate_points(P1, P2, x1_norm, x2_norm)
        X2 = X @ R.T + t
        depth_ok = (
            np.all(np.isfinite(X), axis=1)
            & (X[:, 2] > 1e-12)
            & (X2[:, 2] > 1e-12)
        )

        # calculate angle
        d1 = r1 / np.linalg.norm(r1, axis=1, keepdims=True)
        d2 = r2 / np.linalg.norm(r2, axis=1, keepdims=True)
        d2_world = d2 @ R
        cos_angle = einx.dot("N [i], N [i] -> N", d1, d2_world)
        angles = np.arccos(np.clip(cos_angle, -1.0, 1.0))
        angle_ok = angles > min_angle

        seed_mask = pose_mask & depth_ok
        item['seed_positive_points'] = int(np.count_nonzero(seed_mask))
        item['seed_angle_points'] = int(np.count_nonzero(seed_mask & angle_ok))
        if item['seed_angle_points'] < min_points:
            item['rejection_reason'] = 'Insufficient positive-depth points with configured parallax'
            continue

        # F -> constrained E -> R,t is a linear seed, not a pixel-error optimum.
        # Refine before applying the strict pixel gate; never alter that gate to
        # accommodate a biased seed. Both intrinsics and the unit baseline stay fixed.
        R, t, refined_points, refinement = refine_two_view(
            R, t, X[seed_mask], K1, K2, x1_px[seed_mask], x2_px[seed_mask],
        )
        item['refinement'] = refinement
        X[seed_mask] = refined_points
        pose_mask = seed_mask
        X2 = X @ R.T + t
        depth_ok = (np.all(np.isfinite(X), axis=1)
                    & (X[:, 2] > 1e-12) & (X2[:, 2] > 1e-12))
        d2_world = d2 @ R
        cos_angle = einx.dot("N [i], N [i] -> N", d1, d2_world)
        angles = np.arccos(np.clip(cos_angle, -1.0, 1.0))
        angle_ok = angles > min_angle

        # Reprojection in pixel coordinates
        proj1_h = X @ K1.T
        proj2_h = X2 @ K2.T
        with np.errstate(divide="ignore", invalid="ignore"):
            proj1 = proj1_h[:, :2] / proj1_h[:, 2:3]
            proj2 = proj2_h[:, :2] / proj2_h[:, 2:3]
            error1 = np.linalg.norm(proj1 - x1_px, axis=1)
            error2 = np.linalg.norm(proj2 - x2_px, axis=1)
        reproj_ok = (
            np.isfinite(error1)
            & np.isfinite(error2)
            & (error1 <= max_error)
            & (error2 <= max_error)
        )

        valid = (
            pose_mask
            & depth_ok
            & angle_ok
            & reproj_ok
        )
        item['positive_points'] = int(np.count_nonzero(pose_mask & depth_ok))
        item['angle_points'] = int(np.count_nonzero(pose_mask & depth_ok & angle_ok))
        item['reprojection_points'] = int(np.count_nonzero(valid))

        accepted = {}
        for k in np.flatnonzero(valid):
            fi, fj = map(int, indices[k])
            obs1 = (i, fi)
            obs2 = (j, fj)
            key = frozenset((obs1, obs2))
            track_id = track_lookup.get(key)

            if track_id is None:
                continue

            candidate_error = error1[k] + error2[k]
            if (
                track_id not in accepted
                or candidate_error < accepted[track_id][4]
            ):
                accepted[track_id] = (
                    X[k].copy(),
                    obs1, obs2, k,
                    candidate_error,
                )

        count = len(accepted)
        item['accepted_tracks'] = count
        if count < min_points:
            item['rejection_reason'] = 'Too few tracks after final initialization quality checks'
            continue

        # Prefer more points; break ties by median angle
        chosen_rows = [v[3] for v in accepted.values()]
        median_angle = float(np.median(angles[chosen_rows]))
        score = (count, median_angle)
        if score > best_score or (score == best_score and original_idx < best_original_idx):
            best_score = score
            best_original_idx = original_idx
            best = (i, j, R.copy(), t.copy(), accepted, item)

    if best is None:
        raise ValueError("No suitable initial image pair found")

    # Build candidate records without mutating model
    i, j, R, t, accepted, selected_report = best
    poses = {
        i: Pose(np.eye(3), np.zeros(3)),
        j: Pose(R, t),
    }
    points = {}

    for point_id, (_, (xyz, obs1, obs2, _, _)) in enumerate(
        sorted(accepted.items()), start=1
    ):
        img_id, feat_id = obs1
        color = feature_colors[img_id][feat_id]
        points[point_id] = Point3D(
            id=point_id,
            xyz=xyz,
            color=color,
            observations=(obs1, obs2),
        )

    # Validate everything before committing
    candidate = Reconstruction(
        cameras=model.cameras,
        images=model.images,
        poses=poses,
        points=points,
    )
    validate_reconstruction(candidate)
    model.poses.update(poses)
    model.points.update(points)
    selected_report['selected'] = True


def register_next_image(
    model: Reconstruction,
    tracks: list[Track],
    config: PipelineConfig,
) -> bool:
    """M2: add exactly one camera via existing point-track correspondences, or return False.

    For each unregistered image, gather unique 2D-3D pairs using keypoint/track membership.
    Each Track contains (image_id, zero-based keypoint_idx) observations.
    Link tracks to existing 3D points through model.points[*].observations,
    then read the candidate image's pixel coordinates from model.images.
    Pairwise F matrices/match edges are not needed for this PnP stage.
    Try candidates in descending support order; use backends.solve_pnp (already provided).
    Require mapping.min_pnp_points unique pairs before PnP and at least that many
    retained positive-depth inliers afterwards. Pass mapping.pnp_threshold_px
    (pixels, NOT squared) and config['seed'] to backends.solve_pnp.
    Added observations must also satisfy mapping.max_reprojection_px; recheck
    min_pnp_points after this final filter. No inlier-ratio threshold is configured.
    A failed candidate must not corrupt model or prevent trying other candidates.
    On success add its Pose and valid observations to already triangulated points.
    Match reprojection thresholds with that image's Camera.K; IDs are not array indices.
    Return False only after all remaining candidates were tried without a valid registration.
    """
    if len(model.poses) < 2:
        raise ValueError("Register images only after two-view initialization")

    min_points = config["mapping"]["min_pnp_points"]
    pnp_threshold = config["mapping"]["pnp_threshold_px"]
    max_reproj = config["mapping"]["max_reprojection_px"]

    # Observation -> track ID
    obs_to_track = {}
    for track_id, track in enumerate(tracks):
        for obs in track:
            if obs in obs_to_track:
                raise ValueError("Observation belongs to multiple tracks")
            obs_to_track[obs] = track_id

    # Track ID -> existing Point3D ID
    track_to_point = {}
    for point_id, point in model.points.items():
        track_ids = {
            obs_to_track[obs]
            for obs in point.observations
            if obs in obs_to_track
        }
        if len(track_ids) != 1:
            raise ValueError("Point has inconsistent track membership")
        (track_id,) = track_ids
        if track_id in track_to_point:
            raise ValueError("Multiple points share one track")

        track_to_point[track_id] = point_id

    # Collect 2D-3D pairs for every unregistered image
    candidates = []
    for image_id, image in model.images.items():
        if image_id in model.poses:
            continue

        correspondences = []
        used_features = set()
        for track_id, point_id in track_to_point.items():
            track = tracks[track_id]
            for obs_image_id, feat_id in track:
                if obs_image_id != image_id:
                    continue
                if feat_id >= len(image.keypoints):
                    continue
                if feat_id in used_features:
                    continue

                used_features.add(feat_id)
                correspondences.append((point_id, feat_id))

        if len(correspondences) < min_points:
            continue

        candidates.append((image_id, correspondences))

    # Most support images first; deterministic tie-break
    candidates.sort(key=lambda item: (-len(item[1]), item[0]))

    for image_id, correspondences in candidates:
        image = model.images[image_id]
        K = model.cameras[image.camera_id].K

        point_ids = [p for p, _ in correspondences]
        feature_ids = [f for _, f in correspondences]
        points3d = np.array([
            model.points[p].xyz
            for p in point_ids
        ], dtype=np.float64)
        pixels = image.keypoints[feature_ids]

        try:
            R, t, pnp_mask = backends.solve_pnp(
                points3d, pixels, K,
                threshold_px=pnp_threshold,
                seed=config["seed"],
            )
        except ValueError:
            continue

        Xc = points3d @ R.T + t
        projected_h = Xc @ K.T
        with np.errstate(divide="ignore", invalid="ignore"):
            projected = projected_h[:, :2] / projected_h[:, 2:3]
            errors = np.linalg.norm(projected - pixels, axis=1)
        valid = (
            pnp_mask
            & np.all(np.isfinite(Xc), axis=1)
            & (Xc[:, 2] > 1e-8)
            & np.isfinite(errors)
            & (errors <= max_reproj)
        )

        if np.count_nonzero(valid) < min_points:
            continue

        # Stage updated observation
        updated_points = {}
        for k in np.flatnonzero(valid):
            point_id, feat_id = correspondences[k]
            old = model.points[point_id]
            new_observations = (old.observations + ((image_id, feat_id),))
            updated_points[point_id] = Point3D(
                id=old.id,
                xyz=old.xyz,
                color=old.color,
                observations=new_observations,
            )

        new_pose = Pose(R, t)
        candidates_model = Reconstruction(
            cameras=model.cameras,
            images=model.images,
            poses={**model.poses, image_id: new_pose},
            points={**model.points, **updated_points}
        )
        try:
            validate_reconstruction(candidates_model)
        except ValueError:
            continue

        model.poses[image_id] = new_pose
        model.points.update(updated_points)
        return True

    # All candidates failed
    return False


def triangulate_new_tracks(
    model: Reconstruction,
    tracks: list[Track],
    config: PipelineConfig,
    feature_colors: FeatureColors,
) -> int:
    """M3: create valid new points from tracks with >=2 registered observations.

    Choose a pair with sufficient ray angle. Use G7 and check EVERY registered observation.
    Keep only finite points with positive depth, angle>=min_angle_deg and bounded pixel error.
    Never create a second Point3D for a track already associated with a point.
    Gather RGB from feature_colors[image_id][keypoint_idx], allocate an unused positive point ID.
    tracks: list of Track tuples, each containing (image_id, keypoint_idx).
    feature_colors: image-ID dictionary of uint8 RGB arrays (N_i,3), whose
    row indices exactly match that image's keypoints (not Point3D IDs).
    Config: mapping.min_angle_deg (degrees) and mapping.max_reprojection_px
    (Euclidean pixels per observation). geometry.min_inliers is NOT a minimum
    point batch size here: one valid new point is allowed.
    Return number of new points; existing point/observation links remain consistent.
    """
    if len(model.poses) < 2:
        raise ValueError("Triangulation requires registered cameras")
    min_angle = np.deg2rad(config["mapping"]["min_angle_deg"])
    max_error = config["mapping"]["max_reprojection_px"]

    # Find tracks already represented by existing points
    obs_to_track = {}
    for track_id, track in enumerate(tracks):
        for obs in track:
            if obs in obs_to_track:
                raise ValueError("Observation belongs to multiple tracks")
            obs_to_track[obs] = track_id

    existing_tracks = set()
    occupied = set()

    for point in model.points.values():
        linked = {
            obs_to_track[obs]
            for obs in point.observations
            if obs in obs_to_track
        }
        if len(linked) > 1:
            raise ValueError(f"Point3D {point.id} belongs to multiple tracks")
        existing_tracks.update(linked)
        occupied.update(point.observations)

    new_points = {}
    next_id = max(model.points.keys(), default=0) + 1

    # Process tracks not yet triangulated
    for track_id, track in enumerate(tracks):
        if track_id in existing_tracks:
            continue

        registered = [
            (image_id, feat_id)
            for image_id, feat_id in track
            if image_id in model.poses
        ]
        # Reject invalid or already occupied observations
        if len(registered) < 2:
            continue
        if any(obs in occupied for obs in registered):
            continue
        if len({i for i, _ in registered}) != len(registered):
            continue

        # Collect projection matrices, pixels and world rays
        records = []
        valid_input = True
        for image_id, feat_id in registered:
            image = model.images[image_id]

            if feat_id < 0 or feat_id >= len(image.keypoints):
                valid_input = False
                break

            pose = model.poses[image_id]
            K = model.cameras[image.camera_id].K
            pixel = image.keypoints[feat_id]
            pixel_h = np.array([pixel[0], pixel[1], 1.0])

            ray_camera = np.linalg.solve(K, pixel_h)
            ray_norm = np.linalg.norm(ray_camera)

            if ray_norm == 0 or not np.isfinite(ray_norm):
                valid_input = False
                break

            # Column-vector world direction
            ray_world = pose.R.T @ (ray_camera / ray_norm)
            P = np.column_stack([pose.R, pose.t])
            records.append({
                "obs": (image_id, feat_id),
                "pixel": pixel,
                "K": K,
                "pose": pose,
                "P": P,
                "ray": ray_world,
                "norm_xy": ray_camera[:2] / ray_camera[2]
            })

        if not valid_input:
            continue

        # Choose pair with maximum triangulation angle
        best_pair = None
        best_angle = -1.0
        for a in range(len(records)):
            for b in range(a + 1, len(records)):
                d1 = records[a]["ray"]
                d2 = records[b]["ray"]
                cos_angle = np.clip(np.dot(d1, d2), -1.0, 1.0)
                angle = np.arccos(cos_angle)

                if angle > best_angle:
                    best_angle = angle
                    best_pair = (a, b)

        if best_pair is None or best_angle < min_angle:
            continue

        # Triangulate
        rec1, rec2 = records[best_pair[0]], records[best_pair[1]]
        X = triangulate_points(
            rec1["P"],
            rec2["P"],
            rec1["norm_xy"][None, :],
            rec2["norm_xy"][None, :]
        )[0]

        if not np.all(np.isfinite(X)):
            continue

        # Validate EVERY registered observation
        good = True
        for rec in records:
            pose = rec["pose"]
            K = rec["K"]
            Xc = pose.R @ X + pose.t

            if not np.all(np.isfinite(Xc)) or Xc[2] <= 1e-12:
                good = False
                break

            projected_h = K @ Xc

            if projected_h[2] == 0:
                good = False
                break

            projected = projected_h[:2] / projected_h[2]
            error = np.linalg.norm(projected - rec["pixel"])

            if not np.isfinite(error) or error > max_error:
                good = False
                break

        if not good:
            continue

        # Create one Point3D for this track
        image_id, feat_id = registered[0]
        color = feature_colors[image_id][feat_id]
        new_points[next_id] = Point3D(
            id=next_id,
            xyz=X,
            color=color,
            observations=tuple(registered),
        )
        occupied.update(registered)
        next_id += 1

    candidate = Reconstruction(
        cameras=model.cameras,
        images=model.images,
        poses=model.poses,
        points={**model.points, **new_points},
    )
    validate_reconstruction(candidate)
    model.points.update(new_points)
    return len(new_points)
