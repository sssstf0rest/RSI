import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from rsi.process import ProcessCancelled, execute, process_control, process_identity, reconcile_process
from rsi.core import RSIError
from rsi.runtime import Worker, prune, worker_lock
from test_evolution import EvolutionFixture
from test_seed import TemporaryWorkspace


class ProcessRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryWorkspace()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "worker.json"

    def test_prestart_cancellation_does_not_launch_process(self):
        marker = self.root / "should-not-exist"
        with process_control(cancelled=lambda: True):
            with self.assertRaises(ProcessCancelled):
                execute([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(marker)],
                        cwd=self.root, log=self.root / "worker.log", timeout=5)
        self.assertFalse(marker.exists())

    def test_running_cancellation_stops_worker_and_clears_ownership(self):
        cancel = threading.Event()
        identities = []
        def heartbeat():
            if self.registry.exists():
                identities.append(json.loads(self.registry.read_text()))
                if len(identities) == 2:
                    cancel.set()
        with process_control(registry=self.registry, cancelled=cancel.is_set, heartbeat=heartbeat):
            with self.assertRaises(ProcessCancelled):
                execute([sys.executable, "-c", "import time;time.sleep(60)"], cwd=self.root,
                        log=self.root / "worker.log", timeout=5)
        self.assertFalse(self.registry.exists())
        self.assertTrue(identities)
        self.assertNotEqual(process_identity(identities[0]["pid"]), identities[0]["identity"])

    def test_success_heartbeats_and_removes_worker_registration(self):
        heartbeats = []
        with process_control(registry=self.registry, heartbeat=lambda: heartbeats.append(True)):
            code = execute([sys.executable, "-c", "print('finished')"], cwd=self.root,
                           log=self.root / "worker.log", timeout=5)
        self.assertEqual(code, 0)
        self.assertTrue(heartbeats)
        self.assertFalse(self.registry.exists())
        self.assertIn("finished", (self.root / "worker.log").read_text())

    def test_reused_pid_is_not_terminated(self):
        self.registry.write_text(json.dumps({"pid": os.getpid(), "identity": "stale-creation-time"}))
        with patch("rsi.process.subprocess.run") as taskkill, patch("rsi.process.os.killpg", create=True) as killpg:
            self.assertFalse(reconcile_process(self.registry))
        taskkill.assert_not_called()
        killpg.assert_not_called()
        self.assertFalse(self.registry.exists())

    def test_reconciliation_terminates_a_real_registered_orphan(self):
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        worker = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], **kwargs)
        def cleanup():
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)
        self.addCleanup(cleanup)
        identity = process_identity(worker.pid)
        self.assertIsNotNone(identity)
        self.registry.write_text(json.dumps({"pid": worker.pid, "identity": identity}))
        self.assertTrue(reconcile_process(self.registry))
        worker.wait(timeout=5)
        self.assertFalse(self.registry.exists())


