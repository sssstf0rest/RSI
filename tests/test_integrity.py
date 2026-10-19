import json
from pathlib import Path
import sys
import unittest

from rsi.archive import policy_digest
from rsi.core import RSIError
from rsi.policy_runner import PolicyRunner
from rsi.process import ProcessTimeout, execute, process_control
from rsi.runtime import Worker
from test_evolution import EvolutionFixture
from test_seed import TemporaryWorkspace


class ArtifactIntegrityTests(unittest.TestCase):
    def test_length_framing_distinguishes_ambiguous_file_boundaries(self):
        with TemporaryWorkspace() as directory:
            root = Path(directory)
            one, two = root / "one", root / "two"
            for policy in (one, two):
                policy.mkdir()
                (policy / "policy.py").write_text("print('fixture')", encoding="utf-8")
            (one / "a.md").write_bytes(b"x\0b.md\0y")
            (two / "a.md").write_bytes(b"x")
            (two / "b.md").write_bytes(b"y")
            self.assertNotEqual(policy_digest(one), policy_digest(two))

    def test_bytecode_caches_are_not_copied_into_policy_execution(self):
        with TemporaryWorkspace() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "policy.py").write_text('print(\'{"selected_ids":[],"instructions":""}\')', encoding="utf-8")
            (source / "__pycache__").mkdir()
            (source / "__pycache__" / "hidden.pyc").write_bytes(b"not executable input")
            def fixture(command, **kwargs):
                self.assertFalse((kwargs["cwd"] / "policy" / "__pycache__").exists())
                return execute(command[command.index("--") + 1:], **kwargs)
            result = PolicyRunner("codex", root / "sandbox", executor=fixture).run(source,
                {"query": "fixture", "documents": [], "experience": [], "max_context_chars": 100},
                artifacts=root / "artifacts")
            self.assertEqual(result["status"], "completed")

    def test_fast_completed_process_still_obeys_output_limit(self):
        with TemporaryWorkspace() as directory:
            root = Path(directory)
            with process_control(max_log_bytes=1000), self.assertRaisesRegex(ProcessTimeout, "output exceeded"):
                execute([sys.executable, "-c", "print('x' * 5000)"], cwd=root,
                        log=root / "output.log", timeout=5)


class PromotionIntegrityTests(EvolutionFixture):
    def test_increased_instructions_cannot_fake_context_efficiency(self):
        metrics = {"baseline": {}, "candidate": {}}
        for split in ("development", "selection"):
            base = {"status": "completed", "score": 1.0, "success_rate": 1.0,
                    "benchmark_digest": "fixed", "evaluator_version": "fixed", "context_chars": 1000,
                    "policy_prompt_chars": 1010}
            candidate = {**base, "context_chars": 500, "policy_prompt_chars": 7500}
            metrics["baseline"][split] = base
            metrics["candidate"][split] = candidate
        eligible, _ = self.evolution._gate(metrics, 0)
        self.assertFalse(eligible)

    def test_external_configured_benchmark_change_invalidates_promotion(self):
        version = self.experiment()
        # Configured external corpora participate in the same trusted digest.
        external = self.root / "external.json"
        external.write_bytes((self.repo / "benchmarks" / "context.json").read_bytes())
        self.seed.config["benchmark_path"] = str(external)
        with self.assertRaisesRegex(RSIError, "Trusted evaluator"):
            self.evolution.promote(version["id"])

    def test_crashed_model_with_unavailable_usage_pauses_before_dispatch(self):
        run = self.seed.run_task("A model attempt that died before structured accounting")
        (self.seed.artifacts(run["id"]) / "execution.json").unlink()
        with self.seed.connection() as db:
            db.execute("UPDATE runs SET execution=NULL,status='running' WHERE id=?", (run["id"],))
        worker = Worker(self.seed, handlers={"fixture": lambda payload: self.fail("Unknown usage must pause dispatch")})
        worker.queue.submit("fixture", {}, "next-task")
        self.assertEqual(worker.run(max_jobs=1), [])
        self.assertTrue(worker.queue.status()["usage_unknown"])
        self.assertEqual(worker.queue.get("next-task")["status"], "queued")
        restored = json.loads(self.seed.get(run["id"])["execution"])
        self.assertIsNone(restored["usage"]["input_tokens"])


if __name__ == "__main__":
    unittest.main()
