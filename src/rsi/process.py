"""Bounded processes without shell interpolation."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Callable


class ProcessTimeout(RuntimeError):
    pass


class ProcessCancelled(RuntimeError):
    pass


@dataclass
class Control:
    registry: Path | None = None
    cancelled: Callable[[], bool] | None = None
    heartbeat: Callable[[], object] | None = None
    max_log_bytes: int = 20_000_000


_control = ContextVar("rsi_process_control", default=Control())


@contextmanager
def process_control(**options):
    """Attach persistent ownership and queue cancellation to nested processes."""
    token = _control.set(replace(_control.get(), **options))
    try:
        yield
    finally:
        _control.reset(token)


def process_identity(pid: int) -> str | None:
    """A PID alone is unsafe after reuse; record the OS process creation time."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        values = [wintypes.FILETIME() for _ in range(4)]
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                return None
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        # comm can contain spaces and parentheses, so split after its last ')'.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return None if fields[0] == "Z" else boot + ":" + fields[19]
    except (OSError, IndexError):
        return None


def reconcile_process(registry: Path) -> bool:
    """Stop an orphan from our previous operation before starting new work."""
    if not registry.exists():
        return False
    record = json.loads(registry.read_text(encoding="utf-8"))
    pid = record.get("pid")
    if type(pid) is not int or pid <= 0 or not isinstance(record.get("identity"), str):
        raise RuntimeError(f"Invalid worker ownership record: {registry}")
    identity = process_identity(pid)
    stopped = identity == record["identity"]
    if stopped:
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                    capture_output=True, check=False)
            if result.returncode and process_identity(pid) == record["identity"]:
                raise RuntimeError("Unable to stop the previous RSI worker.")
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for _ in range(50):
            if process_identity(pid) != record["identity"]:
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("Previous RSI worker has not stopped; refusing to overlap workers.")
    elif os.name != "nt" and identity is None:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if record["identity"].startswith(boot + ":"):
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError("A previous process group remains without an identifiable leader; inspect it before retrying.")
    registry.unlink(missing_ok=True)
    return stopped


def stop_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait()


def execute(
    args: list[str],
    *,
    cwd: Path,
    log: Path,
    timeout: int,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Capture output on disk and terminate the process tree on timeout/cancel."""
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    control = _control.get()
    if control.cancelled and control.cancelled():
        raise ProcessCancelled("Task cancelled before process start.")
    with log.open("wb") as output:
        job = None
        if os.name == "nt":
            from rsi.windows_job import JobObject
            job = JobObject()
        try:
            process = subprocess.Popen(
                args, cwd=cwd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, env=env, **options,
            )
        except BaseException:
            if job:
                job.close()
            raise
        registered = False
        try:
            if job:
                job.attach(process)
            if control.registry:
                identity = process_identity(process.pid)
                if identity is None and process.poll() is None:
                    raise RuntimeError("Could not identify worker process; refusing unowned execution.")
                if identity is not None:
                    temporary = control.registry.with_suffix(".tmp")
                    temporary.write_text(json.dumps({"pid": process.pid, "identity": identity}), encoding="utf-8")
                    temporary.replace(control.registry)
                    registered = True
            deadline = time.monotonic() + timeout
            pending = input_text.encode("utf-8") if input_text is not None else None
            while True:
                if control.heartbeat:
                    control.heartbeat()
                if control.cancelled and control.cancelled():
                    raise ProcessCancelled("Task cancelled; candidate artifacts retained.")
                if log.stat().st_size > control.max_log_bytes:
                    raise ProcessTimeout(f"Process output exceeded {control.max_log_bytes} bytes; see {log}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessTimeout(f"Process exceeded {timeout} seconds; see {log}")
                try:
                    process.communicate(input=pending, timeout=min(1, remaining))
                    if log.stat().st_size > control.max_log_bytes:
                        raise ProcessTimeout(f"Process output exceeded {control.max_log_bytes} bytes; see {log}")
                    break
                except subprocess.TimeoutExpired:
                    pending = None
        except BaseException:
            stop_process(process)
            raise
        finally:
            if job:
                job.close()
            elif os.name != "nt":
                # A finished group leader can leave children alive. Group IDs
                # remain allocated while those children exist.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if registered:
                control.registry.unlink(missing_ok=True)
        return process.returncode


def worker_environment() -> dict[str, str]:
    """Do not pass Telegram credentials or API billing overrides to Codex."""
    return {
        key: value
        for key, value in os.environ.items()
        if not any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD"))
        and not key.upper().endswith(("_KEY", "_API_KEY"))
    }


def check_environment(workspace: Path, sandbox_home: Path) -> dict[str, str]:
    """Checks need interpreter paths, not model or Telegram credentials."""
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP",
        "TMPDIR", "LANG", "LC_ALL", "VIRTUAL_ENV",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["CODEX_HOME"] = str(sandbox_home)
    temporary = str(workspace / ".rsi" / "check-tmp")
    env.update(TEMP=temporary, TMP=temporary, TMPDIR=temporary)
    env["PYTHONPATH"] = str(workspace / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
