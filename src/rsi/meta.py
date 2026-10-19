"""Bounded mutation-instruction comparisons using matched policy descendants."""

import hashlib
import json
import math
from pathlib import Path
import uuid

from rsi.archive import Archive
from rsi.core import RSIError, git, now, require_clean
from rsi.evolution import Evolution, observed_tokens, trusted_fingerprint
from rsi.process import ProcessCancelled


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _instructions(path: Path) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 48000:
        raise RSIError("Mutation instructions must be a real UTF-8 text file of at most 12000 characters.")
    try:
        text = path.read_text(encoding="utf-8").strip()
    except UnicodeError as exc:
        raise RSIError("Mutation instructions must be UTF-8 text.") from exc
    if not text or len(text) > 12000 or "\0" in text:
        raise RSIError("Mutation instructions require 1–12000 characters without NUL bytes.")
    return text


class StrategyStore:
    def __init__(self, seed):
        self.seed = seed
        self.root = seed.state / "strategies"
        if self.root.is_symlink():
            raise RSIError("Strategy storage must not be a symlink.")
        self.root.mkdir(exist_ok=True)
        with seed.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mutation_strategies (
                    id TEXT PRIMARY KEY, parent_id TEXT, digest TEXT NOT NULL,
                    created_at TEXT NOT NULL, eligible INTEGER NOT NULL,
                    decision TEXT NOT NULL, report TEXT NOT NULL,
                    provenance TEXT NOT NULL, ever_accepted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS mutation_strategy_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), active_id TEXT NOT NULL
                );
            """)

    def get(self, strategy_id: str) -> dict:
        with self.seed.connection() as db:
            row = db.execute("SELECT * FROM mutation_strategies WHERE id=?", (strategy_id,)).fetchone()
        if row is None:
            raise RSIError("Unknown mutation strategy.")
        result = dict(row)
        path = self.root / strategy_id / "instructions.txt"
        _instructions(path)
        text = path.read_text(encoding="utf-8")
        if _digest(text) != result["digest"]:
            raise RSIError("Archived mutation instructions changed after recording.")
        result.update(text=text, path=str(path), eligible=bool(result["eligible"]),
                      ever_accepted=bool(result["ever_accepted"]),
                      report=json.loads(result["report"]), provenance=json.loads(result["provenance"]))
        return result

    def versions(self) -> list[dict]:
        with self.seed.connection() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM mutation_strategies ORDER BY created_at DESC")]
        return [self.get(strategy_id) for strategy_id in ids]

    def record(self, text: str, *, strategy_id: str, parent_id: str | None,
               eligible: bool, decision: str, report: dict, provenance: dict) -> dict:
        if not strategy_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in strategy_id):
            raise RSIError("Invalid mutation strategy ID.")
        directory = self.root / strategy_id
        directory.mkdir()
        (directory / "instructions.txt").write_text(text, encoding="utf-8")
        with self.seed.connection() as db:
            db.execute("INSERT INTO mutation_strategies(id,parent_id,digest,created_at,eligible,decision,report,provenance) "
                       "VALUES(?,?,?,?,?,?,?,?)", (strategy_id, parent_id, _digest(text), now(), int(eligible), decision,
                        json.dumps(report, allow_nan=False), json.dumps(provenance, allow_nan=False)))
        return self.get(strategy_id)

    def active(self) -> dict:
        with self.seed.connection() as db:
            row = db.execute("SELECT active_id FROM mutation_strategy_state WHERE singleton=1").fetchone()
        if row:
            return self.get(row[0])
        from rsi.evolution import MUTATION_PROMPT
        text = MUTATION_PROMPT.strip()
        strategy_id = "initial-" + _digest(text)[:12]
        # A status query may race the first worker. Serialize creation in SQLite
        # without taking the repository lock, which a calling worker may own.
        with self.seed.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT active_id FROM mutation_strategy_state WHERE singleton=1").fetchone()
            if row:
                strategy_id = row[0]
            else:
                exists = db.execute("SELECT id FROM mutation_strategies WHERE id=?", (strategy_id,)).fetchone()
                if not exists:
                    directory = self.root / strategy_id
                    directory.mkdir(exist_ok=True)
                    path = directory / "instructions.txt"
                    if path.exists() and (path.is_symlink() or path.read_text(encoding="utf-8") != text):
                        raise RSIError("Initial mutation strategy artifact changed before initialization.")
                    path.write_text(text, encoding="utf-8")
                    provenance = {"initial": True, "base_commit": git(self.seed.repo, "rev-parse", "HEAD").decode().strip(),
                                  "trusted_fingerprint": trusted_fingerprint(self.seed),
                                  "model": self.seed.config.get("model"), "reasoning_effort": self.seed.config.get("reasoning_effort")}
                    db.execute("INSERT INTO mutation_strategies(id,digest,created_at,eligible,decision,report,provenance,ever_accepted) "
                               "VALUES(?,?,?,1,?,?,?,1)", (strategy_id, _digest(text), now(), "Initial fixed mutation instructions.",
                                                         json.dumps({"status": "initial"}), json.dumps(provenance)))
                db.execute("INSERT INTO mutation_strategy_state(singleton,active_id) VALUES(1,?)", (strategy_id,))
        return self.get(strategy_id)

    def activate(self, strategy_id: str, *, rollback: bool = False) -> dict:
        with self.seed.operation():
            require_clean(self.seed.repo)
            strategy = self.get(strategy_id)
            if rollback and not strategy["ever_accepted"]:
                raise RSIError("Strategy rollback requires a previously accepted version.")
            if not rollback and not strategy["eligible"]:
                raise RSIError("Mutation strategy has not passed matched-comparison gates.")
            provenance = strategy["provenance"]
            if provenance:
                if trusted_fingerprint(self.seed) != provenance["trusted_fingerprint"]:
                    raise RSIError("Trusted evaluator or configuration changed; repeat the meta experiment.")
                if (self.seed.config.get("model"), self.seed.config.get("reasoning_effort")) != (
                        provenance["model"], provenance["reasoning_effort"]):
                    raise RSIError("Model configuration changed; repeat the meta experiment.")
                if git(self.seed.repo, "rev-parse", "HEAD").decode().strip() != provenance["base_commit"]:
                    raise RSIError("Base code changed; repeat the meta experiment.")
                if not provenance.get("initial"):
                    archive = Archive(self.seed)
                    if archive.ensure_baseline()["id"] != provenance["active_policy_id"]:
                        raise RSIError("Active policy changed; repeat matched descendants from the current baseline.")
                    parent = archive.get(provenance["policy_parent_id"])
                    archive.directory(parent)
                    if parent["digest"] != provenance["policy_parent_digest"]:
                        raise RSIError("The matched parent policy changed.")
                    if not rollback and self.active()["id"] != provenance["baseline_strategy_id"]:
                        raise RSIError("Active mutation strategy changed; repeat the meta experiment.")
            with self.seed.connection() as db:
                db.execute("UPDATE mutation_strategy_state SET active_id=? WHERE singleton=1", (strategy_id,))
                db.execute("UPDATE mutation_strategies SET ever_accepted=1 WHERE id=?", (strategy_id,))
            return self.get(strategy_id)


class MetaEvolution:
    def __init__(self, seed, *, evolution_factory=Evolution):
        self.seed = seed
        self.store = StrategyStore(seed)
        self.evolution_factory = evolution_factory

    def propose(self, task: str = "") -> dict:
        """Ask the existing Codex backend for text only, using development summaries."""
        from rsi.backends.codex import CodexBackend
        from rsi.core import permission_args
        if not self.seed.config.get("model"):
            raise RSIError("Set an explicit available model before proposing mutation instructions.")
        with self.seed.operation():
            require_clean(self.seed.repo)
            baseline = self.store.active()
            summaries = []
            for version in Archive(self.seed).versions()[:8]:
                development = version["metrics"].get("candidate", {}).get("development")
                if development:
                    summaries.append({"score": development["score"], "success_rate": development["success_rate"],
                                      "failures": development.get("errors", {}),
                                      "failed_case_count": sum(not case.get("success", False) for case in development.get("cases", []))})
            proposal_id = "proposal-" + uuid.uuid4().hex[:12]
            directory = self.seed.state / "meta-proposals" / proposal_id
            workspace, artifacts = directory / "workspace", directory / "artifacts"
            workspace.mkdir(parents=True)
            git(workspace, "init")
            prompt = (
                "Draft improved mutation instructions for a context-policy improvement worker. "
                "Write only instructions.txt: 1-12000 characters of plain UTF-8 instructions. "
                "Preserve the fixed supervisor, evaluator and policy JSON contract. "
                "Use only the development failure summaries below; do not invent benchmark results. "
                "Do not write program code, commit, contact external services, or start processes beyond this task.\n"
                "Current instructions:\n" + baseline["text"] + "\nDevelopment summaries:\n" +
                json.dumps(summaries, allow_nan=False)[:20000] + "\nUser guidance:\n" + task[:4000]
            )
            from rsi.queue import QueueStore
            queue = QueueStore(self.seed.state / "state.sqlite3")
            if queue.status()["paused"]:
                raise RSIError("Scheduling is paused; inspect and explicitly resume before proposing instructions.")
            limit = self.seed.config.get("max_observed_tokens", 200000)
            # An unknown-usage fence survives a hard supervisor crash. Persist
            # completed telemetry before clearing it, so no crash window can
            # silently permit an unaccounted proposal followed by new work.
            queue.record_usage(None, limit=limit)
            result = CodexBackend(self.seed.codex, self.seed.config["model"], self.seed.config.get("reasoning_effort")).run(
                prompt, workspace, artifacts, self.seed.config["task_timeout_seconds"],
                permission_options=permission_args(workspace=workspace, restricted=True),
            )
            tokens = observed_tokens(result.to_dict())
            if type(tokens) is not int or tokens < 0:
                tokens = None
            queue.record_usage(tokens, limit=limit, usage_id=proposal_id)
            if tokens is not None:
                with self.seed.connection() as db:
                    db.execute("UPDATE scheduling SET usage_unknown=0,paused=CASE WHEN observed_tokens>=? THEN 1 ELSE 0 END,"
                               "reason=CASE WHEN observed_tokens>=? THEN 'local_usage_cap' ELSE '' END "
                               "WHERE reason='usage_unknown'", (limit, limit))
            if not result.success:
                if result.error_category in ("quota", "auth"):
                    from rsi.queue import QueueStore
                    QueueStore(self.seed.state / "state.sqlite3").pause(result.error_category)
                error = RSIError(f"Mutation proposal failed: {result.error}; see {result.execution_path}")
                error.category = result.error_category or "infrastructure"
                raise error
            changed = git(workspace, "status", "--porcelain", "--untracked-files=all").decode().splitlines()
            if any(line[3:] != "instructions.txt" for line in changed):
                raise RSIError("Mutation proposer wrote files outside its text-only contract.")
            text = _instructions(workspace / "instructions.txt")
            candidate = directory / "instructions.txt"
            candidate.write_text(text, encoding="utf-8")
            candidate.with_suffix(candidate.suffix + ".proposal.json").write_text(json.dumps({
                "proposal_id": proposal_id, "candidate_digest": _digest(text), "execution": result.to_dict(),
                "baseline_strategy_id": baseline["id"], "feedback_split": "development",
            }, indent=2, allow_nan=False), encoding="utf-8")
            return {"candidate_path": str(candidate), "execution": result.to_dict(), "proposal_id": proposal_id}

    def experiment(self, candidate: Path, trials: int = 1, parent_id=None, task: str = "") -> dict:
        if type(trials) is not int or not 1 <= trials <= 3:
            raise RSIError("Meta experiments require 1–3 matched trials.")
        text = _instructions(candidate)
        if not self.seed.config.get("model"):
            raise RSIError("Set an explicit available model before comparing mutation strategies.")
        cap = self.seed.config.get("max_meta_observed_tokens", self.seed.config.get("max_observed_tokens", 200000))
        tolerance = self.seed.config.get("meta_token_tolerance", 0.10)
        if type(cap) is not int or cap < 1 or type(tolerance) not in (int, float) or not math.isfinite(tolerance) or not 0 <= tolerance <= 1:
            raise RSIError("Meta token cap must be positive and comparison tolerance must be between 0 and 1.")
        with self.seed.operation():
            require_clean(self.seed.repo)
            archive = Archive(self.seed)
            active = archive.ensure_baseline()
            parent = archive.get(parent_id) if parent_id else active
            archive.directory(parent)
            baseline = self.store.active()
            provenance = {"base_commit": git(self.seed.repo, "rev-parse", "HEAD").decode().strip(),
                "trusted_fingerprint": trusted_fingerprint(self.seed), "active_policy_id": active["id"],
                "policy_parent_id": parent["id"], "policy_parent_digest": parent["digest"],
                "baseline_strategy_id": baseline["id"], "baseline_prompt_digest": baseline["digest"],
                "candidate_prompt_digest": _digest(text), "model": self.seed.config["model"],
                "reasoning_effort": self.seed.config.get("reasoning_effort"), "memory_enabled": False}
        strategy_id = "strategy-" + uuid.uuid4().hex[:12]
        report = {"status": "completed", "trials_requested": trials, "pairs": [], "executions": [],
                  "observed_tokens": 0, "usage_known": True, "token_cap": cap, "token_tolerance": tolerance,
                  "small_sample": True, "causal_or_statistical_claim": False, "proposal_observed_tokens": 0}
        interrupted = False
        stopped = False
        metadata_path = Path(candidate).with_suffix(Path(candidate).suffix + ".proposal.json")
        report["proposal_source"] = "provided_instructions"
        if metadata_path.exists():
            if metadata_path.is_symlink() or metadata_path.stat().st_size > 1000000:
                raise RSIError("Invalid mutation proposal metadata.")
            try:
                proposal = json.loads(metadata_path.read_text(encoding="utf-8"))
                if proposal["candidate_digest"] != _digest(text):
                    raise RSIError("Model-proposed instructions changed after proposal accounting.")
                execution = proposal["execution"]
                if not isinstance(execution, dict) or execution.get("success") is not True:
                    raise RSIError("Mutation proposal did not complete successfully.")
            except (ValueError, KeyError, TypeError) as exc:
                raise RSIError("Invalid mutation proposal metadata.") from exc
            tokens = observed_tokens(execution)
            if type(tokens) is not int or tokens < 0:
                tokens = None
            report["proposal_source"] = "model_proposal"
            report["executions"].append({"trial": 0, "label": "candidate", "phase": "meta_proposal",
                                         "source_run": proposal.get("proposal_id"), "execution": execution,
                                         "observed_tokens": tokens})
            report["usage_known"] = tokens is not None
            report["observed_tokens"] = tokens or 0
            report["proposal_observed_tokens"] = tokens
            if tokens is None or tokens >= cap:
                report.update(status="inconclusive", error="Proposal usage is unknown or leaves no observed-work budget.")
                stopped = True
        for trial in range(0 if stopped else trials):
            pair = {"trial": trial + 1}
            report["pairs"].append(pair)
            # Alternate order to reduce a fixed ordering confound; every parent remains identical.
            for label in (("baseline", "candidate") if trial % 2 == 0 else ("candidate", "baseline")):
                if report["observed_tokens"] >= cap:
                    report.update(status="inconclusive", error="Observed model-work cap reached before the next call.")
                    stopped = True
                    break
                cursor = self.seed.run_cursor()
                version = None
                try:
                    if (Archive(self.seed).ensure_baseline()["id"] != active["id"]
                            or trusted_fingerprint(self.seed) != provenance["trusted_fingerprint"]
                            or git(self.seed.repo, "rev-parse", "HEAD").decode().strip() != provenance["base_commit"]
                            or (self.seed.config.get("model"), self.seed.config.get("reasoning_effort")) != (
                                provenance["model"], provenance["reasoning_effort"])
                            or self.store.active()["id"] != baseline["id"]):
                        raise RSIError("Matched-comparison state changed during the meta experiment.")
                    version = self.evolution_factory(self.seed).experiment(
                        task, parent_id=parent["id"], memory_enabled=False,
                        strategy_text=baseline["text"] if label == "baseline" else text,
                        strategy_version=baseline["id"] if label == "baseline" else strategy_id,
                    )
                    if (version["provenance"].get("model"), version["provenance"].get("reasoning_effort")) != (
                            provenance["model"], provenance["reasoning_effort"]):
                        raise RSIError("A descendant used a different requested model configuration.")
                    pair[label] = {"version_id": version["id"], "metrics": version["metrics"],
                                   "execution": version["provenance"]["execution"]}
                except KeyboardInterrupt:
                    interrupted = stopped = True
                    report.update(status="interrupted", error="Meta experiment interrupted; completed observations retained.")
                except Exception as exc:
                    category = getattr(exc, "category", "infrastructure")
                    new_runs = self.seed.runs_after(cursor)
                    for run in new_runs:
                        category = run.get("error_category") or category
                    message = str(exc).lower()
                    if any(term in message for term in ("quota", "usage limit", "rate limit")):
                        category = "quota"
                    elif any(term in message for term in ("authentication", "login", "unauthorized")):
                        category = "auth"
                    if isinstance(exc, ProcessCancelled):
                        category = "cancelled"
                    report.update(status="paused" if category in ("quota", "auth") else "inconclusive",
                                  error=str(exc)[:2000], error_category=category)
                    if category in ("quota", "auth"):
                        from rsi.queue import QueueStore
                        QueueStore(self.seed.state / "state.sqlite3").pause(category)
                    stopped = True
                # Include every observed model run, including failed calls which never yielded an archive version.
                executions = []
                for run in self.seed.runs_after(cursor):
                    if run.get("execution"):
                        execution = json.loads(run["execution"]) if isinstance(run["execution"], str) else run["execution"]
                        executions.append({"source_run": run["id"], "execution": execution})
                if version is not None and not any(item["source_run"] == version.get("source_run") for item in executions):
                    executions.append({"source_run": version.get("source_run"), "execution": version["provenance"]["execution"]})
                if not executions:
                    report["usage_known"] = False
                for item in executions:
                    tokens = observed_tokens(item["execution"])
                    if type(tokens) is not int or tokens < 0:
                        tokens = None
                    report["executions"].append({"trial": trial + 1, "label": label, **item, "observed_tokens": tokens})
                    if tokens is None:
                        report["usage_known"] = False
                    else:
                        report["observed_tokens"] += tokens
                if not report["usage_known"] or report["observed_tokens"] > cap:
                    if report["status"] == "completed":
                        report.update(status="inconclusive", error="Unknown usage or observed model-work cap exceeded.")
                    stopped = True
                if stopped:
                    break
            if stopped:
                break
        eligible, decision = self._gate(report)
        report["known_observed_tokens"] = report["observed_tokens"]
        if not report["usage_known"]:
            report["observed_tokens"] = None
        record = self.store.record(text, strategy_id=strategy_id, parent_id=baseline["id"], eligible=eligible,
                                   decision=decision, report=report, provenance=provenance)
        if interrupted:
            raise KeyboardInterrupt
        return record

    def _gate(self, report) -> tuple[bool, str]:
        if report["status"] != "completed" or not report["usage_known"]:
            return False, "Matched comparison is incomplete or usage is unknown; no promotion established."
        totals = {"baseline": 0, "candidate": 0}
        scores = {"baseline": 0.0, "candidate": 0.0}
        contexts = {"baseline": 0.0, "candidate": 0.0}
        for field in ("actual_model", "actual_reasoning_effort", "codex_version"):
            known = {entry["execution"][field] for entry in report["executions"]
                     if entry.get("phase") != "meta_proposal" and isinstance(entry["execution"].get(field), str)}
            if len(known) > 1:
                return False, "Observed model or backend configuration changed across matched descendants."
        for entry in report["executions"]:
            totals[entry["label"]] += entry["observed_tokens"]
        for pair in report["pairs"]:
            for split in ("development", "selection"):
                baseline = pair["baseline"]["metrics"]["candidate"][split]
                candidate = pair["candidate"]["metrics"]["candidate"][split]
                if baseline["status"] != "completed" or candidate["status"] != "completed":
                    return False, "A descendant evaluation failed."
                if (baseline["benchmark_digest"], baseline["evaluator_version"]) != (candidate["benchmark_digest"], candidate["evaluator_version"]):
                    return False, "Descendants used different evaluation configurations."
                if candidate["score"] < baseline["score"] or candidate["success_rate"] < baseline["success_rate"]:
                    return False, "A matched descendant regressed on task quality."
                if candidate["context_chars"] > self.seed.config.get("max_context_chars", 6000):
                    return False, "A descendant exceeded the context budget."
                if split == "selection":
                    for label, value in (("baseline", baseline), ("candidate", candidate)):
                        scores[label] += value["score"]
                        contexts[label] += value.get("policy_prompt_chars", value["context_chars"])
        count = len(report["pairs"])
        gain = (scores["candidate"] - scores["baseline"]) / count
        if gain >= self.seed.config.get("meta_min_score_gain", self.seed.config.get("min_score_gain", 0.01)):
            if totals["candidate"] <= totals["baseline"] * (1 + report["token_tolerance"]):
                return True, "Matched descendants improved selection quality at comparable observed model work."
            return False, "Quality improved, but observed model work exceeded the comparison tolerance."
        if abs(gain) <= 1e-12 and totals["candidate"] < totals["baseline"] and contexts["candidate"] <= contexts["baseline"]:
            return True, "Matched descendants retained quality and context size with less observed model work."
        return False, "No matched selection-quality gain or model-work reduction was established."
