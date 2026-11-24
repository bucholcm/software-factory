from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from factory.store import Store
from factory.supervisor import Supervisor
from factory.worker import atomic_json, process_identity, process_matches, sanitize_result


REVISION = "a" * 40


def result_for(job):
    stage = job["stage"]
    if stage in {"implementing", "fixing"}:
        return {"status": "completed", "revision": REVISION}
    if stage == "testing":
        return {"status": "completed", "revision": REVISION, "tests_passed": True}
    return {"status": "completed", "review_result": "approved", "reviewed_revision": REVISION}


def command_for(result, path, delay=0, counter=None, exit_code=0):
    code = "import json,time,sys;from pathlib import Path;"
    if counter:
        code += f"open({str(counter)!r},'a').write('run\\n');"
    code += f"time.sleep({delay!r});Path({str(path)!r}).write_text({json.dumps(result)!r});sys.exit({exit_code})"
    return [sys.executable, "-c", code]


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "Application Support" / "factory state"
        self.config = {"repo": str(self.root), "state_dir": str(self.state), "poll_seconds": 0.05,
                       "startup_grace_seconds": 0.2, "retry_base_seconds": 0.05,
                       "job_timeout_seconds": 10}
        self.supervisors = []

    def tearDown(self):
        for supervisor in self.supervisors:
            for child in list(supervisor._children.values()):
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=5)
            supervisor.close()
        self.temporary.cleanup()

    def supervisor(self, builder=None, verifier=lambda job, result: True):
        value = Supervisor(self.config, builder or (lambda job, path: command_for(result_for(job), path)), verifier)
        self.supervisors.append(value)
        return value

    def wait(self, supervisor, job_id, stage, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            supervisor.tick()
            job = supervisor.store.get(job_id)
            if job["stage"] == stage:
                return job
            time.sleep(0.03)
        self.fail(f"did not reach {stage}: {supervisor.store.get(job_id)}")

    def enqueue(self, supervisor, task="synthetic-task"):
        return supervisor.store.enqueue(task, self.root, "test-branch", "fake", {"requirements": "synthetic only"})

    def test_full_lifecycle_requires_exact_revision_tests_and_review(self):
        supervisor = self.supervisor()
        job = self.enqueue(supervisor)
        finished = self.wait(supervisor, job["id"], "ready")
        self.assertTrue(finished["tests_passed"])
        self.assertEqual(finished["revision"], finished["reviewed_revision"])
        self.assertEqual("approved", finished["review_result"])
        self.assertEqual("synthetic only", finished["payload"]["requirements"])

    def test_single_supervisor_lock_and_idempotent_enqueue(self):
        supervisor = self.supervisor()
        first = self.enqueue(supervisor)
        second = self.enqueue(supervisor)
        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(RuntimeError, "supervisor_already_running"):
            self.supervisor()
        self.assertEqual(1, len(supervisor.store.jobs()))

    def test_repository_lock_covers_distinct_state_directories(self):
        repo = self.root / "repository"
        created = subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True)
        self.assertEqual(0, created.returncode)
        self.config["repo"] = str(repo)
        first = self.supervisor()
        self.config["state_dir"] = str(self.root / "other-state")
        with self.assertRaisesRegex(RuntimeError, "supervisor_already_running"):
            self.supervisor()
        first.close()
        replacement = self.supervisor()
        self.assertFalse(replacement._lock.closed)

    def test_restart_adopts_worker_and_does_not_launch_twice(self):
        counter = self.root / "count"
        builder = lambda job, path: command_for(result_for(job), path, delay=0.25, counter=counter)
        first = self.supervisor(builder)
        job = self.enqueue(first)
        first.tick()
        first.close()
        replacement = self.supervisor(builder)
        finished = self.wait(replacement, job["id"], "ready")
        self.assertEqual(REVISION, finished["reviewed_revision"])
        self.assertEqual(3, len(counter.read_text().splitlines()))

    def test_launch_discovery_matches_full_unquoted_spaced_spec_path(self):
        spec = self.state / "jobs" / "synthetic" / "spec.json"
        command = f"/path with spaces/python -m factory.worker --spec {spec}"
        output = "\n".join([
            f"100 {command}-other",
            f"101 {command} extra-argument",
            f"102 {command}",
        ])
        result = subprocess.CompletedProcess([], 0, stdout=output)
        with patch("factory.supervisor.subprocess.run", return_value=result), patch(
            "factory.supervisor.process_identity", return_value="identity"
        ) as identify:
            self.assertEqual({"pid": 102, "identity": "identity"}, Supervisor._find_launch(spec))
        identify.assert_called_once_with(102)

    def test_restart_adopts_spaced_launch_without_pid_or_identity_receipt(self):
        counter = self.root / "count"
        builder = lambda job, path: command_for(result_for(job), path, delay=1, counter=counter)
        first = self.supervisor(builder)
        job = self.enqueue(first)
        first.tick()
        launched = first.store.get(job["id"])
        identity_path = Path(launched["attempt_dir"]) / "identity.json"
        end = time.monotonic() + 5
        while not identity_path.exists() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(identity_path.exists())
        identity = json.loads(identity_path.read_text())
        # Reproduce the durable state immediately after Popen but before either
        # the parent PID update or the wrapper identity receipt is persisted.
        identity_path.unlink()
        first.store.update(job["id"], pid=None, process_identity="")
        first.close()
        replacement = self.supervisor(builder)
        replacement.tick()
        adopted = replacement.store.get(job["id"])
        self.assertEqual(identity["pid"], adopted["pid"])
        self.assertEqual(identity["identity"], adopted["process_identity"])
        self.assertEqual(1, adopted["attempt"])
        # The original wrapper's delayed receipt becomes durable after adoption.
        atomic_json(identity_path, identity)
        ready = self.wait(replacement, job["id"], "ready")
        self.assertEqual(REVISION, ready["reviewed_revision"])
        self.assertEqual(3, len(counter.read_text().splitlines()))

    def test_actual_supervisor_process_kill_and_restart(self):
        store = Store(self.state)
        job = store.enqueue("kill-restart", self.root, "test", "fake")
        counter = self.root / "counter"
        marker = self.root / "started"
        program = "\n".join([
            "import json,sys,time",
            "from pathlib import Path",
            "from factory.supervisor import Supervisor",
            f"config={self.config!r}",
            "def builder(job,path):",
            f" result={{'status':'completed','revision':{REVISION!r}}}",
            " if job['stage']=='testing': result['tests_passed']=True",
            f" if job['stage']=='reviewing': result.update(review_result='approved',reviewed_revision={REVISION!r})",
            f" code=\"import json,time;from pathlib import Path;open(\"+repr({str(counter)!r})+\",'a').write('run\\\\n');time.sleep(0.4);Path(\"+repr(str(path))+\").write_text(\"+repr(json.dumps(result))+\")\"",
            " return [sys.executable,'-c',code]",
            "s=Supervisor(config,builder,lambda j,r:True)",
            "s.tick()",
            f"Path({str(marker)!r}).write_text('ready')",
            "s.run()",
        ])
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        process = subprocess.Popen([sys.executable, "-c", program], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            end = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < end and process.poll() is None:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            process.kill()
            process.wait(timeout=5)
            replacement = self.supervisor(lambda j, p: command_for(result_for(j), p, counter=counter))
            self.wait(replacement, job["id"], "ready")
            self.assertEqual(3, len(counter.read_text().splitlines()))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_launch_intent_without_process_is_retried_after_restart(self):
        supervisor = self.supervisor()
        job = self.enqueue(supervisor)
        attempt_dir = self.state / "jobs" / job["id"] / "interrupted-launch"
        attempt_dir.mkdir(parents=True)
        supervisor.store.update(job["id"], attempt=1, attempt_dir=str(attempt_dir), started_at=time.time() - 5)
        supervisor.tick()
        scheduled = supervisor.store.get(job["id"])
        self.assertEqual("worker_lost", scheduled["blocker"])
        self.assertFalse(scheduled["attempt_dir"])
        self.wait(supervisor, job["id"], "ready")

    def test_completion_publication_between_identity_reads_advances_exactly_once(self):
        supervisor = self.supervisor()
        job = self.enqueue(supervisor)
        attempt_dir = self.state / "jobs" / job["id"] / "implementing-1-100"
        attempt_dir.mkdir(parents=True)
        job = supervisor.store.update(job["id"], attempt=1, attempt_dir=str(attempt_dir),
                                      started_at=time.time() - 1)
        receipt = {"pid": 123456, "identity": "a" * 64}

        def publish_after_identity_read(spec_path):
            self.assertEqual(attempt_dir / "spec.json", spec_path)
            atomic_json(attempt_dir / "identity.json", receipt)
            atomic_json(attempt_dir / "completion.json", dict(receipt, result=result_for(job)))
            return None  # The wrapper exited before process discovery.

        with patch.object(supervisor, "_find_launch", side_effect=publish_after_identity_read), patch.object(
            supervisor, "_launch"
        ) as launch:
            supervisor.tick()
            advanced = supervisor.store.get(job["id"])
            self.assertEqual("testing", advanced["stage"])
            self.assertEqual(REVISION, advanced["revision"])
            self.assertEqual("", advanced["blocker"])
            self.assertEqual("", advanced["attempt_dir"])
            supervisor.tick()
            with supervisor.store.connect() as db:
                advances = db.execute("SELECT count(*) FROM events WHERE job_id=? AND code='stage_advanced'",
                                      (job["id"],)).fetchone()[0]
                starts = db.execute("SELECT count(*) FROM events WHERE job_id=? AND code='worker_started'",
                                    (job["id"],)).fetchone()[0]
            self.assertEqual(1, advances)
            self.assertEqual(0, starts)
            self.assertTrue(all(call.args[0]["stage"] == "testing" for call in launch.call_args_list))

    def test_completion_requires_exact_durable_identity_after_publication(self):
        for forged in ({"pid": 654321, "identity": "a" * 64},
                       {"pid": 123456, "identity": "b" * 64},
                       {"pid": True, "identity": "a" * 64},
                       {"pid": 123456, "identity": "not-a-process-identity"}):
            with self.subTest(forged=forged):
                supervisor = self.supervisor()
                job = self.enqueue(supervisor, "forged-" + str(len(self.supervisors)))
                attempt_dir = self.state / "jobs" / job["id"] / "implementing-1-100"
                attempt_dir.mkdir(parents=True)
                job = supervisor.store.update(job["id"], attempt=1, attempt_dir=str(attempt_dir),
                                              started_at=time.time() - 1)

                def publish(spec_path):
                    atomic_json(attempt_dir / "identity.json", {"pid": 123456, "identity": "a" * 64})
                    atomic_json(attempt_dir / "completion.json", dict(forged, result=result_for(job)))
                    return None

                with patch.object(supervisor, "_find_launch", side_effect=publish), patch.object(
                    supervisor, "_launch"
                ) as launch:
                    supervisor.tick()
                    updated = supervisor.store.get(job["id"])
                    self.assertEqual("blocked", updated["stage"])
                    self.assertEqual("invalid_worker_completion", updated["blocker"])
                    self.assertEqual("", updated["revision"])
                    supervisor.tick()
                    launch.assert_not_called()
                supervisor.close()

    def test_completion_cannot_replace_an_already_observed_identity(self):
        supervisor = self.supervisor()
        job = self.enqueue(supervisor)
        attempt_dir = self.state / "jobs" / job["id"] / "implementing-1-100"
        attempt_dir.mkdir(parents=True)
        job = supervisor.store.update(job["id"], attempt=1, attempt_dir=str(attempt_dir),
                                      started_at=time.time() - 1)
        original = {"pid": 123456, "identity": "a" * 64}
        replacement = {"pid": 123456, "identity": "b" * 64}
        with patch.object(supervisor, "_read_json", side_effect=[
            original, dict(replacement, result=result_for(job)), replacement
        ]), patch("factory.supervisor.process_matches", return_value=False), patch(
            "factory.supervisor.group_exists", return_value=False
        ):
            self.assertFalse(supervisor._reconcile(job))
        updated = supervisor.store.get(job["id"])
        self.assertEqual("blocked", updated["stage"])
        self.assertEqual("invalid_worker_completion", updated["blocker"])
        self.assertEqual("", updated["revision"])

    def test_command_failure_retries_bounded_without_logging_output(self):
        counter = self.root / "attempts"
        supervisor = self.supervisor(lambda j, p: command_for({"status": "completed"}, p, counter=counter, exit_code=4))
        job = self.enqueue(supervisor)
        failed = self.wait(supervisor, job["id"], "failed")
        self.assertEqual(3, failed["attempt"])
        self.assertEqual(3, len(counter.read_text().splitlines()))
        self.assertEqual("command_failed", failed["blocker"])

    def test_stale_review_is_blocked(self):
        def builder(job, path):
            result = result_for(job)
            if job["stage"] == "reviewing":
                result["reviewed_revision"] = "b" * 40
            return command_for(result, path)
        supervisor = self.supervisor(builder)
        job = self.enqueue(supervisor)
        blocked = self.wait(supervisor, job["id"], "blocked")
        self.assertEqual("stale_review", blocked["blocker"])

    def test_verifier_fails_closed(self):
        supervisor = self.supervisor(verifier=None)
        job = self.enqueue(supervisor)
        blocked = self.wait(supervisor, job["id"], "blocked")
        self.assertEqual("verification_unavailable", blocked["blocker"])

    def test_failed_test_claim_cannot_advance(self):
        def builder(job, path):
            result = result_for(job)
            if job["stage"] == "testing":
                result["tests_passed"] = False
            return command_for(result, path)
        supervisor = self.supervisor(builder)
        job = self.enqueue(supervisor)
        blocked = self.wait(supervisor, job["id"], "blocked")
        self.assertEqual("test_gate_failed", blocked["blocker"])

    def test_review_fix_retests_and_invalidates_old_review(self):
        reviews = []
        def builder(job, path):
            result = result_for(job)
            if job["stage"] == "reviewing":
                reviews.append(job["fix_count"])
                if job["fix_count"] == 0:
                    result["review_result"] = "changes_requested"
            return command_for(result, path)
        supervisor = self.supervisor(builder)
        job = self.enqueue(supervisor)
        ready = self.wait(supervisor, job["id"], "ready")
        self.assertEqual([0, 1], reviews)
        self.assertEqual(1, ready["fix_count"])
        self.assertTrue(ready["tests_passed"])

    def test_deterministic_test_failure_requests_fix_then_retests(self):
        stages = []
        def builder(job, path):
            stages.append(job["stage"])
            result = result_for(job)
            if job["stage"] == "testing" and job["fix_count"] == 0:
                result = {"status": "failed", "blocker": "quality_gate_failed"}
            if job["stage"] == "fixing":
                self.assertEqual("quality_gate_failed", job["outcome"]["blocker"])
                self.assertFalse(job["tests_passed"])
                self.assertEqual("", job["reviewed_revision"])
            return command_for(result, path)
        supervisor = self.supervisor(builder)
        job = self.enqueue(supervisor)
        ready = self.wait(supervisor, job["id"], "ready")
        self.assertEqual(["implementing", "testing", "fixing", "testing", "reviewing"], stages)
        self.assertEqual(1, ready["fix_count"])
        self.assertTrue(ready["tests_passed"])

    def test_deterministic_test_fix_loop_is_bounded(self):
        self.config["max_fix_attempts"] = 1
        def builder(job, path):
            result = result_for(job)
            if job["stage"] == "testing":
                result = {"status": "failed", "blocker": "quality_gate_failed"}
            return command_for(result, path)
        supervisor = self.supervisor(builder)
        job = self.enqueue(supervisor)
        blocked = self.wait(supervisor, job["id"], "blocked")
        self.assertEqual("test_fix_limit", blocked["blocker"])
        self.assertEqual(1, blocked["fix_count"])
        self.assertEqual("testing", blocked["resume_stage"])

    def test_pid_identity_does_not_accept_reused_pid(self):
        identity = process_identity(os.getpid())
        self.assertTrue(process_matches(os.getpid(), identity))
        self.assertFalse(process_matches(os.getpid(), "0" * 64))
        supervisor = self.supervisor()
        job = self.enqueue(supervisor)
        attempt_dir = self.state / "jobs" / job["id"] / "reused"
        attempt_dir.mkdir(parents=True)
        job = supervisor.store.update(job["id"], attempt=1, attempt_dir=str(attempt_dir),
                                      pid=os.getpid(), process_identity="0" * 64, started_at=time.time() - 10)
        # Reconcile the stale identity without tick's later scheduling pass:
        # retry backoff may expire during SQLite writes and allow a valid new
        # worker to launch, which clears the transient blocker under test.
        with patch("factory.supervisor.os.kill") as signal_pid, patch(
            "factory.supervisor.os.killpg"
        ) as signal_group:
            supervisor._reconcile(job)
        signal_pid.assert_not_called()
        self.assertTrue(all(call.args[1] == 0 for call in signal_group.call_args_list))
        updated = supervisor.store.get(job["id"])
        self.assertNotEqual(identity, updated["process_identity"])
        self.assertTrue(process_matches(os.getpid(), identity))
        self.assertIn(updated["blocker"], {"worker_lost", "orphaned_process_group"})
        self.assertEqual({}, supervisor._children)
        self.assertEqual(1, updated["attempt"])

    def test_result_redaction(self):
        result = sanitize_result({"status": "blocked", "blocker": "private value with spaces",
                                  "summary": "secret", "stderr": "secret", "token": "secret"})
        self.assertEqual({"status": "blocked", "blocker": "agent_result_redacted"}, result)

    def test_worker_crash_with_live_child_never_launches_duplicate(self):
        counter = self.root / "orphan-counter"
        supervisor = self.supervisor(lambda j, p: command_for(result_for(j), p, delay=1, counter=counter))
        job = self.enqueue(supervisor)
        supervisor.tick()
        end = time.monotonic() + 3
        while not counter.exists() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(counter.exists())
        running = supervisor.store.get(job["id"])
        wrapper = supervisor._children[running["pid"]]
        wrapper.kill()
        wrapper.wait(timeout=5)
        supervisor.tick()
        waiting = supervisor.store.get(job["id"])
        self.assertEqual("orphaned_process_group", waiting["blocker"])
        self.assertEqual("implementing", waiting["stage"])
        with self.assertRaisesRegex(ValueError, "job_not_resumable"):
            supervisor.store.resume(job["id"])
        self.assertEqual(1, len(counter.read_text().splitlines()))
        self.wait(supervisor, job["id"], "ready")
        # One interrupted implementation attempt, then exactly one replacement
        # and the test/review stages. No user continuation was needed.
        self.assertEqual(4, len(counter.read_text().splitlines()))

    def test_worker_timeout_is_bounded(self):
        self.config["job_timeout_seconds"] = 0.15
        self.config["max_attempts"] = 1
        supervisor = self.supervisor(lambda j, p: command_for(result_for(j), p, delay=30))
        job = self.enqueue(supervisor)
        failed = self.wait(supervisor, job["id"], "failed")
        self.assertEqual("worker_timeout", failed["blocker"])

    def test_maximum_two_workers(self):
        supervisor = self.supervisor(lambda j, p: command_for(result_for(j), p, delay=1))
        for number in range(3):
            self.enqueue(supervisor, f"synthetic-{number}")
        state = supervisor.tick()
        self.assertEqual(2, state["active"])
        self.assertEqual(2, sum(bool(job["attempt_dir"]) for job in supervisor.store.jobs()))


if __name__ == "__main__":
    unittest.main()
