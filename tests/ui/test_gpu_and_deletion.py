"""Task lifecycle and explicit GPU choice, using isolated CPU-only fixtures."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from minicolmap.ui.app import create_app
from minicolmap.ui.deletion import delete, plan
from minicolmap.ui.gpu import selected_gpu, select_gpu, worker_environment
from minicolmap.ui.gpu_lease import acquire, release
from minicolmap.ui.store import Store
from minicolmap.ui.worker import atomic_json


def task(store, name="owned", **extra):
    output = store.root / "outputs" / name
    output.mkdir(parents=True)
    (output / "model.json").write_text("{}")
    record = dict(
        id=name,
        name=name,
        status="completed",
        pipeline="sfm",
        stage="done",
        snapshots=[],
        output_path=str(output),
        **extra,
    )
    store.runs[name] = record
    atomic_json(store.state / "runs" / f"{name}.json", record)
    log = store.state / "logs" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("own log")
    return output


@pytest.fixture
def fake_devices(monkeypatch):
    from minicolmap.ui import gpu

    run = Mock(
        return_value=SimpleNamespace(
            stdout="2, GPU-two, Test two, 1000, 500\n7, GPU-seven, Test seven, 2000, 1500\n"
        )
    )
    monkeypatch.setattr(gpu.subprocess, "run", run)
    return run


def test_gpu_selection_persists_and_workers_override_inherited_visibility(
    tmp_path, fake_devices, monkeypatch
):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "7")
    assert selected_gpu(tmp_path) == 7
    assert select_gpu(tmp_path, 2)["selected"] == 2
    assert worker_environment(tmp_path)["CUDA_VISIBLE_DEVICES"] == "2"
    assert selected_gpu(tmp_path) == 2
    assert __import__("os").environ["CUDA_VISIBLE_DEVICES"] == "7"
    for invalid in (True, "7", -1, 8):
        with pytest.raises(ValueError):
            select_gpu(tmp_path, invalid)
    assert selected_gpu(tmp_path) == 2


def test_training_launch_records_and_passes_selected_gpu_without_starting_cuda(
    tmp_path, fake_devices, monkeypatch
):
    store = Store(tmp_path)
    select_gpu(tmp_path, 2)
    from minicolmap.ui import store as module

    popen = Mock(return_value=SimpleNamespace(poll=lambda: 0))
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    run = dict(
        id="test", pipeline="3dgs", config={"device": "cuda"}, status="completed"
    )
    store._launch(run)
    assert popen.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "2"
    assert store.get("test")["gpu_index"] == 2
    store.close()


def test_gpu_api_blocks_running_live_and_offline_work(tmp_path, fake_devices):
    with TestClient(create_app(tmp_path)) as client:
        store = client.app.state.store
        live = client.app.state.live
        assert client.get("/api/gpu").json()["selected"] == 7
        assert client.post("/api/gpu", json={"index": 2}).json()["selected"] == 2
        task(store)
        store.runs["owned"]["status"] = "running"
        atomic_json(store.state / "runs/owned.json", store.runs["owned"])
        assert client.get("/api/gpu").json()["busy"]
        assert client.post("/api/gpu", json={"index": 7}).status_code == 409
        store.runs["owned"]["status"] = "completed"
        atomic_json(store.state / "runs/owned.json", store.runs["owned"])
        live.active = {"run_id": "owned"}
        assert client.post("/api/gpu", json={"index": 7}).status_code == 409
        live.active = None
        token = acquire(tmp_path, tmp_path / "offline")
        try:
            assert client.post("/api/gpu", json={"index": 7}).status_code == 409
        finally:
            release(tmp_path, token)
        assert client.post("/api/gpu", json={"index": 7}).status_code == 200
        assert client.post("/api/gpu", json={"index": 7, "extra": 1}).status_code == 422


def test_deletion_removes_output_index_and_log_without_resurrection(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        store = client.app.state.store
        output = task(store)
        photos = tmp_path / "data/photos/original.jpg"
        photos.parent.mkdir(parents=True)
        photos.write_bytes(b"photo")
        task(store, "kept")
        preview = client.get("/api/runs/owned/deletion").json()
        assert preview["files"] == 3 and not preview["blockers"]
        deleted = client.request(
            "DELETE", "/api/runs/owned", json={"token": preview["token"]}
        )
        assert deleted.status_code == 200 and not deleted.json()["cleanup_pending"]
        assert not output.exists() and photos.read_bytes() == b"photo"
        assert (
            not (store.state / "runs/owned.json").exists()
            and not (store.state / "logs/owned.log").exists()
        )
        assert client.get("/api/runs/owned").status_code == 404
        assert [r["id"] for r in client.get("/api/runs?discover=true").json()] == [
            "kept"
        ]
    assert [r["id"] for r in Store(tmp_path).list_runs()] == ["kept"]


@pytest.mark.parametrize(
    "kind",
    [
        "running",
        "live",
        "child",
        "dataset",
        "scene_config",
        "symlink",
        "lease",
        "shared_output",
    ],
)
def test_deletion_blocks_active_or_shared_results(tmp_path, kind, make_symlink):
    store = Store(tmp_path)
    output = task(store)
    live = None
    token = None
    if kind == "running":
        store.runs["owned"]["status"] = "running"
        atomic_json(store.state / "runs/owned.json", store.runs["owned"])
    elif kind == "live":
        live = SimpleNamespace(active={"run_id": "owned"})
    elif kind == "child":
        task(store, "child", source_run_id="owned")
    elif kind == "shared_output":
        task(store, "alias")
        store.runs["alias"]["output_path"] = str(output)
        atomic_json(store.state / "runs/alias.json", store.runs["alias"])
    elif kind == "dataset":
        store.datasets["shared"] = output / "dataset.json"
    elif kind == "scene_config":
        atomic_json(tmp_path / "configs/semantic_waldo.json", {"rgb_run": str(output)})
    elif kind == "symlink":
        import shutil

        shutil.rmtree(output)
        elsewhere = tmp_path / "original"
        elsewhere.mkdir()
        make_symlink(output, elsewhere, target_is_directory=True)
    elif kind == "lease":
        token = acquire(tmp_path, output / "job.json")
    try:
        value = plan(store, "owned", live)
        assert value["blockers"]
        with pytest.raises(RuntimeError):
            delete(store, "owned", value["token"], live)
        assert output.exists()
    finally:
        if token:
            release(tmp_path, token)


def test_deletion_cascades_only_scene_owned_files(tmp_path):
    store = Store(tmp_path)
    output = task(store)
    task(store, "kept")
    scene_id = "scene-" + "a" * 16
    folder = tmp_path / "data/semantic_scenes" / scene_id
    config = tmp_path / "configs" / f"semantic_{scene_id}.json"
    atomic_json(
        folder / "scene.json", dict(id=scene_id, rgb_run_id="owned", config=config.stem)
    )
    atomic_json(config, dict(rgb_run=str(output), workspace=str(folder)))
    job = tmp_path / ".cache/scene-jobs/job.json"
    atomic_json(job, dict(scene_id=scene_id, status="completed"))
    job.with_suffix(".log").write_text("scene log")
    candidate = tmp_path / "data/object_selections" / ("scene-" + "b" * 24)
    atomic_json(candidate / "selection.json", dict(source_scene=scene_id))
    unrelated = tmp_path / "data/object_selections" / ("object-" + "c" * 24)
    atomic_json(unrelated / "selection.json", dict(source_scene="kitchen"))
    value = plan(store, "owned")
    assert not value["blockers"] and value["scene_ids"] == [scene_id]
    delete(store, "owned", value["token"])
    for path in (output, folder, config, job, job.with_suffix(".log"), candidate):
        assert not path.exists()
    assert unrelated.is_dir() and (tmp_path / "outputs/kept").is_dir()


def test_stale_preview_requires_confirmation_again(tmp_path):
    store = Store(tmp_path)
    output = task(store)
    value = plan(store, "owned")
    (output / "new.txt").write_text("new file")
    with pytest.raises(ValueError, match="变化"):
        delete(store, "owned", value["token"])
    assert output.exists()


def test_move_failure_rolls_back_already_moved_files(tmp_path, monkeypatch):
    store = Store(tmp_path)
    output = task(store)
    value = plan(store, "owned")
    original = Path.rename

    def rename(path, target):
        if path == store.state / "runs/owned.json":
            raise OSError("unavailable")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError):
        delete(store, "owned", value["token"])
    assert (
        output.exists()
        and (store.state / "runs/owned.json").is_file()
        and store.get("owned")
    )


def test_unavailable_inventory_retains_explicit_choice(
    tmp_path, fake_devices, monkeypatch
):
    from minicolmap.ui import gpu

    select_gpu(tmp_path, 2)
    fake_devices.side_effect = OSError("nvidia-smi unavailable")
    status = gpu.inventory(tmp_path)
    assert status["selected"] == 2 and status["devices"] == [] and status["message"]
    with pytest.raises(ValueError):
        select_gpu(tmp_path, 7)
    assert selected_gpu(tmp_path) == 2


def test_live_worker_receives_selected_card_before_entering_renderer(
    tmp_path, fake_devices, monkeypatch
):
    from minicolmap.ui import live

    select_gpu(tmp_path, 2)
    context = Mock()
    parent, child = Mock(), Mock()
    context.Pipe.return_value = (parent, child)
    context.Process.return_value.is_alive.return_value = False
    monkeypatch.setattr(live.multiprocessing, "get_context", lambda _: context)
    renderer = Mock()
    output = tmp_path / "outputs/test"
    worker = live.ProcessWorker(output, target=renderer, extra_args=("request",))
    launch = context.Process.call_args.kwargs
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "7")
    launch["target"](*launch["args"])  # CPU protocol double; no CUDA import.
    assert __import__("os").environ["CUDA_VISIBLE_DEVICES"] == "2"
    renderer.assert_called_once_with(child, str(output), "request")
    worker.close()


def test_cleanup_failure_is_reported_without_rediscovering_hidden_task(
    tmp_path, monkeypatch
):
    from minicolmap.ui import deletion

    store = Store(tmp_path)
    output = task(store)
    value = plan(store, "owned")

    def unavailable(_):
        raise PermissionError("file temporarily locked")

    monkeypatch.setattr(deletion.shutil, "rmtree", unavailable)
    receipt = delete(store, "owned", value["token"])
    assert receipt["deleted"] and receipt["cleanup_pending"]
    assert not output.exists() and Store(tmp_path).list_runs() == []
    assert list((store.state / "deleting").glob("*"))
    assert list((store.state / "deletions").glob("*.json"))
