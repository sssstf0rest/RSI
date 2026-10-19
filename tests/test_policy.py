import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
import uuid

from rsi.policy_runner import PolicyError, PolicyRunner, validate_response
from rsi.process import execute


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("rsi-policy-" + uuid.uuid4().hex)
        self.root.mkdir(mode=0o755)
        self.addCleanup(self.cleanup)
        self.policy = self.root / "source"
        self.policy.mkdir()
        self.request = {"query": "alpha task", "documents": [
            {"id": "a", "text": "alpha evidence"}, {"id": "b", "text": "irrelevant beta"}],
            "experience": [], "max_context_chars": 100}
        self.runner = PolicyRunner("codex", self.root / "sandbox-home", timeout=3, executor=self.fixture_execute)

    def cleanup(self):
        if self.root.resolve().parent != Path(tempfile.gettempdir()).resolve():
            raise RuntimeError("Unsafe cleanup path")
        def retry(function, path, info):
            os.chmod(path, stat.S_IWRITE)
            function(path)
        shutil.rmtree(self.root, onerror=retry)

    def fixture_execute(self, command, **kwargs):
        # Only our tiny fixture programs bypass the OS sandbox during unit tests.
        self.assertEqual(command[1], "sandbox")
        self.assertIn("-I", command)
        self.assertNotIn("TOKEN", kwargs["env"])
        return execute(command[command.index("--") + 1:], **kwargs)

    def write(self, source):
        (self.policy / "policy.py").write_text(source, encoding="utf-8")

    def run_policy(self):
        return self.runner.run(self.policy, self.request, artifacts=self.root / uuid.uuid4().hex)

    def test_real_child_context_is_built_from_corpus_not_candidate_text(self):
        self.write('import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({"selected_ids":["a"],"instructions":"use evidence"}))\n')
        result = self.run_policy()
        self.assertEqual(result["context"], "[a]\nalpha evidence\n")
        self.assertEqual(result["context_chars"], len(result["context"]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["policy_digest"]), 64)

    def test_protocol_rejects_unknown_duplicate_ids_claimed_scores_and_wrong_types(self):
        for response in ({"selected_ids": ["missing"], "instructions": ""},
                         {"selected_ids": ["a", "a"], "instructions": ""},
                         {"selected_ids": "a", "instructions": ""},
                         {"selected_ids": ["a"], "instructions": 7},
                         {"selected_ids": ["a"], "instructions": "", "score": 1}):
            with self.subTest(response=response):
                self.write("print(" + repr(json.dumps(response)) + ")\n")
                with self.assertRaises(PolicyError) as caught:
                    self.run_policy()
                self.assertEqual(caught.exception.category, "candidate")

    def test_complete_blocks_respect_context_budget(self):
        result = validate_response({"selected_ids": ["a", "b"], "instructions": ""},
                                   {**self.request, "max_context_chars": 19})
        self.assertEqual(result["selected_ids"], ["a"])
        self.assertLessEqual(result["context_chars"], 19)
        self.assertNotIn("beta", result["context"])

    def test_exception_and_timeout_are_candidate_failures_after_start(self):
        for source in ('raise ValueError("broken policy")\n', 'while True: pass\n'):
            with self.subTest(source=source):
                self.write(source)
                with self.assertRaises(PolicyError) as caught:
                    self.run_policy()
                self.assertEqual(caught.exception.category, "candidate")

    def test_sandbox_launch_failure_is_infrastructure(self):
        self.write('print("{}")\n')
        def broken(*args, **kwargs):
            raise FileNotFoundError("missing codex")
        self.runner.executor = broken
        with self.assertRaises(PolicyError) as caught:
            self.run_policy()
        self.assertEqual(caught.exception.category, "infrastructure")

    def test_self_modification_is_detected_in_fixture_child(self):
        self.write('from pathlib import Path\nPath("policy/policy.py").write_text("changed")\nprint(\'{"selected_ids":[],"instructions":""}\')\n')
        with self.assertRaisesRegex(PolicyError, "mutated"):
            self.run_policy()

    def test_isolated_interpreter_ignores_pythonpath(self):
        self.write('import os,json,sys\nassert not any("injected" in item for item in sys.path)\nprint(json.dumps({"selected_ids":[],"instructions":""}))\n')
        self.assertEqual(self.run_policy()["selected_ids"], [])

    def test_input_cannot_include_answer_fields_and_output_is_bounded(self):
        self.write('print("x" * 200000)\n')
        with self.assertRaises(PolicyError):
            self.run_policy()
        self.request["expected_ids"] = ["a"]
        with self.assertRaisesRegex(PolicyError, "unsupported"):
            self.run_policy()

    def test_artifact_source_disallows_non_policy_assets(self):
        self.write('print("{}")\n')
        (self.policy / "private.env").write_text("do not copy")
        with self.assertRaisesRegex(PolicyError, "Ineligible"):
            self.run_policy()


if __name__ == "__main__":
    unittest.main()
