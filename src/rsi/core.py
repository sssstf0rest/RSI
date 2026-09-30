"""Persistent candidate workflow. No automatic installation or release."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid

from rsi.process import check_environment, execute, worker_environment


class RSIError(RuntimeError):
    pass


PROTECTED = (
    "src/rsi/", "tests/test_seed.py", "pyproject.toml", "rsi.toml",
    ".gitignore", "AGENTS.md", ".codex/", ".rsi/",
)
def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode:
        raise RSIError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def find_repo(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").decode().strip()).resolve()


def require_clean(repo: Path) -> None:
    if git(repo, "status", "--porcelain", "--untracked-files=all").strip():
        raise RSIError("Commit or stash your changes first. Candidates start from a clean commit.")


def permission_args() -> list[str]:
    return [
        "-c", 'permissions.rsi.extends=":workspace"',
        "-c", "permissions.rsi.network.enabled=false",
    ]


def sandbox_command(codex: str, workspace: Path, command: list[str]) -> list[str]:
    return [
        codex, "sandbox", "--permission-profile", "rsi", "-C", str(workspace),
        "--include-managed-config", *permission_args(),
        "--", *command,
    ]


class Seed:
    def __init__(self, repo: Path, *, codex: str = "codex"):
        self.repo = find_repo(repo)
        self.codex = codex
        self.state = self.repo / ".rsi"
        if self.state.is_symlink():
            raise RSIError(".rsi must not be a symlink.")
        self.state.mkdir(exist_ok=True)
        self.sandbox_home = self.state / "sandbox-home"
        self.sandbox_home.mkdir(exist_ok=True)
        (self.state / "check-tmp").mkdir(exist_ok=True)
        config_file = self.repo / "rsi.toml"
        with config_file.open("rb") as source:
            self.config = tomllib.load(source)
        checks = self.config.get("checks")
        if not isinstance(checks, list) or not checks or any(
            not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part for part in command)
            for command in checks
        ):
            raise RSIError("rsi.toml needs a non-empty checks array of argument lists.")
        for key in ("task_timeout_seconds", "check_timeout_seconds"):
            if type(self.config.get(key)) is not int or self.config[key] <= 0:
                raise RSIError(f"{key} must be a positive integer.")
        with self.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
                    branch TEXT NOT NULL, workspace TEXT NOT NULL, base TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '', verified_digest TEXT
                )
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.state / "state.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def operation(self):
        """An OS-owned lock is automatically released on process death."""
        path = self.state / "operation.lock"
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RSIError("Another RSI operation is running in this repository.") from exc
            try:
                # A previous process died without completing its database update.
                with self.connection() as db:
                    db.execute(
                        "UPDATE runs SET status='interrupted', updated_at=?, detail=? "
                        "WHERE status IN ('running','verifying','applying')",
                        (now(), "Previous operation ended unexpectedly; inspect its artifacts."),
                    )
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def get(self, run_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise RSIError(f"Unknown run: {run_id}")
        return dict(row)

    def runs(self) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY created_at DESC")]

    def update(self, run_id: str, status: str, detail: str = "", digest: str | None = None):
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET status=?, detail=?, verified_digest=?, updated_at=? WHERE id=?",
                (status, detail, digest, now(), run_id),
            )

    def artifacts(self, run_id: str) -> Path:
        self.get(run_id)
        return self.state / "runs" / run_id

    def workspace(self, run: dict) -> Path:
        workspace = Path(run["workspace"]).resolve()
        if not workspace.is_relative_to((self.state / "worktrees").resolve()):
            raise RSIError("Candidate workspace is outside RSI's worktree directory.")
        if git(workspace, "rev-parse", "HEAD").decode().strip() != run["base"]:
            raise RSIError("Candidate HEAD changed. Inspect it manually; RSI will not release it.")
        return workspace

    def snapshot(self, run_id: str) -> bytes:
        run = self.get(run_id)
        workspace = self.workspace(run)
        # Include new, non-ignored files in the reviewable patch.
        git(workspace, "add", "--intent-to-add", "--", ".")
        names = git(workspace, "diff", "--name-only", "-z", "HEAD").decode().split("\0")
        baseline_tests = set(
            git(workspace, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", "tests/").decode().split("\0")
        )
        for name in filter(None, names):
            if any(name == protected or name.startswith(protected)
                   for protected in PROTECTED):
                raise RSIError(f"Protected seed file changed: {name}")
            if name in baseline_tests:
                raise RSIError(f"Existing regression test changed: {name}")
            if Path(name).name.startswith(".env"):
                raise RSIError(f"Secret configuration must not be released: {name}")
            if (workspace / name).is_symlink():
                raise RSIError(f"Candidate symlinks are not supported: {name}")
        patch = git(workspace, "diff", "--binary", "HEAD", "--")
        (self.artifacts(run_id) / "candidate.patch").write_bytes(patch)
        return patch

    def doctor(self) -> dict:
        report = {
            "python": sys.version.split()[0],
            "repository": str(self.repo),
            "codex": shutil.which(self.codex),
            "clean": not bool(git(self.repo, "status", "--porcelain").strip()),
        }
        if not report["codex"]:
            report["ready"] = False
            report["reason"] = "Install the official Codex CLI."
            return report
        auth = subprocess.run(
            [self.codex, "login", "status"],
            capture_output=True, timeout=30, env=worker_environment(),
        )
        auth_text = (auth.stdout + auth.stderr).decode("utf-8", errors="replace")
        report["chatgpt_authenticated"] = auth.returncode == 0 and "chatgpt" in auth_text.lower()
        log = self.state / "sandbox-probe.log"
        try:
            probe = (
                "import tempfile; "
                "f = tempfile.TemporaryFile(dir='.'); f.write(b'probe'); f.close(); "
                "t = tempfile.TemporaryFile(); t.write(b'probe'); t.close(); "
                "print('sandbox workspace and temp writes ready')"
            )
            code = execute(
                sandbox_command(self.codex, self.repo, [sys.executable, "-c", probe]),
                cwd=self.repo, log=log, timeout=self.config["check_timeout_seconds"],
                env=check_environment(self.repo, self.sandbox_home),
            )
            report["sandbox"] = code == 0
        except (OSError, RuntimeError) as exc:
            report["sandbox"] = False
            report["sandbox_error"] = str(exc)
        report["sandbox_log"] = str(log)
        report["ready"] = all(report.get(key) for key in ("clean", "chatgpt_authenticated", "sandbox"))
        return report

    def run_task(self, task: str) -> dict:
        if not task.strip():
            raise RSIError("A task must not be empty.")
        with self.operation():
            require_clean(self.repo)
            run_id = uuid.uuid4().hex[:12]
            branch = f"codex/rsi-{run_id}"
            workspace = self.state / "worktrees" / run_id
            base = git(self.repo, "rev-parse", "HEAD").decode().strip()
            artifacts = self.state / "runs" / run_id
            artifacts.mkdir(parents=True)
            with self.connection() as db:
                db.execute(
                    "INSERT INTO runs (id,task,status,branch,workspace,base,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, task, "running", branch, str(workspace), base, now(), now()),
                )
            print(f"Run {run_id}: {workspace}", flush=True)
            try:
                git(self.repo, "worktree", "add", "-b", branch, str(workspace), base)
                prompt = (
                    "Implement the user's task in this candidate workspace. "
                    "Use Python's standard library unless the task explicitly requires otherwise. "
                    "Preserve existing tests and add meaningful tests for the new behavior. "
                    "The following seed paths are protected: " + ", ".join(PROTECTED) + ". "
                    "Add capabilities in new packages under src/rsi_*. "
                    "Do not commit, push, deploy, start persistent services, or contact users. "
                    "Never request or write credentials into code or task artifacts. "
                    "Document any configuration that a human must provide. "
                    "Report what changed, tests, and anything unfinished. "
                    "The supervisor will independently run checks before a human can apply changes.\n\n"
                    "Task:\n" + task
                )
                (artifacts / "task.txt").write_text(task, encoding="utf-8")
                command = [
                    self.codex, "exec", "--json", "--ignore-user-config",
                    "-c", 'default_permissions="rsi"', *permission_args(),
                    "-c", 'approval_policy="never"',
                    "-c", 'forced_login_method="chatgpt"',
                    "-c", 'shell_environment_policy.inherit="core"',
                    "-c", "shell_environment_policy.ignore_default_excludes=false",
                    "-C", str(workspace), "-o", str(workspace / ".rsi-response.txt"),
                ]
                if self.config.get("model"):
                    command.extend(["--model", self.config["model"]])
                command.append("-")
                code = execute(
                    command, cwd=workspace, log=artifacts / "worker.log",
                    timeout=self.config["task_timeout_seconds"], input_text=prompt,
                    env=worker_environment(),
                )
                response = workspace / ".rsi-response.txt"
                if response.exists():
                    shutil.move(str(response), str(artifacts / "response.txt"))
                if code:
                    raise RSIError(f"Codex exited {code}; inspect {artifacts / 'worker.log'}.")
                events = []
                for line in (artifacts / "worker.log").read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        events.append(event.get("type"))
                if "turn.completed" not in events or any(
                    event in ("turn.failed", "error") for event in events
                ):
                    raise RSIError(f"No successful Codex turn; inspect {artifacts / 'worker.log'}.")
                self.snapshot(run_id)
                self.update(run_id, "proposed", "Candidate is ready for independent verification.")
            except KeyboardInterrupt:
                self.update(run_id, "interrupted", "Cancelled by user.")
                raise
            except Exception as exc:
                self.update(run_id, "failed", str(exc))
                raise RSIError(f"Run {run_id}: {exc}") from exc
            return self.get(run_id)

    def verify(self, run_id: str) -> dict:
        with self.operation():
            run = self.get(run_id)
            if run["status"] == "applied":
                raise RSIError("This candidate has already been applied.")
            workspace = self.workspace(run)
            (workspace / ".rsi" / "check-tmp").mkdir(parents=True, exist_ok=True)
            self.update(run_id, "verifying")
            try:
                patch = self.snapshot(run_id)
                if not patch:
                    raise RSIError("No changes to verify.")
                digest = hashlib.sha256(patch).hexdigest()
                for index, check in enumerate(self.config["checks"], start=1):
                    args = [sys.executable if check[0] == "python" else check[0], *check[1:]]
                    log = self.artifacts(run_id) / f"check-{index}.log"
                    result = execute(
                        sandbox_command(self.codex, workspace, args),
                        cwd=workspace, log=log, timeout=self.config["check_timeout_seconds"],
                        env=check_environment(workspace, self.sandbox_home),
                    )
                    if result:
                        raise RSIError(f"Check {index} exited {result}; inspect {log}.")
                if hashlib.sha256(self.snapshot(run_id)).hexdigest() != digest:
                    raise RSIError("Candidate changed during verification. Review and verify again.")
                self.update(run_id, "verified", "Configured checks passed.", digest)
            except KeyboardInterrupt:
                self.update(run_id, "interrupted", "Verification cancelled.")
                raise
            except Exception as exc:
                self.update(run_id, "failed", str(exc))
                raise RSIError(f"Verification failed: {exc}") from exc
            return self.get(run_id)

    def apply(self, run_id: str) -> dict:
        with self.operation():
            run = self.get(run_id)
            if run["status"] != "verified":
                raise RSIError("Verify this candidate successfully before applying it.")
            require_clean(self.repo)
            if git(self.repo, "rev-parse", "HEAD").decode().strip() != run["base"]:
                raise RSIError("The main checkout changed since this candidate started.")
            patch = self.snapshot(run_id)
            if hashlib.sha256(patch).hexdigest() != run["verified_digest"]:
                raise RSIError("Candidate changed after verification. Verify again.")
            git(self.repo, "apply", "--check", input_bytes=patch)
            self.update(run_id, "applying")
            try:
                git(self.repo, "apply", input_bytes=patch)
            except Exception as exc:
                self.update(run_id, "failed", str(exc))
                raise
            self.update(run_id, "applied", "Applied to checkout; review and commit before the next task.")
            return self.get(run_id)
