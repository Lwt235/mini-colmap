"""The UI is optional; these checks run when its optional dependencies are installed."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient

from minicolmap.io import save_reconstruction
from minicolmap.models import Camera, ImageRecord, Point3D, Pose, Reconstruction
from minicolmap.ui import create_app, run
from minicolmap.ui.scene import make_scene
from minicolmap.ui.store import Store
from minicolmap.ui.worker import atomic_json


def model_fixture():
    camera = Camera(1, 64, 48, [[100, 0, 32], [0, 100, 24], [0, 0, 1]])
    rotation = np.array([[0, -1., 0], [1., 0, 0], [0, 0, 1]])
    poses = {3: Pose(np.eye(3), np.zeros(3)), 11: Pose(rotation, -rotation @ np.array([1., 0, 0]))}
    xyz = np.array([.2, .3, 4.])
    images = {}
    for image_id, pose in poses.items():
        projected = camera.K @ (pose.R @ xyz + pose.t)
        images[image_id] = ImageRecord(image_id, f"{image_id}.png", 1, [projected[:2] / projected[2]])
    return Reconstruction({1: camera}, images, poses,
                          {17: Point3D(17, xyz, [12, 34, 56], ((3, 0), (11, 0)))})


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def png(width=64, height=48):
    import cv2
    ok, encoded = cv2.imencode(".png", np.zeros((height, width, 3), np.uint8))
    assert ok
    return encoded.tobytes()


def photo_upload(client, *, camera=None, second_size=(64, 48)):
    return client.post("/api/datasets/import", data={"mode": "photos", "camera": json.dumps(camera or
                       {"fx": 100, "fy": 100, "cx": 32, "cy": 24})},
                       files=[("files", ("first.png", png(), "image/png")),
                              ("files", ("second.png", png(*second_size), "image/png"))])


def import_model(client, tmp_path):
    path = tmp_path / "fixture.json"
    save_reconstruction(model_fixture(), path)
    response = client.post("/api/results/import", files={"file": ("model.json", path.read_bytes(), "application/json")})
    assert response.status_code == 200, response.text
    return response.json()


def test_camera_center_and_frustum_preserve_world_to_camera_convention():
    model = model_fixture()
    scene = make_scene(model, "1", "final", [])
    for record in scene["cameras"]:
        pose = model.poses[record["image_id"]]
        np.testing.assert_allclose(record["center"], pose.center)
        for corner, pixel in zip(record["frustum"], [[0, 0], [64, 0], [64, 48], [0, 48]]):
            camera_point = pose.R @ np.array(corner) + pose.t
            assert camera_point[2] == pytest.approx(1)
            homogeneous = model.cameras[1].K @ camera_point
            np.testing.assert_allclose(homogeneous[:2] / homogeneous[2], pixel, atol=1e-12)
    np.testing.assert_allclose(scene["cameras"][1]["center"], [1, 0, 0])


@pytest.mark.parametrize('case', ['regular', 'zero_depth', 'empty'])
def test_scene_batch_errors_match_details_without_expanding_all_tracks(monkeypatch, case):
    from minicolmap.ui import scene as module
    model = model_fixture()
    if case == 'empty':
        model.points.clear()
    elif case == 'zero_depth':
        model.points[17].xyz[2] = 0
    else:
        model.cameras[1].K[0, 1] = .7
        for image in model.images.values():
            image.keypoints = np.vstack((image.keypoints, image.keypoints + [2., -3.]))
        model.points[41] = Point3D(41, [-.4, .2, 3.], [20, 40, 60], ((3, 1), (11, 1)))
    expected = [module.point_details(model, point_id)['mean_error'] for point_id in sorted(model.points)]

    def full_track_must_be_on_demand(*args):
        pytest.fail('Scene overview must not construct complete observation dictionaries')

    monkeypatch.setattr(module, 'point_details', full_track_must_be_on_demand)
    result = module.make_scene(model, 'test', 'source', [])
    for actual, reference in zip(result['points']['errors'], expected):
        assert actual == pytest.approx(reference) if reference is not None else actual is None
    assert result['points']['ids'] == sorted(model.points)
    if case == 'zero_depth':
        assert result['summary']['mean_reprojection_error_px'] is None
        assert 'measurement_error' in result['summary']


def test_imported_model_scene_tracks_artifacts_and_missing_photos(client, tmp_path):
    run = import_model(client, tmp_path)
    assert run["status"] == "imported" and not run["images_available"]
    scene = client.get(f"/api/runs/{run['id']}/scene").json()
    assert scene["points"]["ids"] == [17]
    assert scene["summary"]["observation_count"] == 2
    point = client.get(f"/api/runs/{run['id']}/points/17").json()
    assert point["mean_error"] == pytest.approx(0)
    assert len(point["observations"]) == 2
    assert point["observations"][0]["projected_xy"] == point["observations"][0]["xy"]
    assert client.get(f"/api/runs/{run['id']}/images/3").status_code == 404
    names = [item["name"] for item in client.get(f"/api/runs/{run['id']}/artifacts").json()]
    assert {"model.json", "points.ply", "sparse.zip", "results.zip"} <= set(names)
    assert client.get(f"/api/runs/{run['id']}/artifacts/sparse.zip").content[:2] == b"PK"
    assert client.get(f"/api/runs/{run['id']}/artifacts/%2e%2e/config.json").status_code == 404
    assert client.get(f"/api/runs/{run['id']}/scene?snapshot=../../model").status_code == 404


def test_imported_runs_survive_server_restart(client, tmp_path):
    imported = import_model(client, tmp_path)
    restarted = Store(tmp_path)
    assert restarted.get(imported["id"])["status"] == "imported"
    assert restarted.model(imported["id"])[0].points[17].id == 17


def test_timings_report_is_optional_and_downloadable(client, tmp_path):
    imported = import_model(client, tmp_path)
    prefix = f"/api/runs/{imported['id']}"
    assert client.get(prefix + '/reports').json()['timings'] is None
    output = Path(client.app.state.store.get(imported['id'])['output_path'])
    timing = {'status': 'completed', 'unit': 'seconds', 'total_seconds': 2.,
              'stages_seconds': {'features': .5, 'pair_geometry': 1.5},
              'details': {'fundamental_ransac': {'seconds': 1., 'calls': 3}}}
    atomic_json(output / 'timings.json', timing)
    assert client.get(prefix + '/reports').json()['timings'] == timing
    assert client.get(prefix + '/artifacts/timings.json').json() == timing
    names = [item['name'] for item in client.get(prefix + '/artifacts').json()]
    assert 'timings.json' in names


def test_status_poll_does_not_rediscover_historical_models(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client.app.state.store, 'discover', lambda: calls.append(True))
    assert client.get('/api/runs').status_code == 200
    assert not calls
    assert client.get('/api/runs?discover=true').status_code == 200
    assert calls == [True]


def test_image_identity_cache_invalidates_when_model_changes(tmp_path, monkeypatch):
    import minicolmap.ui.store as module
    store = Store(tmp_path)
    model = model_fixture()
    output = tmp_path / 'model-output'
    save_reconstruction(model, output / 'model.json')
    run = {'dataset_path': str(tmp_path / 'dataset.json'), 'output_path': str(output)}
    checked = []
    def dataset(path):
        checked.append(path)
        return {'images': model.images, 'image_paths': {}}
    monkeypatch.setattr(module, 'load_dataset', dataset)
    reads = []
    original = module.read_json
    def read(path, *args):
        reads.append(path)
        return original(path, *args)
    monkeypatch.setattr(module, 'read_json', read)
    assert store.image_dataset(run) is not None
    assert store.image_dataset(run) is not None
    assert len(reads) == 1 and len(checked) == 2
    changed = json.loads((output / 'model.json').read_text())
    changed['images'][0]['name'] = 'different-photo.png'
    atomic_json(output / 'model.json', changed)
    assert store.image_dataset(run) is None
    assert len(reads) == 2


def test_single_download_checks_path_without_scanning_all_artifacts(client, tmp_path, monkeypatch):
    run = import_model(client, tmp_path)
    output = Path(client.app.state.store.get(run['id'])['output_path'])
    atomic_json(output / 'ui' / 'private.json', {'hidden': True})
    def unexpected(*args):
        raise AssertionError('A single download must not enumerate the result tree')
    monkeypatch.setattr('minicolmap.ui.app.artifact_paths', unexpected)
    prefix = f"/api/runs/{run['id']}/artifacts/"
    assert client.get(prefix + 'model.json').status_code == 200
    assert client.get(prefix + 'ui/private.json').status_code == 404
    assert client.get(prefix + '%2e%2e/config.json').status_code == 404


def test_config_uses_actual_pipeline_validator(client):
    valid = client.post("/api/config/validate", json={"bundle_adjustment": {"enabled": True}})
    assert valid.status_code == 200
    assert valid.json()["features"]["max_features"] == 4000
    assert client.post("/api/config/validate", json={"mapping": {"min_angle_deg": -1}}).status_code == 422
    assert client.post("/api/config/validate", json={"invented": 10}).status_code == 422


def test_ui_config_validates_and_returns_adaptive_ransac_parameters(client):
    defaults = client.get('/api/bootstrap').json()['defaults']['geometry']
    assert defaults['confidence'] == .99 and defaults['min_trials'] == 20
    config = client.post('/api/config/validate', json={'geometry': {'confidence': .975, 'min_trials': 37}})
    assert config.status_code == 200
    assert config.json()['geometry']['confidence'] == .975
    assert config.json()['geometry']['min_trials'] == 37
    for invalid in ({'confidence': 1}, {'confidence': True}, {'min_trials': 0}, {'min_trials': 2.5}):
        assert client.post('/api/config/validate', json={'geometry': invalid}).status_code == 422


def test_photo_import_reuses_calibration_and_serves_only_registered_images(client):
    response = photo_upload(client)
    assert response.status_code == 200, response.text
    dataset = response.json()
    assert dataset["image_count"] == 2
    assert dataset["cameras"][0]["K"][0][0] == 100
    assert client.get(dataset["images"][0]["image_url"]).content == png()
    assert client.get(f"/api/datasets/{dataset['id']}/images/999").status_code == 404


@pytest.mark.parametrize("camera", [{"fx": -1, "fy": 100, "cx": 32, "cy": 24}, {"fx": 100}])
def test_photo_import_rejects_invalid_intrinsics(client, camera):
    assert photo_upload(client, camera=camera).status_code == 422
    assert client.get("/api/bootstrap").json()["datasets"] == []


def test_photo_import_rejects_mismatched_dimensions(client):
    assert photo_upload(client, second_size=(32, 24)).status_code == 422
    assert client.get("/api/bootstrap").json()["datasets"] == []


def test_manifest_import_rejection_does_not_reappear_in_discovery(client):
    manifest = {"cameras": [{"id": 1, "width": 100, "height": 100,
                             "K": [[100, 0, 32], [0, 100, 24], [0, 0, 1]]}],
                "images": [{"id": i, "camera_id": 1, "name": f"{i}.png", "path": f"{i}.png"}
                           for i in (1, 2)]}
    response = client.post("/api/datasets/import", data={"mode": "manifest", "manifest_path": "custom.json"},
                           files=[("files", ("custom.json", json.dumps(manifest), "application/json")),
                                  ("files", ("1.png", png(), "image/png")),
                                  ("files", ("2.png", png(), "image/png"))])
    assert response.status_code == 422
    assert client.get("/api/bootstrap").json()["datasets"] == []


def test_upload_path_traversal_and_cross_origin_requests_are_rejected(client):
    response = client.post("/api/datasets/import", data={"mode": "manifest", "paths": "../escape.json"},
                           files=[("files", ("dataset.json", "{}", "application/json"))])
    assert response.status_code == 422
    assert client.post("/api/runs", json={}, headers={"Origin": "https://foreign.example"}).status_code == 403
    assert client.get("/api/bootstrap", headers={"Host": "foreign.example"}).status_code == 400


def test_existing_run_reports_ba_limit_and_partial_registration(client, tmp_path):
    output = tmp_path / "outputs" / "existing"
    save_reconstruction(model_fixture(), output / "model.json")
    atomic_json(output / "run_status.json", {"status": "completed", "stage": "done"})
    atomic_json(output / "ba.json", {"accepted": True, "solver_success": False})
    atomic_json(output / "summary.json", {"point_count": 1, "unregistered_image_ids": [50]})
    record = next(item for item in client.get("/api/bootstrap").json()["runs"] if item["name"] == "existing")
    assert any("尚未收敛" in item for item in record["warnings"])
    assert any("未注册" in item for item in record["warnings"])
    assert client.get(f"/api/runs/{record['id']}/reports").json()["ba"]["accepted"]


def test_historical_images_match_model_names_when_original_manifest_changed(client, tmp_path):
    model = model_fixture()
    manifests = {}
    for folder, prefix in (("original", ""), ("changed", "wrong-")):
        directory = tmp_path / "data" / folder
        directory.mkdir(parents=True)
        for image_id in model.images:
            (directory / f"{image_id}.png").write_bytes(png())
        manifest = directory / "dataset.json"
        atomic_json(manifest, {"cameras": [{"id": 1, "width": 64, "height": 48,
                                            "K": model.cameras[1].K.tolist()}],
                               "images": [{"id": image_id, "camera_id": 1,
                                           "name": prefix + image.name, "path": f"{image_id}.png"}
                                          for image_id, image in model.images.items()]})
        manifests[folder] = manifest
    output = tmp_path / "outputs" / "historical"
    save_reconstruction(model, output / "model.json")
    atomic_json(output / "run_status.json", {"status": "completed", "stage": "done",
                                              "dataset": str(manifests["changed"])})
    record = client.get("/api/bootstrap").json()["runs"][0]
    selected = client.get(f"/api/datasets/{record['dataset_id']}").json()
    assert selected["path"] == str(manifests["original"])
    assert record["images_available"]
    assert client.get(f"/api/runs/{record['id']}/images/3").content == png()


def test_sse_replays_only_events_after_last_event_id(client, tmp_path):
    run = import_model(client, tmp_path)
    events = Path(run["output_path"]) / "ui" / "events.jsonl"
    events.parent.mkdir()
    events.write_text("\n".join(json.dumps({"seq": seq, "kind": "stage", "stage": f"stage{seq}"})
                                for seq in (1, 2, 3)) + "\n", encoding="utf-8")
    response = client.get(f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": "1"})
    assert response.status_code == 200
    assert "id: 1\n" not in response.text
    assert "id: 2\n" in response.text and "id: 3\n" in response.text
    assert '"kind": "terminal"' in response.text


def test_single_active_task_and_clean_cancellation(client, monkeypatch):
    class Process:
        stopped = False
        def __init__(self, *args, **kwargs):
            pass
        def poll(self):
            return 0 if self.stopped else None
        def terminate(self):
            self.stopped = True
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("minicolmap.ui.store.subprocess.Popen", Process)
    dataset = photo_upload(client).json()
    run = client.post("/api/runs", json={"dataset_id": dataset["id"], "config": {}}).json()
    assert run["status"] == "running"
    assert client.post("/api/runs", json={"dataset_id": dataset["id"]}).status_code == 409
    cancelled = client.post(f"/api/runs/{run['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and "error" not in cancelled
    assert json.loads((Path(run["output_path"]) / "run_status.json").read_text())["status"] == "cancelled"
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "cancelled"


def test_worker_failure_retains_valid_snapshot_and_stage(tmp_path, monkeypatch):
    from minicolmap.ui.worker import main
    path = tmp_path / "model.json"
    save_reconstruction(model_fixture(), path)
    serialized = json.loads(path.read_text())
    metadata = tmp_path / "state.json"
    output = tmp_path / "output"
    atomic_json(metadata, {"id": "test", "output_path": str(output), "dataset_path": "ignored",
                           "config": {}, "status": "running", "stage": "starting", "snapshots": []})
    def reconstruct(dataset, target, config, *, on_event):
        target.mkdir()
        on_event({"kind": "snapshot", "stage": "initialization", "label": "初始化", "model": serialized})
        on_event({"kind": "stage", "stage": "incremental_mapping"})
        raise ValueError("test failure")
    monkeypatch.setattr("minicolmap.pipeline.reconstruct", reconstruct)
    assert main(str(metadata)) == 1
    record = json.loads(metadata.read_text(encoding="utf-8"))
    assert record["status"] == "failed" and record["stage"] == "incremental_mapping"
    assert record["registered_images"] == 2
    assert (output / "ui" / "snapshots" / "snapshot-0001.json").exists()
    assert "model" not in (output / "ui" / "events.jsonl").read_text(encoding="utf-8")


def test_rejected_zero_depth_import_is_not_discovered_as_completed(client, tmp_path):
    model = model_fixture()
    model.points[17].xyz[2] = 0
    path = tmp_path / "zero.json"
    save_reconstruction(model, path)
    response = client.post("/api/results/import", files={"file": ("model.json", path.read_bytes(), "application/json")})
    assert response.status_code == 422
    assert client.get("/api/bootstrap").json()["runs"] == []


def test_real_worker_reports_initialization_failure(client):
    import time
    dataset = photo_upload(client).json()
    response = client.post("/api/runs", json={"dataset_id": dataset["id"], "config": {}})
    assert response.status_code == 200
    run_id = response.json()["id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            break
        time.sleep(.1)
    assert run["status"] == "failed", run
    assert run["stage"] == "initialization"
    assert run["snapshots"] == []
    assert "verified pairs" in run["error"].lower()


def test_server_never_binds_external_addresses():
    with pytest.raises(ValueError, match="127.0.0.1"):
        run(host="0.0.0.0", open_browser=False)


def test_atomic_metadata_replace_retries_temporary_windows_locks(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    path.write_text('{"status":"old"}', encoding="utf-8")
    original_replace = Path.replace
    attempts = []
    sleeps = []

    def transient_lock(source, target):
        attempts.append(target)
        if len(attempts) <= 2:
            raise PermissionError("temporary sharing violation")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", transient_lock)
    monkeypatch.setattr("minicolmap.ui.worker.time.sleep", sleeps.append)
    atomic_json(path, {"status": "new"})
    assert len(attempts) == 3
    assert sleeps == [.01, .02]
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "new"}
    assert not path.with_suffix(".json.tmp").exists()


def test_atomic_metadata_replace_propagates_persistent_lock(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    original = '{"status":"old"}'
    path.write_text(original, encoding="utf-8")
    sleeps = []
    attempts = []

    def permanent_lock(source, target):
        attempts.append(target)
        raise PermissionError("permanent sharing violation")

    monkeypatch.setattr(Path, "replace", permanent_lock)
    monkeypatch.setattr("minicolmap.ui.worker.time.sleep", sleeps.append)
    with pytest.raises(PermissionError, match="permanent"):
        atomic_json(path, {"status": "new"})
    assert len(attempts) == 21
    assert len(sleeps) == 20 and sum(sleeps) == pytest.approx(1.75)
    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob('*.tmp'))


def test_worker_recovers_progress_after_locked_metadata_without_disabling_observer(tmp_path, monkeypatch, caplog):
    from minicolmap.ui import worker
    path = tmp_path / 'model.json'
    save_reconstruction(model_fixture(), path)
    model = json.loads(path.read_text())
    metadata = tmp_path / 'state.json'
    output = tmp_path / 'output'
    atomic_json(metadata, {'id': 'test', 'output_path': str(output), 'dataset_path': 'ignored',
                           'config': {}, 'status': 'running', 'stage': 'starting', 'snapshots': []})
    original = worker.atomic_json
    locked = False

    def publish(target, value):
        if target == metadata and locked:
            raise PermissionError('Windows metadata reader denies replacement')
        return original(target, value)

    def reconstruct(dataset, target, config, *, on_event):
        nonlocal locked
        target.mkdir()
        locked = True
        on_event({'kind': 'progress', 'stage': 'incremental_mapping', 'completed': 114, 'total': 312})
        on_event({'kind': 'progress', 'stage': 'incremental_mapping', 'completed': 115, 'total': 312})
        locked = False
        on_event({'kind': 'snapshot', 'stage': 'incremental_mapping', 'model': model})
        on_event({'kind': 'progress', 'stage': 'incremental_mapping', 'completed': 116, 'total': 312})
        return {'registered_images': 312, 'image_count': 312, 'point_count': 17459}

    monkeypatch.setattr(worker, 'atomic_json', publish)
    monkeypatch.setattr('minicolmap.pipeline.reconstruct', reconstruct)
    assert worker.main(str(metadata)) == 0
    record = json.loads(metadata.read_text(encoding='utf-8'))
    assert record['status'] == 'completed' and record['completed'] == record['total'] == 312
    assert len(record['warnings']) == 1
    assert (output / 'ui/snapshots/snapshot-0001.json').is_file()
    events = [json.loads(line) for line in (output / 'ui/events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [event['kind'] for event in events] == ['snapshot', 'progress', 'complete']
    assert events[1]['completed'] == 116
    assert [event['seq'] for event in events] == [1, 2, 3]
    assert 'disabling' not in caplog.text


def test_atomic_metadata_uses_independent_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / 'run.json'
    legacy = path.with_suffix('.json.tmp')
    legacy.write_text('another writer', encoding='utf-8')
    temporary_names = []
    original = Path.replace

    def replace(source, target):
        temporary_names.append(source)
        return original(source, target)

    monkeypatch.setattr(Path, 'replace', replace)
    atomic_json(path, {'version': 1})
    atomic_json(path, {'version': 2})
    assert len(set(temporary_names)) == 2
    assert all(not temporary.exists() for temporary in temporary_names)
    assert legacy.read_text() == 'another writer'
    assert json.loads(path.read_text()) == {'version': 2}


@pytest.mark.skipif(os.name != 'nt', reason='Windows native sharing semantics')
def test_atomic_metadata_survives_windows_read_lock_beyond_old_retry_budget(tmp_path):
    import ctypes
    import threading
    import time
    from ctypes import wintypes

    path = tmp_path / 'run.json'
    atomic_json(path, {'version': 1})
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value

    def reader():
        time.sleep(.35)  # Previously 21 attempts exhausted their .2-second budget.
        kernel.CloseHandle(handle)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        atomic_json(path, {'version': 2})
    finally:
        thread.join()
    assert json.loads(path.read_text()) == {'version': 2}