class WorkerTests(EvolutionFixture):
    def test_worker_executes_a_durable_task_and_records_its_run(self):
        worker = Worker(self.seed)
        worker.queue.submit("task", {"task": "Implement a fixture helper"}, "task-1")
        results = worker.run(max_jobs=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "completed")
        self.assertEqual(results[0]["result"]["status"], "proposed")
        self.assertEqual(len(self.prompts), 1)
        self.assertIsNone(worker.queue.status()["active"])
        self.assertEqual(worker.queue.get("task-1")["attempts"], 1)

    def test_quota_failure_pauses_new_model_jobs_without_retrying(self):
        worker = Worker(self.seed)
        worker.queue.submit("task", {"task": "First helper"}, "first")
        worker.queue.submit("task", {"task": "Second helper"}, "second")
        def quota_process(command, **kwargs):
            if "exec" in command:
                kwargs["log"].write_text(json.dumps({
                    "type": "error", "message": "Usage limit reached; quota exhausted.",
                }) + "\n", encoding="utf-8")
                return 1
            return self.fixture_execute(command, **kwargs)
        with patch("rsi.core.execute", side_effect=quota_process):
            results = worker.run(max_jobs=2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "paused")
        self.assertEqual(results[0]["error_category"], "quota")
        self.assertTrue(worker.queue.status()["paused"])
        self.assertEqual(worker.queue.get("second")["status"], "queued")
        self.assertEqual(worker.queue.get("first")["attempts"], 1)

    def test_unknown_usage_pauses_after_completed_job(self):
        worker = Worker(self.seed)
        worker.queue.submit("task", {"task": "First helper"}, "first")
        worker.queue.submit("task", {"task": "Second helper"}, "second")
        def missing_usage(command, **kwargs):
            code = self.fixture_execute(command, **kwargs)
            if "exec" in command:
                kwargs["log"].write_text('{"type":"turn.completed"}\n', encoding="utf-8")
            return code
        with patch("rsi.core.execute", side_effect=missing_usage):
            results = worker.run(max_jobs=2)
        self.assertEqual(results[0]["status"], "completed")
        self.assertEqual(worker.queue.status()["reason"], "usage_unknown")
        self.assertEqual(worker.queue.get("second")["status"], "queued")

    def test_explicit_cancel_stops_handler_and_prevents_model_call(self):
        worker = Worker(self.seed)
        def handler(payload):
            worker.queue.cancel("cancel-me")
            return execute([sys.executable, "-c", "raise RuntimeError('must not launch')"],
                           cwd=self.root, log=self.root / "cancelled.log", timeout=5)
        worker.handlers = {"task": handler}
        worker.queue.submit("task", {"task": "Cancelled helper"}, "cancel-me")
        result = worker.run()[0]
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error_category"], "cancelled")
        self.assertFalse(self.prompts)
        self.assertFalse((self.root / "cancelled.log").exists())

    def test_new_exclusive_worker_reconciles_abandoned_job_before_dispatch(self):
        worker = Worker(self.seed, handlers={"task": lambda payload: {"handled": payload["task"]}})
        worker.queue.submit("task", {"task": "abandoned"}, "abandoned")
        worker.queue.claim("dead-supervisor", lease_seconds=120)
        worker.queue.submit("task", {"task": "new work"}, "next")
        result = worker.run()[0]
        self.assertEqual(worker.queue.get("abandoned")["status"], "interrupted")
        self.assertEqual(result["id"], "next")
        self.assertEqual(result["status"], "completed")

    def test_worker_lock_prevents_second_worker_from_claiming_work(self):
        worker = Worker(self.seed)
        worker.queue.submit("task", {"task": "Waiting helper"}, "waiting")
        with worker_lock(self.seed):
            with self.assertRaisesRegex(RSIError, "queue worker owns"):
                worker.run()
        self.assertEqual(worker.queue.get("waiting")["status"], "queued")
        self.assertFalse(self.prompts)

    def test_finite_worker_batch_does_not_exceed_job_bound(self):
        worker = Worker(self.seed, handlers={"task": lambda payload: {"handled": payload["task"]}})
        for index in range(3):
            worker.queue.submit("task", {"task": str(index)}, f"job-{index}")
        results = worker.run(max_jobs=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(worker.queue.get("job-2")["status"], "queued")

    def test_retention_preserves_archived_source_and_reproduction_patch(self):
        version = self.experiment()
        self.evolution.promote(version["id"])
        older = self.seed.run_task("An older fixture helper")
        recent = self.seed.run_task("The latest fixture helper")
        artifacts = self.seed.artifacts(older["id"])
        (artifacts / "worker.log").write_text("A" * 110_000, encoding="utf-8")
        result = prune(self.seed, keep=1)
        self.assertIn(older["id"], result["removed_worktrees"])
        self.assertFalse(Path(older["workspace"]).exists())
        self.assertTrue(Path(recent["workspace"]).exists())
        self.assertTrue(Path(self.seed.get(version["source_run"])["workspace"]).exists())
        self.assertTrue((artifacts / "candidate.patch").read_bytes())
        self.assertTrue((artifacts / "execution.json").exists())
        self.assertTrue((artifacts / "worker.summary.txt").exists())


if __name__ == "__main__":
    unittest.main()
