"""Worker scheduling failures never prevent reconstruction or affect other hosts."""
from minicolmap.ui import runtime


def test_windows_worker_requests_high_qos_by_default(monkeypatch):
    monkeypatch.delenv('MINICOLMAP_WORKER_QOS', raising=False)
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    calls = []
    monkeypatch.setattr(runtime, '_windows_high_qos', lambda: calls.append(True) or {'before': 0})
    result = runtime.configure_worker_runtime()
    assert calls == [True]
    assert result['qos_applied'] is True and result['qos_policy'] == 'high'


def test_unavailable_windows_api_is_diagnostic_only(monkeypatch):
    monkeypatch.setenv('MINICOLMAP_WORKER_QOS', 'high')
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    def unavailable():
        raise OSError('unsupported policy')
    monkeypatch.setattr(runtime, '_windows_high_qos', unavailable)
    result = runtime.configure_worker_runtime()
    assert result['qos_applied'] is False
    assert result['qos_error'] == 'unsupported policy'


def test_system_default_and_non_windows_leave_scheduling_alone(monkeypatch):
    def unexpected():
        raise AssertionError('Should not call Windows API')
    monkeypatch.setattr(runtime, '_windows_high_qos', unexpected)
    monkeypatch.setenv('MINICOLMAP_WORKER_QOS', 'auto')
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    assert runtime.configure_worker_runtime()['qos_applied'] is False
    monkeypatch.setenv('MINICOLMAP_WORKER_QOS', 'high')
    monkeypatch.setattr(runtime.sys, 'platform', 'linux')
    assert runtime.configure_worker_runtime()['qos_applied'] is False
