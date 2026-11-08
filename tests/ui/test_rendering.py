"""Rendering UI transport tests use explicit protocol stubs, never a fake trainer."""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient

from minicolmap.io import save_reconstruction
from minicolmap.models import Camera, ImageRecord, Point3D, Pose, Reconstruction
from minicolmap.ui.app import create_app
from minicolmap.ui.store import Store
from minicolmap.ui.worker import atomic_json, main


class IdleProcess:
    """A non-computing subprocess protocol stub, used only for lock/cancel tests."""
    def __init__(self, *args, **kwargs):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


def png():
    ok, encoded = cv2.imencode(".png", np.zeros((12, 16, 3), np.uint8))
    assert ok
    return encoded.tobytes()


@pytest.fixture
def rendering_client(tmp_path, monkeypatch):
    camera = Camera(1, 16, 12, [[20, 0, 8], [0, 20, 6], [0, 0, 1]])
    poses = {3: Pose(np.eye(3), [0, 0, 0]), 11: Pose(np.eye(3), [-1, 0, 0])}
    xyz = np.array([.2, .1, 4.])
    images = {}
    for image_id, pose in poses.items():
        pixel = camera.K @ (pose.R @ xyz + pose.t)
        images[image_id] = ImageRecord(image_id, f"{image_id}.png", 1, [pixel[:2] / pixel[2]])
    model = Reconstruction({1: camera}, images, poses,
                           {17: Point3D(17, xyz, [12, 34, 56], ((3, 0), (11, 0)))})
    data = tmp_path / "data" / "fixture"
    data.mkdir(parents=True)
    for image in images.values():
        (data / image.name).write_bytes(png())
    manifest = data / "dataset.json"
    atomic_json(manifest, {"cameras": [{"id": 1, "width": 16, "height": 12, "K": camera.K.tolist()}],
                           "images": [{"id": image.id, "name": image.name, "camera_id": 1, "path": image.name}
                                      for image in images.values()]})
    output = tmp_path / "outputs" / "source"
    save_reconstruction(model, output / "model.json")
    atomic_json(output / "run_status.json", {"status": "completed", "stage": "done", "dataset": str(manifest)})
    monkeypatch.setattr("minicolmap.ui.store.subprocess.Popen", IdleProcess)
    monkeypatch.setattr("minicolmap.ui.rendering.capabilities", lambda: {"torch_available": True})
    with TestClient(create_app(tmp_path)) as client:
        source = client.get("/api/bootstrap").json()["runs"][0]
        yield client, source, manifest


def start(client, source, method="nerf"):
    return client.post("/api/rendering/runs", json={"source_run_id": source["id"], "config": {"method": method}})


def test_capabilities_and_configuration_use_shared_validator(tmp_path):
    import sys
    already_loaded = "torch" in sys.modules
    with TestClient(create_app(tmp_path)) as client:
        capabilities = client.get("/api/rendering/capabilities").json()
        assert capabilities["methods"] == ["nerf", "3dgs"]
        assert capabilities["cuda_available"] is None
        assert isinstance(capabilities["gsplat_native_available"], bool)
        assert ("torch" in sys.modules) == already_loaded
        response = client.post("/api/rendering/config/validate", json={"method": "3dgs", "iterations": 3})
        assert response.status_code == 200
        assert response.json()["iterations"] == 3
        adapted = {"initial_scale_mode": "knn", "scene_normalization": "camera_extent",
                   "backend": "gsplat", "adam_eps": 1e-15, "sh_rest_lr_ratio": .05,
                   "absgrad": True, "grow_screen_radius": .05, "split_mode": "stochastic"}
        response = client.post("/api/rendering/config/validate", json={"method": "3dgs", "device": "cuda", "gaussian": adapted})
        assert response.status_code == 200
        assert all(response.json()['gaussian'][key] == value for key, value in adapted.items())
        for invalid in ({"iterations": 0}, {"method": "invented"}, {"gaussian": {"initial_opacity": 1}}):
            assert client.post("/api/rendering/config/validate", json=invalid).status_code == 422


