"""Windows process-tree lifetime control, independent of the worker's PID.

Create a job before Popen, attach immediately, and close it in finally. Closing
the supervisor's last job handle also terminates descendants whose parent has
already exited. The job handle cannot be inherited and breakaway is disabled.

Popen starts its process before attach can run; descendants created in that
interval are outside this guarantee. This helper is lifetime control rather
than an execution sandbox.
"""

import ctypes
from ctypes import wintypes
import os


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobObject:
    """Own one Windows job; close/parent death kills its associated processes."""

    def __init__(self):
        self._handle = None
        if os.name != "nt":
            raise OSError("Windows Job Objects are only available on Windows.")
        kernel = self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # NULL security attributes create a non-inheritable, unnamed handle.
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            raise ctypes.WinError(error)
        self._handle = handle

    def attach(self, process) -> None:
        """Attach a Popen process before allowing it to create descendants."""
        if self._handle is None:
            raise RuntimeError("Cannot attach a process to a closed Windows job.")
        handle = getattr(process, "_handle", None)
        opened = handle is None
        if opened:
            # PROCESS_SET_QUOTA | PROCESS_TERMINATE, as AssignProcessToJobObject requires.
            handle = self._kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel.AssignProcessToJobObject(self._handle, int(handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if opened:
                self._kernel.CloseHandle(handle)

    def terminate(self, exit_code: int = 1) -> None:
        """Explicitly terminate the entire job while retaining its handle."""
        if self._handle is not None and not self._kernel.TerminateJobObject(self._handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        """Close once; the OS kills every remaining member of the job."""
        if self._handle is not None:
            if not self._kernel.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except (OSError, AttributeError):
            pass
