import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
import uuid

from rsi.evaluation import ContextEvaluator, EvaluationError
from rsi.policy_runner import PolicyError, PolicyRunner
from rsi.process import execute


REPO = Path(__file__).resolve().parents[1]


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("rsi-eval-" + uuid.uuid4().hex)
        self.root.mkdir(mode=0o755)
        self.addCleanup(self.cleanup)
        self.evaluator = ContextEvaluator(REPO / "benchmarks" / "context.json")
        self.runner = PolicyRunner("codex", self.root / "sandbox-home", executor=self.fixture_execute)

    def cleanup(self):
        if self.root.resolve().parent != Path(tempfile.gettempdir()).resolve():
            raise RuntimeError("Unsafe cleanup path")
        def retry(function, path, info):
            os.chmod(path, stat.S_IWRITE)
            function(path)
        shutil.rmtree(self.root, onerror=retry)

    def fixture_execute(self, command, **kwargs):
        self.assertEqual(command[1], "sandbox")
        return execute(command[command.index("--") + 1:], **kwargs)

    def evaluate(self, policy, **kwargs):
        return self.evaluator.evaluate(policy, self.runner, artifacts=self.root / uuid.uuid4().hex, **kwargs)

    def test_measured_lexical_improvement_uses_real_subprocesses(self):
        baseline = self.evaluate(REPO / "src" / "rsi_policy")
        improved = self.evaluate(REPO / "benchmarks" / "examples" / "lexical")
        self.assertEqual(baseline["status"], "completed")
        self.assertGreater(improved["score"], baseline["score"])
        self.assertLess(improved["context_chars"], baseline["context_chars"])
        self.assertEqual(improved["score"], 1.0)
        self.assertFalse(improved["held_out_claim"])
        self.assertEqual(len(improved["cases"]), 12)

    def test_grading_answers_never_enter_policy_request(self):
        test = self
        class Inspector:
            def run(self, policy_dir, request, *, artifacts):
                test.assertEqual(set(request), {"query", "documents", "experience", "max_context_chars"})
                test.assertNotIn("expected_ids", json.dumps(request))
                return {"status": "completed", "selected_ids": [], "context_chars": 0, "elapsed_seconds": 0}
        report = self.evaluator.evaluate(REPO / "src" / "rsi_policy", Inspector(), artifacts=self.root / "inspect")
        self.assertEqual(report["score"], 0)
        self.assertEqual(report["success_rate"], 0)

    def test_candidate_and_infrastructure_failures_are_separate(self):
        for category in ("infrastructure", "candidate"):
            class Broken:
                def run(self, *args, **kwargs):
                    raise PolicyError("failure", category=category)
            report = self.evaluator.evaluate(REPO / "src" / "rsi_policy", Broken(), artifacts=self.root / category)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(len(report["errors"][category]), 12)
            self.assertEqual(report["score"], 0)

    def test_memory_ablation_really_removes_experience(self):
        requests = []
        class Inspect:
            def run(self, policy_dir, request, *, artifacts):
                requests.append(request)
                return {"status": "completed", "selected_ids": [], "context_chars": 0, "elapsed_seconds": 0}
        self.evaluator.evaluate(REPO / "src" / "rsi_policy", Inspect(), artifacts=self.root / "off",
                                memory_enabled=False, experience=[{"reusable_procedure": "test memory"}])
        self.assertTrue(all(request["experience"] == [] for request in requests))

    def test_rejects_audit_duplicate_ids_queries_and_missing_documents(self):
        original = json.loads((REPO / "benchmarks" / "context.json").read_text())
        mutations = [lambda d: d["cases"][0].update(split="audit"),
                     lambda d: d["cases"][1].update(id=d["cases"][0]["id"]),
                     lambda d: d["cases"][1].update(query=d["cases"][0]["query"]),
                     lambda d: d["cases"][0].update(expected_ids=["absent"]),
                     lambda d: d["documents"].append(d["documents"][0])]
        for mutate in mutations:
            data = json.loads(json.dumps(original))
            mutate(data)
            path = self.root / (uuid.uuid4().hex + ".json")
            path.write_text(json.dumps(data))
            with self.assertRaises(EvaluationError):
                ContextEvaluator(path)
        with self.assertRaises(EvaluationError):
            self.evaluate(REPO / "src" / "rsi_policy", split="audit")

    def test_external_audit_requires_explicit_method(self):
        data = {"documents": [{"id": "a", "text": "actual evidence"}], "cases": [
            {"id": "fresh-01", "query": "fresh question", "expected_ids": ["a"], "split": "audit"}]}
        path = self.root / "separate-audit.json"
        path.write_text(json.dumps(data))
        evaluator = ContextEvaluator(path, external_audit=True)
        with self.assertRaises(EvaluationError):
            evaluator.evaluate(REPO / "src" / "rsi_policy", self.runner, artifacts=self.root / "wrong")
        policy = self.root / "audit-policy"
        policy.mkdir()
        (policy / "policy.py").write_text('print(\'{"selected_ids":["a"],"instructions":""}\')')
        result = evaluator.evaluate_audit(policy, self.runner, artifacts=self.root / "explicit")
        self.assertEqual(result["score"], 1)

    def test_audit_answers_inside_candidate_source_are_rejected_before_execution(self):
        policy = self.root / "candidate-with-labels"
        policy.mkdir()
        (policy / "policy.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")
        path = policy / "audit.json"
        path.write_text(json.dumps({"documents": [{"id": "a", "text": "evidence"}], "cases": [
            {"id": "audit-1", "query": "a fresh query", "expected_ids": ["a"], "split": "audit"}]}), encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "policy workspace"):
            ContextEvaluator(path, external_audit=True).evaluate_audit(policy, self.runner,
                artifacts=self.root / "must-not-run")
        self.assertFalse((self.root / "must-not-run").exists())


if __name__ == "__main__":
    unittest.main()
