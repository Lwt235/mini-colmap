"""CPU-only audit of saved teatime retraining artifacts, on a common PNG grid.

Does not initialize CUDA, rerender checkpoints, retrain or change saved runs.
PNG metrics are clipped/quantized and are distinct from training float metrics.
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
import torch
from minicolmap.rendering.gaussian_training import ssim_image

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / "outputs/ui_3dgs_20261020_104007_c41d30"
NEW = ROOT / "outputs/ui_3dgs_20261024_112609_a1d94a"
OUT = ROOT / "reports/teatime-retrain-check-20261026"


def read(path):
    return json.loads(path.read_text())


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image(path):
    raw = cv2.imread(str(path))
    if raw is None:
        raise ValueError(f"Missing image {path}")
    return cv2.cvtColor(raw, cv2.COLOR_BGR2RGB).astype(np.float32) / 255


def metrics(prediction, target):
    error = float(np.mean((prediction - target) ** 2, dtype=np.float64))
    return {
        "mse": error,
        "psnr": -10 * math.log10(max(error, 1e-12)),
        "ssim_valid": float(
            ssim_image(torch.from_numpy(prediction), torch.from_numpy(target))
        ),
    }


def main():
    torch.set_num_threads(4)
    OUT.mkdir(parents=True, exist_ok=True)
    summaries = {
        name: read(folder / "summary.json")
        for name, folder in [("old", OLD), ("new", NEW)]
    }
    configs = {
        name: read(folder / "config.json")
        for name, folder in [("old", OLD), ("new", NEW)]
    }
    assert summaries["old"]["validation_ids"] == summaries["new"]["validation_ids"]
    assert summaries["old"]["train_ids"] == summaries["new"]["train_ids"]
    assert sha(OLD / "source_model.json") == sha(NEW / "source_model.json")
    rows = []
    for image_id in summaries["old"]["validation_ids"]:
        old = image(OLD / f"validation/image-{image_id}.png")
        new_native = image(NEW / f"validation/image-{image_id}.png")
        target = image(OLD / f"previews/reference-{image_id}.png")
        native_target = image(NEW / f"previews/reference-{image_id}.png")
        h, w = target.shape[:2]
        new = cv2.resize(new_native, (w, h), interpolation=cv2.INTER_AREA)
        reduced_target = cv2.resize(native_target, (w, h), interpolation=cv2.INTER_AREA)
        assert np.max(np.abs(reduced_target - target)) <= 1 / 255 + 1e-6
        row = {
            "image_id": image_id,
            "width": w,
            "height": h,
            "old": metrics(old, target),
            "new": metrics(new, target),
        }
        row["delta_psnr"] = row["new"]["psnr"] - row["old"]["psnr"]
        row["delta_ssim"] = row["new"]["ssim_valid"] - row["old"]["ssim_valid"]
        rows.append(row)
    result = {
        "status": "passed",
        "device": "cpu",
        "scope": "Saved clipped 8-bit validation PNGs; new prediction area-downsampled to old 494x365 grid; same 8 held-out views; NOT native-grid rerendering or official benchmark metrics",
        "per_view": rows,
        "config_changes": {},
        "checks": {
            "same_train_views": True,
            "same_validation_views": True,
            "identical_source_model": True,
            "downsampled_references_match_within_one_code_value": True,
        },
        "runs": {},
    }
    for name, folder in [("old", OLD), ("new", NEW)]:
        summary = summaries[name]
        mse = float(np.mean([row[name]["mse"] for row in rows]))
        result[name] = {
            "psnr": -10 * math.log10(max(mse, 1e-12)),
            "mean_view_psnr": float(np.mean([row[name]["psnr"] for row in rows])),
            "ssim_valid": float(np.mean([row[name]["ssim_valid"] for row in rows])),
        }
        checkpoint = torch.load(
            folder / "checkpoint.pt", map_location="cpu", weights_only=True
        )
        assert checkpoint["step"] == 30000
        assert checkpoint["config"] == configs[name]
        assert checkpoint["train_ids"] == summary["train_ids"]
        assert checkpoint["validation_ids"] == summary["validation_ids"]
        expected = {
            "means": (summary["point_count"], 3),
            "log_scales": (summary["point_count"], 3),
            "quaternions": (summary["point_count"], 4),
            "opacity_logits": (summary["point_count"],),
            "sh_coeffs": (summary["point_count"], 16, 3),
        }
        for key, shape in expected.items():
            value = checkpoint["state"][key]
            assert tuple(value.shape) == shape and torch.isfinite(value).all(), key
        assert (checkpoint["state"]["quaternions"].norm(dim=-1) > 0).all()
        opacity = checkpoint["state"]["opacity_logits"].sigmoid()
        density = read(folder / "density.json")
        saturated = next(
            (
                r["step"]
                for r in density
                if r["after"] == configs[name]["gaussian"]["max_gaussians"]
            ),
            None,
        )
        result["runs"][name] = {
            "directory": str(folder.relative_to(ROOT)),
            "checkpoint_sha256": sha(folder / "checkpoint.pt"),
            "completed_step": checkpoint["step"],
            "gaussians": summary["point_count"],
            "finite_parameters": True,
            "first_capacity_step": saturated,
            "last_density_step": density[-1]["step"],
            "opacity_median": float(opacity.median()),
            "opacity_max": float(opacity.max()),
            "reported_native_metrics": summary["metrics"],
            "reported_training_seconds": summary["elapsed_seconds"],
            "checkpoint_mb": (folder / "checkpoint.pt").stat().st_size / 1024**2,
            "peak_allocated_mb": summary.get("peak_cuda_allocated_mb"),
            "peak_reserved_mb": summary.get("peak_cuda_reserved_mb"),
        }
        del checkpoint, opacity
    for key, value in configs["new"].items():
        if key != "gaussian" and value != configs["old"][key]:
            result["config_changes"][key] = {"old": configs["old"][key], "new": value}
    for key, value in configs["new"]["gaussian"].items():
        if value != configs["old"]["gaussian"][key]:
            result["config_changes"]["gaussian." + key] = {
                "old": configs["old"]["gaussian"][key],
                "new": value,
            }
    result["delta"] = {
        "psnr": result["new"]["psnr"] - result["old"]["psnr"],
        "ssim_valid": result["new"]["ssim_valid"] - result["old"]["ssim_valid"],
        "improved_psnr_views": sum(r["delta_psnr"] > 0 for r in rows),
        "improved_ssim_views": sum(r["delta_ssim"] > 0 for r in rows),
    }
    (OUT / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Show every validation view, avoiding a single flattering camera.
    fig, axes = plt.subplots(len(rows), 3, figsize=(12, 3 * len(rows)), dpi=120)
    for axisrow, row in zip(axes, rows):
        image_id = row["image_id"]
        h, w = row["height"], row["width"]
        visuals = [
            image(OLD / f"previews/reference-{image_id}.png"),
            image(OLD / f"validation/image-{image_id}.png"),
            cv2.resize(
                image(NEW / f"validation/image-{image_id}.png"),
                (w, h),
                interpolation=cv2.INTER_AREA,
            ),
        ]
        for ax, pixels, label in zip(
            axisrow, visuals, ["Reference", "Old 100k", "New 1M"]
        ):
            ax.imshow(pixels, interpolation="nearest")
            ax.axis("off")
            ax.set_title(f"View {image_id} / {label}", fontsize=10)
    fig.tight_layout(pad=0.5)
    fig.savefig(OUT / "all-views.png")
    plt.close(fig)
    # Larger native-size crop comparison of the first camera and its weak area.
    frame = 20
    original = image(NEW / f"previews/reference-{frame}.png")
    new = image(NEW / f"validation/image-{frame}.png")
    old = cv2.resize(
        image(OLD / f"validation/image-{frame}.png"),
        (original.shape[1], original.shape[0]),
        interpolation=cv2.INTER_LINEAR,
    )
    fig, axes = plt.subplots(2, 3, figsize=(15, 8), dpi=110)
    for col, (pixels, label) in enumerate(
        [(original, "Reference"), (old, "Old (upsampled for display)"), (new, "New")]
    ):
        axes[0, col].imshow(pixels, interpolation="nearest")
        axes[0, col].set_title(label)
        axes[1, col].imshow(pixels[420:710, 320:910], interpolation="nearest")
        axes[1, col].set_title("Under-table detail")
    for ax in axes.flat:
        ax.axis("off")
    fig.tight_layout(pad=0.5)
    fig.savefig(OUT / "comparison.png")
    plt.close(fig)
    print(
        json.dumps(
            {k: result[k] for k in ("status", "old", "new", "delta", "runs")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
