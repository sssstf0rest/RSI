"""Official Codex CLI worker; authentication remains inside Codex."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time

from rsi.process import ProcessCancelled, ProcessTimeout, execute, worker_environment


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")


@dataclass
class ExecutionResult:
    success: bool = False
    outcome: str = "running"
    error_category: str | None = None
    error: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    actual_model: str | None = None
    actual_reasoning_effort: str | None = None
    codex_version: str | None = None
    thread_id: str | None = None
    session_id: str | None = None
    started_at: str = ""
    finished_at: str | None = None
    elapsed_seconds: float = 0.0
    exit_code: int | None = None
    usage: dict = field(default_factory=lambda: dict.fromkeys(TOKEN_FIELDS))
    response_path: str | None = None
    log_path: str = ""
    execution_path: str = ""
    event_types: list[str] = field(default_factory=list)
    reported_usage: list[dict] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def _token(value):
    return value if type(value) is int and value >= 0 else None


def _usage(event: dict) -> dict:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return dict.fromkeys(TOKEN_FIELDS)
    values = {key: _token(usage.get(key)) for key in TOKEN_FIELDS}
    if values["reasoning_tokens"] is None:
        values["reasoning_tokens"] = _token(usage.get("reasoning_output_tokens"))
    for field_name, details_name, nested_name in (
        ("cached_input_tokens", "input_tokens_details", "cached_tokens"),
        ("reasoning_tokens", "output_tokens_details", "reasoning_tokens"),
    ):
        details = usage.get(details_name)
        if values[field_name] is None and isinstance(details, dict):
            values[field_name] = _token(details.get(nested_name))
    return values


def _message(event: dict) -> str:
    error = event.get("error", event.get("message"))
    if isinstance(error, dict):
        parts = [error.get(key) for key in ("code", "type", "message")]
        return ": ".join(part for part in parts if isinstance(part, str)) or "Codex reported an error."
    return error if isinstance(error, str) else "Codex reported an error."


def _category(message: str, default: str = "infrastructure") -> str:
    message = message.lower()
    if any(word in message for word in (
        "quota", "usage limit", "rate limit", "rate_limit", "limit_reached", "too_many_requests", "429",
    )):
        return "quota"
    if any(word in message for word in (
        "authentication", "unauthorized", "not authenticated", "invalid api key", "401",
        "login", "log in", "sign in", "token expired", "expired token", "invalid token",
    )):
        return "auth"
    if any(word in message for word in ("candidate_failure", "candidate_error", "refusal")):
        return "candidate"
    return default


def _diagnostics(text: str) -> str:
    """Use stderr text for classification without reading task/response JSON as errors."""
    lines = []
    for line in text.splitlines():
        try:
            json.loads(line)
        except json.JSONDecodeError:
            if not line.lstrip().startswith(("{", "[")):
                lines.append(line)
    return "\n".join(lines)[-2000:]


def _parse_events(text: str, result: ExecutionResult) -> str | None:
    """Accept terminal JSONL evidence; plain stderr diagnostics may share the log."""
    failures = []
    malformed = False
    open_turn = False
    open_items = set()
    completed = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.startswith(("{", "[")):
                malformed = True
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        kind = event["type"]
        result.event_types.append(kind)
        for source in (event, event.get("metadata"), event.get("session"), event.get("turn")):
            if not isinstance(source, dict):
                continue
            for source_name, target_name in (
                ("model", "actual_model"), ("reasoning_effort", "actual_reasoning_effort"),
                ("thread_id", "thread_id"), ("session_id", "session_id"),
            ):
                value = source.get(source_name)
                if isinstance(value, str) and value:
                    setattr(result, target_name, value)
        if kind == "turn.started":
            if open_turn:
                malformed = True
            open_turn = True
        elif kind == "turn.completed":
            open_turn = False
            completed.append(_usage(event))
            if isinstance(event.get("usage"), dict):
                result.reported_usage.append({key: _token(value) for key, value in event["usage"].items()
                                              if isinstance(key, str) and type(value) is int})
        elif kind == "item.started":
            item = event.get("item")
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                open_items.add(item["id"])
            else:
                malformed = True
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                open_items.discard(item.get("id"))
        if kind == "error" or kind.endswith(".failed"):
            failures.append(_message(event))
    if completed:
        # An omitted counter in any turn makes the aggregate unknown.
        result.usage = {
            key: sum(values[key] for values in completed)
            if all(values[key] is not None for values in completed) else None
            for key in TOKEN_FIELDS
        }
    if failures:
        return "; ".join(failures)
    if malformed or open_turn or open_items:
        return "Incomplete or malformed Codex event stream; no successful execution was established."
    if not completed:
        return "No successful Codex turn; the event stream has no turn.completed event."
    return None


class CodexBackend:
    def __init__(self, codex: str = "codex", model: str | None = None, reasoning_effort: str | None = None):
        self.codex = codex
        self.model = model
        self.reasoning_effort = reasoning_effort

    def run(
        self, prompt: str, workspace: Path, artifacts: Path, timeout: int,
        *, execute_fn=None, permission_options: list[str] | None = None, env: dict | None = None,
    ) -> ExecutionResult:
        workspace, artifacts = Path(workspace).resolve(), Path(artifacts).resolve()
        artifacts.mkdir(parents=True, exist_ok=True)
        log = artifacts / "worker.log"
        response = artifacts / "response.txt"
        staged_response = workspace / ".rsi-response.txt"
        record = artifacts / "execution.json"
        result = ExecutionResult(
            model=self.model, reasoning_effort=self.reasoning_effort,
            started_at=datetime.now(timezone.utc).isoformat(),
            log_path=str(log), execution_path=str(record),
        )
        started = time.monotonic()
        interrupted = False
        exception_message = None
        try:
            # Never inherit API billing overrides or unrelated service credentials.
            environment = worker_environment() if env is None else {
                key: value for key, value in env.items()
                if not any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD"))
                and not key.upper().endswith(("_KEY", "_API_KEY"))
            }
            try:
                version = subprocess.run(
                    [self.codex, "--version"], capture_output=True, timeout=10, env=environment,
                )
                if version.returncode == 0:
                    result.codex_version = version.stdout.decode("utf-8", errors="replace").strip()[:256] or None
            except (OSError, subprocess.TimeoutExpired):
                pass
            options = permission_options if permission_options is not None else [
                "-c", 'permissions.rsi.extends=":workspace"',
                "-c", "permissions.rsi.network.enabled=false",
            ]
            command = [
                self.codex, "exec", "--json", "--ignore-user-config",
                "-c", 'default_permissions="rsi"', *options,
                "-c", 'approval_policy="never"',
                "-c", 'forced_login_method="chatgpt"',
                "-c", "agents.enabled=false", "-c", "features.multi_agent=false",
                "-c", "features.goals=false",
                "-c", 'shell_environment_policy.inherit="core"',
                "-c", "shell_environment_policy.ignore_default_excludes=false",
                "-C", str(workspace), "-o", str(staged_response),
            ]
            if self.model is not None:
                command.extend(["--model", self.model])
            if self.reasoning_effort is not None:
                command.extend(["-c", "model_reasoning_effort=" + json.dumps(self.reasoning_effort)])
            command.append("-")
            staged_response.unlink(missing_ok=True)
            response.unlink(missing_ok=True)
            log.write_text("", encoding="utf-8")
            result.exit_code = (execute_fn or execute)(
                command, cwd=workspace, log=log, timeout=timeout, input_text=prompt, env=environment,
            )
        except KeyboardInterrupt:
            interrupted = True
            result.outcome, result.error_category = "interrupted", "interrupted"
            exception_message = "Codex execution interrupted."
        except ProcessCancelled as exc:
            result.outcome, result.error_category = "interrupted", "cancelled"
            exception_message = str(exc)
        except (ProcessTimeout, subprocess.TimeoutExpired) as exc:
            result.outcome, result.error_category = "timeout", "timeout"
            exception_message = str(exc)
        except Exception as exc:
            result.outcome, result.error_category = "failed", "infrastructure"
            exception_message = str(exc)
        try:
            if staged_response.is_symlink():
                raise ValueError("Codex response must not be a symlink.")
            if staged_response.is_file():
                shutil.move(str(staged_response), str(response))
                result.response_path = str(response)
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            event_error = _parse_events(text, result)
            if exception_message is None:
                if result.exit_code != 0:
                    result.error = f"Codex exited {result.exit_code}; {event_error or text[-2000:] or 'inspect worker.log.'}"
                else:
                    result.error = event_error
                result.success = result.error is None
                result.outcome = "completed" if result.success else "failed"
                result.error_category = None if result.success else _category(result.error + "\n" + _diagnostics(text))
            else:
                result.error = exception_message
                if result.error_category == "infrastructure":
                    result.error_category = _category(exception_message + "\n" + _diagnostics(text))
        except Exception as exc:
            result.success = False
            result.error = exception_message or str(exc)
            if exception_message is None:
                result.outcome, result.error_category = "failed", "infrastructure"
        finally:
            result.elapsed_seconds = round(time.monotonic() - started, 6)
            result.finished_at = datetime.now(timezone.utc).isoformat()
            temporary = artifacts / "execution.json.tmp"
            temporary.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
            temporary.replace(record)
        if interrupted:
            raise KeyboardInterrupt
        return result
