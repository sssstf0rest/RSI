import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from rsi.archive import Archive, policy_digest
from rsi.core import RSIError, Seed, git
from rsi.evolution import Evolution, task_context
from rsi.policy_runner import PolicyRunner
from rsi.process import execute
from test_seed import TemporaryWorkspace


PROJECT = Path(__file__).resolve().parents[1]
BASELINE_POLICY = Path(__file__).resolve().parent / "fixtures" / "baseline_policy"


REGRESSIONS = '''import ast, json, pathlib, subprocess, sys, unittest
class PolicyRegressions(unittest.TestCase):
    def test_all_policy_modules_parse(self):
        for source in pathlib.Path("src/rsi_policy").rglob("*.py"):
            ast.parse(source.read_text(encoding="utf-8"))
    def test_policy_keeps_its_json_contract(self):
        request = {"query": "Telegram polling", "documents": [
            {"id": "telegram", "text": "Telegram polling offsets"}],
            "experience": [], "max_context_chars": 1000}
        result = subprocess.run([sys.executable, "src/rsi_policy/policy.py"],
            input=json.dumps(request), text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(set(output), {"selected_ids", "instructions"})
        self.assertIsInstance(output["instructions"], str)
        self.assertEqual(output["selected_ids"], ["telegram"])
unittest.main()
'''


class EvolutionFixture(unittest.TestCase):
    """Real Git, policy subprocesses and grading; no model or external services."""

    def setUp(self):
        self.temp = TemporaryWorkspace()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.name", "RSI Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / ".gitignore").write_text(".rsi/\n__pycache__/\n", encoding="utf-8")
        (self.repo / "README.md").write_text("A personal agent seed.\n", encoding="utf-8")
        (self.repo / "rsi.toml").write_text(
            'checks = [["python", "tests/test_contract.py"]]\n'
            'task_timeout_seconds = 10\ncheck_timeout_seconds = 10\n'
            'policy_timeout_seconds = 10\nbenchmark_path = "benchmarks/context.json"\n'
            'max_context_chars = 1600\nmin_score_gain = 0.01\n', encoding="utf-8",
        )
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_contract.py").write_text(REGRESSIONS, encoding="utf-8")
        shutil.copytree(BASELINE_POLICY, self.repo / "src" / "rsi_policy",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.repo / "benchmarks").mkdir()
        data = json.loads((PROJECT / "benchmarks" / "context.json").read_text())
        # Small representative fixture: one development and one selection task.
        data["cases"] = [data["cases"][0], data["cases"][-1]]
        (self.repo / "benchmarks" / "context.json").write_text(json.dumps(data), encoding="utf-8")
        self.lexical = self.root / "lexical"
        shutil.copytree(PROJECT / "benchmarks" / "examples" / "lexical", self.lexical,
                        ignore=shutil.ignore_patterns("__pycache__"))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Fixed evolution test fixture")
        self.seed = Seed(self.repo, codex="not-an-installed-codex")
        self.runner = PolicyRunner(self.seed.codex, self.seed.sandbox_home, 10, executor=self.fixture_execute)
        self.prompts = []
        # task_context constructs its own runner; retain the real protocol and
        # only substitute execution of trusted, test-owned fixture programs.
        runner_patch = patch("rsi.evolution.PolicyRunner", side_effect=self.policy_runner)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
        execute_patch = patch("rsi.core.execute", side_effect=self.fixture_execute)
        execute_patch.start()
        self.addCleanup(execute_patch.stop)
        self.evolution = Evolution(self.seed, runner=self.runner)

    def policy_runner(self, *args, **kwargs):
        kwargs["executor"] = self.fixture_execute
        return PolicyRunner(*args, **kwargs)

    def fixture_execute(self, command, *, cwd, log, timeout, input_text=None, env=None):
        self.assertTrue(Path(cwd).resolve().is_relative_to(self.root))
        if "exec" in command:
            self.prompts.append(input_text)
            package = Path(cwd) / "src" / "rsi_demo"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
            (Path(cwd) / ".rsi-response.txt").write_text("Implemented fixture capability.", encoding="utf-8")
            log.write_text(json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 12, "output_tokens": 5}}) + "\n", encoding="utf-8")
            return 0
        self.assertEqual(command[1], "sandbox")
        self.assertIn("--", command)
        # This substitution is restricted to fixtures created by this test.
        inner = command[command.index("--") + 1:]
        return execute(inner, cwd=cwd, log=log, timeout=timeout, input_text=input_text, env=env)

    def experiment(self, source=None, **kwargs):
        return self.evolution.experiment(
            "Improve Telegram polling and process cancellation context", candidate=source or self.lexical, **kwargs,
        )


