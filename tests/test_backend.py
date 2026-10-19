import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from rsi.backends.codex import CodexBackend
from rsi.process import ProcessCancelled, ProcessTimeout
from test_seed import TemporaryWorkspace


class CodexBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryWorkspace()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "candidate with spaces"
        self.workspace.mkdir()
        self.artifacts = Path(self.temp.name) / "artifacts"
        version = subprocess.CompletedProcess([], 0, stdout=b"codex-cli 1.2.3\n", stderr=b"")
        self.version = patch("rsi.backends.codex.subprocess.run", return_value=version)
        self.version.start()
        self.addCleanup(self.version.stop)

    def run_worker(self, events, *, exit_code=0, exception=None, backend=None):
        def worker(command, *, cwd, log, timeout, input_text, env):
            self.command, self.env, self.prompt = command, env, input_text
            log.write_text(
                "\n".join(event if isinstance(event, str) else json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            (cwd / ".rsi-response.txt").write_text("Candidate response", encoding="utf-8")
            if exception is not None:
                raise exception
            return exit_code
        return (backend or CodexBackend()).run(
            "Implement a task; $(literal)", self.workspace, self.artifacts, 10,
            execute_fn=worker, env={"PATH": "trusted", "TELEGRAM_TOKEN": "secret", "OPENAI_API_KEY": "secret"},
        )

    def test_completed_run_persists_provenance_and_unknown_usage(self):
        result = self.run_worker([
            {"type": "thread.started", "thread_id": "thread-123", "session_id": "session-456",
             "model": "observed-model", "reasoning_effort": "high"},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"id": "command-1", "type": "command_execution"}},
            {"type": "item.completed", "item": {"id": "command-1", "exit_code": 1}},
            {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 7}},
        ])
        self.assertTrue(result.success)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.thread_id, "thread-123")
        self.assertEqual(result.session_id, "session-456")
        self.assertEqual(result.actual_model, "observed-model")
        self.assertEqual(result.actual_reasoning_effort, "high")
        self.assertEqual(result.codex_version, "codex-cli 1.2.3")
        self.assertEqual(result.usage["input_tokens"], 12)
        self.assertIsNone(result.usage["cached_input_tokens"])
        self.assertIsNone(result.usage["total_tokens"])
        self.assertEqual(Path(result.response_path).read_text(), "Candidate response")
        self.assertEqual(json.loads(Path(result.execution_path).read_text()), result.to_dict())
        self.assertNotIn("TELEGRAM_TOKEN", self.env)
        self.assertNotIn("OPENAI_API_KEY", self.env)
        self.assertIn('forced_login_method="chatgpt"', self.command)
        self.assertIn("--ignore-user-config", self.command)
        self.assertIn("permissions.rsi.network.enabled=false", self.command)
        self.assertEqual(self.prompt, "Implement a task; $(literal)")

    def test_model_and_effort_are_explicit_arguments(self):
        result = self.run_worker(
            [{"type": "turn.completed"}],
            backend=CodexBackend(model="test model", reasoning_effort="medium"),
        )
        self.assertTrue(result.success)
        self.assertEqual(self.command[self.command.index("--model") + 1], "test model")
        self.assertIn('model_reasoning_effort="medium"', self.command)
        self.assertEqual(result.model, "test model")
        self.assertEqual(result.reasoning_effort, "medium")
        self.assertIsNone(result.actual_model)

    def test_error_and_failed_event_cannot_be_overridden_by_completed_turn(self):
        for event_type, message, category in (
            ("error", "Usage limit reached; quota exhausted", "quota"),
            ("turn.failed", "Authentication required; please login", "auth"),
        ):
            with self.subTest(event_type=event_type):
                result = self.run_worker([
                    {"type": event_type, "error": {"message": message}},
                    {"type": "turn.completed"},
                ])
                self.assertFalse(result.success)
                self.assertEqual(result.error_category, category)
                self.assertIn(message, result.error)

    def test_partial_or_malformed_stream_fails_even_with_output_file(self):
        for events in (
            [{"type": "thread.started", "thread_id": "partial"}],
            [{"type": "turn.completed"}, {"type": "turn.started"}],
            [{"type": "item.started", "item": {"id": "unfinished"}}, {"type": "turn.completed"}],
            [{"type": "turn.completed"}, '{"type":"turn.failed"'],
        ):
            with self.subTest(events=events):
                result = self.run_worker(events)
                self.assertFalse(result.success)
                self.assertEqual(result.error_category, "infrastructure")
                self.assertIsNotNone(result.response_path)

    def test_stderr_warning_is_allowed_but_response_text_is_not_an_error_category(self):
        result = self.run_worker(["WARN: optional configuration was unavailable", {"type": "turn.completed"}])
        self.assertTrue(result.success)
        result = self.run_worker([
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Implement user login."}},
        ])
        self.assertFalse(result.success)
        self.assertEqual(result.error_category, "infrastructure")

    def test_usage_is_conservative_across_multiple_turns(self):
        result = self.run_worker([
            {"type": "turn.completed", "usage": {
                "input_tokens": 8, "output_tokens": 3, "cached_input_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 2},
            }},
            {"type": "turn.completed", "usage": {
                "input_tokens": 2, "output_tokens": 1, "cached_input_tokens": True,
                "reasoning_tokens": "2", "total_tokens": -5,
            }},
        ])
        self.assertTrue(result.success)
        self.assertEqual(result.usage["input_tokens"], 10)
        self.assertEqual(result.usage["output_tokens"], 4)
        self.assertIsNone(result.usage["cached_input_tokens"])
        self.assertIsNone(result.usage["reasoning_tokens"])
        self.assertIsNone(result.usage["total_tokens"])

    def test_nonzero_exit_fails_and_uses_stderr_diagnostic_category(self):
        result = self.run_worker(["ERROR: login is required"], exit_code=1)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.error_category, "auth")

    def test_timeout_record_keeps_partial_metadata(self):
        result = self.run_worker(
            [{"type": "thread.started", "thread_id": "interrupted-thread"}],
            exception=ProcessTimeout("Process exceeded 10 seconds"),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(result.error_category, "timeout")
        self.assertEqual(result.thread_id, "interrupted-thread")
        self.assertIsNone(result.exit_code)
        self.assertEqual(json.loads(Path(result.execution_path).read_text())["outcome"], "timeout")

    def test_interrupt_is_persisted_before_reraising(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_worker([], exception=KeyboardInterrupt())
        record = json.loads((self.artifacts / "execution.json").read_text())
        self.assertEqual(record["outcome"], "interrupted")
        self.assertFalse(record["success"])
        self.assertEqual(record["error_category"], "interrupted")

    def test_explicit_cancellation_remains_interrupted_and_is_not_retryable_failure(self):
        result = self.run_worker(
            [{"type": "turn.completed"}], exception=ProcessCancelled("Task cancelled by user."),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, "interrupted")
        self.assertEqual(result.error_category, "cancelled")
        self.assertEqual(json.loads(Path(result.execution_path).read_text())["error_category"], "cancelled")

    def test_missing_executable_is_structured_infrastructure_failure(self):
        result = self.run_worker([], exception=FileNotFoundError("No Codex executable"))
        self.assertFalse(result.success)
        self.assertEqual(result.error_category, "infrastructure")
        self.assertIn("No Codex executable", result.error)
        self.assertTrue(Path(result.execution_path).exists())


if __name__ == "__main__":
    unittest.main()
