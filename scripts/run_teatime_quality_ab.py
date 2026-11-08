"""Prepare/run a seed-0 fixed-split teatime baseline vs reviewed blur filter.

Preparation is CPU-only. --run starts only on a free GPU (7 -> 0), rechecks
before CUDA, uses the project lease and never modifies existing trained models.
Filtering is confined to a worker-local TrainingScene; production code is intact.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs/ui_3dgs_20261024_112609_a1d94a"
DATASET = ROOT / "data/lerf_ovs/teatime/dataset_all.json"
DEFAULT = ROOT / "reports/teatime-quality-diagnosis-20261026/ab-experiment"
# Visual review of INPUT photographs only; automatic Laplacian candidates are
# not automatically accepted. IDs are verified against their exact filenames.
REVIEWED = {108: "frame_00145.jpg", 174: "frame_00177.jpg", 172: "frame_00178.jpg"}


def read(path):
    return json.loads(path.read_text())


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write(path, value):
    from minicolmap.ui.worker import atomic_json

    atomic_json(path, value)


def prepare(report):
    original = read(SOURCE / "summary.json")
    config = read(SOURCE / "config.json")
    manifest = read(DATASET)
    images = {i["id"]: i for i in manifest["images"]}
    for i, name in REVIEWED.items():
        if i not in original["train_ids"] or images[i]["name"] != name:
            raise ValueError("Reviewed IDs do not match original training inputs")
    training = set(original["train_ids"])
    validation = set(original["validation_ids"])
    if training & validation or set(REVIEWED) & validation:
        raise ValueError("Train/validation separation violated")
    versions = {
        str(p.relative_to(ROOT)): sha(p)
        for p in sorted((ROOT / "src/minicolmap/rendering").glob("*.py"))
    }
    inputs = {i["name"]: sha(DATASET.parent / i["path"]) for i in manifest["images"]}
    plan = {
        "schema_version": 1,
        "scope": "Development-set fixed-geometry seed-0 comparison; not an independent test or official upstream training.",
        "source_model": str(SOURCE / "source_model.json"),
        "source_model_sha256": sha(SOURCE / "source_model.json"),
        "dataset": str(DATASET),
        "dataset_sha256": sha(DATASET),
        "image_sha256": inputs,
        "config": config,
        "validation_ids": original["validation_ids"],
        "original_train_ids": original["train_ids"],
        "normalization": read(SOURCE / "training_scene.json")["normalization"],
        "source_versions": versions,
        "launcher_sha256": sha(Path(__file__)),
        "reviewed_blurry_train_images": REVIEWED,
        "variants": {
            "baseline": {"excluded_train_ids": []},
            "reviewed_blur_filter": {"excluded_train_ids": sorted(REVIEWED)},
        },
        "normalization_policy": "Compute original scene/normalization from all original train IDs, then filter ONLY train_ids. Keep all frames, source points and validation IDs unchanged.",
        "warning": "Filtering may reduce coverage and can worsen results. No quality gain is assumed. No deblurring or replacement of original photos. Single-seed differences require caution.",
    }
    signature = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    plan["signature"] = signature
    plan["outputs"] = {
        name: str(ROOT / "outputs" / f"teatime_quality_{name}_{signature[:12]}")
        for name in plan["variants"]
    }
    path = report / "plan.json"
    if path.exists() and read(path) != plan:
        raise ValueError("Existing plan differs; choose a new --report directory")
    write(path, plan)
    return plan


def verify_inputs(plan):
    if (
        sha(SOURCE / "source_model.json") != plan["source_model_sha256"]
        or sha(DATASET) != plan["dataset_sha256"]
    ):
        raise ValueError("Source geometry/dataset changed")
    for name, value in plan["source_versions"].items():
        if sha(ROOT / name) != value:
            raise ValueError(f"Source code changed: {name}")
    manifest = read(DATASET)
    for item in manifest["images"]:
        if sha(DATASET.parent / item["path"]) != plan["image_sha256"][item["name"]]:
            raise ValueError("Input photo changed")
    if sha(Path(__file__)) != plan["launcher_sha256"]:
        raise ValueError("Experiment launcher changed")


def filter_scene(scene, excluded, expected_validation):
    if scene.validation_ids != expected_validation:
        raise ValueError("Validation split changed")
    if set(excluded) & set(scene.validation_ids) or not set(excluded) <= set(
        scene.train_ids
    ):
        raise ValueError("Only original training views may be excluded")
    scene.train_ids = [i for i in scene.train_ids if i not in set(excluded)]
    if len(scene.train_ids) < 2:
        raise ValueError("Not enough training views")
    return scene


def worker(args):
    from check_lerf_rgb_renderers import idle_devices
    from minicolmap.ui import gpu_lease

    plan = read(args.report / "plan.json")
    verify_inputs(plan)
    if str(args.gpu) != os.environ.get("CUDA_VISIBLE_DEVICES"):
        raise ValueError("Worker GPU visibility differs")
    if args.gpu not in idle_devices():
        raise RuntimeError("Selected physical GPU became busy; no CUDA initialized")
    token = gpu_lease.adopt(ROOT, os.environ.get("MINICOLMAP_QUERY_RESERVATION"))
    try:
        from minicolmap.rendering import pipeline
        from minicolmap.rendering.data import load_training_scene

        output = Path(plan["outputs"][args.variant])
        if output.exists():
            raise ValueError("Refusing to overwrite existing output")
        output.mkdir(parents=True)
        excluded = plan["variants"][args.variant]["excluded_train_ids"]

        def fixed_scene(source, dataset, config):
            scene = load_training_scene(source, dataset, config)
            if scene.train_ids != plan["original_train_ids"]:
                raise ValueError("Original training split changed")
            norm = plan["normalization"]
            import numpy as np

            if not np.allclose(
                scene.center, norm["center"], atol=1e-12, rtol=0
            ) or not np.isclose(scene.scale, norm["scale"], atol=1e-12, rtol=0):
                raise ValueError("Scene normalization changed")
            return filter_scene(scene, excluded, plan["validation_ids"])

        pipeline.load_training_scene = fixed_scene
        write(
            output / "training_override.json",
            {
                "signature": plan["signature"],
                "variant": args.variant,
                "excluded_train_ids": excluded,
                "validation_ids": plan["validation_ids"],
                "physical_gpu": args.gpu,
                "normalization_policy": plan["normalization_policy"],
            },
        )

        def event(value):
            if value.get("kind") == "preview" or value.get("completed", 0) % 1000 == 0:
                print(json.dumps(value, ensure_ascii=False), flush=True)

        result = pipeline.train(
            SOURCE / "source_model.json",
            DATASET,
            output,
            copy.deepcopy(plan["config"]),
            on_event=event,
        )
        write(
            args.report / f"{args.variant}-result.json",
            {
                "status": "completed",
                "signature": plan["signature"],
                "physical_gpu": args.gpu,
                "output": str(output),
                "summary": result,
            },
        )
    finally:
        gpu_lease.release(ROOT, token)


def run(plan, report):
    from check_lerf_rgb_renderers import idle_devices, guard_workbench
    from minicolmap.ui import gpu_lease

    records = []
    for variant in plan["variants"]:
        result = report / f"{variant}-result.json"
        if result.exists():
            old = read(result)
            if old.get("signature") != plan["signature"]:
                raise ValueError("Previous result differs")
            records.append({"variant": variant, "status": "reused"})
            continue
        verify_inputs(plan)
        free = idle_devices()
        if not free:
            value = {
                "status": "skipped_all_gpus_busy",
                "completed": records,
                "pending_variants": [
                    v
                    for v in plan["variants"]
                    if not (report / f"{v}-result.json").exists()
                ],
                "message": "No training started on occupied GPUs; no background queue was created.",
            }
            write(report / "execution.json", value)
            print(json.dumps(value))
            return
        guard_workbench()
        gpu = free[0]
        token = gpu_lease.acquire(ROOT, report)
        try:
            environment = dict(
                os.environ,
                CUDA_VISIBLE_DEVICES=str(gpu),
                OMP_NUM_THREADS="4",
                OPENBLAS_NUM_THREADS="4",
                MINICOLMAP_QUERY_RESERVATION=token,
                TORCH_EXTENSIONS_DIR=str(ROOT / ".cache/torch_extensions"),
            )
            with (report / f"{variant}.log").open("wb") as stream:
                process = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--report",
                        str(report),
                        "--variant",
                        variant,
                        "--gpu",
                        str(gpu),
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
            if process.returncode:
                raise RuntimeError(f"{variant} failed; see {variant}.log")
            records.append(
                {"variant": variant, "status": "completed", "physical_gpu": gpu}
            )
            write(
                report / "execution.json", {"status": "running", "completed": records}
            )
        finally:
            gpu_lease.release(ROOT, token)
    write(report / "execution.json", {"status": "completed", "completed": records})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--variant",
        choices=["baseline", "reviewed_blur_filter"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--gpu", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.report = args.report.resolve()
    args.report.mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args)
        return
    plan = prepare(args.report)
    if args.run:
        run(plan, args.report)
    else:
        print(
            json.dumps(
                {
                    "status": "prepared",
                    "signature": plan["signature"],
                    "original_train": len(plan["original_train_ids"]),
                    "filtered_train": len(plan["original_train_ids"]) - len(REVIEWED),
                    "validation": plan["validation_ids"],
                    "report": str(args.report),
                }
            )
        )


if __name__ == "__main__":
    main()
