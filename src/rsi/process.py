"""Bounded processes without shell interpolation."""

import os
from pathlib import Path
import signal
import subprocess


class ProcessTimeout(RuntimeError):
    pass


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
    with log.open("wb") as output:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=env,
            **options,
        )
        try:
            process.communicate(
                input=input_text.encode("utf-8") if input_text is not None else None,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stop_process(process)
            raise ProcessTimeout(f"Process exceeded {timeout} seconds; see {log}") from exc
        except BaseException:
            stop_process(process)
            raise
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
    return env
