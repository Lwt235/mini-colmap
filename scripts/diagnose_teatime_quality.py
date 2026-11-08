"""CPU-only input, camera, regional-error and static-scene diagnostics.

Epipolar violations are motion/mismatch/calibration candidates, NOT person masks.
Uses saved validation PNGs; no model training or original-file modifications.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")
os.environ.setdefault("MPLCONFIGDIR", "/tmp/minicolmap-mpl")
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs/ui_3dgs_20261024_112609_a1d94a"
OLD = ROOT / "outputs/ui_3dgs_20261020_104007_c41d30"
DATA = ROOT / "data/lerf_ovs/teatime"
OUT = ROOT / "reports/teatime-quality-diagnosis-20261026"


def read(path):
    return json.loads(path.read_text())


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def projection(K, R, t, xyz):
    xc = xyz @ R.T + t
    projected = xc @ K.T
    return projected[:, :2] / projected[:, 2:3], xc[:, 2]


def fundamental(K1, R1, t1, K2, R2, t2):
    rotation = R2 @ R1.T
    translation = t2 - rotation @ t1
    if np.linalg.norm(translation) < 1e-10:
        raise ValueError("Zero camera baseline")
    x, y, z = translation
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    matrix = np.linalg.inv(K2).T @ cross @ rotation @ np.linalg.inv(K1)
    return matrix / np.linalg.norm(matrix)


def epipolar_distance(matrix, p1, p2):
    a = np.column_stack([p1, np.ones(len(p1))])
    b = np.column_stack([p2, np.ones(len(p2))])
    line2 = a @ matrix.T
    line1 = b @ matrix
    denominator = np.sqrt(np.sum(line2[:, :2] ** 2 + line1[:, :2] ** 2, axis=1))
    return np.abs(np.sum(b * line2, axis=1)) / np.maximum(denominator, 1e-12)


def stats(values):
    a = np.asarray(values)
    a = a[np.isfinite(a)]
    if not len(a):
        return {"count": 0}
    return {
        "count": len(a),
        "mean": float(np.mean(a)),
        "median": float(np.median(a)),
        "p90": float(np.percentile(a, 90)),
        "p95": float(np.percentile(a, 95)),
        "p99": float(np.percentile(a, 99)),
    }


def load_rgb(path):
    value = cv2.imread(str(path))
    if value is None:
        raise ValueError(f"Missing image {path}")
    return cv2.cvtColor(value, cv2.COLOR_BGR2RGB)


def main():
    cv2.setNumThreads(4)
    OUT.mkdir(parents=True, exist_ok=True)
    document = read(RUN / "source_model.json")
    manifest = read(DATA / "dataset_all.json")
    provenance = read(DATA / "provenance.json")
    summary = read(RUN / "summary.json")
    images = {i["id"]: i for i in document["images"]}
    inputs = {i["id"]: i for i in manifest["images"]}
    cameras = {c["id"]: c for c in document["cameras"]}
    input_cameras = {c["id"]: c for c in manifest["cameras"]}
    poses = {
        p["image_id"]: (np.array(p["R"]), np.array(p["t"])) for p in document["poses"]
    }
    photos = {i: (DATA / inputs[i]["path"]).resolve() for i in images}
    for i, record in images.items():
        assert (
            record["name"] == inputs[i]["name"]
            and record["camera_id"] == inputs[i]["camera_id"]
        )
        assert cameras[record["camera_id"]] == input_cameras[record["camera_id"]]
        assert sha(photos[i]) == provenance["image_sha256"][record["name"]]
        rgb = load_rgb(photos[i])
        c = cameras[record["camera_id"]]
        assert rgb.shape[:2] == (c["height"], c["width"])
    print("177 photo hashes, camera identities and dimensions checked", flush=True)
    result = {
        "status": "running",
        "device": "cpu",
        "input_checks": {
            "images": len(images),
            "hashes_match_provenance": True,
            "image_names_camera_ids_intrinsics_dimensions_match": True,
            "same_source_model_as_old": sha(RUN / "source_model.json")
            == sha(OLD / "source_model.json"),
        },
        "camera_geometry": {},
        "view_diagnostics": {},
    }
    observations = {i: [] for i in images}
    for point in document["points"]:
        xyz = point["xyz"]
        for image_id, keypoint in point["observations"]:
            observations[image_id].append(
                (xyz, images[image_id]["keypoints"][keypoint])
            )
    per_camera = {}
    for i, obs in observations.items():
        xyz = np.array([p[0] for p in obs])
        xy = np.array([p[1] for p in obs])
        R, t = poses[i]
        K = np.array(cameras[images[i]["camera_id"]]["K"])
        prediction, z = projection(K, R, t, xyz)
        valid = np.isfinite(prediction).all(axis=1) & (z > 0)
        residual = prediction[valid] - xy[valid]
        errors = np.linalg.norm(residual, axis=1)
        per_camera[i] = {
            "name": images[i]["name"],
            "reprojection_px": stats(errors),
            "positive_depth_fraction": float(np.mean(z > 0)),
            "fraction_over_3px": float(np.mean(errors > 3)),
            "median_residual_xy": np.median(residual, axis=0).tolist(),
        }
    result["camera_geometry"]["all_views"] = per_camera
    result["camera_geometry"]["median_of_view_medians"] = float(
        np.median([v["reprojection_px"]["median"] for v in per_camera.values()])
    )
    centers = {i: -poses[i][0].T @ poses[i][1] for i in poses}
    extent = read(RUN / "training_scene.json")["normalization"]["scale"]
    for i in summary["validation_ids"]:
        R, t = poses[i]
        neighbors = []
        for j in summary["train_ids"]:
            angle = math.degrees(
                math.acos(np.clip(np.dot(R[2], poses[j][0][2]), -1, 1))
            )
            neighbors.append(
                {
                    "image_id": j,
                    "name": images[j]["name"],
                    "normalized_center_distance": float(
                        np.linalg.norm(centers[i] - centers[j]) / extent
                    ),
                    "view_axis_angle_degrees": angle,
                }
            )
        neighbors.sort(key=lambda r: r["normalized_center_distance"])
        target = load_rgb(OLD / f"previews/reference-{i}.png").astype(np.float32) / 255
        old = load_rgb(OLD / f"validation/image-{i}.png").astype(np.float32) / 255
        new = cv2.resize(
            load_rgb(RUN / f"validation/image-{i}.png").astype(np.float32) / 255,
            (target.shape[1], target.shape[0]),
            interpolation=cv2.INTER_AREA,
        )
        eo = np.mean((old - target) ** 2, axis=2)
        en = np.mean((new - target) ** 2, axis=2)
        tiles = []
        h, w = eo.shape
        for y in range(4):
            for x in range(4):
                region = np.s_[
                    round(y * h / 4) : round((y + 1) * h / 4),
                    round(x * w / 4) : round((x + 1) * w / 4),
                ]
                a = float(eo[region].sum())
                b = float(en[region].sum())
                tiles.append(
                    {
                        "row": y,
                        "column": x,
                        "old_sse": a,
                        "new_sse": b,
                        "delta_sse": b - a,
                        "new_error_share": b / float(en.sum()),
                    }
                )
        regions = {}
        for name, (y0, y1) in {
            "upper_half": (0, h // 2),
            "lower_half": (h // 2, h),
        }.items():
            a = float(eo[y0:y1].sum())
            b = float(en[y0:y1].sum())
            regions[name] = {
                "old_sse": a,
                "new_sse": b,
                "delta_sse": b - a,
                "new_error_share": b / float(en.sum()),
            }
        item = {
            "name": images[i]["name"],
            "geometry": per_camera[i],
            "nearest_train_cameras": neighbors[:5],
            "regions": regions,
            "tiles": tiles,
        }
        result["view_diagnostics"][i] = item
        heat = np.clip(en / 0.025, 0, 1)
        cv2.imwrite(
            str(OUT / f"error-{i}.png"),
            cv2.applyColorMap((heat * 255).astype(np.uint8), cv2.COLORMAP_INFERNO),
        )
    save(OUT / "diagnosis.json", result)
    print("Sparse reprojection and regional errors checked", flush=True)
    # Temporal ordering MUST use image filenames, not COLMAP IDs.
    sequence = sorted(images, key=lambda i: images[i]["name"])
    position = {i: n for n, i in enumerate(sequence)}
    pairs = set()
    for i in summary["validation_ids"]:
        n = position[i]
        for delta in (-2, -1, 1, 2):
            if 0 <= n + delta < len(sequence):
                pairs.add((min(n, n + delta), max(n, n + delta)))
    flow = []
    for n, m in sorted(pairs):
        i, j = sequence[n], sequence[m]
        a = load_rgb(photos[i])
        b = load_rgb(photos[j])
        ga = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
        gb = cv2.cvtColor(b, cv2.COLOR_RGB2GRAY)
        p = cv2.goodFeaturesToTrack(
            ga, maxCorners=1800, qualityLevel=0.01, minDistance=6, blockSize=5
        )
        if p is None:
            continue
        q, ok, error = cv2.calcOpticalFlowPyrLK(
            ga,
            gb,
            p,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 40, 0.01),
        )
        back, reverse, _ = cv2.calcOpticalFlowPyrLK(
            gb,
            ga,
            q,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 40, 0.01),
        )
        good = (
            (ok.ravel() > 0)
            & (reverse.ravel() > 0)
            & (np.linalg.norm(back - p, axis=2).ravel() < 0.75)
            & (error.ravel() < 20)
        )
        x, y = p[:, 0][good], q[:, 0][good]
        if len(x) < 8:
            flow.append(
                {
                    "source_id": i,
                    "target_id": j,
                    "source_name": images[i]["name"],
                    "target_name": images[j]["name"],
                    "tracks": len(x),
                    "status": "insufficient_tracks",
                }
            )
            continue
        K1 = np.array(cameras[images[i]["camera_id"]]["K"])
        K2 = np.array(cameras[images[j]["camera_id"]]["K"])
        F = fundamental(K1, *poses[i], K2, *poses[j])
        distance = epipolar_distance(F, x, y)
        bad = distance > 2.5
        flow.append(
            {
                "source_id": i,
                "target_id": j,
                "source_name": images[i]["name"],
                "target_name": images[j]["name"],
                "tracks": len(x),
                "epipolar_px": stats(distance),
                "fraction_over_2_5px": float(np.mean(bad)),
                "upper_half_outliers": int(np.sum(bad & (x[:, 1] < a.shape[0] / 2))),
                "lower_half_outliers": int(np.sum(bad & (x[:, 1] >= a.shape[0] / 2))),
                "points_source": x.tolist(),
                "points_target": y.tolist(),
                "distance_px": distance.tolist(),
            }
        )
        if i in summary["validation_ids"] or j in summary["validation_ids"]:
            overlay = cv2.cvtColor(a, cv2.COLOR_RGB2BGR)
            for point, d in zip(x, distance):
                cv2.circle(
                    overlay,
                    tuple(np.round(point).astype(int)),
                    3,
                    (220, 40, 220) if d > 2.5 else (30, 180, 30),
                    -1,
                )
            cv2.imwrite(str(OUT / f"flow-{i}-{j}.jpg"), overlay)
    result["temporal_epipolar_checks"] = flow
    result[
        "epipolar_scope"
    ] = "Forward/backward-consistent LK tracks and known-pose Sampson distance; violations may be motion, mismatches, occlusion or calibration. No automatic masks and no proof from inliers that a pixel is static."
    result["status"] = "completed"
    save(OUT / "diagnosis.json", result)
    print(
        json.dumps(
            {
                "status": "completed",
                "median_reprojection_px": result["camera_geometry"][
                    "median_of_view_medians"
                ],
                "flow_pairs": len(flow),
                "report": str(OUT),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ids = [20, 60, 80, 100, 161]
    fig, axes = plt.subplots(len(ids), 4, figsize=(16, 3 * len(ids)), dpi=100)
    for row, i in enumerate(ids):
        h, w = 365, 494
        target = load_rgb(OLD / f"previews/reference-{i}.png")
        old = load_rgb(OLD / f"validation/image-{i}.png")
        new = cv2.resize(
            load_rgb(RUN / f"validation/image-{i}.png"),
            (w, h),
            interpolation=cv2.INTER_AREA,
        )
        for col, (pixels, title) in enumerate(
            [
                (target, "Reference"),
                (old, "Old"),
                (new, "New"),
                (
                    cv2.cvtColor(
                        cv2.imread(str(OUT / f"error-{i}.png")), cv2.COLOR_BGR2RGB
                    ),
                    "New error / fixed scale",
                ),
            ]
        ):
            axes[row, col].imshow(pixels, interpolation="nearest")
            axes[row, col].set_title(f"{i} / {title}")
            axes[row, col].axis("off")
    fig.tight_layout(pad=0.5)
    fig.savefig(OUT / "weak-view-errors.png")
    plt.close(fig)
    fig, axes = plt.subplots(3, 4, figsize=(16, 9), dpi=100)
    for row, view in enumerate((60, 80, 161)):
        n = position[view]
        for col, offset in enumerate((-2, -1, 0, 1)):
            i = sequence[max(0, min(len(sequence) - 1, n + offset))]
            axes[row, col].imshow(load_rgb(photos[i]), interpolation="nearest")
            axes[row, col].set_title(f'ID {i} / {images[i]["name"]}')
            axes[row, col].axis("off")
    fig.tight_layout(pad=0.5)
    fig.savefig(OUT / "temporal-neighbors.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
