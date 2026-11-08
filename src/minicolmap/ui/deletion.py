"""Plan and delete an inactive task's owned files with dependency checks."""
import hashlib
import json
from pathlib import Path
import shutil
import uuid
from .store import read_json
from .worker import atomic_json
from .gpu_lease import active_lease, process_identity


def _within(path, base):
    try:
        return Path(path).resolve().is_relative_to(Path(base).resolve())
    except (OSError, ValueError):
        return False


def plan(store, run_id, live=None):
    root = store.root
    run = store.get(run_id)
    output = Path(run["output_path"])
    blockers = []
    if output.is_symlink() or output.resolve().parent != (root / "outputs").resolve():
        blockers.append("任务输出不在可清理的 outputs 子目录中")
    process = store.processes.get(run_id)
    if run["status"] == "running" or (process and process.poll() is None):
        blockers.append("任务仍在运行，请先停止")
    if live and live.active and live.active["run_id"] == run_id:
        blockers.append("请先关闭此任务的实时渲染")
    for other in store.list_runs():
        if other["id"] == run_id:
            continue
        raw = store.get(other["id"])
        if raw.get("source_run_id") == run_id or any(
            raw.get(key) and _within(raw[key], output)
            for key in ("source_model_path", "dataset_path", "output_path")
        ):
            blockers.append(f"其他任务依赖此结果：{other['name']}")
    for dataset in store.datasets.values():
        if _within(dataset, output):
            blockers.append("输出目录中包含仍在使用的数据集，请先迁移原始数据")
    paths = [
        output,
        store.state / "runs" / f"{run_id}.json",
        store.state / "logs" / f"{run_id}.log",
    ]
    scene_ids = []
    scene_job_ids = []
    for p in (root / "data/semantic_scenes").glob("scene-*/scene.json"):
        scene = read_json(p, {})
        config = root / "configs" / f"{scene.get('config','__missing__')}.json"
        settings = read_json(config, {})
        if (
            scene.get("rgb_run_id") != run_id
            and (root / settings.get("rgb_run", "__missing__")).resolve()
            != output.resolve()
        ):
            continue
        folder = p.parent
        if (
            folder.is_symlink()
            or not _within(folder, root / "data/semantic_scenes")
            or not _within(config, root / "configs")
        ):
            blockers.append("关联语义目录的路径无效")
            continue
        if (
            root / settings.get("workspace", "__missing__")
        ).resolve() != folder.resolve():
            blockers.append("语义目录存在共享或不一致引用")
            continue
        scene_ids.append(scene["id"])
        paths.extend([folder, config])
    for p in (root / "configs").glob("semantic_*.json"):
        config = read_json(p, {})
        if (
            root / config.get("rgb_run", "__missing__")
        ).resolve() == output.resolve() and p not in paths:
            blockers.append(f"模型仍由场景配置使用：{p.stem}")
    for p in (root / ".cache/scene-jobs").glob("*.json"):
        job = read_json(p, {})
        if job.get("scene_id") not in scene_ids:
            continue
        if (
            job.get("status") == "running"
            and process_identity(job.get("pid")) is not None
        ):
            blockers.append("关联场景仍有正在运行的任务")
        scene_job_ids.append(p.stem)
        paths.extend([p, p.with_suffix(".log")])
    for p in (root / "data/object_selections").glob("scene-*/selection.json"):
        if read_json(p, {}).get("source_scene") in scene_ids:
            paths.append(p.parent)
    lease = active_lease(root)
    if lease and any(_within(lease.get("report", ""), p) for p in paths):
        blockers.append("关联文件仍被 GPU 任务使用")
    safe = []
    for p in paths:
        if p.is_symlink() or not _within(p, root):
            blockers.append("清理路径存在符号链接或越界")
            continue
        if p.exists() and not any(_within(p, parent) for parent in safe):
            safe.append(p)
    files = []
    size = 0
    for path in safe:
        entries = path.rglob("*") if path.is_dir() else [path]
        for p in entries:
            if p.is_symlink():
                files.append(
                    (str(p.relative_to(root)), "symlink", p.lstat().st_mtime_ns)
                )
                continue
            if p.is_file():
                stat = p.stat()
                size += stat.st_size
                files.append((str(p.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    payload = dict(
        run_id=run_id,
        run_name=run["name"],
        paths=[str(p.relative_to(root)) for p in safe],
        bytes=size,
        files=len(files),
        scene_ids=scene_ids,
        scene_job_ids=scene_job_ids,
        blockers=sorted(set(blockers)),
        preserves="原始照片与共享数据集",
    )
    payload["token"] = hashlib.sha256(
        json.dumps([payload, sorted(files)], sort_keys=True).encode()
    ).hexdigest()
    return payload


def delete(store, run_id, token, live=None):
    with store.lock:
        value = plan(store, run_id, live)
        if value["blockers"]:
            raise RuntimeError("；".join(value["blockers"]))
        if not isinstance(token, str) or token != value["token"]:
            raise ValueError("任务或文件已变化，请刷新清理预览")
        staging = store.state / "deleting" / uuid.uuid4().hex
        staging.mkdir(parents=True)
        moved = []
        try:
            log = store.logs.pop(run_id, None)
            if log:
                log.close()
            for i, relative in enumerate(value["paths"]):
                source = store.root / relative
                target = staging / str(i)
                source.rename(target)
                moved.append((source, target))
        except OSError:
            for source, target in reversed(moved):
                target.rename(source)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        store.runs.pop(run_id, None)
        store.processes.pop(run_id, None)
        store.image_identities.clear()
        try:
            shutil.rmtree(staging)
            pending = False
        except OSError:
            pending = True
        receipt = {**value, "deleted": True, "cleanup_pending": pending}
        receipt.pop("token", None)
        atomic_json(store.state / "deletions" / f"{staging.name}.json", receipt)
        return receipt
