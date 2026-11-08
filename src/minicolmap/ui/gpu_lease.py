"""A process-bound local GPU reservation for reproducible offline experiments."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid


def process_identity(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX existence probe only; never used on Windows.
    except (ProcessLookupError, PermissionError):
        return None
    return 'posix-alive'


def lease_path(root):
    return Path(root) / '.cache/3dgs-ablation-gpu.json'


def active_lease(root):
    try:
        lease = json.loads(lease_path(root).read_text(encoding='utf-8'))
        identity = process_identity(lease['pid'])
        return lease if identity is not None and identity == lease.get('process_identity') else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def ensure_experiment_idle(root):
    lease = active_lease(root)
    if lease is not None:
        raise RuntimeError('GPU 正在处理任务，请等待结束；任务目录：' + lease['report'])


def acquire(root, report, *, pid=None):
    from .worker import atomic_json
    ensure_experiment_idle(root)
    pid = os.getpid() if pid is None else pid
    identity = process_identity(pid)
    if identity is None:
        raise ValueError('Cannot reserve GPU for a process that is not running')
    lease = {'pid': pid, 'process_identity': identity, 'report': str(Path(report).resolve()), 'token': uuid.uuid4().hex}
    atomic_json(lease_path(root), lease)
    return lease['token']


def release(root, token):
    try:
        path = lease_path(root)
        if json.loads(path.read_text(encoding='utf-8')).get('token') == token:
            path.unlink()
    except (OSError, ValueError):
        pass


def adopt(root, token):
    """A spawned worker takes over its parent's already-held reservation."""
    from .worker import atomic_json
    lease = active_lease(root)
    if not token or lease is None or lease.get('token') != token:
        raise RuntimeError('GPU 查询预留已失效，请重新提交查询')
    lease.update(pid=os.getpid(), process_identity=process_identity(os.getpid()))
    atomic_json(lease_path(root),lease)
    return token
