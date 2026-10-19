from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import unittest

from rsi.queue import QueueError, QueueStore
from test_seed import TemporaryWorkspace


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryWorkspace()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.stamp = 1000.0
        self.queue = QueueStore(self.path, clock=lambda: self.stamp)

    def submit(self, job_id=None):
        return self.queue.submit("task", {"task": "build helper"}, job_id)

    def test_idempotency_requires_same_request_and_survives_restart(self):
        job = self.submit("one")
        self.assertEqual(job, self.submit("one"))
        reopened = QueueStore(self.path)
        self.assertEqual(reopened.get("one")["payload"], {"task": "build helper"})
        with self.assertRaises(QueueError):
            self.queue.submit("experiment", {"task": "build helper"}, "one")
        with self.assertRaises(QueueError):
            self.queue.submit("task", {"task": "different"}, "one")
        self.assertEqual(len(self.queue.list()), 1)

    def test_claim_is_transactional_and_never_runs_two_workers(self):
        self.submit("first")
        self.submit("second")
        barrier = threading.Barrier(2)

        def claim(owner):
            queue = QueueStore(self.path, clock=lambda: self.stamp)
            barrier.wait()
            return queue.claim(owner)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(claim, ["a", "b"]))
        self.assertEqual(sum(job is not None for job in claimed), 1)
        active = next(job for job in claimed if job)
        self.assertEqual(active["id"], "first")
        self.assertEqual(active["attempts"], 1)
        self.assertIsNone(self.queue.claim("c"))

    def test_owner_heartbeat_evaluation_and_cancellation(self):
        job = self.submit()
        self.queue.claim("owner", 10)
        with self.assertRaises(QueueError):
            self.queue.heartbeat(job["id"], "other")
        with self.assertRaises(QueueError):
            self.queue.finish(job["id"], "other", "completed")
        self.stamp += 5
        self.queue.heartbeat(job["id"], "owner", 20)
        self.queue.set_evaluating(job["id"], "owner")
        self.assertEqual(self.queue.get(job["id"])["status"], "evaluating")
        self.queue.cancel(job["id"])
        self.assertTrue(self.queue.is_cancel_requested(job["id"]))
        self.queue.finish(job["id"], "owner", "completed", {"ignored": True})
        self.assertEqual(self.queue.get(job["id"])["status"], "cancelled")

    def test_expired_lease_retains_interrupted_job_without_auto_retry(self):
        self.submit("abandoned")
        self.submit("pending")
        self.queue.claim("lost", 10)
        self.stamp += 10
        self.assertIsNone(self.queue.claim("new"))
        with self.assertRaises(QueueError):
            self.queue.heartbeat("abandoned", "lost")
        recovered = self.queue.recover_expired()
        self.assertEqual([job["id"] for job in recovered], ["abandoned"])
        self.assertEqual(recovered[0]["status"], "interrupted")
        self.assertEqual(self.queue.claim("new")["id"], "pending")
        with self.assertRaises(QueueError):
            self.queue.retry("abandoned")

    def test_quota_and_authentication_pause_new_work_persistently(self):
        for category in ("quota", "authentication"):
            with self.subTest(category=category):
                self.queue.resume()
                job = self.submit()
                self.queue.claim("worker")
                self.queue.finish(job["id"], "worker", "failed", error_category=category)
                reopened = QueueStore(self.path, clock=lambda: self.stamp)
                self.assertTrue(reopened.status()["paused"])
                self.assertEqual(reopened.status()["reason"], category)
                self.assertIsNone(reopened.claim("worker"))
                with self.assertRaises(QueueError):
                    reopened.retry(job["id"])

    def test_recovery_after_os_lock_reconciliation_does_not_wait_for_lease(self):
        self.submit("lost")
        self.submit("pending")
        self.queue.claim("old", 600)
        self.queue.cancel("lost")
        reopened = QueueStore(self.path, clock=lambda: self.stamp)
        # The supervisor owns/reconciles actual processes; this API documents
        # that external precondition rather than pretending a lease proves it.
        recovered = reopened.recover_after_lock()
        self.assertEqual([job["id"] for job in recovered], ["lost"])
        self.assertEqual(recovered[0]["status"], "interrupted")
        self.assertTrue(recovered[0]["cancel_requested"])
        self.assertIsNone(recovered[0]["owner"])
        self.assertIsNone(recovered[0]["lease_until"])
        self.assertEqual(reopened.claim("new")["id"], "pending")
        self.assertEqual(reopened.recover_expired(), [])

    def test_retry_is_finite_and_only_for_failed_infrastructure(self):
        self.submit("retry")
        for attempt in (1, 2):
            self.queue.claim("worker")
            self.queue.finish("retry", "worker", "failed", error_category="infrastructure")
            if attempt == 1:
                self.queue.retry("retry", max_attempts=2)
        self.assertEqual(self.queue.get("retry")["attempts"], 2)
        with self.assertRaises(QueueError):
            self.queue.retry("retry", max_attempts=2)
        self.submit("candidate")
        self.queue.claim("worker")
        self.queue.finish("candidate", "worker", "failed", error_category="candidate")
        with self.assertRaises(QueueError):
            self.queue.retry("candidate")

    def test_usage_budget_survives_restart_and_only_explicit_resume_resets_it(self):
        self.assertFalse(self.queue.record_usage(60, limit=100)["paused"])
        reopened = QueueStore(self.path)
        self.assertEqual(reopened.status()["observed_tokens"], 60)
        state = reopened.record_usage(40, limit=100)
        self.assertTrue(state["paused"])
        self.assertEqual(state["reason"], "local_usage_cap")
        self.submit("waiting")
        self.assertIsNone(QueueStore(self.path).claim("worker"))
        reopened.resume()
        self.assertEqual(reopened.status()["observed_tokens"], 0)
        self.assertIsNotNone(reopened.claim("worker"))

    def test_unknown_usage_preserves_quota_reason_and_telegram_resume_resets_budget(self):
        for reason in ("quota", "authentication", "manual"):
            with self.subTest(reason=reason):
                self.queue.resume()
                self.queue.record_usage(50, limit=100)
                self.queue.pause(reason)
                state = self.queue.record_usage(None, limit=100)
                self.assertEqual(state["reason"], reason)
                self.assertTrue(state["usage_unknown"])
        self.queue.accept_update(1, "resume")
        self.assertEqual(self.queue.status()["observed_tokens"], 0)
        self.assertFalse(self.queue.status()["usage_unknown"])
        self.assertEqual(self.queue.record_usage(None, limit=100)["reason"], "usage_unknown")
        self.assertTrue(QueueStore(self.path).status()["usage_unknown"])
        with self.assertRaises(QueueError):
            self.queue.record_usage(-1, limit=100)

    def test_concurrent_usage_schema_migration_preserves_existing_pause_and_jobs(self):
        self.submit("legacy")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE scheduling")
            db.execute("CREATE TABLE scheduling(singleton INTEGER PRIMARY KEY, paused INTEGER NOT NULL, reason TEXT NOT NULL)")
            db.execute("INSERT INTO scheduling VALUES(1,1,'authentication')")
        barrier = threading.Barrier(2)

        def reopen(_):
            barrier.wait()
            return QueueStore(self.path).status()

        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(reopen, range(2)))
        for state in states:
            self.assertEqual(state["reason"], "authentication")
            self.assertTrue(state["paused"])
            self.assertEqual(state["observed_tokens"], 0)
            self.assertFalse(state["usage_unknown"])
        self.assertEqual(self.queue.get("legacy")["status"], "queued")

    def test_usage_ledger_is_idempotent_across_restart_and_resume(self):
        self.queue.record_usage(60, limit=100, usage_id="run-1")
        reopened = QueueStore(self.path)
        self.assertEqual(reopened.record_usage(60, limit=100, usage_id="run-1")["observed_tokens"], 60)
        with self.assertRaises(QueueError):
            reopened.record_usage(61, limit=100, usage_id="run-1")
        reopened.record_usage(None, limit=100, usage_id="run-unknown")
        reopened.resume()
        # Previously accounted IDs do not consume the explicitly reset window,
        # nor does old unknown telemetry re-pause a new window.
        self.assertEqual(reopened.record_usage(60, limit=100, usage_id="run-1")["observed_tokens"], 0)
        self.assertFalse(reopened.record_usage(None, limit=100, usage_id="run-unknown")["paused"])
        self.assertEqual(reopened.record_usage(40, limit=100, usage_id="run-2")["observed_tokens"], 40)

    def test_unaccounted_runs_recovers_persisted_execution_before_new_jobs(self):
        self.assertEqual(self.queue.unaccounted_runs(), [])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE runs(id TEXT PRIMARY KEY,execution TEXT)")
            db.executemany("INSERT INTO runs(id,execution) VALUES(?,?)", [
                ("older", '{"usage":{"input_tokens":40,"output_tokens":20}}'),
                ("newer", '{"usage":{"input_tokens":20,"output_tokens":20}}'),
                ("no-execution", None),
            ])
        self.assertEqual([run["id"] for run in self.queue.unaccounted_runs(limit=1)], ["older"])
        self.queue.record_usage(60, limit=100, usage_id="older")
        reopened = QueueStore(self.path)
        self.assertEqual([run["id"] for run in reopened.unaccounted_runs()], ["newer"])
        reopened.record_usage(40, limit=100, usage_id="newer")
        self.assertEqual(reopened.unaccounted_runs(), [])
        self.assertEqual(reopened.status()["reason"], "local_usage_cap")

    def test_pause_resume_and_queued_cancel(self):
        self.submit("cancel")
        self.queue.pause("manual")
        self.assertIsNone(self.queue.claim("worker"))
        self.queue.cancel("cancel")
        self.queue.resume()
        self.assertEqual(self.queue.get("cancel")["status"], "cancelled")
        self.assertIsNone(self.queue.claim("worker"))

    def test_telegram_mutation_and_cursor_are_atomic(self):
        accepted = self.queue.accept_update(50, "task", "build an adapter")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["result"]["id"], "telegram-50")
        self.assertEqual(self.queue.telegram_offset(), 51)
        reopened = QueueStore(self.path)
        self.assertFalse(reopened.accept_update(50, "task", "build an adapter")["accepted"])
        with self.assertRaises(QueueError):
            reopened.accept_update(51, "cancel", "missing")
        self.assertEqual(reopened.telegram_offset(), 51)
        self.assertEqual(len(reopened.list()), 1)
        reopened.accept_update(51, "pause")
        self.assertTrue(reopened.status()["paused"])
        reopened.accept_update(52, "resume")
        self.assertFalse(reopened.status()["paused"])

    def test_version_change_rejected_while_worker_active(self):
        self.submit("active")
        self.queue.claim("worker")
        for command in ("rollback", "promote"):
            with self.assertRaises(QueueError):
                self.queue.accept_update(1, command, "version")
        self.assertEqual(self.queue.telegram_offset(), 0)
        self.queue.finish("active", "worker", "completed")
        result = self.queue.accept_update(1, "rollback", "version")
        self.assertEqual(result["result"]["payload"], {"version_id": "version"})


if __name__ == "__main__":
    unittest.main()