class EvolutionTests(EvolutionFixture):
    def test_offline_candidate_passes_real_regression_and_behavior_gates(self):
        version = self.experiment()
        self.assertTrue(version["eligible"], version["decision"])
        self.assertGreater(version["metrics"]["candidate"]["selection"]["score"],
                           version["metrics"]["baseline"]["selection"]["score"])
        self.assertEqual(version["metrics"]["candidate"]["selection"]["success_rate"], 1)
        self.assertFalse(version["metrics"]["candidate"]["selection"]["held_out_claim"])
        run = self.seed.get(version["source_run"])
        self.assertEqual(run["status"], "verified")
        self.assertEqual(version["provenance"]["patch_digest"], run["verified_digest"])
        self.assertIn("Ran 2 tests", (self.seed.artifacts(run["id"]) / "check-1.log").read_text())
        self.assertFalse(git(self.repo, "status", "--porcelain").strip())

    def test_promotion_persists_and_selected_policy_affects_next_model_prompt(self):
        initial = self.evolution.initialize()
        version = self.experiment()
        self.evolution.promote(version["id"])
        restarted = Seed(self.repo, codex=self.seed.codex)
        archive = Archive(restarted)
        self.assertEqual(archive.active()["id"], version["id"])
        self.assertEqual(policy_digest(archive.directory(archive.active())), version["digest"])
        run = restarted.run_task("Add a small helper")
        self.assertEqual(run["policy_version"], version["id"])
        self.assertIn("Verify the selected evidence before acting.", self.prompts[-1])
        self.assertNotIn("Use the selected evidence when relevant.", self.prompts[-1])
        self.assertFalse(git(self.repo, "status", "--porcelain").strip())
        self.evolution.promote(initial["id"], rollback=True)
        self.assertEqual(Archive(Seed(self.repo)).active()["id"], initial["id"])

    def test_regression_failure_cannot_be_overridden_by_better_behavior(self):
        (self.lexical / "broken.py").write_text("def broken(\n", encoding="utf-8")
        with self.assertRaisesRegex(RSIError, "Check 1 exited"):
            self.experiment()
        self.assertEqual(self.seed.runs()[0]["status"], "failed")
        self.assertEqual(len(self.evolution.archive.versions()), 1)  # Only the initial baseline.

    def test_invalid_policy_is_recorded_as_rejected_not_promoted(self):
        (self.lexical / "policy.py").write_text(
            "import json,sys\nrequest=json.load(sys.stdin)\n"
            "print(json.dumps({'selected_ids':['missing-evidence'],'instructions':'bad'}))\n",
            encoding="utf-8",
        )
        # Keep the fixed regression contract satisfied for its one known input;
        # malformed selection on other tasks is caught by the independent runner.
        source = (self.lexical / "policy.py").read_text().replace(
            "['missing-evidence']", "[request['documents'][0]['id']] if len(request['documents']) == 1 else ['missing-evidence']",
        )
        (self.lexical / "policy.py").write_text(source, encoding="utf-8")
        version = self.experiment()
        self.assertFalse(version["eligible"])
        self.assertTrue(version["metrics"]["candidate"]["selection"]["errors"]["candidate"])
        with self.assertRaisesRegex(RSIError, "promotion gates"):
            self.evolution.promote(version["id"])

    def test_no_behavior_improvement_is_rejected(self):
        source = self.root / "unchanged behavior"
        shutil.copytree(self.repo / "src" / "rsi_policy", source)
        with (source / "policy.py").open("a", encoding="utf-8") as output:
            output.write("\n# A candidate revision with unchanged retrieval behavior.\n")
        version = self.experiment(source)
        self.assertFalse(version["eligible"])
        self.assertIn("No selection-quality improvement", version["decision"])

    def test_archived_policy_tampering_blocks_activation(self):
        version = self.experiment()
        with (Path(version["path"]) / "policy.py").open("a", encoding="utf-8") as output:
            output.write("\n# changed after evaluation\n")
        with self.assertRaisesRegex(RSIError, "changed after recording"):
            self.evolution.promote(version["id"])

    def test_candidate_changes_during_behavior_grading_invalidate_verified_patch(self):
        evaluate = self.evolution.evaluator.evaluate
        def mutate_after_grading(directory, *args, **kwargs):
            result = evaluate(directory, *args, **kwargs)
            if "worktrees" in Path(directory).parts and kwargs.get("split") == "selection":
                with (Path(directory) / "policy.py").open("a", encoding="utf-8") as output:
                    output.write("\n# Changed after grading this exact candidate.\n")
            return result
        with patch.object(self.evolution.evaluator, "evaluate", side_effect=mutate_after_grading):
            with self.assertRaisesRegex(RSIError, "Candidate changed while evaluating behavior"):
                self.experiment()
        self.assertEqual(len(self.evolution.archive.versions()), 1)

    def test_changed_base_or_trusted_evaluator_blocks_promotion(self):
        for change in ("base", "evaluator"):
            with self.subTest(change=change):
                version = self.experiment()
                if change == "base":
                    with (self.repo / "README.md").open("a", encoding="utf-8") as output:
                        output.write("A new base revision.\n")
                    git(self.repo, "add", "README.md")
                    git(self.repo, "commit", "-m", "Advance base")
                    expected = "Base code changed"
                else:
                    with (self.repo / "tests" / "test_contract.py").open("a", encoding="utf-8") as output:
                        output.write("\n# Trusted evaluator changed.\n")
                    expected = "Trusted evaluator/check configuration changed"
                with self.assertRaisesRegex(RSIError, expected):
                    self.evolution.promote(version["id"])

    def test_selected_parent_ancestry_is_preserved(self):
        parent = self.experiment()
        child = self.experiment(parent_id=parent["id"])
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["baseline_id"], parent["baseline_id"])
        from rsi.meta import StrategyStore
        self.assertEqual(child["strategy_version"], StrategyStore(self.seed).active()["id"])
        self.assertEqual(self.seed.get(child["source_run"])["parent_id"], parent["id"])

    def test_memory_retrieval_enters_context_and_can_be_disabled(self):
        archive = self.evolution.archive
        with self.seed.connection() as db:
            db.execute("INSERT INTO experience(source_run,task,outcome,detail,updated_at) VALUES(?,?,?,?,?)",
                       ("relevant", "Telegram offset restart", "verified", "Persist the Telegram offset cursor.", "now"))
            db.execute("INSERT INTO experience(source_run,task,outcome,detail,updated_at) VALUES(?,?,?,?,?)",
                       ("unrelated", "Garden irrigation", "failed", "Water the flowers.", "now"))
        self.assertEqual([entry["source_run"] for entry in archive.relevant("Telegram offset")], ["relevant"])
        enabled, disabled = self.root / "memory-on", self.root / "memory-off"
        enabled.mkdir()
        disabled.mkdir()
        on, _ = task_context(self.seed, "Telegram offset", enabled)
        off, _ = task_context(self.seed, "Telegram offset", disabled, memory_enabled=False)
        self.assertIn("Persist the Telegram offset cursor.", on)
        self.assertNotIn("Persist the Telegram offset cursor.", off)
        self.assertEqual(json.loads((enabled / "context.json").read_text())["experience_sources"], ["relevant"])
        self.assertEqual(json.loads((disabled / "context.json").read_text())["experience_sources"], [])

    def test_forbidden_root_file_is_rejected_by_enforced_path_allowlist(self):
        def forbidden_worker(command, **kwargs):
            code = self.fixture_execute(command, **kwargs)
            if "exec" in command:
                (kwargs["cwd"] / "unexpected.py").write_text("raise RuntimeError('outside allowlist')\n")
            return code
        with patch("rsi.core.execute", side_effect=forbidden_worker):
            with self.assertRaisesRegex(RSIError, "outside the modification allowlist"):
                self.seed.run_task("Add a capability")
        self.assertEqual(self.seed.runs()[0]["status"], "failed")

    def test_legacy_database_migration_preserves_existing_runs(self):
        with self.seed.connection() as db:
            db.execute("DROP TABLE runs")
            db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,"
                       "branch TEXT NOT NULL,workspace TEXT NOT NULL,base TEXT NOT NULL,created_at TEXT NOT NULL,"
                       "updated_at TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',verified_digest TEXT)")
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)", (
                "old-run", "Legacy task", "verified", "codex/old", str(self.root / "old"),
                "old-base", "created", "updated", "preserve this detail", "digest",
            ))
        restarted = Seed(self.repo)
        run = restarted.get("old-run")
        self.assertEqual(run["task"], "Legacy task")
        self.assertEqual(run["detail"], "preserve this detail")
        self.assertEqual(run["verified_digest"], "digest")
        self.assertIn("execution", run)
        self.assertIsNone(run["policy_version"])


if __name__ == "__main__":
    unittest.main()
