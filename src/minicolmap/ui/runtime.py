"""Process-local scheduling for the user-requested reconstruction worker.

Windows can infer a low QoS for windowless/background work. Explicit HighQoS
avoids that inference for this compute job; it is not a priority-class change,
CPU pinning, or a system power-plan edit. Failure is diagnostic, never fatal.

Reference: https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-setprocessinformation
"""
from __future__ import annotations

import os
import sys


def _windows_high_qos():
    import ctypes
    from ctypes import wintypes

    class State(ctypes.Structure):
        _fields_ = [(name, wintypes.ULONG) for name in ('Version', 'ControlMask', 'StateMask')]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    signature = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.GetProcessInformation.argtypes = signature
    kernel.GetProcessInformation.restype = wintypes.BOOL
    kernel.SetProcessInformation.argtypes = signature
    kernel.SetProcessInformation.restype = wintypes.BOOL
    handle = kernel.GetCurrentProcess()
    state = State(1, 0, 0)  # PROCESS_POWER_THROTTLING_CURRENT_VERSION
    if not kernel.GetProcessInformation(handle, 4, ctypes.byref(state), ctypes.sizeof(state)):
        raise ctypes.WinError(ctypes.get_last_error())
    before = {'control_mask': state.ControlMask, 'state_mask': state.StateMask}
    # ProcessPowerThrottling = 4; EXECUTION_SPEED = 1. Preserve other mechanisms.
    state.ControlMask |= 1
    state.StateMask &= ~1
    if not kernel.SetProcessInformation(handle, 4, ctypes.byref(state), ctypes.sizeof(state)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'before': before, 'requested': {'control_mask': state.ControlMask, 'state_mask': state.StateMask}}


def configure_worker_runtime():
    """Call inside the child before numerical imports; never change the server."""
    policy = os.environ.get('MINICOLMAP_WORKER_QOS', 'high')
    record = {'cuda_visible_devices':os.environ.get('CUDA_VISIBLE_DEVICES'), 'platform': sys.platform, 'cpu_count': os.cpu_count(),
              'python_trace_active': sys.gettrace() is not None,
              'qos_policy': policy, 'qos_applied': False}
    if policy not in ('high', 'auto'):
        record['qos_error'] = 'MINICOLMAP_WORKER_QOS must be high or auto; using system defaults'
    elif sys.platform == 'win32' and policy == 'high':
        try:
            record['windows_power_throttling'] = _windows_high_qos()
            record['qos_applied'] = True
        except (OSError, AttributeError) as exc:
            record['qos_error'] = str(exc)
    return record
