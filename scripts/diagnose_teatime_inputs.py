"""CPU training-image blur and descriptor-track checks for teatime.

Writes an audit and proposed train-only filter; never removes source images.
The filter is a hypothesis for a later fixed-split A/B run, not proven repair.
"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")
os.environ.setdefault("MPLCONFIGDIR", "/tmp/minicolmap-mpl")
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "teatime_geometry", ROOT / "scripts/diagnose_teatime_quality.py"
)
geo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(geo)
OUT = geo.OUT


def main():
    cv2.setNumThreads(4)
    document = geo.read(geo.RUN / "source_model.json")
    manifest = geo.read(geo.DATA / "dataset_all.json")
    summary = geo.read(geo.RUN / "summary.json")
    images = {i["id"]: i for i in document["images"]}
    inputs = {i["id"]: i for i in manifest["images"]}
    poses = {
        p["image_id"]: (np.array(p["R"]), np.array(p["t"])) for p in document["poses"]
    }
    cameras = {c["id"]: c for c in document["cameras"]}
    photos = {i: geo.DATA / inputs[i]["path"] for i in images}
    sequence = sorted(images, key=lambda i: images[i]["name"])
    position = {i: n for n, i in enumerate(sequence)}
    train = set(summary["train_ids"])
    validation = set(summary["validation_ids"])
    quality = []
    greys = {}
    for i in sequence:
        gray = cv2.imread(str(photos[i]), cv2.IMREAD_GRAYSCALE)
        greys[i] = gray
        small = cv2.resize(gray, (494, 365), interpolation=cv2.INTER_AREA)
        gx = cv2.Sobel(small, cv2.CV_64F, 1, 0)
        gy = cv2.Sobel(small, cv2.CV_64F, 0, 1)
        quality.append(
            {
                "image_id": i,
                "name": images[i]["name"],
                "split": "train" if i in train else "validation",
                "laplacian_variance": float(cv2.Laplacian(small, cv2.CV_64F).var()),
                "mean_luminance": float(small.mean()),
                "gradient_x_energy": float((gx**2).mean()),
                "gradient_y_energy": float((gy**2).mean()),
            }
        )
    by_id = {q["image_id"]: q for q in quality}
    train_values = [q["laplacian_variance"] for q in quality if q["image_id"] in train]
    cutoff = float(np.percentile(train_values, 20))
    for q in quality:
        n = position[q["image_id"]]
        neighbors = [
            by_id[sequence[m]]["laplacian_variance"]
            for m in range(max(0, n - 3), min(len(sequence), n + 4))
            if sequence[m] in train and m != n
        ]
        q["train_neighbor_median"] = float(np.median(neighbors))
        q["relative_to_train_neighbors"] = q["laplacian_variance"] / max(
            q["train_neighbor_median"], 1e-6
        )
        q["train_blur_candidate"] = bool(
            q["image_id"] in train
            and q["relative_to_train_neighbors"] < 0.4
            and q["laplacian_variance"] < cutoff
        )
    candidates = [q for q in quality if q["train_blur_candidate"]]
    print(
        json.dumps(
            {
                "laplacian_train_p20": cutoff,
                "candidate_ids": [q["image_id"] for q in candidates],
                "candidates": candidates,
            }
        ),
        flush=True,
    )
    # SIFT mutual-ratio tracks tolerate the larger changes where LK had few tracks.
    sift = cv2.SIFT_create(nfeatures=4500)
    cache = {}
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    tracks = []
    pairs = set()
    for v in (60, 80, 100, 161):
        n = position[v]
        for offset in (-2, -1, 1, 2):
            if 0 <= n + offset < len(sequence):
                pairs.add((sequence[min(n, n + offset)], sequence[max(n, n + offset)]))
    for i, j in sorted(pairs):
        for v in (i, j):
            if v not in cache:
                cache[v] = sift.detectAndCompute(greys[v], None)
        ki, di = cache[i]
        kj, dj = cache[j]
        if di is None or dj is None:
            continue
        forward = matcher.knnMatch(di, dj, k=2)
        reverse = matcher.knnMatch(dj, di, k=2)
        f = {
            a.queryIdx: a.trainIdx
            for pair in forward
            if len(pair) == 2
            for a, b in [pair]
            if a.distance < 0.7 * b.distance
        }
        b = {
            a.queryIdx: a.trainIdx
            for pair in reverse
            if len(pair) == 2
            for a, c in [pair]
            if a.distance < 0.7 * c.distance
        }
        match = [(a, v) for a, v in f.items() if b.get(v) == a]
        if not match:
            continue
        x = np.array([ki[a].pt for a, _ in match])
        y = np.array([kj[b].pt for _, b in match])
        K1 = np.array(cameras[images[i]["camera_id"]]["K"])
        K2 = np.array(cameras[images[j]["camera_id"]]["K"])
        F = geo.fundamental(K1, *poses[i], K2, *poses[j])
        errors = geo.epipolar_distance(F, x, y)
        bad = errors > 5
        row = {
            "source_id": i,
            "target_id": j,
            "source_name": images[i]["name"],
            "target_name": images[j]["name"],
            "tracks": len(x),
            "epipolar_px": geo.stats(errors),
            "outliers_over_5px": int(bad.sum()),
            "outliers_upper_half": int(np.sum(bad & (x[:, 1] < greys[i].shape[0] / 2))),
            "outliers_lower_half": int(
                np.sum(bad & (x[:, 1] >= greys[i].shape[0] / 2))
            ),
            "source_xy": x.tolist(),
            "target_xy": y.tolist(),
            "distance_px": errors.tolist(),
        }
        tracks.append(row)
        a = cv2.cvtColor(greys[i], cv2.COLOR_GRAY2BGR)
        for point, d in zip(x, errors):
            cv2.circle(
                a,
                tuple(np.round(point).astype(int)),
                3,
                (220, 30, 220) if d > 5 else (30, 180, 30),
                -1,
            )
        cv2.imwrite(str(OUT / f"sift-{i}-{j}.jpg"), a)
    result = {
        "status": "completed",
        "device": "cpu",
        "image_quality": quality,
        "laplacian_train_p20": cutoff,
        "blur_candidates": candidates,
        "proposed_excluded_train_ids": [q["image_id"] for q in candidates],
        "validation_ids_unchanged": summary["validation_ids"],
        "candidate_rule": "Train only; 494x365 Laplacian variance below train-only 20th percentile AND below 0.4 times median of filename-neighbor training views within +-3. Content-dependent proxy; visual review required.",
        "sift_epipolar_tracks": tracks,
        "scope": "Post-hoc CPU diagnosis only. Image names establish order; COLMAP IDs are not temporal indices. Epipolar violations alone do not prove motion. No training performed and no original files modified.",
    }
    geo.save(OUT / "input-quality.json", result)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    shown = (
        candidates
        or sorted(
            [q for q in quality if q["image_id"] in train],
            key=lambda q: q["laplacian_variance"],
        )[:8]
    )
    n = (len(shown) + 3) // 4
    fig, axes = plt.subplots(n, 4, figsize=(16, 3 * n), dpi=100, squeeze=False)
    for ax, q in zip(axes.flat, shown):
        ax.imshow(geo.load_rgb(photos[q["image_id"]]), interpolation="nearest")
        ax.set_title(
            f'ID {q["image_id"]} / {q["name"]}\nLaplacian {q["laplacian_variance"]:.1f}, relative {q["relative_to_train_neighbors"]:.2f}'
        )
        ax.axis("off")
    for ax in list(axes.flat)[len(shown) :]:
        ax.axis("off")
    fig.tight_layout(pad=0.5)
    fig.savefig(OUT / "blur-candidates.png")
    plt.close(fig)
    print(
        json.dumps(
            {
                "status": "completed",
                "sift_pairs": len(tracks),
                "candidate_ids": result["proposed_excluded_train_ids"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
