"""Windows kernel ownership of a worker and its future subprocesses."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority_class", wintypes.DWORD), ("scheduling_class", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
        ("page_faults", wintypes.DWORD), ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD), ("terminated_processes", wintypes.DWORD)]


class WindowsJob:
    """A non-inheritable parent handle; closing its last copy kills every member."""
    def __init__(self, pid: int, process_start: str):
        if os.name != "nt":
            raise OSError("Windows Job Objects require Windows")
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = arguments, result
        # NULL security attributes produce a non-inheritable handle. Do not allow
        # breakaway: all helpers created after ACK must stay in the owning job.
        self._handle = self._kernel.CreateJobObjectW(None, None)
        self._check(self._handle)
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self._check(self._kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            process = self._kernel.OpenProcess(0x100 | 0x1 | 0x1000, False, pid)
            self._check(process)
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                self._check(self._kernel.GetProcessTimes(process, *(ctypes.byref(value) for value in times)))
                ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                # Match psutil's C conversion: subtract in integer space, then
                # cast to double before division (Python int division differs).
                created = float(ticks - 116444736000000000) / 10000000
                if str(created) != process_start:
                    raise ValueError("PROCESS_IDENTITY_CHANGED: 工作进程标识变化")
                # The identity check and assignment use the same open process
                # handle, so PID reuse cannot attach an unrelated process.
                self._check(self._kernel.AssignProcessToJobObject(self._handle, process))
            finally:
                self._kernel.CloseHandle(process)
        except BaseException:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
            raise

    @staticmethod
    def _check(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self._handle is None:
            return
        # Terminate via kernel membership, even after every original root exited.
        # Wait for active membership to reach zero before publishing terminal state.
        try:
            self._check(self._kernel.TerminateJobObject(self._handle, 1))
            deadline = time.monotonic() + 5
            while True:
                accounting = _Accounting()
                self._check(self._kernel.QueryInformationJobObject(self._handle, 1,
                    ctypes.byref(accounting), ctypes.sizeof(accounting), None))
                if accounting.active_processes == 0:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("PROCESS_CLEANUP_TIMEOUT: 工作进程组未停止")
                time.sleep(0.005)
        finally:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
