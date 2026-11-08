"""Reconcile missing history outputs without deleting models or active jobs."""
from pathlib import Path

import pytest

pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient

from minicolmap.ui.app import create_app
from minicolmap.ui.store import Store
from minicolmap.ui.worker import atomic_json


def record(store, run_id, *, status='completed', create_output=True):
    output = store.root / 'outputs' / run_id
    if create_output:
        output.mkdir(parents=True)
    run = {'id': run_id, 'name': run_id, 'output_path': str(output),
           'status': status, 'stage': 'done', 'snapshots': [], 'images_available': False}
    metadata = store.state / 'runs' / f'{run_id}.json'
    atomic_json(metadata, run)
    store.runs[run_id] = run
    return output, metadata


def test_history_refresh_removes_missing_index_and_restart_does_not_restore_it(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        store = client.app.state.store
        output, metadata = record(store, 'deleted')
        kept_output, kept_metadata = record(store, 'kept')
        output.rmdir()
        assert {run['id'] for run in client.get('/api/runs').json()} == {'deleted', 'kept'}
        refreshed = client.get('/api/runs?discover=true')
        assert refreshed.status_code == 200
        assert [run['id'] for run in refreshed.json()] == ['kept']
        assert not metadata.exists() and kept_metadata.is_file() and kept_output.is_dir()
        assert client.get('/api/runs/deleted').status_code == 404
        assert [run['id'] for run in Store(tmp_path).list_runs()] == ['kept']


def test_bootstrap_also_removes_stale_history(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        _, metadata = record(client.app.state.store, 'missing', create_output=False)
        assert client.get('/api/bootstrap').json()['runs'] == []
        assert not metadata.exists()


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled', 'interrupted', 'todo', 'imported'])
def test_existing_output_is_retained_without_requiring_a_model_or_checkpoint(tmp_path, status):
    store = Store(tmp_path)
    output, metadata = record(store, 'incomplete', status=status)
    store.discover()
    assert store.get('incomplete')['status'] == status
    assert output.is_dir() and metadata.exists()


def test_starting_job_without_output_directory_is_not_pruned(tmp_path):
    store = Store(tmp_path)
    output, metadata = record(store, 'starting', status='running', create_output=False)
    store.discover()
    assert store.get('starting')['status'] == 'running'
    assert metadata.is_file() and not output.exists()


def test_live_process_is_retained_even_if_metadata_says_completed(tmp_path):
    class Alive:
        def poll(self):
            return None

    store = Store(tmp_path)
    _, metadata = record(store, 'finishing', create_output=False)
    store.processes['finishing'] = Alive()
    store.discover()
    assert 'finishing' in store.runs and metadata.exists()


def test_permission_error_does_not_count_as_missing_output(tmp_path, monkeypatch):
    store = Store(tmp_path)
    output, metadata = record(store, 'inaccessible')
    original = Path.stat

    def stat(path, *args, **kwargs):
        if path == output:
            raise PermissionError('temporarily unavailable')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', stat)
    store.discover()
    assert 'inaccessible' in store.runs and metadata.exists()


def test_index_delete_failure_keeps_record_to_avoid_reappearing_on_restart(tmp_path, monkeypatch):
    store = Store(tmp_path)
    _, metadata = record(store, 'locked', create_output=False)
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == metadata:
            raise PermissionError('index is locked')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', unlink)
    store.discover()
    assert 'locked' in store.runs and metadata.exists()


def test_discovered_cli_result_is_removed_without_a_persistent_index(tmp_path):
    store = Store(tmp_path)
    output = tmp_path / 'outputs' / 'cli-result'
    status = output / 'run_status.json'
    atomic_json(status, {'status': 'failed', 'stage': 'initialization'})
    store.discover()
    assert len(store.list_runs()) == 1
    status.unlink()
    output.rmdir()
    store.discover()
    assert store.list_runs() == []