@pytest.mark.parametrize("method", ["nerf", "3dgs"])
def test_render_run_freezes_sfm_and_serves_original_scene(rendering_client, method):
    client, source, _ = rendering_client
    response = start(client, source, method)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["pipeline"] == method and run["source_run_id"] == source["id"]
    output = Path(run["output_path"])
    assert (output / "source_model.json").read_bytes() == (Path(source["output_path"]) / "model.json").read_bytes()
    scene = client.get(f"/api/runs/{run['id']}/scene").json()
    assert scene["snapshot_id"] == "source" and scene["points"]["ids"] == [17]
    assert client.get(f"/api/runs/{run['id']}/images/3").content == png()
    assert client.get(f"/api/runs/{run['id']}/points/17").json()["track_length"] == 2
    assert client.get(f"/api/runs/{run['id']}/scene?snapshot=final").status_code == 404
    result = client.get(f"/api/rendering/runs/{run['id']}/result").json()
    assert result["previews"] == [] and "checkpoint_url" not in result
    assert result["config"]["method"] == method


def test_sfm_and_rendering_share_one_active_worker(rendering_client):
    client, source, _ = rendering_client
    run = start(client, source).json()
    assert start(client, source).status_code == 409
    assert client.post("/api/runs", json={"dataset_id": source["dataset_id"]}).status_code == 409
    assert client.post(f"/api/runs/{run['id']}/cancel").json()["status"] == "cancelled"
    sfm = client.post("/api/runs", json={"dataset_id": source["dataset_id"]})
    assert sfm.status_code == 200
    assert start(client, source).status_code == 409


@pytest.mark.parametrize("status", ["running", "failed", "cancelled", "todo"])
def test_reject_unfinished_or_failed_sfm_source(rendering_client, status):
    client, source, _ = rendering_client
    client.app.state.store.runs[source["id"]]["status"] = status
    assert start(client, source).status_code in (409, 422)


def test_render_job_cannot_be_reused_as_sfm_source(rendering_client):
    client, source, _ = rendering_client
    run = start(client, source).json()
    client.post(f"/api/runs/{run['id']}/cancel")
    assert start(client, run).status_code == 422


@pytest.mark.parametrize("change", ["missing_photo", "dimensions", "calibration", "camera_assignment"])
def test_reject_source_photos_that_do_not_match_model(rendering_client, change):
    client, source, manifest = rendering_client
    document = json.loads(manifest.read_text())
    if change == "missing_photo":
        (manifest.parent / "3.png").unlink()
    elif change == "dimensions":
        ok, image = cv2.imencode(".png", np.zeros((6, 8, 3), np.uint8))
        assert ok
        (manifest.parent / "3.png").write_bytes(image.tobytes())
    elif change == "calibration":
        document["cameras"][0]["K"][0][0] = 40
        atomic_json(manifest, document)
    else:
        document["cameras"].append({**document["cameras"][0], "id": 2})
        document["images"][0]["camera_id"] = 2
        atomic_json(manifest, document)
    assert start(client, source).status_code == 422


def test_missing_torch_is_a_clear_validation_error(rendering_client, monkeypatch):
    client, source, _ = rendering_client
    monkeypatch.setattr("minicolmap.ui.rendering.capabilities", lambda: {"torch_available": False})
    response = start(client, source)
    assert response.status_code == 422 and "PyTorch" in response.text


def worker_run(rendering_client):
    client, source, _ = rendering_client
    run = start(client, source).json()
    store = client.app.state.store
    # The real worker is invoked synchronously; remove only the idle protocol
    # stub so Store.get does not try to classify that stub's process exit.
    store.processes.pop(run["id"])
    return client, run, store.state / "runs" / f"{run['id']}.json"


