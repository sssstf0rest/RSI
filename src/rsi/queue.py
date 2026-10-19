"""Durable single-worker jobs and idempotent Telegram control commands."""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


class QueueError(RuntimeError):
    pass


ACTIVE = ("running", "evaluating")
TERMINAL = ("completed", "failed", "paused", "interrupted", "cancelled")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class QueueStore:
    """Leases detect abandoned jobs; recovery never automatically repeats them.

    The caller must also hold the supervisor's OS worker lock for the entire
    execution. A lease cannot by itself kill a child that survived its parent.
    """

    def __init__(self, db_path: Path, *, clock=time.time):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, owner TEXT,
                    lease_until REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result TEXT, error_category TEXT, detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS jobs_by_state ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS scheduling (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    paused INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                    observed_tokens INTEGER NOT NULL DEFAULT 0,
                    usage_unknown INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO scheduling(singleton) VALUES(1);
                CREATE TABLE IF NOT EXISTS usage_ledger (
                    usage_id TEXT PRIMARY KEY, tokens INTEGER, recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id INTEGER PRIMARY KEY, accepted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_cursor (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    next_offset INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO telegram_cursor(singleton) VALUES(1);
            """)
            # executescript commits its enclosing transaction. Acquire a new
            # write transaction so concurrent startup cannot race ALTER TABLE.
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(scheduling)")}
            if "observed_tokens" not in columns:
                db.execute("ALTER TABLE scheduling ADD COLUMN observed_tokens INTEGER NOT NULL DEFAULT 0")
            if "usage_unknown" not in columns:
                db.execute("ALTER TABLE scheduling ADD COLUMN usage_unknown INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    def _submit(self, db, kind, payload, job_id=None):
        if not isinstance(kind, str) or not kind.strip():
            raise QueueError("A job kind is required.")
        if not isinstance(payload, dict):
            raise QueueError("A job payload must be an object.")
        encoded = _json(payload)
        if job_id is not None and (not isinstance(job_id, str) or not job_id.strip()):
            raise QueueError("A supplied job ID must be a non-empty string.")
        job_id = job_id if job_id is not None else uuid.uuid4().hex[:12]
        existing = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if existing:
            if existing["kind"] != kind or existing["payload"] != encoded:
                raise QueueError("That job ID already identifies a different request.")
            return self._decode(existing)
        stamp = self.clock()
        db.execute(
            "INSERT INTO jobs(id,kind,payload,status,created_at,updated_at) "
            "VALUES(?,?,?,'queued',?,?)", (job_id, kind, encoded, stamp, stamp),
        )
        return self._decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def submit(self, kind: str, payload: dict, job_id: str | None = None) -> dict:
        with self._connection() as db:
            return self._submit(db, kind, payload, job_id)

    def get(self, job_id: str) -> dict:
        with self._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise QueueError("Unknown job ID.")
            return self._decode(row)

    def list(self, limit=20) -> list[dict]:
        if type(limit) is not int or limit < 1:
            raise QueueError("Limit must be a positive integer.")
        with self._connection() as db:
            return [self._decode(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,),
            )]

    def status(self) -> dict:
        with self._connection() as db:
            scheduling = db.execute("SELECT * FROM scheduling WHERE singleton=1").fetchone()
            active = db.execute("SELECT * FROM jobs WHERE status IN ('running','evaluating')").fetchone()
            queued = db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
            return {"paused": bool(scheduling["paused"]), "reason": scheduling["reason"],
                    "active": self._decode(active), "queued": queued,
                    "observed_tokens": scheduling["observed_tokens"],
                    "usage_unknown": bool(scheduling["usage_unknown"])}

    def claim(self, owner: str, lease_seconds=30) -> dict | None:
        self._validate_lease(owner, lease_seconds)
        with self._connection() as db:
            if db.execute("SELECT paused FROM scheduling WHERE singleton=1").fetchone()[0]:
                return None
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('running','evaluating')").fetchone():
                return None
            row = db.execute(
                "SELECT id FROM jobs WHERE status='queued' AND cancel_requested=0 "
                "ORDER BY created_at, rowid LIMIT 1",
            ).fetchone()
            if row is None:
                return None
            stamp = self.clock()
            db.execute(
                "UPDATE jobs SET status='running',owner=?,lease_until=?,attempts=attempts+1,"
                "updated_at=? WHERE id=?", (owner, stamp + lease_seconds, stamp, row["id"]),
            )
            return self._decode(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    @staticmethod
    def _validate_lease(owner, lease_seconds):
        if (not isinstance(owner, str) or not owner.strip()
                or type(lease_seconds) not in (int, float)
                or not math.isfinite(lease_seconds) or lease_seconds <= 0):
            raise QueueError("An owner and positive lease duration are required.")

    def _owned(self, db, job_id, owner):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["owner"] != owner or row["status"] not in ACTIVE:
            raise QueueError("The active job is not owned by this worker.")
        if row["lease_until"] <= self.clock():
            raise QueueError("The worker lease has expired; reconcile the job first.")
        return row

    def heartbeat(self, job_id: str, owner: str, lease_seconds=30):
        self._validate_lease(owner, lease_seconds)
        with self._connection() as db:
            self._owned(db, job_id, owner)
            stamp = self.clock()
            db.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE id=?",
                       (stamp + lease_seconds, stamp, job_id))

    def set_evaluating(self, job_id: str, owner: str):
        with self._connection() as db:
            self._owned(db, job_id, owner)
            db.execute("UPDATE jobs SET status='evaluating',updated_at=? WHERE id=?",
                       (self.clock(), job_id))

    def finish(self, job_id: str, owner: str, status: str, result: dict | None = None,
               error_category=None, detail=""):
        if status not in TERMINAL:
            raise QueueError("Invalid terminal job status.")
        with self._connection() as db:
            row = self._owned(db, job_id, owner)
            if row["cancel_requested"]:
                status = "cancelled"
            db.execute(
                "UPDATE jobs SET status=?,result=?,error_category=?,detail=?,owner=NULL,"
                "lease_until=NULL,updated_at=? WHERE id=?",
                (status, _json(result) if result is not None else None,
                 error_category, detail, self.clock(), job_id),
            )
            if error_category in ("quota", "authentication", "auth"):
                db.execute("UPDATE scheduling SET paused=1,reason=? WHERE singleton=1",
                           (error_category,))

    def _cancel(self, db, job_id):
        row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise QueueError("Unknown job ID.")
        active = row["status"] in ACTIVE
        if active or row["status"] in ("queued", "paused", "interrupted"):
            db.execute("UPDATE jobs SET cancel_requested=1,status=?,updated_at=? WHERE id=?",
                       (row["status"] if active else "cancelled", self.clock(), job_id))

    def cancel(self, job_id: str):
        with self._connection() as db:
            self._cancel(db, job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        return self.get(job_id)["cancel_requested"]

    def pause(self, reason="manual"):
        with self._connection() as db:
            db.execute("UPDATE scheduling SET paused=1,reason=? WHERE singleton=1", (reason,))

    def resume(self):
        with self._connection() as db:
            db.execute("UPDATE scheduling SET paused=0,reason='',observed_tokens=0,usage_unknown=0 WHERE singleton=1")

    def record_usage(self, tokens: int | None, *, limit: int, usage_id: str | None = None) -> dict:
        """Account observed model work across restarts until explicit resume.

        This stops future scheduling, rather than claiming a hard per-call cap.
        Missing telemetry is unknown and pauses work. Existing auth/quota/manual
        pause reasons remain visible instead of being replaced by usage errors.
        """
        if type(limit) is not int or limit < 1:
            raise QueueError("Usage limit must be a positive integer.")
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise QueueError("Observed usage must be a nonnegative integer or unknown.")
        if usage_id is not None and (not isinstance(usage_id, str) or not usage_id.strip()):
            raise QueueError("A supplied usage ID must be a non-empty string.")
        with self._connection() as db:
            if usage_id is not None:
                existing = db.execute("SELECT tokens FROM usage_ledger WHERE usage_id=?", (usage_id,)).fetchone()
                if existing is not None:
                    if existing["tokens"] != tokens:
                        raise QueueError("That usage ID already records different telemetry.")
                    state = db.execute("SELECT * FROM scheduling WHERE singleton=1").fetchone()
                    return self._usage_state(state)
                db.execute("INSERT INTO usage_ledger(usage_id,tokens,recorded_at) VALUES(?,?,?)",
                           (usage_id, tokens, self.clock()))
            if tokens is not None:
                db.execute("UPDATE scheduling SET observed_tokens=observed_tokens+? WHERE singleton=1", (tokens,))
            else:
                db.execute("UPDATE scheduling SET usage_unknown=1 WHERE singleton=1")
            state = db.execute("SELECT * FROM scheduling WHERE singleton=1").fetchone()
            reason = "usage_unknown" if state["usage_unknown"] else "local_usage_cap" if state["observed_tokens"] >= limit else ""
            if reason and (not state["paused"] or state["reason"] in ("usage_unknown", "local_usage_cap")):
                db.execute("UPDATE scheduling SET paused=1,reason=? WHERE singleton=1", (reason,))
            final = db.execute("SELECT * FROM scheduling WHERE singleton=1").fetchone()
            return self._usage_state(final)

    @staticmethod
    def _usage_state(row):
        return {"paused": bool(row["paused"]), "reason": row["reason"],
                "observed_tokens": row["observed_tokens"],
                "usage_unknown": bool(row["usage_unknown"])}

    def unaccounted_runs(self, limit=100) -> list[dict]:
        """Return a bounded backlog of persisted execution records, oldest first.

        A worker reconciles this before claiming new work and after executing a
        job. The ledger makes a crash between execution and accounting safe:
        the next worker accounts the record once, before scheduling inference.
        Standalone queue databases need not have the supervisor's runs table.
        """
        if type(limit) is not int or limit < 1:
            raise QueueError("Limit must be a positive integer.")
        with self._connection() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone() is None:
                return []
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "execution" not in columns:
                return []  # A legacy database is migrated by Seed on startup.
            return [dict(row) for row in db.execute(
                "SELECT runs.id,runs.execution FROM runs "
                "LEFT JOIN usage_ledger ON usage_ledger.usage_id=runs.id "
                "WHERE runs.execution IS NOT NULL AND usage_ledger.usage_id IS NULL "
                "ORDER BY runs.rowid LIMIT ?", (limit,),
            )]

    def recover_expired(self) -> list[dict]:
        with self._connection() as db:
            stamp = self.clock()
            ids = [row[0] for row in db.execute(
                "SELECT id FROM jobs WHERE status IN ('running','evaluating') AND lease_until<=?", (stamp,),
            )]
            for job_id in ids:
                db.execute(
                    "UPDATE jobs SET status='interrupted',owner=NULL,lease_until=NULL,updated_at=?,"
                    "error_category='infrastructure',detail='Worker lease expired; inspect before retrying.' "
                    "WHERE id=?", (stamp, job_id),
                )
            return [self._decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                    for job_id in ids]

    def recover_after_lock(self) -> list[dict]:
        """Reconcile a dead owner immediately, without waiting for its lease.

        PRECONDITION: the caller holds the OS worker lock and has reconciled
        the previous process registry, including stopping any surviving child.
        This database operation cannot establish either condition itself.
        Interrupted work stays retained and is never automatically requeued.
        """
        with self._connection() as db:
            ids = [row[0] for row in db.execute(
                "SELECT id FROM jobs WHERE status IN ('running','evaluating')",
            )]
            stamp = self.clock()
            for job_id in ids:
                db.execute(
                    "UPDATE jobs SET status='interrupted',owner=NULL,lease_until=NULL,updated_at=?,"
                    "error_category='infrastructure',detail='Previous worker reconciled under the OS lock; inspect before retrying.' "
                    "WHERE id=?", (stamp, job_id),
                )
            return [self._decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                    for job_id in ids]

    def retry(self, job_id: str, *, max_attempts=3):
        """Explicitly retry a failed transient job; interrupted jobs require inspection."""
        if type(max_attempts) is not int or max_attempts < 1:
            raise QueueError("Retry limit must be positive.")
        with self._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (row is None or row["status"] != "failed" or row["cancel_requested"]
                    or row["error_category"] not in ("infrastructure", "transient")
                    or row["attempts"] >= max_attempts):
                raise QueueError("This job is not eligible for a bounded retry.")
            db.execute("UPDATE jobs SET status='queued',result=NULL,error_category=NULL,detail='',"
                       "updated_at=? WHERE id=?", (self.clock(), job_id))

    def telegram_offset(self) -> int:
        with self._connection() as db:
            return db.execute("SELECT next_offset FROM telegram_cursor WHERE singleton=1").fetchone()[0]

    def accept_update(self, update_id: int, command: str = "", argument: str = "") -> dict:
        """Atomically persist a Telegram command and its cursor before replying.

        A crash after acceptance can lose a response, but cannot repeat a job or
        control action. Empty/read-only commands only advance the cursor.
        """
        if type(update_id) is not int or update_id < 0:
            raise QueueError("Invalid Telegram update ID.")
        with self._connection() as db:
            offset = db.execute("SELECT next_offset FROM telegram_cursor WHERE singleton=1").fetchone()[0]
            if update_id < offset or db.execute(
                "SELECT 1 FROM telegram_updates WHERE update_id=?", (update_id,),
            ).fetchone():
                return {"accepted": False, "result": None}
            result = None
            if command in ("task", "experiment", "rollback", "promote"):
                if command in ("task", "rollback", "promote") and not argument.strip():
                    raise QueueError("This command needs an argument.")
                if command in ("rollback", "promote") and db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('running','evaluating')",
                ).fetchone():
                    raise QueueError("Wait for the active worker before changing the accepted version.")
                payload = {"version_id": argument.strip()} if command in ("rollback", "promote") else {"task": argument}
                result = self._submit(db, command, payload, f"telegram-{update_id}")
            elif command == "pause":
                db.execute("UPDATE scheduling SET paused=1,reason='telegram' WHERE singleton=1")
            elif command == "resume":
                db.execute("UPDATE scheduling SET paused=0,reason='',observed_tokens=0,usage_unknown=0 WHERE singleton=1")
            elif command == "cancel":
                self._cancel(db, argument.strip())
            db.execute("INSERT INTO telegram_updates(update_id,accepted_at) VALUES(?,?)",
                       (update_id, self.clock()))
            db.execute("UPDATE telegram_cursor SET next_offset=? WHERE singleton=1", (update_id + 1,))
            return {"accepted": True, "result": result}
