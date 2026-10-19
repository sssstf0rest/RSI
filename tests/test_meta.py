from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from rsi.archive import Archive
from rsi.backends.codex import ExecutionResult
from rsi.core import RSIError, git
from rsi.meta import MetaEvolution, StrategyStore
from rsi.queue import QueueStore
from test_evolution import EvolutionFixture


class MetaEvolutionTests(EvolutionFixture):
    def setUp(self):
        super().setUp()
        self.seed.config.update(model="matched-test-model", reasoning_effort="medium")
        self.candidate = self.root / "mutation-instructions.txt"
        self.candidate.write_text("Prefer small context changes supported by development failures.", encoding="utf-8")
        self.calls = []
        self.settings = {
            "baseline": {"score": 0.5, "success_rate": 0.5, "tokens": 100, "context_chars": 100},
            "candidate": {"score": 0.8, "success_rate": 0.8, "tokens": 100, "context_chars": 100},
        }
        self.transform = None

    def fake_factory(self, seed):
        owner = self
        class FakeEvolution:
            def experiment(self, task, **kwargs):
                owner.calls.append({"task": task, **kwargs})
                label = "candidate" if kwargs["strategy_text"] == owner.candidate.read_text().strip() else "baseline"
                values = owner.settings[label]
                if values.get("error"):
                    raise RSIError(values["error"])
                metrics = {split: {
                    "status": "completed", "score": values["score"], "success_rate": values["success_rate"],
                    "context_chars": values["context_chars"], "benchmark_digest": "matched-benchmark",
                    "evaluator_version": "matched-evaluator",
                } for split in ("development", "selection")}
                if values.get("development_score") is not None:
                    metrics["development"]["score"] = values["development_score"]
                usage = {} if values["tokens"] is None else {"total_tokens": values["tokens"]}
                result = {
                    "id": f"descendant-{len(owner.calls)}", "source_run": f"run-{len(owner.calls)}",
                    "metrics": {"candidate": metrics},
                    "provenance": {"model": seed.config["model"], "reasoning_effort": seed.config.get("reasoning_effort"),
                                   "execution": {"usage": usage, "model": seed.config["model"]}},
                }
                return owner.transform(deepcopy(result), label) if owner.transform else result
            def promote(self, *args, **kwargs):
                raise AssertionError("Meta trials must never activate their descendants.")
        return FakeEvolution()

    def meta(self):
        return MetaEvolution(self.seed, evolution_factory=self.fake_factory)

    def test_matched_descendants_keep_parent_model_and_memory_fixed(self):
        initial = Archive(self.seed).ensure_baseline()
        with patch.object(Archive, "activate", side_effect=AssertionError("No policy promotions during meta trials")):
            result = self.meta().experiment(self.candidate, trials=2, parent_id=initial["id"], task="Compare mutation strategies")
        self.assertTrue(result["eligible"], result["decision"])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual({call["parent_id"] for call in self.calls}, {initial["id"]})
        self.assertTrue(all(call["memory_enabled"] is False for call in self.calls))
        self.assertTrue(all(call.get("candidate") is None for call in self.calls))
        labels = [entry["label"] for entry in result["report"]["executions"]]
        self.assertEqual(labels, ["baseline", "candidate", "candidate", "baseline"])
        self.assertEqual(result["report"]["observed_tokens"], 400)
        self.assertFalse(result["report"]["causal_or_statistical_claim"])
        self.assertEqual(Archive(self.seed).active()["id"], initial["id"])
        self.assertEqual(len(Archive(self.seed).versions()), 1)

    def test_strategy_activation_persists_without_promoting_a_policy(self):
        meta = self.meta()
        initial = meta.store.active()
        result = meta.experiment(self.candidate)
        self.assertEqual(meta.store.active()["id"], initial["id"])
        meta.store.activate(result["id"])
        restored = StrategyStore(self.seed).active()
        self.assertEqual(restored["id"], result["id"])
        self.assertEqual(restored["text"], self.candidate.read_text())
        self.assertEqual(len(StrategyStore(self.seed).versions()), 2)
        meta.store.activate(initial["id"], rollback=True)
        self.assertEqual(meta.store.active()["id"], initial["id"])

    def test_unknown_usage_is_inconclusive_and_stops_further_calls(self):
        self.settings["baseline"]["tokens"] = None
        result = self.meta().experiment(self.candidate, trials=3)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["report"]["status"], "inconclusive")
        self.assertFalse(result["report"]["usage_known"])
        self.assertIsNone(result["report"]["observed_tokens"])
        self.assertEqual(len(self.calls), 1)

    def test_cumulative_observed_usage_cap_stops_before_next_trial(self):
        self.seed.config["max_meta_observed_tokens"] = 150
        result = self.meta().experiment(self.candidate, trials=3)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["report"]["observed_tokens"], 200)
        self.assertEqual(result["report"]["status"], "inconclusive")
        self.assertEqual(len(self.calls), 2)

    def test_higher_quality_at_excessive_model_work_is_rejected(self):
        self.settings["candidate"]["tokens"] = 111
        result = self.meta().experiment(self.candidate)
        self.assertFalse(result["eligible"])
        self.assertIn("comparison tolerance", result["decision"])

    def test_equal_quality_with_fewer_observed_tokens_is_eligible(self):
        self.settings["candidate"].update(score=0.5, success_rate=0.5, tokens=90)
        result = self.meta().experiment(self.candidate)
        self.assertTrue(result["eligible"], result["decision"])
        self.assertIn("less observed model work", result["decision"])

    def test_development_regression_overrides_selection_improvement(self):
        self.settings["candidate"]["development_score"] = 0.2
        result = self.meta().experiment(self.candidate)
        self.assertFalse(result["eligible"])
        self.assertIn("regressed", result["decision"])

    def test_context_budget_is_independent_of_selection_score(self):
        self.settings["candidate"]["context_chars"] = self.seed.config["max_context_chars"] + 1
        result = self.meta().experiment(self.candidate)
        self.assertFalse(result["eligible"])
        self.assertIn("context budget", result["decision"])

    def test_larger_instructions_cannot_appear_as_a_cheaper_equal_quality_policy(self):
        self.settings["candidate"].update(score=0.5, success_rate=0.5, tokens=90, context_chars=50)
        def add_instruction_size(result, label):
            for split in ("development", "selection"):
                result["metrics"]["candidate"][split]["policy_prompt_chars"] = 500 if label == "candidate" else 150
            return result
        self.transform = add_instruction_size
        result = self.meta().experiment(self.candidate)
        self.assertFalse(result["eligible"])
        self.assertIn("No matched", result["decision"])

    def test_meta_accounting_never_loads_the_entire_run_history(self):
        with patch.object(self.seed, "runs", side_effect=AssertionError("Unbounded run history must not be scanned")):
            result = self.meta().experiment(self.candidate)
        self.assertTrue(result["eligible"], result["decision"])

    def test_quota_failure_aborts_and_persists_queue_pause(self):
        self.settings["baseline"]["error"] = "Usage limit reached: quota exhausted."
        result = self.meta().experiment(self.candidate, trials=3)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["report"]["status"], "paused")
        self.assertEqual(result["report"]["error_category"], "quota")
        self.assertTrue(QueueStore(self.seed.state / "state.sqlite3").status()["paused"])
        self.assertEqual(len(self.calls), 1)

    def test_prompt_file_tampering_blocks_activation_even_for_whitespace(self):
        meta = self.meta()
        result = meta.experiment(self.candidate)
        with Path(result["path"]).open("a", encoding="utf-8") as output:
            output.write("\n")
        with self.assertRaisesRegex(RSIError, "changed after recording"):
            meta.store.activate(result["id"])

    def test_fixed_evaluator_change_invalidates_strategy_promotion(self):
        meta = self.meta()
        result = meta.experiment(self.candidate)
        with (self.repo / "tests" / "test_contract.py").open("a", encoding="utf-8") as output:
            output.write("\n# evaluator revision\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Change evaluator")
        with self.assertRaisesRegex(RSIError, "Trusted evaluator"):
            meta.store.activate(result["id"])

    def test_changed_active_policy_invalidates_matched_comparison(self):
        meta = self.meta()
        result = meta.experiment(self.candidate)
        archive = Archive(self.seed)
        active = archive.active()
        another = archive.record(self.lexical, baseline_id=active["id"], eligible=True)
        archive.activate(another["id"])
        with self.assertRaisesRegex(RSIError, "Active policy changed"):
            meta.store.activate(result["id"])

    def test_candidate_is_bounded_plain_text_and_trial_count_is_finite(self):
        meta = self.meta()
        for trials in (0, 4, True):
            with self.subTest(trials=trials), self.assertRaisesRegex(RSIError, "1–3"):
                meta.experiment(self.candidate, trials=trials)
        self.candidate.write_text("x" * 12001, encoding="utf-8")
        with self.assertRaises(RSIError):
            meta.experiment(self.candidate)
        self.assertFalse(self.calls)

    def test_proposer_is_text_only_uses_development_feedback_and_records_its_cost(self):
        meta = self.meta()
        archive = Archive(self.seed)
        archive.record(self.lexical, metrics={"candidate": {
            "development": {"score": 0.4, "success_rate": 0.2, "errors": {}, "cases": [{"success": False}]},
            "selection": {"hidden_selection_marker": "must-not-enter-proposal"},
        }})
        prompts = []
        def propose(backend, prompt, workspace, artifacts, timeout, **kwargs):
            prompts.append(prompt)
            self.assertIn("permissions.rsi.network.enabled=false", kwargs["permission_options"])
            self.assertTrue(any('\":root\"=\"deny\"' in option for option in kwargs["permission_options"]))
            self.assertTrue((workspace / ".git").is_dir())
            (workspace / "instructions.txt").write_text(self.candidate.read_text(), encoding="utf-8")
            artifacts.mkdir(parents=True)
            return ExecutionResult(success=True, outcome="completed", model=self.seed.config["model"],
                                   usage={"total_tokens": 30}, execution_path=str(artifacts / "execution.json"))
        with patch("rsi.backends.codex.CodexBackend.run", autospec=True, side_effect=propose):
            proposal = meta.propose("Use observed failures")
        self.assertNotIn("must-not-enter-proposal", prompts[0])
        self.assertIn('"failed_case_count": 1', prompts[0])
        self.assertTrue(Path(proposal["candidate_path"]).is_file())
        self.candidate = Path(proposal["candidate_path"])
        self.settings["candidate"]["tokens"] = 60
        result = meta.experiment(self.candidate)
        self.assertTrue(result["eligible"], result["decision"])
        self.assertEqual(result["report"]["observed_tokens"], 190)
        self.assertEqual(result["report"]["executions"][0]["phase"], "meta_proposal")
        self.assertEqual(result["report"]["proposal_source"], "model_proposal")

    def test_model_proposal_cost_can_disqualify_an_apparent_quality_improvement(self):
        from rsi.meta import _digest
        sidecar = self.candidate.with_suffix(self.candidate.suffix + ".proposal.json")
        sidecar.write_text(json.dumps({"candidate_digest": _digest(self.candidate.read_text()),
            "proposal_id": "earlier-proposal", "execution": {"success": True, "usage": {"total_tokens": 50}}}), encoding="utf-8")
        result = self.meta().experiment(self.candidate)
        self.assertFalse(result["eligible"])
        self.assertIn("comparison tolerance", result["decision"])
        self.assertEqual(result["report"]["observed_tokens"], 250)

    def test_unknown_proposal_usage_prevents_descendant_model_calls(self):
        from rsi.meta import _digest
        sidecar = self.candidate.with_suffix(self.candidate.suffix + ".proposal.json")
        sidecar.write_text(json.dumps({"candidate_digest": _digest(self.candidate.read_text()),
            "execution": {"success": True, "usage": {}}}), encoding="utf-8")
        result = self.meta().experiment(self.candidate)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["report"]["status"], "inconclusive")
        self.assertFalse(self.calls)

    def test_proposer_crash_preserves_unknown_usage_pause_before_model_result(self):
        meta = self.meta()
        with patch("rsi.backends.codex.CodexBackend.run", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                meta.propose("Draft instructions")
        status = QueueStore(self.seed.state / "state.sqlite3").status()
        self.assertTrue(status["paused"])
        self.assertTrue(status["usage_unknown"])
        self.assertEqual(status["reason"], "usage_unknown")

    def test_concurrent_initial_strategy_queries_create_one_consistent_baseline(self):
        store = StrategyStore(self.seed)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: store.active(), range(4)))
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(len(store.versions()), 1)

    def test_observed_model_change_invalidates_matched_comparison(self):
        def change_model(result, label):
            result["provenance"]["execution"]["actual_model"] = "backend-model-" + label
            return result
        self.transform = change_model
        result = self.meta().experiment(self.candidate)
        self.assertFalse(result["eligible"])
        self.assertIn("configuration changed", result["decision"])


if __name__ == "__main__":
    unittest.main()
