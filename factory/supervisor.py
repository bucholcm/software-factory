"""Deterministic process reconciliation around replaceable agent adapters."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .store import Store
from .worker import atomic_json, group_exists, process_identity, process_matches, sanitize_result


ACTIVE_STAGES = frozenset({"implementing", "testing", "reviewing", "fixing"})


class Supervisor:
    """One supervisor per state directory.

    ``command_builder(job, result_path)`` returns an argv list, or a dict with
    ``argv`` and optional ``cwd``, ``stdin_path`` and ``timeout_seconds``.
    ``verifier(job, result)`` returns True or a sanitized verified result dict.
    False/None/exception blocks the job; agent claims alone never pass a gate.
    """

    def __init__(self, config: dict, command_builder, verifier=None, startup_callback=None):
        self.config = dict(config)
        self.store = Store(config["state_dir"])
        self.command_builder = command_builder
        self.verifier = verifier
        self.poll_seconds = max(0.05, float(config.get("poll_seconds", 2)))
        self.max_attempts = max(1, int(config.get("max_attempts", 3)))
        self.max_active_jobs = min(2, max(1, int(config.get("max_active_jobs", 2))))
        self._stop = threading.Event()
        self._children = {}
        self._repo_lock = None
        self._lock = open(self.store.state_dir / "supervisor.lock", "a+")
        os.chmod(self.store.state_dir / "supervisor.lock", 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise RuntimeError("supervisor_already_running") from None
        try:
            if startup_callback is not None:
                startup_callback()
            if config.get("repo"):
                repo = Path(config["repo"]).resolve()
                common = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
                                        capture_output=True, text=True, timeout=10)
                if common.returncode == 0:
                    common_path = Path(common.stdout.strip())
                    if not common_path.is_absolute():
                        common_path = repo / common_path
                    lock_path = common_path.resolve() / "factory-supervisor.lock"
                    self._repo_lock = open(lock_path, "a+")
                    os.chmod(lock_path, 0o600)
                    try:
                        fcntl.flock(self._repo_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        raise RuntimeError("supervisor_already_running") from None
                elif config.get("runtime") in {"codex", "opencode"}:
                    raise RuntimeError("invalid_repository")
                # Injected fake runners may deliberately use a non-Git workspace.
        except Exception:
            self.close()
            raise
        self._lock.seek(0)
        self._lock.truncate()
        self._lock.write(str(os.getpid()))
        self._lock.flush()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._stop.set()
        if self._repo_lock is not None and not self._repo_lock.closed:
            fcntl.flock(self._repo_lock, fcntl.LOCK_UN)
            self._repo_lock.close()
        if not self._lock.closed:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()

    def stop(self):
        """Stop the coordinator. Its owned worker wrappers keep running."""
        self._stop.set()

    def run(self):
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.poll_seconds)
        finally:
            self.close()

    def _reap(self):
        for pid, child in list(self._children.items()):
            if child.poll() is not None:
                del self._children[pid]

    @staticmethod
    def _read_json(path: Path):
        try:
            if path.stat().st_size > 65536:
                return None
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _block(self, job, code):
        self.store.update(job["id"], stage="blocked", resume_stage=job["stage"], blocker=code)
        self.store.event(job["id"], job["stage"], code)

    def _retry(self, job, code):
        if job["attempt"] >= self.max_attempts:
            self.store.update(job["id"], stage="failed", resume_stage=job["stage"], blocker=code,
                              pid=None, process_identity="", attempt_dir="", started_at=0)
            self.store.event(job["id"], job["stage"], "attempts_exhausted")
        else:
            backoff = max(0.05, float(self.config.get("retry_base_seconds", 5))) * (2 ** max(0, job["attempt"] - 1))
            self.store.update(job["id"], pid=None, process_identity="", attempt_dir="", started_at=0,
                              blocker=code, next_run=time.time() + backoff)
            self.store.event(job["id"], job["stage"], "retry_scheduled")

    @staticmethod
    def _find_launch(spec_path: Path):
        """Find a wrapper launched immediately before its parent crashed."""
        try:
            result = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=5)
            if result.returncode:
                return None
            # ps joins argv with spaces without shell quoting. Splitting that
            # representation destroys spec paths such as Application Support.
            # Our wrapper has one fixed argv prefix and the known full spec path
            # as its final argument, so match that exact suffix instead.
            wrapper_suffix = " -m factory.worker --spec " + str(spec_path)
            for line in result.stdout.splitlines():
                try:
                    value, command = line.strip().split(None, 1)
                    if command.endswith(wrapper_suffix):
                        pid = int(value)
                        identity = process_identity(pid)
                        if identity:
                            return {"pid": pid, "identity": identity}
                except ValueError:
                    continue
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None

    def _finish(self, job, result):
        result = sanitize_result(result)
        if result["status"] == "blocked":
            self._block(job, result["blocker"])
            return
        if (job["stage"] == "testing" and result["status"] == "failed"
                and result.get("blocker") in {"quality_gate_failed", "security_gate_failed"}):
            if job["fix_count"] >= max(1, int(self.config.get("max_fix_attempts", 3))):
                self.store.update(job["id"], outcome=result)
                self._block(job, "test_fix_limit")
                return
            self.store.update(job["id"], stage="fixing", fix_count=job["fix_count"] + 1,
                              pid=None, process_identity="", attempt_dir="", started_at=0,
                              next_run=0, attempt=0, blocker="", outcome=result,
                              tests_passed=False, reviewed_revision="", review_result="")
            self.store.event(job["id"], "fixing", "test_fix_requested")
            return
        if result["status"] != "completed":
            self._retry(job, result.get("blocker", "worker_failed"))
            return
        if self.verifier is None:
            self._block(job, "verification_unavailable")
            return
        try:
            verified = self.verifier(job, result)
            if verified is True:
                verified = result
            if not isinstance(verified, dict) or verified.get("status") != "completed":
                self._block(job, "verification_failed")
                return
            result = sanitize_result(verified)
        except Exception:
            self._block(job, "verification_failed")
            return
        stage = job["stage"]
        changes = dict(pid=None, process_identity="", attempt_dir="", started_at=0,
                       next_run=0, attempt=0, blocker="", outcome=result)
        if "runtime_session_id" in result:
            changes["runtime_session_id"] = result["runtime_session_id"]
        if stage in {"implementing", "fixing"}:
            if not result.get("revision"):
                self._block(job, "missing_revision")
                return
            changes.update(stage="testing", revision=result["revision"], tests_passed=False,
                           reviewed_revision="", review_result="")
        elif stage == "testing":
            if result.get("tests_passed") is not True or result.get("revision") != job["revision"]:
                self._block(job, "test_gate_failed")
                return
            changes.update(stage="reviewing", tests_passed=True)
        elif stage == "reviewing":
            if result.get("reviewed_revision") != job["revision"] or not job["tests_passed"]:
                self._block(job, "stale_review")
                return
            if result.get("review_result") == "approved":
                changes.update(stage="ready", reviewed_revision=result["reviewed_revision"], review_result="approved")
            elif result.get("review_result") == "changes_requested":
                if job["fix_count"] >= max(1, int(self.config.get("max_fix_attempts", 3))):
                    self._block(job, "review_fix_limit")
                    return
                changes.update(stage="fixing", fix_count=job["fix_count"] + 1, tests_passed=False,
                               review_result="changes_requested", reviewed_revision=result["reviewed_revision"])
            else:
                self._block(job, "invalid_review")
                return
        self.store.update(job["id"], **changes)
        self.store.event(job["id"], changes["stage"], "stage_advanced")

    def _reconcile(self, job) -> bool:
        """Return True while this attempt still owns a live process."""
        attempt_dir = Path(job["attempt_dir"])
        identity = self._read_json(attempt_dir / "identity.json")
        if not identity:
            identity = self._find_launch(attempt_dir / "spec.json")
        if identity and isinstance(identity.get("pid"), int) and isinstance(identity.get("identity"), str):
            if job["pid"] != identity["pid"] or job["process_identity"] != identity["identity"]:
                job = self.store.update(job["id"], pid=identity["pid"], process_identity=identity["identity"])
        alive = bool(job["pid"] and process_matches(job["pid"], job["process_identity"]))
        if alive:
            # Wrapper handles its child timeout. Never signal an unverified PID.
            hard_limit = float(self.config.get("job_timeout_seconds", 1800)) + 30
            if time.time() - job["started_at"] > hard_limit:
                self._block(job, "worker_watchdog_timeout")
            return True
        if job["pid"] and group_exists(job["pid"]):
            # A crashed wrapper can leave its child alive. Wait for that owned
            # attempt to settle rather than duplicating it or asking a human to
            # resume a transient technical condition.
            hard_limit = float(self.config.get("job_timeout_seconds", 1800)) + 30
            if time.time() - job["started_at"] > hard_limit:
                self._block(job, "orphan_watchdog_timeout")
            elif job["blocker"] != "orphaned_process_group":
                self.store.update(job["id"], blocker="orphaned_process_group")
                self.store.event(job["id"], job["stage"], "orphaned_process_group")
            return True
        completion = self._read_json(attempt_dir / "completion.json")
        if completion:
            if (not identity or completion.get("pid") != identity.get("pid")
                    or completion.get("identity") != identity.get("identity")):
                self._block(job, "invalid_worker_completion")
                return False
            self._finish(job, completion.get("result"))
            return False
        grace = max(0.1, float(self.config.get("startup_grace_seconds", 5)))
        if time.time() - job["started_at"] > grace:
            self._retry(job, "worker_lost")
            return False
        return True

    def _launch(self, job):
        attempt = job["attempt"] + 1
        attempt_dir = self.store.state_dir / "jobs" / job["id"] / f"{job['stage']}-{attempt}-{time.time_ns()}"
        attempt_dir.mkdir(parents=True, mode=0o700)
        outcome_path = attempt_dir / "outcome.json"
        try:
            command = self.command_builder(job, outcome_path)
            if isinstance(command, list):
                command = {"argv": command}
            if not isinstance(command, dict) or not isinstance(command.get("argv"), list) or not command["argv"]:
                self._block(job, "invalid_runtime_command")
                return
            spec = {"argv": command["argv"], "cwd": str(command.get("cwd", job["worktree"])),
                    "outcome_path": str(outcome_path), "identity_path": str(attempt_dir / "identity.json"),
                    "completion_path": str(attempt_dir / "completion.json"),
                    "timeout_seconds": float(command.get("timeout_seconds", self.config.get("job_timeout_seconds", 1800)))}
            if command.get("stdin_path"):
                spec["stdin_path"] = str(command["stdin_path"])
            atomic_json(attempt_dir / "spec.json", spec)
            # Commit the launch intent before Popen. A restarted supervisor can
            # adopt the wrapper by this unique spec path even before PID storage.
            job = self.store.update(job["id"], attempt=attempt, attempt_dir=str(attempt_dir),
                                    started_at=time.time(), pid=None, process_identity="", blocker="")
            package_root = str(Path(__file__).resolve().parent.parent)
            env = dict(os.environ)
            env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            child = subprocess.Popen([sys.executable, "-m", "factory.worker", "--spec", str(attempt_dir / "spec.json")],
                                     cwd=package_root, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, close_fds=True)
            self._children[child.pid] = child
            self.store.update(job["id"], pid=child.pid, process_identity=process_identity(child.pid) or "")
            self.store.event(job["id"], job["stage"], "worker_started")
        except Exception:
            # Do not copy exception messages, command text or runtime output into
            # operational records. A committed launch intent gets reconciled.
            current = self.store.get(job["id"])
            if current["attempt_dir"]:
                self.store.event(job["id"], job["stage"], "launch_reconcile_required")
            else:
                self._block(job, "runtime_launch_failed")

    def tick(self) -> dict:
        if self._lock.closed:
            raise RuntimeError("supervisor_closed")
        self._reap()
        active = 0
        # Reconcile every previous attempt before starting any new work.
        for job in self.store.jobs():
            if job["stage"] in ACTIVE_STAGES and job["attempt_dir"]:
                active += int(self._reconcile(job))
            elif job["stage"] in {"blocked", "failed"} and job["pid"]:
                # A blocked watchdog/orphan still consumes capacity.
                if process_matches(job["pid"], job["process_identity"]) or group_exists(job["pid"]):
                    active += 1
        for job in self.store.jobs():
            if active >= self.max_active_jobs:
                break
            if (job["stage"] in ACTIVE_STAGES and not job["attempt_dir"]
                    and job["next_run"] <= time.time()):
                self._launch(job)
                current = self.store.get(job["id"])
                if current["attempt_dir"]:
                    active += 1
        jobs = self.store.jobs()
        return {"status": "running", "pid": os.getpid(), "active": active, "jobs": jobs,
                "updated_at": time.time()}
