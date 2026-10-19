"""One durable worker, explicit scheduling, and bounded artifact retention."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
import uuid

from rsi.archive import Archive
from rsi.core import RSIError, git
from rsi.evolution import Evolution, observed_tokens
from rsi.process import ProcessCancelled, ProcessTimeout, process_control
from rsi.queue import QueueStore


@contextmanager
def worker_lock(seed):
    """Held for the entire worker lifetime, including idle service waits."""
    path = seed.state / "worker.lock"
    with path.open("a+b") as handle:
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
            raise RSIError("A queue worker owns this repository. Enqueue version changes or stop it first.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Worker:
    def __init__(self, seed, *, handlers=None):
        self.seed = seed
        self.queue = QueueStore(seed.state / "state.sqlite3")
        self.owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        self.handlers = handlers or {
            "task": lambda payload: seed.run_task(payload["task"]),
            "experiment": lambda payload: Evolution(seed).experiment(payload.get("task", "")),
            "promote": lambda payload: Evolution(seed).promote(payload["version_id"]),
            "rollback": lambda payload: Evolution(seed).promote(payload["version_id"], rollback=True),
        }

    def _perform(self, job: dict, stop_event) -> dict:
        job_id = job["id"]
        before = self.seed.run_cursor()
        def heartbeat():
            self.queue.heartbeat(job_id, self.owner, lease_seconds=120)
        def cancelled():
            return stop_event.is_set() or self.queue.is_cancel_requested(job_id)
        try:
            with process_control(cancelled=cancelled, heartbeat=heartbeat):
                if cancelled():
                    raise ProcessCancelled("Job cancelled.")
                handler = self.handlers.get(job["kind"])
                if handler is None:
                    raise RSIError("Unsupported queued job kind.")
                if job["kind"] == "experiment":
                    self.queue.set_evaluating(job_id, self.owner)
                result = handler(job["payload"])
                heartbeat()
                self.queue.finish(job_id, self.owner, "completed", result=result)
        except KeyboardInterrupt:
            heartbeat()
            self.queue.finish(job_id, self.owner, "interrupted", detail="Worker interrupted; artifacts retained.")
            raise
        except Exception as exc:
            category = getattr(exc, "category", "candidate")
            new_runs = self.seed.runs_after(before)
            if new_runs and new_runs[0].get("error_category"):
                category = new_runs[0]["error_category"]
            if isinstance(exc, ProcessTimeout):
                category = "timeout"
            if isinstance(exc, ProcessCancelled) or cancelled():
                category = "cancelled"
            if category not in {"candidate", "infrastructure", "transient", "timeout", "cancelled", "auth", "quota", "interrupted"}:
                category = "candidate"
            heartbeat()
            status = "cancelled" if category == "cancelled" else "paused" if category in ("auth", "quota") else "failed"
            # Raw exceptions may embed provider URLs or inherited service
            # credentials. Persist a category and artifact IDs instead.
            detail = f"Job ended ({category}); inspect its local run artifacts."
            if new_runs:
                detail += " Run " + new_runs[0]["id"]
            self.queue.finish(job_id, self.owner, status, error_category=category, detail=detail)
        return self.queue.get(job_id)

    def _account_pending(self):
        # An idempotent ledger repairs a crash between persisting execution and
        # finishing its job. Explicit resume never charges old calls again.
        while True:
            pending = self.queue.unaccounted_runs(limit=100)
            if not pending:
                return
            for run in pending:
                execution = json.loads(run["execution"])
                self.queue.record_usage(observed_tokens(execution),
                    limit=self.seed.config.get("max_observed_tokens", 200_000), usage_id=run["id"])

    def run(self, *, max_jobs=1, serve=False, poll_seconds=5, stop_event=None) -> list[dict]:
        if type(max_jobs) is not int or max_jobs < 1 or not 0 < poll_seconds <= 60:
            raise RSIError("Worker limits must be positive; polling must be at most 60 seconds.")
        stop_event = stop_event or threading.Event()
        completed = []
        with worker_lock(self.seed):
            # The exclusive lock proves the previous queue supervisor is gone.
            # The operation lock reconciles its surviving child before recovery.
            with self.seed.operation():
                self.queue.recover_after_lock()
            while not stop_event.is_set() and (serve or len(completed) < max_jobs):
                self._account_pending()
                job = self.queue.claim(self.owner, lease_seconds=120)
                if job is None:
                    if not serve:
                        break
                    stop_event.wait(poll_seconds)
                    continue
                completed.append(self._perform(job, stop_event))
                if serve:
                    completed = completed[-max_jobs:]
                self._account_pending()
                # A long-lived service waits for new work; a finite batch exits.
                # Only explicit resume resets a persistent pause.
        return completed


def prune(seed, *, keep=8) -> dict:
    if type(keep) is not int or keep < 1:
        raise RSIError("Retention count must be positive.")
    removed = []
    with worker_lock(seed), seed.operation():
        archive = Archive(seed)
        accepted = [version for version in archive.versions() if version["ever_accepted"]]
        protected = {version["source_run"] for version in accepted[:keep] if version["source_run"]}
        active = archive.active()
        if active and active["source_run"]:
            protected.add(active["source_run"])
        runs = seed.runs()
        for run in runs[keep:]:
            if run["id"] in protected or run["status"] in ("running", "verifying", "applying"):
                continue
            expected = seed.state / "worktrees" / run["id"]
            workspace = Path(run["workspace"])
            # Validate the final absolute target before Git removes a tree.
            if workspace.is_symlink() or workspace.resolve() != expected.resolve() or not workspace.resolve().is_relative_to((seed.state / "worktrees").resolve()):
                raise RSIError("Refusing retention outside the managed worktree directory.")
            if workspace.exists():
                # Preserve a reproduction patch even for a rejected/ineligible
                # candidate; do not call the release allowlist for this archive.
                git(workspace, "add", "--intent-to-add", "--", ".")
                (seed.artifacts(run["id"]) / "candidate.patch").write_bytes(git(workspace, "diff", "--binary", "HEAD", "--"))
                git(seed.repo, "worktree", "remove", "--force", str(workspace.resolve()))
                removed.append(run["id"])
            for log in seed.artifacts(run["id"]).glob("*.log"):
                if log.stat().st_size > 100_000:
                    original_size = log.stat().st_size
                    with log.open("rb") as source:
                        source.seek(max(0, original_size - 4000))
                        tail = source.read(4000).decode("utf-8", errors="replace")
                    (log.with_suffix(".summary.txt")).write_text(
                        f"Full log retired by retention; original size {original_size} bytes.\n" + tail, encoding="utf-8")
                    log.unlink()
    return {"removed_worktrees": removed, "kept_recent": keep,
            "preserved": "All policy snapshots, run records, patches and structured provenance; recent accepted worktrees."}
