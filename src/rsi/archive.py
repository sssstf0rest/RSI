"""Immutable policy snapshots, ancestry, activation, and relevant experience."""

import hashlib
import json
from pathlib import Path
import re
import uuid

from rsi.core import RSIError, now


def policy_files(source: Path) -> list[Path]:
    if Path(source).is_symlink():
        raise RSIError("Policy directories must not be symlinks.")
    source = Path(source).resolve()
    if not source.is_dir() or not (source / "policy.py").is_file():
        raise RSIError("A policy directory must contain policy.py.")
    files = []
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise RSIError("Policy symlinks are not allowed.")
        if path.is_file():
            if path.suffix not in (".py", ".md", ".json") or path.name in ("AGENTS.md", "CLAUDE.md"):
                raise RSIError(f"Ineligible policy artifact: {path.name}")
            files.append(path)
    if len(files) > 32 or sum(p.stat().st_size for p in files) > 262144:
        raise RSIError("Policy artifact exceeds the bounded file/size limit.")
    return files


def policy_digest(source: Path) -> str:
    from rsi.policy_runner import policy_digest as canonical_digest
    policy_files(source)
    return canonical_digest(Path(source))


def copy_policy(source: Path, destination: Path, *, replace: bool = False) -> None:
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if source == destination.resolve():
        raise RSIError("Source and destination policies must differ.")
    files = policy_files(source)
    source = source.resolve()
    if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents):
        raise RSIError("Policy destination must not traverse symlinks.")
    if replace and destination.exists():
        # Delete only eligible files inside the explicitly selected policy root.
        # Unexpected files are rejected rather than recursively removing a tree.
        for path in policy_files(destination):
            path.unlink()
    destination.mkdir(parents=True, exist_ok=True)
    for path in files:
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())


class Archive:
    def __init__(self, seed):
        self.seed = seed
        self.root = seed.state / "versions"
        self.root.mkdir(exist_ok=True)
        with seed.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS policy_versions (
                    id TEXT PRIMARY KEY, parent_id TEXT, baseline_id TEXT,
                    source_run TEXT, strategy_version TEXT NOT NULL,
                    digest TEXT NOT NULL, created_at TEXT NOT NULL,
                    metrics TEXT NOT NULL DEFAULT '{}', eligible INTEGER NOT NULL DEFAULT 0,
                    decision TEXT NOT NULL, ever_accepted INTEGER NOT NULL DEFAULT 0,
                    provenance TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS policy_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), active_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policy_events (
                    id INTEGER PRIMARY KEY, version_id TEXT NOT NULL,
                    action TEXT NOT NULL, created_at TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experience (
                    id INTEGER PRIMARY KEY, source_run TEXT NOT NULL UNIQUE,
                    task TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    def _decode(self, row):
        if row is None:
            raise RSIError("Unknown policy version.")
        value = dict(row)
        value["metrics"] = json.loads(value["metrics"])
        value["provenance"] = json.loads(value["provenance"])
        value["eligible"] = bool(value["eligible"])
        value["ever_accepted"] = bool(value["ever_accepted"])
        value["path"] = str(self.root / value["id"] / "policy")
        return value

    def get(self, version_id: str) -> dict:
        with self.seed.connection() as db:
            return self._decode(db.execute("SELECT * FROM policy_versions WHERE id=?", (version_id,)).fetchone())

    def versions(self) -> list[dict]:
        with self.seed.connection() as db:
            return [self._decode(row) for row in db.execute("SELECT * FROM policy_versions ORDER BY created_at DESC")]

    def active(self) -> dict | None:
        with self.seed.connection() as db:
            row = db.execute("SELECT active_id FROM policy_state WHERE singleton=1").fetchone()
        return self.get(row[0]) if row else None

    def directory(self, version: dict) -> Path:
        path = self.root / version["id"] / "policy"
        if policy_digest(path) != version["digest"]:
            raise RSIError(f"Archived policy {version['id']} changed after recording.")
        return path

    def record(self, source: Path, *, parent_id=None, baseline_id=None, source_run=None,
               metrics=None, eligible=False, decision="unevaluated", provenance=None,
               strategy_version="fixed-v1") -> dict:
        if parent_id:
            self.get(parent_id)
        version_id = uuid.uuid4().hex[:12]
        destination = self.root / version_id / "policy"
        copy_policy(source, destination)
        digest = policy_digest(destination)
        with self.seed.connection() as db:
            db.execute(
                "INSERT INTO policy_versions(id,parent_id,baseline_id,source_run,strategy_version,digest,"
                "created_at,metrics,eligible,decision,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, parent_id, baseline_id, source_run, strategy_version, digest, now(),
                 json.dumps(metrics or {}, allow_nan=False), int(eligible), decision,
                 json.dumps(provenance or {}, allow_nan=False)),
            )
        return self.get(version_id)

    def ensure_baseline(self) -> dict:
        active = self.active()
        if active:
            self.directory(active)
            return active
        initial = self.record(self.seed.repo / "src" / "rsi_policy", decision="initial seed")
        with self.seed.connection() as db:
            db.execute("INSERT INTO policy_state(singleton,active_id) VALUES(1,?)", (initial["id"],))
            db.execute("UPDATE policy_versions SET ever_accepted=1 WHERE id=?", (initial["id"],))
        return self.get(initial["id"])

    def activate(self, version_id: str, *, rollback: bool = False) -> dict:
        version = self.get(version_id)
        self.directory(version)
        if rollback:
            if not version["ever_accepted"]:
                raise RSIError("Rollback can only select a previously accepted version.")
        elif not version["eligible"]:
            raise RSIError("Policy has not passed promotion gates.")
        with self.seed.connection() as db:
            current = db.execute("SELECT active_id FROM policy_state WHERE singleton=1").fetchone()
            if not rollback and (current is None or current[0] != version["baseline_id"]):
                raise RSIError("Active baseline changed; evaluate the candidate again.")
            db.execute("UPDATE policy_state SET active_id=? WHERE singleton=1", (version_id,))
            db.execute("UPDATE policy_versions SET ever_accepted=1 WHERE id=?", (version_id,))
            db.execute("INSERT INTO policy_events(version_id,action,created_at,detail) VALUES(?,?,?,?)",
                       (version_id, "rollback" if rollback else "promote", now(), version["decision"]))
        return self.get(version_id)

    def relevant(self, query: str, limit: int = 4) -> list[dict]:
        terms = set(re.findall(r"[a-z0-9_]{3,}", query.lower()))
        with self.seed.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM experience ORDER BY updated_at DESC LIMIT 200")]
        ranked = []
        for row in rows:
            score = len(terms & set(re.findall(r"[a-z0-9_]{3,}", (row["task"] + " " + row["detail"]).lower())))
            if score:
                ranked.append((score, row))
        ranked.sort(key=lambda value: value[0], reverse=True)
        return [row for _, row in ranked[:limit]]


def remember_run(seed, run_id: str, outcome: str, detail: str) -> None:
    archive = Archive(seed)
    run = seed.get(run_id)
    with seed.connection() as db:
        db.execute(
            "INSERT INTO experience(source_run,task,outcome,detail,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(source_run) DO UPDATE SET outcome=excluded.outcome,detail=excluded.detail,updated_at=excluded.updated_at",
            (run_id, run["task"][:2000], outcome, detail[:1000], now()),
        )
