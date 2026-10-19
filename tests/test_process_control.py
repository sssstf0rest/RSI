import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from rsi.process import process_identity
from rsi.windows_job import JobObject
from test_seed import TemporaryWorkspace


@unittest.skipUnless(os.name == "nt", "Windows Job Object behavior")
class WindowsJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryWorkspace()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Process lifecycle condition was not met within 10 seconds.")

    def cleanup_process(self, process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if process.stdin is not None:
            process.stdin.close()

    def test_close_kills_descendant_after_its_parent_has_exited(self):
        pid_file = self.root / "descendant.pid"
        leader_code = (
            "import pathlib,subprocess,sys; sys.stdin.readline(); "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
        )
        job = JobObject()
        self.addCleanup(job.close)
        leader = subprocess.Popen(
            [sys.executable, "-c", leader_code, str(pid_file)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.cleanup_process, leader)
        job.attach(leader)
        # Gate child creation until its parent has definitely entered the job.
        leader.communicate(input=b"start\n", timeout=10)
        self.assertEqual(leader.returncode, 0)
        descendant = int(pid_file.read_text())
        identity = process_identity(descendant)
        self.assertIsNotNone(identity)
        job.close()
        self.wait_until(lambda: process_identity(descendant) != identity)
        job.close()  # Idempotent after the tree has terminated.

    def test_supervisor_crash_closes_non_inherited_handle_and_kills_worker(self):
        pid_file = self.root / "worker.pid"
        owner_code = (
            "import os,pathlib,subprocess,sys; from rsi.windows_job import JobObject; "
            "job=JobObject(); "
            "worker=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "job.attach(worker); pathlib.Path(sys.argv[1]).write_text(str(worker.pid)); "
            "sys.stdin.readline(); os._exit(0)"
        )
        owner = subprocess.Popen(
            [sys.executable, "-c", owner_code, str(pid_file)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.cleanup_process, owner)
        self.wait_until(pid_file.exists)
        worker = int(pid_file.read_text())
        identity = process_identity(worker)
        self.assertIsNotNone(identity)
        owner.communicate(input=b"crash\n", timeout=10)
        self.assertEqual(owner.returncode, 0)
        self.wait_until(lambda: process_identity(worker) != identity)

    def test_explicit_termination_stops_a_running_member(self):
        with JobObject() as job:
            worker = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.addCleanup(self.cleanup_process, worker)
            job.attach(worker)
            job.terminate(7)
            self.assertEqual(worker.wait(timeout=10), 7)

    def test_closed_job_rejects_new_members(self):
        job = JobObject()
        job.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            job.attach(object())


if __name__ == "__main__":
    unittest.main()
