import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from rsi.core import RSIError, Seed, git
from rsi.process import ProcessTimeout, check_environment, execute, worker_environment


class TemporaryWorkspace:
    """Keep inherited sandbox ACLs on Windows (mkdir 0700 replaces them)."""

    def __init__(self):
        self.parent = Path(tempfile.gettempdir()).resolve()
        self.path = self.parent / f"rsi-test-{uuid.uuid4().hex}"
        self.path.mkdir(mode=0o755)
        self.name = str(self.path)

    def cleanup(self):
        if self.path.resolve().parent != self.parent:
            raise RuntimeError("Refusing to clean a test directory outside its temporary root.")

        def writable_retry(function, filename, exception_info):
            os.chmod(filename, stat.S_IWRITE)
            function(filename)

        if self.path.exists():
            shutil.rmtree(self.path, onerror=writable_retry)

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryWorkspace()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo with spaces"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.name", "RSI Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / ".gitignore").write_text(".rsi/\n__pycache__/\n", encoding="utf-8")
        (self.repo / "rsi.toml").write_text(
            'checks = [["python", "-m", "unittest", "discover", "-s", "tests", "-v"]]\n'
            "task_timeout_seconds = 5\ncheck_timeout_seconds = 5\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "rsi").mkdir(parents=True)
        (self.repo / "src" / "rsi" / "__init__.py").write_text("# protected\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_seed.py").write_text("# Existing regression tests\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Fixture")
        self.seed = Seed(self.repo)

    def fake_process(self, command, *, cwd, log, timeout, input_text=None, env=None):
        if "exec" in command:
            self.assertIn('default_permissions="rsi"', command)
            self.assertIn('permissions.rsi.extends=":workspace"', command)
            self.assertIn('forced_login_method="chatgpt"', command)
            self.assertIn("permissions.rsi.network.enabled=false", command)
            self.assertIn("Task:", input_text)
            package = cwd / "src" / "rsi_example"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
            (cwd / "tests" / "test_example.py").write_text(
                "import unittest\nfrom rsi_example import answer\n"
                "class ExampleTest(unittest.TestCase):\n"
                "    def test_answer(self):\n        self.assertEqual(answer(), 42)\n",
                encoding="utf-8",
            )
            (cwd / ".rsi-response.txt").write_text("Implemented example.", encoding="utf-8")
            log.write_text('{"type":"turn.completed","usage":{"input_tokens":1}}\n', encoding="utf-8")
            return 0
        # Execute our own test fixtures. Production always retains the sandbox.
        self.assertEqual(command[1], "sandbox")
        self.assertIn("permissions.rsi.network.enabled=false", command)
        self.assertEqual(command[command.index("-C") + 1], str(cwd))
        inner = command[command.index("--") + 1:]
        return execute(inner, cwd=cwd, log=log, timeout=timeout, env=env)

    def proposed(self):
        with patch("rsi.core.execute", side_effect=self.fake_process):
            return self.seed.run_task("Add an example capability.")

    def verified(self):
        run = self.proposed()
        with patch("rsi.core.execute", side_effect=self.fake_process):
            return self.seed.verify(run["id"])

    def test_candidate_is_separate_and_only_applies_after_checks(self):
        run = self.proposed()
        self.assertEqual(run["status"], "proposed")
        self.assertFalse((self.repo / "src" / "rsi_example").exists())
        self.assertTrue((self.seed.artifacts(run["id"]) / "response.txt").exists())
        with self.assertRaisesRegex(RSIError, "Verify"):
            self.seed.apply(run["id"])
        with patch("rsi.core.execute", side_effect=self.fake_process):
            checked = self.seed.verify(run["id"])
        self.assertEqual(checked["status"], "verified")
        self.assertEqual(checked["verified_digest"], hashlib.sha256(self.seed.snapshot(run["id"])).hexdigest())
        applied = self.seed.apply(run["id"])
        self.assertEqual(applied["status"], "applied")
        self.assertTrue((self.repo / "src" / "rsi_example" / "__init__.py").exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").decode().strip(), run["base"])

    def test_failed_real_check_prevents_application(self):
        run = self.proposed()
        workspace = Path(run["workspace"])
        (workspace / "src" / "rsi_example" / "__init__.py").write_text(
            "def answer():\n    return 0\n", encoding="utf-8",
        )
        with patch("rsi.core.execute", side_effect=self.fake_process):
            with self.assertRaisesRegex(RSIError, "Check 1 exited"):
                self.seed.verify(run["id"])
        self.assertEqual(self.seed.get(run["id"])["status"], "failed")
        self.assertIn("FAILED", (self.seed.artifacts(run["id"]) / "check-1.log").read_text())
        with self.assertRaises(RSIError):
            self.seed.apply(run["id"])

    def test_changed_candidate_invalidates_verification(self):
        run = self.verified()
        (Path(run["workspace"]) / "README.md").write_text("Changed later", encoding="utf-8")
        with self.assertRaisesRegex(RSIError, "changed after verification"):
            self.seed.apply(run["id"])
        self.assertFalse((self.repo / "README.md").exists())

    def test_changed_main_checkout_rejects_stale_candidate(self):
        run = self.verified()
        (self.repo / "README.md").write_text("New baseline", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Advance baseline")
        with self.assertRaisesRegex(RSIError, "main checkout changed"):
            self.seed.apply(run["id"])

    def test_protected_core_change_is_rejected(self):
        run = self.proposed()
        (Path(run["workspace"]) / "src" / "rsi" / "__init__.py").write_text(
            "# changed\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(RSIError, "Protected seed file"):
            self.seed.verify(run["id"])

    def test_tests_cannot_change_the_candidate_being_verified(self):
        run = self.proposed()

        def mutating_check(command, **kwargs):
            (kwargs["cwd"] / "README.md").write_text("Changed during check", encoding="utf-8")
            return 0

        with patch("rsi.core.execute", side_effect=mutating_check):
            with self.assertRaisesRegex(RSIError, "changed during verification"):
                self.seed.verify(run["id"])
        self.assertIsNone(self.seed.get(run["id"])["verified_digest"])

    def test_older_capability_tests_cannot_be_deleted(self):
        (self.repo / "tests" / "test_previous.py").write_text("# Existing capability\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Existing capability tests")
        run = self.proposed()
        (Path(run["workspace"]) / "tests" / "test_previous.py").unlink()
        with self.assertRaisesRegex(RSIError, "Existing regression test"):
            self.seed.verify(run["id"])

    def test_backend_failure_preserves_run_and_workspace(self):
        def failure(command, **kwargs):
            kwargs["log"].write_text("usage limit reached\n", encoding="utf-8")
            return 1

        with patch("rsi.core.execute", side_effect=failure):
            with self.assertRaisesRegex(RSIError, "Codex exited"):
                self.seed.run_task("Task")
        saved = self.seed.runs()[0]
        self.assertEqual(saved["status"], "failed")
        self.assertTrue(Path(saved["workspace"]).exists())
        self.assertTrue((self.seed.artifacts(saved["id"]) / "worker.log").exists())

    def test_error_event_is_not_reported_as_success(self):
        def failure(command, **kwargs):
            kwargs["log"].write_text('{"type":"turn.failed"}\n', encoding="utf-8")
            return 0

        with patch("rsi.core.execute", side_effect=failure):
            with self.assertRaisesRegex(RSIError, "No successful"):
                self.seed.run_task("Task")
        self.assertEqual(self.seed.runs()[0]["status"], "failed")

    def test_timeout_preserves_failure_state(self):
        with patch("rsi.core.execute", side_effect=ProcessTimeout("time limit")):
            with self.assertRaisesRegex(RSIError, "time limit"):
                self.seed.run_task("Task")
        self.assertEqual(self.seed.runs()[0]["status"], "failed")

    def test_dirty_checkout_is_rejected_before_creating_a_run(self):
        (self.repo / "uncommitted.txt").write_text("User work", encoding="utf-8")
        with self.assertRaisesRegex(RSIError, "Commit or stash"):
            self.seed.run_task("Task")
        self.assertEqual(self.seed.runs(), [])

    def test_operations_are_serialized_and_recover_interrupted_state(self):
        run = self.proposed()
        with self.seed.operation():
            with self.assertRaisesRegex(RSIError, "Another RSI operation"):
                with self.seed.operation():
                    pass
        self.seed.update(run["id"], "running")
        with self.seed.operation():
            self.assertEqual(self.seed.get(run["id"])["status"], "interrupted")

    def test_no_checks_is_rejected(self):
        (self.repo / "rsi.toml").write_text("checks = []\n", encoding="utf-8")
        with self.assertRaisesRegex(RSIError, "non-empty checks"):
            Seed(self.repo)


class ProcessTests(unittest.TestCase):
    def test_timeout_stops_a_real_process(self):
        with TemporaryWorkspace() as directory:
            root = Path(directory)
            with self.assertRaises(ProcessTimeout):
                execute(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=root, log=root / "process.log", timeout=1,
                )

    def test_arguments_are_not_interpreted_by_a_shell(self):
        with TemporaryWorkspace() as directory:
            root = Path(directory)
            text = "literal & | $(echo accidental) ; spaces"
            code = execute(
                [sys.executable, "-c", "import sys; print(sys.argv[1])", text],
                cwd=root, log=root / "process.log", timeout=5,
            )
            self.assertEqual(code, 0)
            self.assertEqual((root / "process.log").read_text().strip(), text)

    def test_children_do_not_inherit_bot_or_api_secrets(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "example", "OPENAI_API_KEY": "example",
            "CODEX_API_KEY": "example", "MY_PASSWORD": "example", "RSI_TEST": "kept",
        }):
            worker = worker_environment()
            check = check_environment(Path("workspace"), Path("sandbox"))
        for key in ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY", "MY_PASSWORD"):
            self.assertNotIn(key, worker)
            self.assertNotIn(key, check)
        self.assertEqual(worker["RSI_TEST"], "kept")
        self.assertEqual(check["CODEX_HOME"], "sandbox")


if __name__ == "__main__":
    unittest.main()
