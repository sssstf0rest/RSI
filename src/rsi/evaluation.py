"""Fixed local retrieval grading; answer labels never cross the policy boundary."""

import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from rsi.policy_runner import PolicyError, validate_request


EVALUATOR_VERSION = "context-v1"


class EvaluationError(ValueError):
    pass


class ContextEvaluator:
    def __init__(self, dataset: Path, *, external_audit: bool = False):
        self.path = Path(dataset).resolve()
        self.external_audit = external_audit
        if external_audit and self.path.is_relative_to((Path(__file__).resolve().parents[2] / "benchmarks").resolve()):
            raise EvaluationError("Final audit data must be provisioned outside the public benchmark directory.")
        if external_audit and any(self.path.is_relative_to(root) for root in
                                  (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())):
            raise EvaluationError("Final audit answers must be outside readable runtime roots.")
        raw = self.path.read_bytes()
        if len(raw) > 4000000:
            raise EvaluationError("Benchmark exceeds its size limit.")
        self.digest = hashlib.sha256(raw).hexdigest()
        try:
            self.data = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise EvaluationError("Benchmark must be UTF-8 JSON.") from exc
        if not isinstance(self.data, dict) or not isinstance(self.data.get("cases"), list) or not self.data["cases"]:
            raise EvaluationError("Benchmark requires nonempty cases.")
        try:
            validated = validate_request({"query": "benchmark validation", "documents": self.data.get("documents"),
                                          "max_context_chars": self.data.get("max_context_chars", 8000)})
        except PolicyError as exc:
            raise EvaluationError(str(exc)) from exc
        self.documents = validated["documents"]
        self.max_context_chars = validated["max_context_chars"]
        ids = {doc["id"] for doc in self.documents}
        case_ids = set()
        queries = set()
        for case in self.data["cases"]:
            if not isinstance(case, dict) or set(case) != {"id", "query", "expected_ids", "split"}:
                raise EvaluationError("Cases require exactly id, query, expected_ids, and split.")
            case_id = case["id"]
            if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", case_id) or case_id in case_ids:
                raise EvaluationError("Case IDs must be unique safe strings across all splits.")
            if not isinstance(case["query"], str) or not case["query"] or len(case["query"]) > 32000:
                raise EvaluationError("Case query must be a nonempty bounded string.")
            normalized_query = " ".join(case["query"].casefold().split())
            if normalized_query in queries:
                raise EvaluationError("Duplicate queries across cases or splits are not allowed.")
            expected = case["expected_ids"]
            if not isinstance(expected, list) or not expected or any(not isinstance(item, str) for item in expected):
                raise EvaluationError("Expected document IDs must be a nonempty list.")
            if len(set(expected)) != len(expected) or not set(expected) <= ids:
                raise EvaluationError("Expected document IDs must be unique and exist in the corpus.")
            allowed_splits = {"audit"} if external_audit else {"development", "selection"}
            if not isinstance(case["split"], str) or case["split"] not in allowed_splits:
                raise EvaluationError("Public datasets allow development/selection only; audit needs a separately provisioned dataset.")
            case_ids.add(case_id)
            queries.add(normalized_query)

    def evaluate(self, policy_dir: Path, runner, *, artifacts: Path, split: str = "development",
                 memory_enabled: bool = True, experience: list[dict] | None = None) -> dict:
        if split not in {"development", "selection"} or self.external_audit:
            raise EvaluationError("Use evaluate_audit explicitly with a separately provisioned final-audit dataset.")
        return self._evaluate(policy_dir, runner, artifacts=artifacts, split=split,
                              memory_enabled=memory_enabled, experience=experience)

    def evaluate_audit(self, policy_dir: Path, runner, *, artifacts: Path,
                       memory_enabled: bool = True, experience: list[dict] | None = None) -> dict:
        if not self.external_audit:
            raise EvaluationError("Final audit requires an external audit dataset.")
        if self.path.is_relative_to(Path(policy_dir).resolve()):
            raise EvaluationError("Final audit answers must not be copied into the policy workspace.")
        return self._evaluate(policy_dir, runner, artifacts=artifacts, split="audit",
                              memory_enabled=memory_enabled, experience=experience)

    def _evaluate(self, policy_dir, runner, *, artifacts, split, memory_enabled, experience):
        selected_cases = [case for case in self.data["cases"] if case["split"] == split]
        if not selected_cases:
            raise EvaluationError(f"Benchmark has no {split} cases.")
        artifacts = Path(artifacts)
        artifacts.mkdir(parents=True, exist_ok=True)
        cases = []
        errors = {"infrastructure": [], "candidate": []}
        for case in selected_cases:
            request = {"query": case["query"], "documents": self.documents,
                       "experience": experience or [] if memory_enabled else [],
                       "max_context_chars": self.max_context_chars}
            try:
                # IDs/labels are grader-owned. Random artifact names do not expose case IDs.
                result = runner.run(Path(policy_dir), request, artifacts=artifacts / uuid.uuid4().hex)
                if result.get("status") != "completed":
                    raise PolicyError("Policy execution did not complete.")
                actual = set(result["selected_ids"])
                expected = set(case["expected_ids"])
                correct = len(actual & expected)
                recall = correct / len(expected)
                precision = correct / len(actual) if actual else 0.0
                f1 = 2 * recall * precision / (recall + precision) if recall + precision else 0.0
                cases.append({"id": case["id"], "selected_ids": result["selected_ids"],
                              "expected_ids": case["expected_ids"], "status": "completed", "score": f1,
                              "evidence_recall": recall, "evidence_precision": precision,
                              "success": actual == expected, "context_chars": result["context_chars"],
                              "policy_prompt_chars": result.get("policy_prompt_chars", result["context_chars"] + len(result.get("instructions", "")) + 1),
                              "elapsed_seconds": result["elapsed_seconds"]})
            except PolicyError as exc:
                errors[exc.category].append({"case_id": case["id"], "detail": str(exc)})
                cases.append({"id": case["id"], "status": exc.category + "_error", "score": 0.0,
                              "evidence_recall": 0.0, "evidence_precision": 0.0, "success": False,
                              "context_chars": 0, "policy_prompt_chars": 0})
        n = len(cases)
        report = {"status": "failed" if any(errors.values()) else "completed",
                  "score": sum(case["score"] for case in cases) / n,
                  "evidence_recall": sum(case["evidence_recall"] for case in cases) / n,
                  "evidence_precision": sum(case["evidence_precision"] for case in cases) / n,
                  "success_rate": sum(case["success"] for case in cases) / n,
                  "context_chars": sum(case["context_chars"] for case in cases) / n,
                  "policy_prompt_chars": sum(case["policy_prompt_chars"] for case in cases) / n,
                  "cases": cases, "errors": errors, "evaluator_version": EVALUATOR_VERSION,
                  "benchmark_digest": self.digest, "split": split, "memory_enabled": memory_enabled,
                  "benchmark_kind": "synthetic_smoke", "held_out_claim": False}
        # Grade output remains outside each worker's readable workspace.
        (artifacts / "evaluation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
