"""Telegram/queue/worker/CLI boundaries without cloud or Telegram calls."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import runpy
import sys
import threading
from unittest.mock import Mock, patch

from rsi.archive import Archive
from rsi.cli import main, telegram_main
from rsi.core import RSIError
from rsi.process import execute
from rsi.runtime import Worker, prune, worker_lock
from rsi_telegram import Bot
from test_evolution import EvolutionFixture


TOKEN = "123456:private-integration-token"


def update(identifier, text):
    return {"update_id": identifier, "message": {
        "from": {"id": 42}, "chat": {"type": "private", "id": 42}, "text": text,
    }}


class ControlsIntegrationTests(EvolutionFixture):
    def make_bot(self, worker):
        return Bot(worker.queue, {}, TOKEN, 42, client=Mock())

    def test_experiment_never_activates_until_explicit_telegram_promotion(self):
        initial = self.evolution.initialize()
        worker = Worker(self.seed, handlers={"experiment": lambda payload: self.experiment()})
        bot = self.make_bot(worker)
        bot.process_update(update(1, "/experiment improve retrieval"))
        self.assertEqual(Archive(self.seed).active()["id"], initial["id"])
        experiment_job = worker.run()[0]
        self.assertEqual(experiment_job["status"], "completed", experiment_job["detail"])
        version = experiment_job["result"]
        self.assertTrue(version["eligible"], version["decision"])
        self.assertEqual(Archive(self.seed).active()["id"], initial["id"])
        bot.process_update(update(2, f"/promote {version['id']}"))
        self.assertEqual(Archive(self.seed).active()["id"], initial["id"])
        original = self.evolution.promote

        def check_lock(*args, **kwargs):
            with self.assertRaisesRegex(RSIError, "queue worker owns"):
                with worker_lock(self.seed):
                    pass
            return original(*args, **kwargs)

        with patch("rsi.runtime.Evolution", return_value=self.evolution), \
                patch.object(self.evolution, "promote", side_effect=check_lock):
            result = Worker(self.seed).run()[0]
        self.assertEqual(result["kind"], "promote")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(Archive(self.seed).active()["id"], version["id"])
        self.assertFalse(self.prompts)  # Only supplied local policy evaluation.

    def test_idle_service_retains_lock_without_inference(self):
        first, second = Worker(self.seed), Worker(self.seed)
        stop = threading.Event()
        attempted = []

        def wait(seconds):
            with self.assertRaisesRegex(RSIError, "queue worker owns"):
                second.run()
            attempted.append(True)
            stop.set()
            return True

        with patch.object(stop, "wait", side_effect=wait):
            self.assertEqual(first.run(serve=True, poll_seconds=0.01, stop_event=stop), [])
        self.assertEqual(attempted, [True])
        self.assertFalse(self.prompts)

    def test_telegram_pause_resume_and_cancel_reach_active_worker(self):
        worker = Worker(self.seed)
        bot = self.make_bot(worker)
        bot.process_update(update(1, "/pause"))
        bot.process_update(update(2, "/task cancel this helper"))
        self.assertEqual(worker.run(), [])
        self.assertEqual(worker.queue.get("telegram-2")["status"], "queued")
        bot.process_update(update(3, "/resume"))
        marker = self.root / "must-not-launch.log"

        def handler(payload):
            bot.process_update(update(4, "/cancel telegram-2"))
            execute([sys.executable, "-c", "raise RuntimeError('must not execute')"],
                    cwd=self.root, log=marker, timeout=5)

        worker.handlers = {"task": handler}
        result = worker.run()[0]
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error_category"], "cancelled")
        self.assertFalse(marker.exists())
        self.assertFalse(self.prompts)

    def test_worker_error_state_does_not_persist_service_token(self):
        def broken(payload):
            raise RuntimeError(f"Connection failed for credential {TOKEN}")
        worker = Worker(self.seed, handlers={"task": broken})
        worker.queue.submit("task", {"task": "fail safely"})
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}):
            result = worker.run()[0]
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(TOKEN, result["detail"])
        self.assertNotIn(TOKEN, str(worker.queue.list()))

    def test_cli_controls_parse_documented_arguments_and_persist_state(self):
        arguments = [
            ["enqueue", "--task", "literal task with spaces; $(no shell)", "--job-id", "cli-task"],
            ["pause"], ["jobs"], ["job", "cli-task"], ["cancel", "cli-task"], ["resume"],
            ["enqueue", "--promote", "candidate-version", "--job-id", "cli-promote"],
        ]
        with redirect_stdout(io.StringIO()), patch("rsi.cli.Seed", return_value=self.seed):
            for command in arguments:
                self.assertEqual(main(["--repo", str(self.repo), *command]), 0)
        queue = Worker(self.seed).queue
        self.assertEqual(queue.get("cli-task")["payload"]["task"], "literal task with spaces; $(no shell)")
        self.assertEqual(queue.get("cli-task")["status"], "cancelled")
        self.assertEqual(queue.get("cli-promote")["payload"], {"version_id": "candidate-version"})
        self.assertFalse(queue.status()["paused"])
        self.assertFalse(self.prompts)

    def test_unknown_error_category_cannot_become_a_credential_field(self):
        class UnsafeError(RuntimeError):
            category = TOKEN

        def broken(payload):
            raise UnsafeError("Unrecognized failure category")

        worker = Worker(self.seed, handlers={"task": broken})
        worker.queue.submit("task", {"task": "fail safely"})
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}):
            result = worker.run()[0]
        self.assertNotIn(TOKEN, str(result))
        self.assertIn(result["error_category"], ("candidate", "infrastructure"))

    def test_observed_usage_cap_persists_across_workers_and_explicit_resume(self):
        self.seed.config["max_observed_tokens"] = 30
        first = Worker(self.seed)
        for identifier in ("one", "two", "three"):
            first.queue.submit("task", {"task": f"Develop fixture {identifier}"}, identifier)
        self.assertEqual(first.run()[0]["status"], "completed")
        self.assertEqual(first.queue.status()["observed_tokens"], 17)
        restarted = Worker(self.seed)
        self.assertEqual(restarted.run()[0]["status"], "completed")
        self.assertEqual(restarted.queue.status()["observed_tokens"], 34)
        self.assertEqual(restarted.queue.status()["reason"], "local_usage_cap")
        self.assertEqual(Worker(self.seed).run(), [])
        self.assertEqual(restarted.queue.get("three")["status"], "queued")
        self.make_bot(restarted).process_update(update(1, "/resume"))
        self.assertEqual(restarted.queue.status()["observed_tokens"], 0)
        self.assertEqual(Worker(self.seed).run()[0]["status"], "completed")
        self.assertEqual(restarted.queue.status()["observed_tokens"], 17)
        self.assertEqual(len(self.prompts), 3)

    def test_startup_accounts_execution_left_between_job_finish_and_usage_charge(self):
        # A completed execution exists, but simulate the prior supervisor dying
        # before its separate queue-accounting step. Startup must charge it
        # before dispatching another model request.
        self.seed.config["max_observed_tokens"] = 10
        self.seed.run_task("Execution completed before simulated accounting crash")
        worker = Worker(self.seed)
        worker.queue.submit("task", {"task": "Must wait for budget review"}, "waiting")
        self.assertEqual(worker.run(), [])
        self.assertEqual(worker.queue.status()["observed_tokens"], 17)
        self.assertEqual(worker.queue.status()["reason"], "local_usage_cap")
        self.assertEqual(worker.queue.get("waiting")["status"], "queued")
        self.assertEqual(len(self.prompts), 1)

    def test_telegram_entrypoint_preserves_failure_exit_code(self):
        with patch("rsi.cli.telegram_main", return_value=1):
            with self.assertRaises(SystemExit) as result:
                runpy.run_module("rsi_telegram", run_name="__main__")
        self.assertEqual(result.exception.code, 1)

    def test_evaluation_failure_has_nonzero_exit_status(self):
        with patch("rsi.cli.Evolution") as evolution, redirect_stdout(io.StringIO()):
            evolution.return_value.evaluate.return_value = {
                "development": {"status": "failed"}, "selection": {"status": "completed"}}
            self.assertEqual(main(["--repo", str(self.repo), "evaluate", "--no-memory"]), 1)
            evolution.return_value.evaluate.return_value = {
                "development": {"status": "completed"}, "selection": {"status": "completed"}}
            self.assertEqual(main(["--repo", str(self.repo), "evaluate", "--no-memory"]), 0)

    def test_telegram_service_errors_do_not_print_token(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_USER_ID": "42"}), \
                patch("rsi.cli.Seed", side_effect=RuntimeError(TOKEN)), redirect_stderr(stderr):
            self.assertEqual(telegram_main(repo=self.repo), 1)
        self.assertNotIn(TOKEN, stderr.getvalue())

    def test_retention_retires_unaccepted_candidate_worktree_but_preserves_version(self):
        version = self.experiment()
        run = self.seed.get(version["source_run"])
        self.assertFalse(version["ever_accepted"])
        self.seed.run_task("Create a newer fixture helper")
        result = prune(self.seed, keep=1)
        self.assertIn(run["id"], result["removed_worktrees"])
        self.assertFalse(Path(run["workspace"]).exists())
        self.assertTrue(Path(Archive(self.seed).get(version["id"])["path"]).is_dir())
        self.assertTrue((self.seed.artifacts(run["id"]) / "candidate.patch").exists())