def test_worker_reports_todo_and_reconnects_without_fabricated_result(rendering_client, monkeypatch):
    client, run, metadata = worker_run(rendering_client)

    # Deliberate NotImplementedError fixture for worker/UI error-state handling.
    def todo(model, dataset, output, config, *, on_event):
        on_event({"kind": "progress", "stage": "nerf_training", "completed": 0, "total": 100})
        raise NotImplementedError("N1 generate_rays 尚待实现")

    monkeypatch.setattr("minicolmap.rendering.pipeline.train", todo)
    assert main(str(metadata)) == 2
    saved = json.loads(metadata.read_text(encoding="utf-8"))
    assert saved["status"] == "todo" and "N1" in saved["error"]
    assert saved["snapshots"][0]["id"] == "source"
    result = client.get(f"/api/rendering/runs/{run['id']}/result").json()
    assert result["status"] == "todo" and result["previews"] == []
    assert "N1" in result["error"] and "checkpoint_url" not in result
    response = client.get(f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": "1"})
    assert "id: 1\n" not in response.text and '"kind": "todo"' in response.text
    assert '"status": "todo"' in response.text


def test_worker_persists_real_preview_files_and_metrics_protocol(rendering_client, monkeypatch):
    client, run, metadata = worker_run(rendering_client)

    def protocol_stub(model, dataset, output, config, *, on_event):
        # This test checks transport and filesystem publication only. These are
        # explicit fixture files, not trained weights or algorithm accuracy.
        preview = output / "previews" / "step-000002.png"
        preview.parent.mkdir()
        preview.write_bytes(png())
        (output / "checkpoint.pt").write_bytes(b"transport fixture, not torch weights")
        record = {"path": "previews/step-000002.png", "image_id": 11, "step": 2}
        metrics = {"step": 2, "total": 2, "loss": .2, "psnr": 7.}
        on_event({"kind": "preview", "stage": "nerf_training", "completed": 2, "total": 2,
                  "metrics": metrics, "preview": record})
        return {"pipeline": "nerf", "image_count": 2, "registered_images": 2, "point_count": 1,
                "step": 2, "metrics": metrics, "previews": [record], "checkpoint": "checkpoint.pt"}

    monkeypatch.setattr("minicolmap.rendering.pipeline.train", protocol_stub)
    assert main(str(metadata)) == 0
    result = client.get(f"/api/rendering/runs/{run['id']}/result").json()
    assert result["status"] == "completed" and result["metrics"]["step"] == 2
    assert client.get(result["previews"][0]["url"]).content == png()
    assert client.get(result["checkpoint_url"]).content.startswith(b"transport fixture")
    record = client.get(f"/api/runs/{run['id']}").json()
    assert record["latest_preview"]["url"] == result["previews"][0]["url"]
    restarted = Store(client.app.state.store.root)
    assert restarted.get(run["id"])["status"] == "completed"
    assert restarted.model(run["id"])[0].points[17].id == 17


def test_png_response_releases_file_before_slow_network_send(rendering_client):
    import asyncio
    from starlette.responses import FileResponse

    client, source, _ = rendering_client
    run = start(client, source).json()
    output = Path(run['output_path'])
    image = output / 'previews' / 'reference-4.png'
    image.parent.mkdir()
    original = png()
    image.write_bytes(original)
    endpoint = next(route.endpoint for route in client.app.routes
                    if getattr(route, 'path', '') == '/api/runs/{run_id}/artifacts/{filename:path}')
    response = endpoint(run['id'], 'previews/reference-4.png')
    assert not isinstance(response, FileResponse)
    replacement = image.with_suffix('.tmp')
    replacement.write_bytes(b'new preview')
    sent = []

    async def send(message):
        if message['type'] == 'http.response.body':
            # Simulate publishing while the client has not consumed this body.
            replacement.replace(image)
            sent.append(message['body'])

    async def receive():
        return {'type': 'http.disconnect'}

    asyncio.run(response({'type': 'http', 'method': 'GET'}, receive, send))
    assert b''.join(sent) == original
    assert image.read_bytes() == b'new preview'
    assert response.media_type == 'image/png'


def test_preview_publication_rejects_uncontained_and_nonexistent_files(rendering_client):
    client, source, _ = rendering_client
    run = start(client, source).json()
    output = Path(run["output_path"])
    (output.parent / "private.png").write_bytes(png())
    atomic_json(output / "summary.json", {"previews": [{"path": "../private.png"}, {"path": "missing.png"},
                                                        {"path": "C:\\private.png"}],
                                               "checkpoint": "../private.pt"})
    result = client.get(f"/api/rendering/runs/{run['id']}/result").json()
    assert result["previews"] == [] and "checkpoint_url" not in result


def test_on_demand_render_requires_checkpoint_and_uses_camera_contract(rendering_client, monkeypatch):
    client, source, _ = rendering_client
    run = start(client, source).json()
    prefix = f"/api/rendering/runs/{run['id']}"
    assert client.post(prefix + "/render", json={"image_id": 3}).status_code == 409
    client.post(f"/api/runs/{run['id']}/cancel")
    assert client.post(prefix + "/render", json={"image_id": 3}).status_code == 422
    output = Path(run["output_path"])
    (output / "checkpoint.pt").write_bytes(b"fixture")
    calls = []

    def render(target, camera=None, image_id=None):
        calls.append((target, camera, image_id))
        (target / "rendered.png").write_bytes(png())
        return {"path": "rendered.png", "image_id": image_id, "step": 2}

    monkeypatch.setattr("minicolmap.rendering.pipeline.render_checkpoint", render)
    response = client.post(prefix + "/render", json={"image_id": 11})
    assert response.status_code == 200, response.text
    assert calls == [(output, None, 11)]
    assert client.get(response.json()["url"]).content == png()
    for invalid in ({}, {"image_id": 3, "camera": {}}, {"path": "elsewhere"}):
        assert client.post(prefix + "/render", json=invalid).status_code == 422
    assert client.get(prefix + "/result").json()["previews"][-1]["image_id"] == 11


def test_cli_training_output_is_discovered_with_source_scene(rendering_client):
    client, source, manifest = rendering_client
    store = client.app.state.store
    output = store.root / "outputs" / "cli-nerf"
    # Persisted error-state fixture tests discovery, not missing production N2.
    atomic_json(output / "run_status.json", {"pipeline": "nerf", "status": "todo", "stage": "preparing",
                                               "source_model": str(Path(source["output_path"]) / "model.json"),
                                               "dataset": str(manifest), "error": "N2 尚待实现"})
    atomic_json(output / "config.json", {"method": "nerf", "iterations": 2})
    runs = client.get("/api/runs?discover=true").json()
    run = next(item for item in runs if item["name"] == "cli-nerf")
    assert run["pipeline"] == "nerf" and run["status"] == "todo"
    assert run["source_run_id"] == source["id"] and run["images_available"]
    scene = client.get(f"/api/runs/{run['id']}/scene").json()
    assert scene["snapshot_id"] == "source" and scene["points"]["ids"] == [17]
    assert client.get(f"/api/rendering/runs/{run['id']}/result").json()["config"]["iterations"] == 2


@pytest.mark.parametrize('fail', [False, True])
def test_cuda_camera_preview_owns_and_releases_worker_lease(rendering_client, monkeypatch, fail):
    client, source, _ = rendering_client
    run = start(client, source, '3dgs').json()
    client.post(f"/api/runs/{run['id']}/cancel")
    output = Path(run['output_path'])
    (output / 'checkpoint.pt').write_bytes(b'protocol fixture')
    atomic_json(output / 'config.json', {'gaussian': {'backend': 'gsplat'}})
    workers = []
    class PreviewWorker:
        def __init__(self, path, *, target, extra_args):
            assert path == output and extra_args == ({'image_id': 3},)
            assert target.__name__ == 'preview_worker_main'
            self.closed = False
            workers.append(self)
        async def receive(self, timeout):
            assert client.app.state.live.active is not None
            if fail:
                return {'type': 'error', 'message': 'test worker failure'}
            (output / 'cuda-preview.png').write_bytes(png())
            return {'type': 'preview', 'preview': {'path': 'cuda-preview.png', 'image_id': 3, 'step': 2}}
        def close(self):
            self.closed = True
    monkeypatch.setattr('minicolmap.ui.live.ProcessWorker', PreviewWorker)
    response = client.post(f"/api/rendering/runs/{run['id']}/render", json={'image_id': 3})
    assert response.status_code == (422 if fail else 200), response.text
    assert workers[0].closed and client.app.state.live.active is None


def test_cancel_keeps_existing_checkpoint_and_published_previews(rendering_client):
    client, source, _ = rendering_client
    run = start(client, source, "3dgs").json()
    output = Path(run["output_path"])
    (output / "checkpoint.pt").write_bytes(b"existing checkpoint transport fixture")
    np.savez(output / "gaussians.npz", means=np.zeros((1, 3)))
    preview = output / "previews" / "step-000001.png"
    preview.parent.mkdir()
    preview.write_bytes(png())
    atomic_json(output / "training.json", {"previews": [{"path": "previews/step-000001.png", "step": 1}]})
    assert client.post(f"/api/runs/{run['id']}/cancel").json()["status"] == "cancelled"
    result = client.get(f"/api/rendering/runs/{run['id']}/result").json()
    assert result["status"] == "cancelled"
    assert client.get(result["checkpoint_url"]).content == b"existing checkpoint transport fixture"
    assert client.get(result["gaussians_url"]).status_code == 200
    assert client.get(result["previews"][0]["url"]).content == png()
    assert client.get(f"/api/runs/{run['id']}/scene").json()["points"]["ids"] == [17]


def add_manifest_poses(source, manifest):
    document = json.loads(manifest.read_text())
    model = json.loads((Path(source["output_path"]) / "model.json").read_text())
    document.update(pose_convention="world_to_camera", poses=model["poses"])
    atomic_json(manifest, document)
    return document


def test_dataset_pose_count_reports_legal_zero_and_posed_input(rendering_client):
    client, source, manifest = rendering_client
    prefix = f"/api/datasets/{source['dataset_id']}"
    assert client.get(prefix).json()["pose_count"] == 0
    document = add_manifest_poses(source, manifest)
    assert client.get(prefix).json()["pose_count"] == 2
    assert client.get("/api/bootstrap").json()["datasets"][0]["pose_count"] == 2
    document["poses"] = []
    atomic_json(manifest, document)
    assert client.get(prefix).json()["pose_count"] == 0


def test_nerf_starts_from_posed_dataset_with_no_cloud_or_sfm_task(rendering_client):
    client, source, manifest = rendering_client
    add_manifest_poses(source, manifest)
    # Removing every SfM history entry proves this path does not require one.
    client.app.state.store.runs.clear()
    response = client.post("/api/rendering/runs", json={"dataset_id": source["dataset_id"],
                                                       "config": {"method": "nerf", "iterations": 2}})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["source_kind"] == "posed_dataset" and run["point_count"] == 0
    assert run["source_dataset_id"] == source["dataset_id"] and "source_run_id" not in run
    scene = client.get(f"/api/runs/{run['id']}/scene").json()
    assert scene["points"]["ids"] == []
    assert scene["summary"]["registered_images"] == 2
    assert all(camera["registered"] for camera in scene["cameras"])
    assert client.get(f"/api/runs/{run['id']}/images/3").content == png()
    result = client.get(f"/api/rendering/runs/{run['id']}/result").json()
    assert result["source_kind"] == "posed_dataset" and result["source_dataset_id"] == source["dataset_id"]


def test_source_result_without_points_supports_nerf_but_rejects_gaussian_initialization(rendering_client):
    client, source, _ = rendering_client
    model_path = Path(source["output_path"]) / "model.json"
    model = json.loads(model_path.read_text())
    model["points"] = []
    atomic_json(model_path, model)
    run = start(client, source).json()
    assert run["pipeline"] == "nerf" and run["point_count"] == 0
    client.post(f"/api/runs/{run['id']}/cancel")
    response = start(client, source, "3dgs")
    assert response.status_code == 422 and "点云" in response.text


@pytest.mark.parametrize("case", ["missing", "empty", "one", "invalid_rotation", "unknown_image", "duplicate", "convention"])
def test_direct_nerf_rejects_missing_insufficient_or_invalid_poses(rendering_client, case):
    client, source, manifest = rendering_client
    document = add_manifest_poses(source, manifest)
    if case == "missing":
        document.pop("poses")
    elif case == "empty":
        document["poses"] = []
    elif case == "one":
        document["poses"] = document["poses"][:1]
    elif case == "invalid_rotation":
        document["poses"][0]["R"] = np.zeros((3, 3)).tolist()
    elif case == "unknown_image":
        document["poses"][0]["image_id"] = 999
    elif case == "duplicate":
        document["poses"].append(document["poses"][0])
    else:
        document["pose_convention"] = "camera_to_world"
    atomic_json(manifest, document)
    response = client.post("/api/rendering/runs", json={"dataset_id": source["dataset_id"], "config": {"method": "nerf"}})
    assert response.status_code == 422, response.text
    if case not in ("missing", "empty", "one"):
        assert client.get(f"/api/datasets/{source['dataset_id']}").status_code == 422
        assert client.get("/api/bootstrap").json()["datasets"] == []


def test_gaussians_require_a_cloud_source_and_sources_are_mutually_exclusive(rendering_client):
    client, source, manifest = rendering_client
    add_manifest_poses(source, manifest)
    response = client.post("/api/rendering/runs", json={"dataset_id": source["dataset_id"], "config": {"method": "3dgs"}})
    assert response.status_code == 422 and "点云" in response.text
    for body in ({}, {"dataset_id": source["dataset_id"], "source_run_id": source["id"]},
                 {"dataset_id": source["dataset_id"], "photo_dataset_id": source["dataset_id"]}):
        assert client.post("/api/rendering/runs", json=body).status_code == 422


def test_imported_model_can_explicitly_associate_matching_photo_dataset(rendering_client):
    client, source, _ = rendering_client
    stored = client.app.state.store.runs[source["id"]]
    stored["status"] = "imported"
    stored.pop("dataset_path")
    stored.pop("dataset_id")
    stored["images_available"] = False
    assert start(client, source, "3dgs").status_code == 422
    response = client.post("/api/rendering/runs", json={"source_run_id": source["id"],
                                                       "photo_dataset_id": source["dataset_id"],
                                                       "config": {"method": "3dgs"}})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["source_kind"] == "sfm_result" and run["dataset_id"] == source["dataset_id"]
    assert client.get(f"/api/runs/{run['id']}/images/11").content == png()


def test_explicit_photo_association_still_rejects_wrong_calibration(rendering_client):
    client, source, manifest = rendering_client
    document = json.loads(manifest.read_text())
    document["cameras"][0]["K"][0][0] = 50
    atomic_json(manifest, document)
    response = client.post("/api/rendering/runs", json={"source_run_id": source["id"],
                                                       "photo_dataset_id": source["dataset_id"],
                                                       "config": {"method": "3dgs"}})
    assert response.status_code == 422 and "内参" in response.text


def test_no_cloud_nerf_worker_reaches_network_boundary(rendering_client, monkeypatch):
    pytest.importorskip("torch")
    client, source, manifest = rendering_client
    add_manifest_poses(source, manifest)
    response = client.post("/api/rendering/runs", json={"dataset_id": source["dataset_id"],
                                                       "config": {"method": "nerf", "iterations": 2}})
    assert response.status_code == 200, response.text
    run = response.json()
    store = client.app.state.store
    store.processes.pop(run["id"])
    metadata = store.state / "runs" / f"{run['id']}.json"
    def network_boundary(*args, **kwargs):
        raise NotImplementedError("TODO N4: explicit test stop at network boundary")
    # Deliberate error-protocol fixture, not a missing production N4 implementation.
    # Real train/data preparation runs; only network construction is stopped.
    monkeypatch.setattr("minicolmap.rendering.algorithms.build_nerf", network_boundary)
    assert main(str(metadata)) == 2
    saved = client.get(f"/api/runs/{run['id']}").json()
    assert saved["status"] == "todo" and "TODO N4" in saved["error"]
    assert saved["point_count"] == 0
    prepared = json.loads((Path(run["output_path"]) / "training_scene.json").read_text(encoding="utf-8"))
    assert prepared["source_points"] == 0
    assert prepared["normalization"]["source"].startswith("camera_centers")
    result = client.get(f"/api/rendering/runs/{run['id']}/result").json()
    assert result["previews"] == [] and "checkpoint_url" not in result


def test_cli_posed_dataset_history_keeps_zero_point_input_and_source_kind(rendering_client):
    client, source, manifest = rendering_client
    add_manifest_poses(source, manifest)
    store = client.app.state.store
    output = store.root / "outputs" / "cli-posed-nerf"
    source_model = json.loads((Path(source["output_path"]) / "model.json").read_text())
    source_model["points"] = []
    atomic_json(output / "source_model.json", source_model)
    atomic_json(output / "run_status.json", {"pipeline": "nerf", "status": "todo", "stage": "preparing",
                                               "source_kind": "posed_dataset", "dataset": str(manifest),
                                               "source_model": str(output / "source_model.json")})
    atomic_json(output / "config.json", {"method": "nerf"})
    discovered = client.get("/api/runs?discover=true").json()
    run = next(item for item in discovered if item["name"] == "cli-posed-nerf")
    assert run["source_kind"] == "posed_dataset" and run["source_dataset_id"] == source["dataset_id"]
    scene = client.get(f"/api/runs/{run['id']}/scene").json()
    assert scene["points"]["ids"] == [] and scene["summary"]["registered_images"] == 2
