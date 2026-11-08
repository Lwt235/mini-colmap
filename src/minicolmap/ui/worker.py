"""Subprocess entry point; delegates every numerical operation to the existing pipeline."""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
import uuid
from contextlib import suppress
from pathlib import Path


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    # Windows can briefly deny replacement while HTTP/SSE readers have the
    # previous file open. Only retry that transient sharing failure; preserve
    # atomic replacement and propagate persistent or unrelated I/O errors.
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        for attempt in range(21):
            try:
                temp.replace(path)
                return
            except PermissionError:
                if attempt == 20:
                    raise
                time.sleep(min(0.01 * 2 ** attempt, 0.1))
    finally:
        with suppress(OSError):
            temp.unlink(missing_ok=True)


def main(metadata: str) -> int:
    from .runtime import configure_worker_runtime
    runtime = configure_worker_runtime()
    cpu_started = time.process_time()
    state_path = Path(metadata)
    run = json.loads(state_path.read_text(encoding="utf-8"))
    run['runtime'] = runtime
    output = Path(run["output_path"])
    ui_dir = output / "ui"
    started = time.monotonic()
    seq = 0

    def publish_event(value):
        nonlocal seq
        record = dict(value)
        model = record.pop("model", None)
        ui_dir.mkdir(parents=True, exist_ok=True)
        if model is not None:
            snapshot_id = f"snapshot-{len(run['snapshots']) + 1:04d}"
            atomic_json(ui_dir / "snapshots" / f"{snapshot_id}.json", model)
            item = {"id": snapshot_id, "label": record.get("label", record.get("stage", "地图")),
                    "stage": record.get("stage", run["stage"])}
            run["snapshots"].append(item)
            run["registered_images"] = len(model.get("poses", []))
            run["image_count"] = len(model.get("images", []))
            run["point_count"] = len(model.get("points", []))
            record["snapshot_id"] = snapshot_id
        run["stage"] = record.get("stage", run["stage"])
        run["elapsed_seconds"] = time.monotonic() - started
        for key in ("completed", "total", "message"):
            if key in record:
                run[key] = record[key]
        if isinstance(record.get("metrics"), dict):
            run["training_metrics"] = record["metrics"]
        if isinstance(record.get("preview"), dict):
            from .rendering import public_preview
            run["latest_preview_record"] = record["preview"]
            run["latest_preview"] = public_preview(run["id"], output, record["preview"])
            record["preview"] = run["latest_preview"]
        if record.get("kind") == "warning" and record.get("message"):
            run.setdefault("warnings", []).append(record["message"])
        atomic_json(state_path, run)
        seq += 1
        record.update(seq=seq, time=time.time(), elapsed_seconds=run["elapsed_seconds"])
        with (ui_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()

    progress_io_failed = False

    def event(value):
        nonlocal progress_io_failed
        try:
            publish_event(value)
        except PermissionError:
            # A locked progress/snapshot file must not permanently disconnect
            # the observer (or fail NeRF/3DGS). The next event publishes the
            # latest in-memory state. Final/error publication remains strict.
            if value.get('kind') in ('complete', 'error', 'todo'):
                raise
            warning = '界面进度写入曾被文件占用阻止；后台计算继续，后续事件自动重试'
            if warning not in run.setdefault('warnings', []):
                run['warnings'].append(warning)
            if not progress_io_failed:
                logging.getLogger(__name__).warning(warning, exc_info=True)
            progress_io_failed = True
        else:
            if progress_io_failed:
                logging.getLogger(__name__).info('UI progress publication recovered')
            progress_io_failed = False

    try:
        is_rendering = run.get("pipeline") in ("nerf", "3dgs")
        if is_rendering:
            from ..rendering.pipeline import train
            summary = train(run["source_model_path"], run["dataset_path"], output, run["config"], on_event=event)
            atomic_json(output / "summary.json", summary)
            run["training_metrics"] = summary.get("metrics", run.get("training_metrics", {}))
        else:
            from ..pipeline import reconstruct
            summary = reconstruct(run["dataset_path"], output, run["config"], on_event=event)
        run.update(status="completed", stage="done", **{
            key: summary[key] for key in ("registered_images", "image_count", "point_count")})
        if not is_rendering:
            run.update(completed=summary['registered_images'], total=summary['image_count'],
                       message=f"Registered {summary['registered_images']}/{summary['image_count']} images; reconstruction completed")
        # The final canonical file remains available even if an optional
        # progress observer was disabled after an I/O failure.
        if not is_rendering:
            run["snapshots"].append({"id": "final", "label": "最终结果", "stage": "done"})
        if summary.get("unregistered_image_ids"):
            run.setdefault("warnings", []).append("部分图像未注册：" + str(summary["unregistered_image_ids"]))
        ba_path = output / "ba.json"
        if ba_path.exists():
            ba = json.loads(ba_path.read_text(encoding="utf-8"))
            if ba.get("accepted") and not ba.get("solver_success"):
                run.setdefault("warnings", []).append("BA 候选已接受，但尚未收敛")
        event({"kind": "complete", "stage": "done", "status": "completed", "summary": summary})
        return 0
    except NotImplementedError as exc:
        run.update(status="todo", error=str(exc))
        event({"kind": "todo", "stage": run["stage"], "status": "todo", "message": str(exc)})
        status_path = output / "run_status.json"
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
        status.update(status="todo", stage=run["stage"], error=str(exc), pipeline=run.get("pipeline", "sfm"))
        atomic_json(status_path, status)
        return 2
    except Exception as exc:
        run.update(status="failed", error=str(exc))
        event({"kind": "error", "stage": run["stage"], "status": "failed", "message": str(exc)})
        if run.get("pipeline") in ("nerf", "3dgs"):
            status_path = output / "run_status.json"
            status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
            status.update(status="failed", stage=run["stage"], error=str(exc), pipeline=run["pipeline"])
            atomic_json(status_path, status)
        traceback.print_exc()
        return 1
    finally:
        run["elapsed_seconds"] = time.monotonic() - started
        runtime['process_cpu_seconds'] = time.process_time() - cpu_started
        if output.exists():
            atomic_json(output / 'runtime.json', runtime)
        atomic_json(state_path, run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
