import json
from pathlib import Path
import tempfile
import time
import unittest
import threading
from unittest.mock import patch

from factory.cli import run, status
from factory.config import write_json
from factory.supervisor import Supervisor


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"state_dir": str(self.root / "state"), "repo": str(self.root), "runtime": "codex",
                       "integration_policy": "documentation_only", "auto_merge": True}
        self.path = self.root / "config.json"
        write_json(self.path, self.config)

    def test_live_process_without_fresh_heartbeat_is_not_healthy(self):
        write_json(Path(self.config["state_dir"]) / "status.json", {"status": "running", "ready": True,
                   "pid": 123, "process_identity": "synthetic", "updated_at": time.time() - 1000})
        with patch("factory.cli.process_matches", return_value=True):
            self.assertEqual(status(self.config)["status"], "unresponsive")

    def test_live_process_initialization_and_blocker_are_visible(self):
        path = Path(self.config["state_dir"]) / "status.json"
        for state in ("starting", "blocked"):
            write_json(path, {"status": state, "ready": False, "pid": 123,
                              "process_identity": "synthetic", "updated_at": time.time()})
            with patch("factory.cli.process_matches", return_value=True):
                self.assertEqual(status(self.config)["status"], state)

    def test_starting_heartbeat_precedes_repository_discovery_failure(self):
        observed = []
        def failing(config, builder, verifier, startup_callback=None):
            startup_callback()
            value = json.loads((Path(config["state_dir"]) / "status.json").read_text())
            observed.append(value)
            raise RuntimeError("synthetic_initialization_failure")
        with patch("factory.cli.Supervisor", side_effect=failing), patch("factory.cli.process_identity", return_value="synthetic"):
            with self.assertRaises(RuntimeError):
                run(self.path)
        self.assertEqual(observed[0]["status"], "starting")
        self.assertFalse(observed[0]["ready"])
        failed = json.loads((Path(self.config["state_dir"]) / "status.json").read_text())
        self.assertEqual(failed["status"], "blocked")
        self.assertEqual(failed["startup_id"], observed[0]["startup_id"])
        self.assertEqual(failed["startup_error"], "initialization_failed")

    def test_duplicate_supervisor_cannot_overwrite_active_heartbeat(self):
        config = {"state_dir": str(self.root / "state"), "runtime": "fake"}
        first = Supervisor(config, lambda *_: [])
        try:
            called = []
            with self.assertRaisesRegex(RuntimeError, "supervisor_already_running"):
                Supervisor(config, lambda *_: [], startup_callback=lambda: called.append(True))
            self.assertFalse(called)
        finally:
            first.close()

    def test_callback_failure_releases_state_lock(self):
        config = {"state_dir": str(self.root / "state"), "runtime": "fake"}
        def fail():
            raise RuntimeError("heartbeat_write_failed")
        with self.assertRaises(RuntimeError):
            Supervisor(config, lambda *_: [], startup_callback=fail)
        with Supervisor(config, lambda *_: []) as replacement:
            self.assertFalse(replacement._lock.closed)

    def test_core_ready_heartbeat_is_written_before_external_tracker_check(self):
        self.config["tracker_enabled"] = True
        write_json(self.path, self.config)
        report = Path(self.config["state_dir"]) / "maintenance" / "maintenance.json"
        write_json(report, {"revision": "a" * 40})
        class StopEvent(threading.Event):
            def wait(self, timeout=None):
                self.set()
        class FakeSupervisor:
            def __init__(self, config, builder, verifier, startup_callback):
                self._stop = StopEvent()
                self.store = type("Store", (), {"jobs": lambda _: []})()
                startup_callback()
            def tick(self):
                return {"jobs": [{"payload": "synthetic-private-requirements"}], "active": 0}
            def stop(self):
                self._stop.set()
            def close(self):
                self._stop.set()
        observed = []
        def slow_tracker(*args):
            value = json.loads((Path(self.config["state_dir"]) / "status.json").read_text())
            observed.append(value)
            return {"status": "synthetic_tracker_result"}
        with patch("factory.cli.Supervisor", FakeSupervisor), patch("factory.cli.process_identity", return_value="synthetic"), \
                patch("factory.cli.signal.signal"), patch("factory.cli.integrate_ready"), \
                patch("factory.cli.git", return_value="a" * 40), patch("factory.tracker.reconcile", side_effect=slow_tracker):
            run(self.path)
        self.assertTrue(observed[0]["ready"])
        self.assertEqual(observed[0]["phase"], "checking_tracker")
        self.assertEqual(observed[0]["jobs"], [])
        self.assertNotIn("synthetic-private", json.dumps(observed[0]))


if __name__ == "__main__":
    unittest.main()
