"""Read-only rendering API helpers; importing this module never imports torch."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath

import cv2
import numpy as np

from ..io import load_reconstruction


def preview_worker_main(connection, output, body):
    """One-shot CUDA PNG render; CUDA ownership stays outside the web process."""
    try:
        import os
        from .gpu import worker_environment
        os.environ.update(worker_environment(Path(output).parent.parent))
        from .runtime import configure_worker_runtime
        from ..rendering.pipeline import render_checkpoint
        configure_worker_runtime()
        preview = render_checkpoint(output, camera=body.get('camera'), image_id=body.get('image_id'),
                                    device='cuda', max_edge=1024)
        connection.send({'type': 'preview', 'preview': preview})
    except Exception as exc:
        connection.send({'type': 'error', 'message': f'{type(exc).__name__}: {exc}'})
    finally:
        connection.close()


def capabilities():
    from ..rendering.config import capabilities as rendering_capabilities
    return rendering_capabilities()


def validate_source(store, source_run_id, method="nerf", photo_dataset_id=None):
    """Read posed photos; only Gaussian initialization requires sparse points."""
    source = store.get(source_run_id)
    if source.get("pipeline", "sfm") != "sfm":
        raise ValueError("训练输入必须是 SfM 结果，不能使用已有训练任务")
    if source["status"] not in ("completed", "imported"):
        raise ValueError("请先完成 SfM 重建，再启动训练")
    model_path = Path(source["output_path"]) / "model.json"
    if not model_path.is_file():
        raise ValueError("源任务没有最终有效 model.json")
    model = load_reconstruction(model_path)
    if len(model.poses) < 2:
        raise ValueError("训练至少需要两张具有有效位姿和原图的图像")
    if method == "3dgs" and not model.points:
        raise ValueError("3DGS 需要有效稀疏点云用于初始化，请先构建或加载点云")
    matched = store.dataset(photo_dataset_id) if photo_dataset_id is not None else store.image_dataset(source)
    if matched is None:
        raise ValueError("源结果缺少对应原始照片，无法训练")
    manifest, dataset = matched
    validate_model_images(model, dataset)
    return source, model_path, manifest, model


def validate_model_images(model, dataset):
    """The selected photos must have exactly the model's image/calibration IDs."""
    expected = {image_id: (image.name, image.camera_id) for image_id, image in model.images.items()}
    actual = {image_id: (image.name, image.camera_id) for image_id, image in dataset["images"].items()}
    if actual != expected:
        raise ValueError("原始数据集的图像 ID、名称或相机分配与源模型不一致")
    for camera_id, camera in model.cameras.items():
        other = dataset["cameras"].get(camera_id)
        if (other is None or (camera.width, camera.height) != (other.width, other.height)
                or not np.allclose(camera.K, other.K, rtol=0, atol=1e-9)):
            raise ValueError("原始数据集内参或图像尺寸与源模型不一致")
    for image_id, path in dataset["image_paths"].items():
        pixels = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        camera = dataset["cameras"][dataset["images"][image_id].camera_id]
        if pixels is None or pixels.shape[:2] != (camera.height, camera.width):
            raise ValueError(f"原始照片尺寸与内参不一致或不能解码：{path.name}")


def artifact_url(run_id: str, output: Path, raw):
    """Only expose real, contained result files, including on Windows."""
    if not isinstance(raw, str) or not raw:
        return None
    relative = Path(raw.replace("\\", "/"))
    windows = PureWindowsPath(raw)
    candidate = (output / relative).resolve()
    if (windows.drive or windows.root or relative.is_absolute() or ".." in relative.parts
            or not candidate.is_relative_to(output.resolve()) or "ui" in relative.parts
            or candidate.suffix.lower() not in {".png", ".json", ".npz", ".pt", ".ply"}
            or not candidate.is_file()):
        return None
    return f"/api/runs/{run_id}/artifacts/{relative.as_posix()}"


def public_preview(run_id: str, output: Path, preview):
    if not isinstance(preview, dict):
        return None
    url = artifact_url(run_id, output, preview.get("path"))
    if url is None:
        return None
    result = {key: preview[key] for key in ("step", "image_id") if key in preview}
    result["url"] = url
    reference = artifact_url(run_id, output, preview.get("reference_path"))
    if reference:
        result["reference_url"] = reference
    return result


def result(store, run_id):
    from .store import read_json
    run = store.get(run_id)
    if run.get("pipeline") not in ("nerf", "3dgs"):
        raise ValueError("该任务不是 NeRF 或 3DGS 训练")
    output = Path(run["output_path"])
    summary = read_json(output / "summary.json", {})
    training = read_json(output / "training.json", {})
    previews = list(summary.get("previews", training.get("previews", [])))
    latest = run.get("latest_preview_record")
    if latest and latest not in previews:
        previews.append(latest)
    public = {"pipeline": run["pipeline"], "status": run["status"],
              "source_run_id": run.get("source_run_id"),
              "source_kind": run.get("source_kind", "sfm_result"),
              "source_dataset_id": run.get("source_dataset_id"),
              "config": run.get("config", {}), "error": run.get("error"),
              "metrics": run.get("training_metrics", summary.get("metrics", {})),
              "previews": [item for preview in previews
                           if (item := public_preview(run_id, output, preview)) is not None]}
    for key, filename in (("checkpoint_url", summary.get("checkpoint", "checkpoint.pt")),
                          ("gaussians_url", summary.get("gaussians", "gaussians.npz"))):
        url = artifact_url(run_id, output, filename)
        if url:
            public[key] = url
    return public
