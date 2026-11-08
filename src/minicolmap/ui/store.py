"""Local job registry. Models and original results are only read, never rewritten."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from stat import S_ISDIR

from ..config import load_config
from ..io import load_dataset, load_reconstruction, save_reconstruction
from ..metrics import reconstruction_summary
from .worker import atomic_json


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def stable_id(prefix: str, path: Path) -> str:
    return prefix + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state = self.root / ".cache" / "mini-colmap-ui"
        self.datasets: dict[str, Path] = {}
        self.runs: dict[str, dict] = {}
        self.processes: dict[str, subprocess.Popen] = {}
        self.logs = {}
        # Cache only image identities, never paths used to serve files. Dataset
        # paths still go through load_dataset's containment/existence checks.
        self.image_identities = {}
        self.lock = threading.RLock()
        for metadata in (self.state / "runs").glob("*.json"):
            record = read_json(metadata)
            if record and record.get("id"):
                if record.get("status") == "running":
                    record.update(status="interrupted", error="上次界面服务已退出，任务中断")
                    atomic_json(metadata, record)
                self.runs[record["id"]] = record
        self.discover()

    def discover(self):
        with self.lock:
            self._prune_missing_outputs()
            data_root = self.root / "data"
            for manifest in data_root.rglob("*.json") if data_root.exists() else []:
                if any((parent / ".invalid-import").exists() for parent in manifest.parents if parent != data_root.parent):
                    continue
                document = read_json(manifest, {})
                if isinstance(document, dict) and "cameras" in document and "images" in document:
                    self.datasets[stable_id("dataset-", manifest)] = manifest.resolve()
            known_outputs = {str(Path(run["output_path"]).resolve()) for run in self.runs.values()}
            outputs = self.root / "outputs"
            for output in sorted(outputs.iterdir()) if outputs.exists() else []:
                try:
                    skip = str(output.resolve()) in known_outputs or not output.is_dir()
                except OSError:
                    continue
                if skip:
                    continue
                status = read_json(output / "run_status.json", {})
                if not status and not (output / "model.json").exists():
                    continue
                run_id = stable_id("result-", output)
                summary = read_json(output / "summary.json", {})
                run = {"id": run_id, "name": output.name, "pipeline": summary.get("pipeline", status.get("pipeline", "sfm")),
                       "status": "interrupted" if status.get("status") == "running" else
                       (status.get("status") if status.get("status") in ("failed", "todo", "cancelled") else "completed"),
                       "stage": status.get("stage", "done"), "output_path": str(output.resolve()),
                       "snapshots": [], "images_available": False, "warnings": []}
                run.update({key: value for key, value in summary.items() if key in (
                    "registered_images", "image_count", "point_count", "elapsed_seconds")})
                if status.get("error"):
                    run["error"] = status["error"]
                if status.get("dataset"):
                    run["dataset_path"] = status["dataset"]
                if (output / "model.json").exists():
                    run["snapshots"] = [{"id": "final", "label": "最终结果", "stage": "done"}]
                if run["pipeline"] in ("nerf", "3dgs"):
                    run["source_kind"] = "posed_dataset" if status.get("source_kind") == "posed_dataset" else "sfm_result"
                    source_model = output / "source_model.json"
                    if not source_model.is_file() and status.get("source_model"):
                        source_model = Path(status["source_model"])
                    run["source_model_path"] = str(source_model)
                    run["config"] = read_json(output / "config.json", {})
                    run["training_metrics"] = summary.get("metrics", {})
                    if source_model.is_file():
                        label = "标定位姿输入（只读）" if run["source_kind"] == "posed_dataset" else "SfM 输入（只读）"
                        run["snapshots"] = [{"id": "source", "label": label, "stage": "source"}]
                    for source_id, source_run in self.runs.items():
                        if (Path(source_run["output_path"]) / "model.json").resolve() == source_model.resolve():
                            run["source_run_id"] = source_id
                            break
                ba = read_json(output / "ba.json", {})
                if ba.get("accepted") and not ba.get("solver_success"):
                    run["warnings"].append("BA 候选已接受，但尚未收敛")
                if summary.get("unregistered_image_ids"):
                    run["warnings"].append("部分图像未注册：" + str(summary["unregistered_image_ids"]))
                self.runs[run_id] = run
            for run in self.runs.values():
                matched = self.image_dataset(run)
                if matched is not None:
                    path, data = matched
                    dataset_id = stable_id("dataset-", path)
                    self.datasets[dataset_id] = path.resolve()
                    run["dataset_id"] = dataset_id
                    if run.get("source_kind") == "posed_dataset":
                        run["source_dataset_id"] = dataset_id
                    run["images_available"] = bool(data["image_paths"])
                else:
                    run["images_available"] = False
                    run.pop("dataset_id", None)

    def _prune_missing_outputs(self):
        """Reconcile history under self.lock; never delete results or active jobs."""
        registry = (self.state / "runs").resolve()
        for run_id, run in list(self.runs.items()):
            process = self.processes.get(run_id)
            if run.get("status") == "running" or (process is not None and process.poll() is None):
                continue  # A starting worker may not have created its output yet.
            raw = run.get("output_path")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                if S_ISDIR(Path(raw).stat().st_mode):
                    continue  # Keep incomplete/failed results, even without a model.
            except (FileNotFoundError, NotADirectoryError):
                pass
            except OSError:
                continue  # Inaccessible is not evidence that the user deleted it.
            metadata = registry / f"{run_id}.json"
            if metadata.resolve().parent != registry:
                continue
            try:
                metadata.unlink(missing_ok=True)
            except OSError:
                continue  # Keep it visible if the persistent index cannot be removed.
            self.runs.pop(run_id, None)
            self.processes.pop(run_id, None)
            log = self.logs.pop(run_id, None)
            if log is not None:
                log.close()

    def dataset(self, dataset_id):
        if dataset_id not in self.datasets:
            raise KeyError("数据集不存在")
        path = self.datasets[dataset_id]
        return path, load_dataset(path)

    def dataset_info(self, dataset_id):
        path, dataset = self.dataset(dataset_id)
        document = read_json(path, {})
        pose_count = 0
        if "poses" in document:
            from ..rendering.data import load_posed_dataset
            pose_count = len(load_posed_dataset(path).poses)
        return {"id": dataset_id, "name": path.parent.name + (" / " + path.stem if path.stem != "dataset" else ""),
                "path": str(path), "image_count": len(dataset["images"]), "pose_count": pose_count,
                "images": [{"id": image.id, "name": image.name, "camera_id": image.camera_id,
                            "width": dataset["cameras"][image.camera_id].width,
                            "height": dataset["cameras"][image.camera_id].height,
                            "image_url": f"/api/datasets/{dataset_id}/images/{image.id}"}
                           for image in dataset["images"].values()],
                "cameras": [{"id": c.id, "width": c.width, "height": c.height, "K": c.K.tolist()}
                            for c in dataset["cameras"].values()]}

    def bootstrap(self):
        self.discover()
        datasets = []
        for dataset_id in self.datasets:
            try:
                info = self.dataset_info(dataset_id)
                datasets.append({key: info[key] for key in ("id", "name", "path", "image_count", "pose_count")})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return {"datasets": datasets, "runs": self.list_runs(), "defaults": load_config()}

    def get(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise KeyError("运行记录不存在")
            process = self.processes.get(run_id)
            metadata = self.state / "runs" / f"{run_id}.json"
            if metadata.exists():
                self.runs[run_id] = read_json(metadata, self.runs[run_id])
            run = self.runs[run_id]
            if process is not None and process.poll() is not None:
                if run["status"] == "running":
                    run.update(status="failed", error="重建进程意外结束；请查看诊断日志")
                    atomic_json(metadata, run)
                self.processes.pop(run_id, None)
                log = self.logs.pop(run_id, None)
                if log:
                    log.close()
            return run

    def public(self, run):
        result = {key: value for key, value in run.items()
                if key not in ("config", "dataset_path", "source_model_path", "latest_preview_record")}
        if run.get('pipeline') == '3dgs':
            result['preview_max_edge'] = 1024 if run.get('config', {}).get('gaussian', {}).get('backend') == 'gsplat' else 512
        return result

    def list_runs(self):
        return [self.public(self.get(run_id)) for run_id in reversed(list(self.runs))]

    def start(self, dataset_id, config):
        with self.lock:
            for run_id in list(self.runs):
                if self.get(run_id)["status"] == "running":
                    raise RuntimeError("已有重建任务正在运行，请等待或取消")
            path, dataset = self.dataset(dataset_id)
            run_id = uuid.uuid4().hex[:16]
            output = self.root / "outputs" / ("ui_" + time.strftime("%Y%m%d_%H%M%S_") + run_id[:6])
            run = {"id": run_id, "name": output.name, "pipeline": "sfm", "status": "running", "stage": "starting",
                   "dataset_id": dataset_id, "dataset_path": str(path), "config": config,
                   "registered_images": 0, "point_count": 0, "image_count": len(dataset["images"]),
                   "elapsed_seconds": 0, "snapshots": [], "images_available": True,
                   "warnings": [], "output_path": str(output.resolve())}
            return self._launch(run)

    def _launch(self, run):
        """Both reconstruction and rendering share one worker registry and lock."""
        run_id = run["id"]
        metadata = self.state / "runs" / f"{run_id}.json"
        atomic_json(metadata, run)
        logs_dir = self.state / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log = (logs_dir / f"{run_id}.log").open("wb")
        from .gpu import worker_environment
        env = worker_environment(self.root)
        run['gpu_index']=int(env['CUDA_VISIBLE_DEVICES']) if run.get('pipeline') in ('nerf','3dgs') and run.get('config',{}).get('device')=='cuda' else None
        atomic_json(metadata,run)
        source_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            process = subprocess.Popen([sys.executable, "-m", "minicolmap.ui.worker", str(metadata)],
                                       cwd=self.root, stdout=log, stderr=subprocess.STDOUT,
                                       env=env, **kwargs)
        except Exception:
            log.close()
            run.update(status="failed", error="无法启动任务子进程")
            atomic_json(metadata, run)
            raise
        self.runs[run_id] = run
        self.processes[run_id] = process
        self.logs[run_id] = log
        return self.public(run)

    def start_rendering(self, source_run_id=None, config=None, *, dataset_id=None, photo_dataset_id=None):
        from .gpu_lease import ensure_experiment_idle
        ensure_experiment_idle(self.root)
        from .rendering import capabilities, validate_source, validate_model_images
        from ..rendering.config import resolve_render_config
        config = resolve_render_config(config)
        with self.lock:
            for run_id in list(self.runs):
                if self.get(run_id)["status"] == "running":
                    raise RuntimeError("已有任务正在运行，请等待或取消")
            if (source_run_id is None) == (dataset_id is None):
                raise ValueError("source_run_id 和 dataset_id 必须且只能指定一项")
            if dataset_id is not None:
                if photo_dataset_id is not None:
                    raise ValueError("直接使用位姿数据集时不能再指定 photo_dataset_id")
                if config["method"] != "nerf":
                    raise ValueError("3DGS 需要点云初始化，请先构建或加载 SfM 点云结果")
                from ..rendering.data import load_posed_dataset
                manifest, dataset = self.dataset(dataset_id)
                model = load_posed_dataset(manifest)
                if len(model.poses) < 2:
                    raise ValueError("NeRF 需要至少两张已提供 K、R、t 的原图；该数据集位姿不足")
                validate_model_images(model, dataset)
                source, model_path = None, None
            else:
                source, model_path, manifest, model = validate_source(
                    self, source_run_id, config["method"], photo_dataset_id)
            if not capabilities()["torch_available"]:
                raise ValueError('未安装 PyTorch，请先安装项目可选依赖：pip install -e ".[render]"')
            run_id = uuid.uuid4().hex[:16]
            method = config["method"]
            output = self.root / "outputs" / (f"ui_{method}_" + time.strftime("%Y%m%d_%H%M%S_") + run_id[:6])
            output.mkdir(parents=True)
            frozen_model = output / "source_model.json"
            if model_path is None:
                save_reconstruction(model, frozen_model)
            else:
                shutil.copy2(model_path, frozen_model)
            atomic_json(output / "config.json", config)
            run = {"id": run_id, "name": output.name, "pipeline": method,
                   "source_kind": "posed_dataset" if source is None else "sfm_result",
                   "source_model_path": str(frozen_model),
                   "status": "running", "stage": "starting", "dataset_path": str(manifest),
                   "config": config, "registered_images": len(model.poses), "point_count": len(model.points),
                   "image_count": len(model.images), "elapsed_seconds": 0,
                   "snapshots": [{"id": "source", "label": "标定位姿输入（只读）" if source is None else "SfM 输入（只读）", "stage": "source"}],
                   "images_available": True, "warnings": [], "output_path": str(output.resolve()),
                   "training_metrics": {}}
            if source is None:
                run["source_dataset_id"] = dataset_id
                run["dataset_id"] = dataset_id
            else:
                run["source_run_id"] = source_run_id
                selected_dataset = photo_dataset_id or source.get("dataset_id")
                if selected_dataset:
                    run["dataset_id"] = selected_dataset
            return self._launch(run)

    def cancel(self, run_id, interrupted=False):
        with self.lock:
            run = self.get(run_id)
            if run["status"] != "running":
                return self.public(run)
            process = self.processes.get(run_id)
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            # Read the worker's final atomic metadata without treating our own
            # intentional process termination as an unexpected worker failure.
            metadata = self.state / "runs" / f"{run_id}.json"
            run = read_json(metadata, run)
            self.processes.pop(run_id, None)
            log = self.logs.pop(run_id, None)
            if log:
                log.close()
            run.update(status="interrupted" if interrupted else "cancelled", stage="cancelled")
            self.runs[run_id] = run
            atomic_json(metadata, run)
            output = Path(run["output_path"])
            ui_dir = output / "ui"
            ui_dir.mkdir(parents=True, exist_ok=True)
            status_path = output / "run_status.json"
            status = read_json(status_path, {})
            status.update(status=run["status"], stage="cancelled")
            atomic_json(status_path, status)
            events = self.events(run_id)
            seq = max((item.get("seq", 0) for item in events), default=0) + 1
            with (ui_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": "cancelled", "status": run["status"],
                                         "stage": "cancelled", "seq": seq}, ensure_ascii=False) + "\n")
            return self.public(run)

    def close(self):
        for run_id in list(self.processes):
            self.cancel(run_id, interrupted=True)

    def events(self, run_id):
        run = self.get(run_id)
        path = Path(run["output_path"]) / "ui" / "events.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                pass  # A concurrently written final line is replayed on the next poll.
        return records

    def model(self, run_id, snapshot="latest"):
        run = self.get(run_id)
        if run.get("pipeline") in ("nerf", "3dgs"):
            if snapshot not in ("latest", "source"):
                raise KeyError("训练任务只提供只读相机与原始输入场景")
            return load_reconstruction(run["source_model_path"]), run, "source"
        snapshots = run["snapshots"]
        if snapshot == "latest":
            if not snapshots:
                raise KeyError("还没有有效地图快照")
            snapshot = snapshots[-1]["id"]
        if snapshot not in {item["id"] for item in snapshots}:
            raise KeyError("地图快照不存在")
        output = Path(run["output_path"])
        path = output / "model.json" if snapshot == "final" else output / "ui" / "snapshots" / f"{snapshot}.json"
        return load_reconstruction(path), run, snapshot

    def image_dataset(self, run):
        preferred = run.get("dataset_path")
        if not preferred:
            return None
        output = Path(run["output_path"])
        model_path = Path(run.get("source_model_path", output / "model.json"))
        if not model_path.exists() and run.get("snapshots"):
            last_id = run["snapshots"][-1]["id"]
            if last_id != "final":
                model_path = output / "ui" / "snapshots" / f"{last_id}.json"
        try:
            stat = model_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        cached = self.image_identities.get(model_path)
        if cached is not None and cached[0] == signature:
            expected = cached[1]
        else:
            model_data = read_json(model_path, {})
            expected = {item["id"]: item["name"] for item in model_data.get("images", [])}
            self.image_identities[model_path] = (signature, expected)
        candidates = [Path(preferred)] + [path for path in self.datasets.values() if path != Path(preferred)]
        for path in candidates:
            try:
                dataset = load_dataset(path)
                actual = {image_id: image.name for image_id, image in dataset["images"].items()}
                if expected and actual != expected:
                    continue
                return path, dataset
            except (ValueError, OSError, KeyError, TypeError):
                continue
        return None

    def image_paths(self, run):
        matched = self.image_dataset(run)
        return matched[1]["image_paths"] if matched is not None else {}
