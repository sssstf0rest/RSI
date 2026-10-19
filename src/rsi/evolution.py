"""Bounded proposal -> regression -> behavioral comparison -> explicit promotion."""

import hashlib
import json
from pathlib import Path

from rsi.archive import Archive, policy_digest, remember_run
from rsi.core import RSIError, git, require_clean
from rsi.evaluation import ContextEvaluator
from rsi.policy_runner import PolicyRunner


STRATEGY = "fixed-context-v1"
MUTATION_PROMPT = """Improve only src/rsi_policy/ for retrieval and context selection.
The policy.py program reads JSON from stdin and returns selected_ids and instructions.
Keep its JSON contract, standard-library implementation, and resource limits.
Generalize from development feedback rather than hardcoding queries or answer IDs.
Do not modify the supervisor, benchmarks, scoring, regression tests or instructions files.
The accepted policy will select context for subsequent real Codex tasks.
"""


def observed_tokens(execution: dict) -> int | None:
    usage = execution.get("usage") or {}
    if type(usage.get("total_tokens")) is int:
        return usage["total_tokens"]
    values = [usage.get("input_tokens"), usage.get("output_tokens")]
    if all(type(value) is int and value >= 0 for value in values):
        return sum(values)
    return None


def trusted_fingerprint(seed) -> str:
    """Invalidate a promotion when the evaluator, checks or configuration changes."""
    paths = [seed.repo / "rsi.toml"]
    benchmark = Path(seed.config.get("benchmark_path", "benchmarks/context.json"))
    paths.append(benchmark if benchmark.is_absolute() else seed.repo / benchmark)
    for folder in ("src/rsi", "tests", "benchmarks"):
        paths.extend(path for path in (seed.repo / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py", ".json"))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.resolve()).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def task_context(seed, task: str, artifacts: Path, *, memory_enabled: bool = True) -> tuple[str, str | None]:
    if not (seed.repo / "src" / "rsi_policy" / "policy.py").is_file():
        return "", None  # Compatible with repositories bootstrapped before policies.
    archive = Archive(seed)
    active = archive.ensure_baseline()
    documents = []
    paths = [seed.repo / "README.md", seed.repo / "rsi.toml"]
    for folder in ("docs", "tasks"):
        paths.extend(sorted((seed.repo / folder).rglob("*.md")))
    for path in paths[:40]:
        if path.is_file() and not path.is_symlink():
            documents.append({"id": path.relative_to(seed.repo).as_posix(),
                              "text": path.read_text(encoding="utf-8", errors="replace")[:12_000]})
    experiences = archive.relevant(task, seed.config.get("memory_limit", 4)) if memory_enabled else []
    for entry in experiences:
        documents.append({"id": "experience:" + entry["source_run"],
                          "text": f"Task: {entry['task']}\nObserved {entry['outcome']}: {entry['detail']}"})
    runner = PolicyRunner(seed.codex, seed.sandbox_home, seed.config.get("policy_timeout_seconds", 60))
    result = runner.run(archive.directory(active), {
        "query": task, "documents": documents, "experience": experiences,
        "max_context_chars": seed.config.get("max_context_chars", 6000),
    }, artifacts=artifacts / "context")
    (artifacts / "context.json").write_text(json.dumps({
        "policy_version": active["id"], "policy_digest": active["digest"],
        "memory_enabled": memory_enabled, "experience_sources": [row["source_run"] for row in experiences],
        "selected_ids": result["selected_ids"], "context_chars": result["context_chars"],
    }, indent=2), encoding="utf-8")
    return result["instructions"] + "\n" + result["context"], active["id"]


class Evolution:
    def __init__(self, seed, *, runner=None):
        self.seed = seed
        self.archive = Archive(seed)
        self.runner = runner or PolicyRunner(seed.codex, seed.sandbox_home, seed.config.get("policy_timeout_seconds", 60))
        benchmark = Path(seed.config.get("benchmark_path", "benchmarks/context.json"))
        self.benchmark = benchmark if benchmark.is_absolute() else seed.repo / benchmark
        self.evaluator = ContextEvaluator(self.benchmark)

    def initialize(self) -> dict:
        with self.seed.operation():
            return self.archive.ensure_baseline()

    def evaluate(self, version_id=None, *, memory_enabled=True) -> dict:
        with self.seed.operation():
            version = self.archive.get(version_id) if version_id else self.archive.ensure_baseline()
            artifacts = self.seed.state / "evaluations" / version["id"] / ("memory" if memory_enabled else "no-memory")
            experiences = self.archive.relevant("context retrieval policy failures", self.seed.config.get("memory_limit", 4))
            return {split: self.evaluator.evaluate(self.archive.directory(version), self.runner,
                     artifacts=artifacts / split, split=split, memory_enabled=memory_enabled,
                     experience=experiences) for split in ("development", "selection")}

    def experiment(self, task="", *, candidate: Path | None = None, parent_id=None,
                   memory_enabled=True, strategy_text=None, strategy_version=None) -> dict:
        # Each stage owns the repository operation lock. A selected baseline is
        # rechecked at promotion, so intervening explicit releases cannot race it.
        with self.seed.operation():
            require_clean(self.seed.repo)
            starting_fingerprint = trusted_fingerprint(self.seed)
            baseline = self.archive.ensure_baseline()
            parent = self.archive.get(parent_id) if parent_id else baseline
            self.archive.directory(parent)
            feedback = self.evaluator.evaluate(self.archive.directory(parent), self.runner,
                artifacts=self.seed.state / "feedback" / parent["id"], split="development",
                memory_enabled=memory_enabled, experience=self.archive.relevant(task))
            if feedback["errors"]["infrastructure"]:
                raise RSIError("Baseline evaluation infrastructure failed; no model proposal was started.")
            self._check_usage_budget()
        if candidate is None and not self.seed.config.get("model"):
            raise RSIError("Set an explicit available model before online evolution comparisons.")
        if strategy_text is None:
            from rsi.meta import StrategyStore
            strategy = StrategyStore(self.seed).active()
            strategy_text, strategy_version = strategy["text"], strategy["id"]
        strategy_version = strategy_version or STRATEGY
        prompt = strategy_text + "\nUser guidance: " + task + "\nDevelopment feedback:\n" + json.dumps(feedback)
        run = self.seed.run_task(prompt, purpose="policy", parent_id=parent["id"],
            parent_policy=self.archive.directory(parent), candidate_policy=candidate,
            memory_enabled=memory_enabled,
            timeout_seconds=self.seed.config.get("proposal_timeout_seconds", 600))
        run = self.seed.verify(run["id"])
        with self.seed.operation():
            require_clean(self.seed.repo)
            if trusted_fingerprint(self.seed) != starting_fingerprint:
                raise RSIError("Trusted source changed during the experiment; comparison is invalid.")
            if self.archive.active()["id"] != baseline["id"]:
                raise RSIError("Active policy changed during proposal; repeat comparison.")
            workspace = self.seed.workspace(run)
            patch_digest = hashlib.sha256(self.seed.snapshot(run["id"])).hexdigest()
            if patch_digest != run["verified_digest"]:
                raise RSIError("Policy changed after regression verification.")
            source = workspace / "src" / "rsi_policy"
            before = policy_digest(source)
            artifacts = self.seed.artifacts(run["id"]) / "behavior"
            metrics = {"baseline": {}, "candidate": {}}
            # Use exactly the same experience bundle for both versions.
            experiences = self.archive.relevant(task, self.seed.config.get("memory_limit", 4)) if memory_enabled else []
            for split in ("development", "selection"):
                for label, directory in (("baseline", self.archive.directory(baseline)), ("candidate", source)):
                    metrics[label][split] = self.evaluator.evaluate(directory, self.runner,
                        artifacts=artifacts / label / split, split=split,
                        memory_enabled=memory_enabled, experience=experiences)
            if policy_digest(source) != before or hashlib.sha256(self.seed.snapshot(run["id"])).hexdigest() != patch_digest:
                raise RSIError("Candidate changed while evaluating behavior.")
            execution_path = self.seed.artifacts(run["id"]) / "execution.json"
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
            tokens = observed_tokens(execution)
            eligible, decision = self._gate(metrics, tokens)
            provenance = {"base_commit": run["base"], "patch_digest": patch_digest,
                          "trusted_fingerprint": starting_fingerprint, "model": self.seed.config.get("model"),
                          "reasoning_effort": self.seed.config.get("reasoning_effort"), "execution": execution,
                          "strategy_digest": hashlib.sha256(strategy_text.encode()).hexdigest(),
                          "memory_enabled": memory_enabled, "experience_sources": [row["source_run"] for row in experiences]}
            version = self.archive.record(source, parent_id=parent["id"], baseline_id=baseline["id"],
                source_run=run["id"], metrics=metrics, eligible=eligible, decision=decision,
                provenance=provenance, strategy_version=strategy_version)
            remember_run(self.seed, run["id"], "eligible" if eligible else "rejected", decision)
            return version

    def _gate(self, metrics, tokens) -> tuple[bool, str]:
        if tokens is None:
            return False, "Observed model usage is unknown; budget compliance was not established."
        if tokens > self.seed.config.get("max_observed_tokens", 200_000):
            return False, "Observed proposal usage exceeded the local scheduling budget."
        improved = False
        shorter = False
        context_increased = False
        for split in ("development", "selection"):
            base, candidate = metrics["baseline"][split], metrics["candidate"][split]
            if base["status"] != "completed" or candidate["status"] != "completed":
                return False, f"{split} evaluation failed; inspect categorized errors."
            if (candidate["benchmark_digest"], candidate["evaluator_version"]) != (base["benchmark_digest"], base["evaluator_version"]):
                return False, "Evaluation configuration changed during comparison."
            if candidate["score"] < base["score"] or candidate["success_rate"] < base["success_rate"]:
                return False, f"Task quality regressed on {split}."
            if split == "selection" and candidate["score"] - base["score"] >= self.seed.config.get("min_score_gain", 0.01):
                improved = True
            base_chars = base.get("policy_prompt_chars", base["context_chars"])
            candidate_chars = candidate.get("policy_prompt_chars", candidate["context_chars"])
            if candidate_chars > base_chars:
                context_increased = True
            elif split == "selection" and candidate_chars < base_chars:
                shorter = True
        shorter = shorter and not context_increased
        if not (improved or shorter):
            return False, "No selection-quality improvement or reduction in context at equal quality."
        return True, "Regressions passed; selection quality improved." if improved else "Regressions passed; quality retained with less context."

    def _check_usage_budget(self):
        from rsi.queue import QueueStore
        state = QueueStore(self.seed.state / "state.sqlite3").status()
        if state["paused"]:
            raise RSIError(f"Scheduling paused ({state['reason']}); recover and explicitly resume first.")

    def promote(self, version_id: str, *, rollback: bool = False) -> dict:
        with self.seed.operation():
            version = self.archive.get(version_id)
            if not rollback:
                provenance = version["provenance"]
                if provenance.get("trusted_fingerprint") != trusted_fingerprint(self.seed):
                    raise RSIError("Trusted evaluator/check configuration changed; repeat the experiment.")
                if git(self.seed.repo, "rev-parse", "HEAD").decode().strip() != provenance.get("base_commit"):
                    raise RSIError("Base code changed; repeat the experiment before promotion.")
            require_clean(self.seed.repo)
            # Activation switches an immutable policy pointer. The source
            # checkout stays clean, so another task can start immediately.
            return self.archive.activate(version_id, rollback=rollback)
