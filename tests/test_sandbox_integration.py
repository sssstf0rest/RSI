"""Opt-in OS boundary checks. No authentication or model inference is used."""

import json
import os
from pathlib import Path
import shutil
import unittest

from rsi.policy_runner import PolicyRunner
from test_seed import TemporaryWorkspace


@unittest.skipUnless(os.environ.get("RSI_REAL_SANDBOX") == "1", "Set RSI_REAL_SANDBOX=1 for real Codex sandbox checks")
class RealSandboxTests(unittest.TestCase):
    def setUp(self):
        codex = shutil.which("codex")
        self.assertIsNotNone(codex, "Install official Codex CLI before opting into sandbox tests")
        self.temp = TemporaryWorkspace()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runner = PolicyRunner(codex, self.root / "sandbox-home", timeout=60)
        self.request = {"query": "Telegram polling offset", "documents": [
            {"id": "telegram", "text": "Telegram polling persists its update offset."}],
            "experience": [], "max_context_chars": 1000}

    def test_real_policy_process_selects_and_constructs_context(self):
        policy = Path(__file__).resolve().parents[1] / "src" / "rsi_policy"
        result = self.runner.run(policy, self.request, artifacts=self.root / "baseline")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected_ids"], ["telegram"])
        self.assertEqual(result["context"], "[telegram]\nTelegram polling persists its update offset.\n")

    def test_external_read_and_policy_write_denied_but_local_temp_allowed(self):
        canary = self.root / "outside-canary.txt"
        canary.write_text("hidden evaluator answers must remain inaccessible", encoding="utf-8")
        policy = self.root / "source"
        policy.mkdir()
        source = '''import json, sys, tempfile
from pathlib import Path
json.load(sys.stdin)
observed = {}
try:
    Path(CANARY).read_text()
    observed["outside_read_denied"] = False
except OSError:
    observed["outside_read_denied"] = True
try:
    Path(__file__).write_text("changed")
    observed["policy_write_denied"] = False
except OSError:
    observed["policy_write_denied"] = True
with tempfile.TemporaryFile() as stream:
    stream.write(b"allowed temporary output")
    stream.flush()
observed["temporary_write_allowed"] = True
print(json.dumps({"selected_ids": [], "instructions": json.dumps(observed)}))
'''.replace("CANARY", repr(str(canary)))
        (policy / "policy.py").write_text(source, encoding="utf-8")
        result = self.runner.run(policy, self.request, artifacts=self.root / "boundary")
        observed = json.loads(result["instructions"])
        self.assertEqual(observed, {"outside_read_denied": True, "policy_write_denied": True,
                                    "temporary_write_allowed": True})
        self.assertEqual(canary.read_text(), "hidden evaluator answers must remain inaccessible")


if __name__ == "__main__":
    unittest.main()
