"""Small supervised process wrapper. Completion survives coordinator restarts.

Agent output is never logged. Only an allowlisted final JSON result is retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


SHA = re.compile(r"^[0-9a-f]{40}$")
CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def atomic_json(path: str | Path, value: dict):
    path = Path(path)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def process_identity(pid: int) -> str | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        found = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,lstart=,command="],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = found.stdout.strip()
    if found.returncode or not value or value.split(None, 1)[0].startswith("Z") or "<defunct>" in value:
        return None
    # Scheduling flags change between reads and are not process identity.
    return hashlib.sha256(value.split(None, 1)[1].encode()).hexdigest()


def process_matches(pid: int, identity: str) -> bool:
    return bool(identity and process_identity(pid) == identity)


def group_exists(pgid: int) -> bool:
    # A completed child may remain a zombie until its original supervisor is
    # reaped. killpg(..., 0) reports that group as present, although it cannot
    # perform more work. Only live members prevent a replacement launch.
    try:
        result = subprocess.run(["ps", "-ax", "-o", "pgid=,stat="],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == str(pgid) and not parts[1].startswith("Z"):
                    return True
            return False
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def sanitize_result(value) -> dict:
    if not isinstance(value, dict):
        return {"status": "failed", "blocker": "invalid_agent_result"}
    result = {"status": value.get("status") if value.get("status") in {"completed", "blocked", "failed"} else "failed"}
    for key in ("revision", "reviewed_revision"):
        if isinstance(value.get(key), str) and SHA.fullmatch(value[key]):
            result[key] = value[key]
    if type(value.get("tests_passed")) is bool:
        result["tests_passed"] = value["tests_passed"]
    if value.get("review_result") in {"approved", "changes_requested"}:
        result["review_result"] = value["review_result"]
    if isinstance(value.get("runtime_session_id"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value["runtime_session_id"]):
        result["runtime_session_id"] = value["runtime_session_id"]
    if result["status"] != "completed":
        blocker = value.get("blocker", "agent_blocked" if result["status"] == "blocked" else "agent_failed")
        result["blocker"] = blocker if isinstance(blocker, str) and CODE.fullmatch(blocker) else "agent_result_redacted"
    return result


def run_spec(path: str | Path) -> int:
    spec_path = Path(path).resolve()
    spec = json.loads(spec_path.read_text())
    pid = os.getpid()
    identity = process_identity(pid)
    if not identity or os.getpgrp() != pid:
        return 2
    atomic_json(spec["identity_path"], {"pid": pid, "identity": identity, "started_at": time.time()})
    result = {"status": "failed", "blocker": "worker_failed"}
    code = 1
    child = None
    try:
        argv = spec["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError("invalid_command")
        stdin = None
        try:
            if spec.get("stdin_path"):
                stdin = open(spec["stdin_path"], "rb")
            child = subprocess.Popen(argv, cwd=spec["cwd"], stdin=stdin or subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                code = child.wait(timeout=float(spec["timeout_seconds"]))
            except subprocess.TimeoutExpired:
                # The wrapper survives while killing the owned process group, so its
                # durable completion can be written after child cleanup.
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                os.killpg(pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                code = 124
                result = {"status": "failed", "blocker": "worker_timeout"}
            else:
                if code == 0:
                    outcome = Path(spec["outcome_path"])
                    if outcome.is_file() and outcome.stat().st_size <= 65536:
                        result = sanitize_result(json.loads(outcome.read_text()))
                    else:
                        result = {"status": "failed", "blocker": "missing_agent_result"}
                else:
                    result = {"status": "failed", "blocker": "command_failed"}
        finally:
            if stdin:
                stdin.close()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        result = {"status": "failed", "blocker": "worker_failed"}
    finally:
        # Remove arbitrary final responses once reduced to the approved fields.
        try:
            Path(spec["outcome_path"]).unlink(missing_ok=True)
        except OSError:
            pass
        atomic_json(spec["completion_path"], {"pid": pid, "identity": identity, "exit_code": code,
                    "finished_at": time.time(), "result": result})
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    raise SystemExit(run_spec(args.spec))


if __name__ == "__main__":
    main()
