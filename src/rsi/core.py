"""Persistent candidate workflow. No automatic installation or release."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid

from rsi.process import check_environment, execute, worker_environment, process_control, reconcile_process


class RSIError(RuntimeError):
    def __init__(self, message: str, *, category: str = "candidate"):
        super().__init__(message)
        self.category = category


PROTECTED = (
    "src/rsi/", "tests/test_seed.py", "pyproject.toml", "rsi.toml",
    ".gitignore", "AGENTS.md", ".codex/", ".rsi/", "benchmarks/", ".github/",
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


def permission_args(*, workspace: Path | None = None, readonly: bool = False,
                    restricted: bool = False, extra_reads=()) -> list[str]:
    options = [
        "-c", 'permissions.rsi.extends=' + json.dumps(":read-only" if restricted else ":workspace"),
        "-c", "permissions.rsi.network.enabled=false",
    ]
    if restricted:
        # The default :workspace profile permits broad reads. Explicitly confine
        # generated programs to their workspace and the interpreter runtime.
        # Inherit no global temp writes. An explicit /tmp deny is also unsafe:
        # Codex applies that mask after mounting readable descendants, hiding
        # policy workspaces and its own helper aliases beneath /tmp.
        entries = {":root": "deny", ":minimal": "read"}
        entries[str(Path(sys.base_prefix).resolve())] = "read"
        entries[str(Path(sys.prefix).resolve())] = "read"
        if sys.platform.startswith("linux"):
            entries["/proc/sys/kernel/random/boot_id"] = "read"
        for path in extra_reads:
            entries[str(Path(path).resolve())] = "read"
        roots = {".": "read" if readonly else "write", ".rsi/check-tmp": "write",
                 ".git": "read", ".agents": "read", ".codex": "read", ".aws": "read", ".env": "deny"}
        if workspace:
            entries[str((workspace / ".rsi" / "check-tmp").resolve())] = "write"
        # The CLI merges individual dotted overrides into an inherited scalar
        # filesystem entry incorrectly. Replace the filesystem table atomically.
        fields = [f"{json.dumps(path)}={json.dumps(mode)}" for path, mode in entries.items()]
        fields.append('\":workspace_roots\"={' + ",".join(f"{json.dumps(path)}={json.dumps(mode)}" for path, mode in roots.items()) + "}")
        options += ["-c", "permissions.rsi.filesystem={" + ",".join(fields) + "}"]
    return options


def _sandbox_runtime_reads(codex: str, sandbox_home: Path) -> list[Path]:
    # Old bubblewrap re-execs through Codex's live PATH alias; newer versions
    # use the native executable directly. Keep both visible without exposing
    # the rest of CODEX_HOME (which may contain credentials).
    reads = [sandbox_home / "tmp" / name for name in ("arg0", "arg0-linux")]
    executable = shutil.which(codex)
    if executable:
        launcher = Path(executable).resolve()
        reads.append(launcher)
        if launcher.name == "codex.js" and launcher.parent.name == "bin":
            package = launcher.parent.parent
            vendors = [package / "vendor"]
            # Match Node's optional-package lookup, including npm's nested
            # dependency layout and platform packages hoisted by other clients.
            for name in ("codex-linux-x64", "codex-linux-arm64"):
                for parent in (package, *package.parents):
                    platform_package = parent / "node_modules" / "@openai" / name
                    if platform_package.is_dir():
                        vendors.append(platform_package / "vendor")
                        break
            for vendor in vendors:
                for pattern in ("*/bin/codex", "*/codex/codex"):
                    reads.extend(path.resolve() for path in vendor.glob(pattern) if path.is_file())
    return reads


def sandbox_command(codex: str, workspace: Path, command: list[str], *, sandbox_home: Path,
                    readonly: bool = False, restricted: bool = True) -> list[str]:
    runtime_reads = _sandbox_runtime_reads(codex, sandbox_home) if restricted and sys.platform.startswith("linux") else ()
    return [
        codex, "sandbox", "--permission-profile", "rsi", "-C", str(workspace),
        "--include-managed-config", *permission_args(workspace=workspace, readonly=readonly, restricted=restricted,
                                                   extra_reads=runtime_reads),
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
        for key in ("proposal_timeout_seconds", "policy_timeout_seconds", "max_context_chars", "max_observed_tokens"):
            if key in self.config and (type(self.config[key]) is not int or self.config[key] <= 0):
                raise RSIError(f"{key} must be a positive integer.")
        if type(self.config.get("memory_limit", 4)) is not int or not 0 <= self.config.get("memory_limit", 4) <= 16:
            raise RSIError("memory_limit must be an integer from 0 to 16.")
        gain = self.config.get("min_score_gain", 0.01)
        if type(gain) not in (int, float) or not 0 < gain <= 1:
            raise RSIError("min_score_gain must be in (0,1].")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
                    branch TEXT NOT NULL, workspace TEXT NOT NULL, base TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '', verified_digest TEXT
                )
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            for column in ("execution", "policy_version", "purpose", "parent_id", "error_category"):
                if column not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")

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
                reconcile_process(self.state / "worker-process.json")
                # A previous process died without completing its database update.
                with self.connection() as db:
                    pending = db.execute("SELECT id,execution FROM runs WHERE status IN ('running','verifying','applying')").fetchall()
                    for row in pending:
                        if row["execution"] is not None:
                            continue
                        artifacts = self.state / "runs" / row["id"]
                        record = artifacts / "execution.json"
                        if record.is_file():
                            metadata = json.loads(record.read_text(encoding="utf-8"))
                        elif (artifacts / "worker.log").is_file():
                            metadata = {"outcome": "interrupted", "error_category": "interrupted",
                                        "usage": {"input_tokens": None, "output_tokens": None},
                                        "log_path": str(artifacts / "worker.log")}
                        else:
                            continue
                        db.execute("UPDATE runs SET execution=? WHERE id=?", (json.dumps(metadata), row["id"]))
                    db.execute(
                        "UPDATE runs SET status='interrupted', updated_at=?, detail=? "
                        "WHERE status IN ('running','verifying','applying')",
                        (now(), "Previous operation ended unexpectedly; inspect its artifacts."),
                    )
                with process_control(registry=self.state / "worker-process.json"):
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

    def run_cursor(self) -> int:
        with self.connection() as db:
            return db.execute("SELECT COALESCE(MAX(rowid),0) FROM runs").fetchone()[0]

    def runs_after(self, cursor: int) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM runs WHERE rowid>? ORDER BY rowid DESC", (cursor,))]

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
            parts = Path(name).parts
            if any(part in ("AGENTS.md", "CLAUDE.md", ".git", ".codex", ".agents") for part in parts):
                raise RSIError(f"Agent configuration is outside the modification allowlist: {name}")
            eligible = (
                bool(re.fullmatch(r"src/rsi_[A-Za-z0-9_]+/.+\.(py|md|json)", name))
                or bool(re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", name))
                or name == "README.md"
                or bool(re.fullmatch(r"(?:docs|tasks)/.+\.md", name))
            )
            if not eligible:
                raise RSIError(f"Path is outside the modification allowlist: {name}")
            if (run.get("purpose") or "capability") == "policy" and not name.startswith("src/rsi_policy/"):
                raise RSIError(f"Policy experiments can only edit src/rsi_policy/: {name}")
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
                sandbox_command(self.codex, self.repo, [sys.executable, "-c", probe], sandbox_home=self.sandbox_home),
                cwd=self.repo, log=log, timeout=self.config["check_timeout_seconds"],
                env=check_environment(self.repo, self.sandbox_home),
            )
            report["sandbox"] = code == 0
            if code:
                report["sandbox_error"] = log.read_text(encoding="utf-8", errors="replace")[-2000:].strip()
        except (OSError, RuntimeError) as exc:
            report["sandbox"] = False
            report["sandbox_error"] = str(exc)
        report["sandbox_log"] = str(log)
        report["ready"] = all(report.get(key) for key in ("clean", "chatgpt_authenticated", "sandbox"))
        return report

    def run_task(self, task: str, *, purpose: str = "capability", parent_id: str | None = None,
                 parent_policy: Path | None = None, candidate_policy: Path | None = None,
                 memory_enabled: bool = True, timeout_seconds: int | None = None) -> dict:
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
            with self.connection() as db:
                db.execute("UPDATE runs SET purpose=?,parent_id=? WHERE id=?", (purpose, parent_id, run_id))
            print(f"Run {run_id}: {workspace}", flush=True)
            try:
                git(self.repo, "worktree", "add", "-b", branch, str(workspace), base)
                if purpose == "policy":
                    from rsi.archive import copy_policy
                    if parent_policy:
                        copy_policy(parent_policy, workspace / "src" / "rsi_policy", replace=True)
                    if candidate_policy:
                        copy_policy(candidate_policy, workspace / "src" / "rsi_policy", replace=True)
                (workspace / ".rsi" / "check-tmp").mkdir(parents=True, exist_ok=True)
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
                from rsi.evolution import task_context
                context, policy_id = task_context(self, task, artifacts, memory_enabled=memory_enabled)
                if policy_id:
                    with self.connection() as db:
                        db.execute("UPDATE runs SET policy_version=? WHERE id=?", (policy_id, run_id))
                if context:
                    prompt += "\n\nSelected policy context (task instructions take precedence):\n" + context
                (artifacts / "task.txt").write_text(task, encoding="utf-8")
                if candidate_policy is None:
                    from rsi.backends.codex import CodexBackend
                    result = CodexBackend(self.codex, self.config.get("model"), self.config.get("reasoning_effort")).run(
                        prompt, workspace, artifacts, timeout_seconds or self.config["task_timeout_seconds"],
                        execute_fn=execute,
                        permission_options=permission_args(workspace=workspace, restricted=True,
                                                          extra_reads=[self.repo / ".git"]),
                    )
                    with self.connection() as db:
                        db.execute("UPDATE runs SET execution=?,error_category=? WHERE id=?",
                                   (json.dumps(result.to_dict()), result.error_category, run_id))
                    if not result.success:
                        prefix = "No successful Codex turn: " if result.exit_code == 0 else ""
                        raise RSIError(f"{prefix}{result.error}; inspect {artifacts / 'worker.log'}.")
                else:
                    (artifacts / "execution.json").write_text(json.dumps({
                        "outcome": "provided_candidate", "usage": {"input_tokens": 0, "output_tokens": 0},
                        "model": self.config.get("model"), "reasoning_effort": self.config.get("reasoning_effort"),
                        "source": str(candidate_policy.resolve()),
                    }, indent=2), encoding="utf-8")
                self.snapshot(run_id)
                self.update(run_id, "proposed", "Candidate is ready for independent verification.")
            except KeyboardInterrupt:
                self.update(run_id, "interrupted", "Cancelled by user.")
                record = artifacts / "execution.json"
                if record.is_file():
                    execution = json.loads(record.read_text(encoding="utf-8"))
                    with self.connection() as db:
                        db.execute("UPDATE runs SET execution=?,error_category=? WHERE id=?",
                                   (json.dumps(execution), execution.get("error_category"), run_id))
                raise
            except Exception as exc:
                self.update(run_id, "failed", str(exc))
                category = self.get(run_id).get("error_category") or getattr(exc, "category", "candidate")
                with self.connection() as db:
                    db.execute("UPDATE runs SET error_category=? WHERE id=?", (category, run_id))
                from rsi.archive import remember_run
                remember_run(self, run_id, "failed", str(exc))
                raise RSIError(f"Run {run_id}: {exc}", category=category) from exc
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
                    wrapped = [sys.executable, "-I", "-c",
                        "import subprocess,sys; print('RSI_CHECK_STARTED',flush=True); sys.exit(subprocess.call(sys.argv[1:]))", *args]
                    result = execute(
                        sandbox_command(self.codex, workspace, wrapped, sandbox_home=self.sandbox_home),
                        cwd=workspace, log=log, timeout=self.config["check_timeout_seconds"],
                        env=check_environment(workspace, self.sandbox_home),
                    )
                    if result:
                        category = "candidate" if "RSI_CHECK_STARTED" in log.read_text(encoding="utf-8", errors="replace") else "infrastructure"
                        raise RSIError(f"Check {index} exited {result}; inspect {log}.", category=category)
                if hashlib.sha256(self.snapshot(run_id)).hexdigest() != digest:
                    raise RSIError("Candidate changed during verification. Review and verify again.")
                self.update(run_id, "verified", "Configured checks passed.", digest)
                from rsi.archive import remember_run
                remember_run(self, run_id, "verified", "Configured regression checks passed.")
            except KeyboardInterrupt:
                self.update(run_id, "interrupted", "Verification cancelled.")
                raise
            except Exception as exc:
                self.update(run_id, "failed", str(exc))
                category = getattr(exc, "category", "candidate")
                with self.connection() as db:
                    db.execute("UPDATE runs SET error_category=? WHERE id=?", (category, run_id))
                raise RSIError(f"Verification failed: {exc}", category=category) from exc
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
