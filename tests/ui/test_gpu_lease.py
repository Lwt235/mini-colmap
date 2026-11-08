"""An offline ablation cannot overlap UI training/live GPU jobs."""
import json
from pathlib import Path

import pytest

from minicolmap.ui.gpu_lease import acquire, active_lease, ensure_experiment_idle, lease_path, release


def test_live_process_reservation_blocks_then_releases(tmp_path):
    token = acquire(tmp_path, tmp_path / 'report')
    try:
        assert active_lease(tmp_path)['report'] == str(tmp_path / 'report')
        with pytest.raises(RuntimeError, match='GPU 正在处理任务'):
            ensure_experiment_idle(tmp_path)
        release(tmp_path, 'someone-elses-token')
        assert active_lease(tmp_path) is not None
    finally:
        release(tmp_path, token)
    assert not lease_path(tmp_path).exists()
    ensure_experiment_idle(tmp_path)


def test_expired_or_reused_pid_does_not_block_ui(tmp_path):
    token = acquire(tmp_path, tmp_path / 'report')
    path = lease_path(tmp_path)
    data = json.loads(path.read_text())
    data['process_identity'] = 'different process creation time'
    path.write_text(json.dumps(data))
    assert active_lease(tmp_path) is None
    ensure_experiment_idle(tmp_path)
    release(tmp_path, token)


def test_gpu_lease_is_checked_before_starting_ui_training(tmp_path):
    from minicolmap.ui.store import Store
    store = Store(tmp_path)
    token = acquire(tmp_path, tmp_path / 'report')
    try:
        with pytest.raises(RuntimeError, match='GPU 正在处理任务'):
            store.start_rendering('missing source', {'method': '3dgs'})
        assert store.runs == {}
    finally:
        release(tmp_path, token)


def test_gpu_lease_is_checked_before_starting_live_rendering(tmp_path):
    from minicolmap.ui.store import Store
    from minicolmap.ui.live import LiveSessions
    live = LiveSessions(Store(tmp_path))
    token = acquire(tmp_path, tmp_path / 'report')
    try:
        with pytest.raises(RuntimeError, match='GPU 正在处理任务'):
            live.claim('missing source')
        assert live.active is None
    finally:
        release(tmp_path, token)
