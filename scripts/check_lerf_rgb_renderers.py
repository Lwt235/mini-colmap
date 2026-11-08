"""Cross-render existing LERF RGB models; no training and no checkpoint changes.

Select an idle physical GPU in descending index order and compare the project
and pinned Graphdeco forward rasterizers at the SAME held-out cameras/grid.
This measures renderer differences, not the quality of retraining upstream.
"""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def idle_devices():
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,utilization.gpu,memory.used",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    processes = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    occupied = {
        row[0].strip()
        for row in csv.reader(io.StringIO(processes.stdout))
        if len(row) == 2
    }
    rows = []
    for row in csv.reader(io.StringIO(result.stdout)):
        index, uuid, utilization, used = [v.strip() for v in row]
        if uuid not in occupied and int(utilization) == 0 and int(used) < 64:
            rows.append(int(index))
    return sorted(rows, reverse=True)


def guard_workbench():
    for p in (ROOT / ".cache/mini-colmap-ui/runs").glob("*.json"):
        if json.loads(p.read_text()).get("status") == "running":
            raise RuntimeError("A workbench task is running")
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8765/api/demo", timeout=5
        ) as response:
            value = json.load(response)
        if value.get("gpu_busy") or value.get("offline_gpu_busy"):
            raise RuntimeError("Workbench rendering or another GPU task is active")
    except urllib.error.URLError as exc:
        if isinstance(exc, urllib.error.HTTPError):
            raise
        # A stopped web service does not prevent the process/file GPU lease.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=ROOT / "reports/lerf-rgb-diagnosis-20261024"
    )
    args = parser.parse_args()
    from minicolmap.ui import gpu_lease
    from minicolmap.ui.worker import atomic_json

    args.report.mkdir(parents=True, exist_ok=True)
    guard_workbench()
    free = idle_devices()
    if not free:
        atomic_json(
            args.report / "renderer-check.json",
            {
                "status": "skipped",
                "reason": "All GPUs are in use; no CUDA process started",
            },
        )
        print("All GPUs are in use; skipped")
        return
    gpu = free[0]
    token = gpu_lease.acquire(ROOT, args.report)
    try:
        # Another user can start work between inventory calls; recheck before CUDA.
        if gpu not in idle_devices():
            raise RuntimeError(
                "Selected GPU became occupied; rerun after checking availability"
            )
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu)
        os.environ.setdefault("OMP_NUM_THREADS", "4")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")
        import numpy as np
        import torch
        import experiment_official_3dgs as upstream
        from minicolmap.rendering.data import load_training_scene
        from minicolmap.rendering.live import CudaRenderer
        from minicolmap.rendering.pipeline import _write_image
        from minicolmap.rendering.gaussian_training import ssim_image
        from minicolmap.rendering.config import resolve_render_config

        torch.set_num_threads(4)
        assert torch.cuda.device_count() == 1
        _, official_render, _, projection, _ = upstream.upstream_import()
        options = upstream.types.SimpleNamespace(
            debug=False,
            antialiasing=False,
            compute_cov3D_python=False,
            convert_SHs_python=False,
        )
        models = {
            "teatime": "ui_3dgs_20261020_104007_c41d30",
            "figurines": "ui_3dgs_20261022_171409_43d932",
        }
        results = {
            "status": "running",
            "physical_gpu": gpu,
            "upstream_revision": json.loads(
                (upstream.UPSTREAM / "build.json").read_text()
            )["revision"],
            "scope": "Immutable project weights, same held-out cameras and training grid; NOT an upstream retraining comparison",
            "scenes": {},
        }
        for name, folder in models.items():
            output = ROOT / "outputs" / folder
            config = resolve_render_config(
                json.loads((output / "config.json").read_text())
            )
            dataset = ROOT / "data/lerf_ovs" / name / "dataset_all.json"
            scene = load_training_scene(output / "source_model.json", dataset, config)
            before = upstream.sha(output / "checkpoint.pt")
            renderer = CudaRenderer(output)
            center = torch.as_tensor(scene.center, device="cuda", dtype=torch.float32)
            state = {k: v.detach() for k, v in renderer.state.items()}
            state["means"] = state["means"] * scene.scale + center
            state["log_scales"] = state["log_scales"] + math.log(scene.scale)
            proxy = upstream.official_proxy(state, renderer.degree)
            rows = []
            with torch.inference_mode():
                for image_id in scene.validation_ids:
                    frame = scene.frames[image_id]
                    world = copy.deepcopy(frame)
                    world.t = frame.t * scene.scale - frame.R @ scene.center
                    camera = upstream.camera_for(world, projection)
                    project_camera = {
                        "width": frame.width,
                        "height": frame.height,
                        "K": frame.K.tolist(),
                        "R": world.R.tolist(),
                        "t": world.t.tolist(),
                    }
                    local = renderer.render_rgb(project_camera).clamp(0, 1)
                    official = (
                        official_render(camera, proxy, options, renderer.background)[
                            "render"
                        ]
                        .permute(1, 2, 0)
                        .clamp(0, 1)
                    )
                    target = torch.as_tensor(
                        frame.rgb, device="cuda", dtype=torch.float32
                    )
                    row = {
                        "image_id": image_id,
                        "width": frame.width,
                        "height": frame.height,
                        "renderer_mse": float((local - official).square().mean()),
                        "renderer_max_abs": float((local - official).abs().max()),
                    }
                    for label, rgb in [("project", local), ("graphdeco", official)]:
                        mse = float((rgb - target).square().mean())
                        row[label] = {
                            "mse": mse,
                            "psnr": -10 * math.log10(max(mse, 1e-12)),
                            "ssim_valid": float(ssim_image(rgb, target)),
                        }
                        _write_image(
                            args.report / name / f"{image_id:06d}-{label}.png",
                            rgb.cpu().numpy(),
                        )
                    _write_image(
                        args.report / name / f"{image_id:06d}-reference.png", frame.rgb
                    )
                    rows.append(row)
            item = {
                "checkpoint_sha256": before,
                "gaussians": len(state["means"]),
                "views": len(rows),
                "per_view": rows,
            }
            for label in ("project", "graphdeco"):
                item[label] = {
                    "psnr": -10
                    * math.log10(max(np.mean([r[label]["mse"] for r in rows]), 1e-12)),
                    "ssim_valid": float(
                        np.mean([r[label]["ssim_valid"] for r in rows])
                    ),
                }
            item["renderer_psnr"] = -10 * math.log10(
                max(np.mean([r["renderer_mse"] for r in rows]), 1e-12)
            )
            assert before == upstream.sha(
                output / "checkpoint.pt"
            ), "Checkpoint was modified"
            results["scenes"][name] = item
            atomic_json(args.report / "renderer-check.json", results)
            print(
                json.dumps(
                    {
                        "scene": name,
                        **{
                            k: item[k]
                            for k in (
                                "gaussians",
                                "views",
                                "project",
                                "graphdeco",
                                "renderer_psnr",
                            )
                        },
                    }
                ),
                flush=True,
            )
            del renderer, state, proxy, center, local, official, target
            torch.cuda.empty_cache()
        results["status"] = "completed"
        atomic_json(args.report / "renderer-check.json", results)
    finally:
        gpu_lease.release(ROOT, token)


if __name__ == "__main__":
    main()
