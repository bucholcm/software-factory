"""Operational CLI; status never includes task text or raw agent output."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

from .adapters import builder, verifier
from .config import command, git, load, write_json
from .integration import integrate_ready
from .recovery import code_checkpoint, restore_code_into
from .store import Store
from .supervisor import Supervisor
from .worker import process_identity, process_matches


def public_jobs(jobs):
    fields = ("id", "task_id", "runtime", "stage", "attempt", "pid", "revision", "reviewed_revision",
              "review_result", "tests_passed", "blocker", "updated_at")
    values = [{key: job[key] for key in fields} for job in jobs]
    for value, job in zip(values, jobs):
        value["decision_id"] = job.get("payload", {}).get("decision_id", "")
        value["next_action"] = ("answer_product_decision" if value["decision_id"] and value["stage"] == "blocked"
                                else "await_runtime_recovery" if value["blocker"]
                                else "await_task" if value["stage"] == "integrated" else value["stage"])
    return values


def status(config):
    path = Path(config["state_dir"]) / "status.json"
    try:
        value = json.loads(path.read_text())
        alive = process_matches(value.get("pid"), value.get("process_identity", ""))
        value["status"] = "running" if alive else "stopped"
    except (OSError, ValueError, TypeError):
        value = {"status": "stopped"}
    value["jobs"] = public_jobs(Store(config["state_dir"]).jobs())
    report = Path(config["state_dir"]) / "maintenance" / "maintenance.json"
    if report.exists():
        try:
            from .maintenance import load_report
            value["maintenance"] = load_report(report.parent, revision=git(config["repo"], "rev-parse", "HEAD"))
        except Exception:
            value["maintenance"] = {"status": "error", "error_code": "report_unavailable"}
    return value


def enqueue(config, task_id, requirements, runtime=None, acceptance="", allowed_paths=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", task_id):
        raise ValueError("invalid_task_id")
    store = Store(config["state_dir"])
    for job in store.jobs():
        if job["task_id"] == task_id:
            return job
    # Enqueue is serialized independently of the supervisor's long-lived lock.
    with open(Path(config["state_dir"]) / "enqueue.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for job in store.jobs():
            if job["task_id"] == task_id:
                return job
        base = git(config["repo"], "rev-parse", config["base_branch"])
        branch = "factory/" + task_id
        worktree = Path(config["state_dir"]) / "worktrees" / task_id
        worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not worktree.exists():
            result = command(["git", "worktree", "add", "-b", branch, str(worktree), base], cwd=config["repo"])
            if result.returncode:
                raise RuntimeError("isolated_worktree_failed")
        elif git(worktree, "rev-parse", "HEAD") != base or git(worktree, "status", "--porcelain"):
            raise RuntimeError("existing_worktree_needs_reconciliation")
        return store.enqueue(task_id, worktree, branch, runtime or config["runtime"],
                             payload={"requirements": requirements, "acceptance": acceptance,
                                      "base_revision": base,
                                      "allowed_paths": allowed_paths or [],
                                      "question_path": str(Path(config["state_dir"]) / "questions" / (task_id + ".json")),
                                      "findings_path": str(Path(config["state_dir"]) / "findings" / (task_id + ".json"))})


def run(config_path):
    config = load(config_path)
    supervisor = Supervisor(config, builder(config_path), verifier)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: supervisor.stop())
    package_root = Path(__file__).resolve().parent.parent
    scan_process = None
    scan_next_run = 0
    tracker_next_run = 0
    tracker_status = {"status": "not_checked"}
    try:
        while not supervisor._stop.is_set():
            snapshot = supervisor.tick()
            if config.get("tracker_enabled") and time.time() >= tracker_next_run:
                try:
                    from .tracker import reconcile
                    tracker_status = reconcile(config, supervisor.store, enqueue)
                except Exception:
                    tracker_status = {"status": "tracker_or_github_unavailable"}
                tracker_next_run = time.time() + config.get("tracker_interval_seconds", 30)
            snapshot["tracker"] = tracker_status
            try:
                integrate_ready(supervisor, config)
                snapshot["integration_error"] = ""
            except Exception:
                snapshot["integration_error"] = "integration_requires_attention"
            snapshot["jobs"] = public_jobs(supervisor.store.jobs())
            snapshot["process_identity"] = process_identity(os.getpid()) or ""
            snapshot["integration_policy"] = config["integration_policy"]
            snapshot["auto_merge"] = config["auto_merge"]
            write_json(Path(config["state_dir"]) / "status.json", snapshot)
            maintenance_dir = Path(config["state_dir"]) / "maintenance"
            report = maintenance_dir / "maintenance.json"
            due = not report.exists() or time.time() - report.stat().st_mtime > config.get("maintenance_interval_seconds", 86400)
            if report.exists() and not due:
                try:
                    due = json.loads(report.read_text()).get("revision") != git(config["repo"], "rev-parse", "HEAD")
                except (OSError, ValueError):
                    due = True
            # A separate scanner lock also covers a surviving scan after restart.
            if due and time.time() >= scan_next_run and (scan_process is None or scan_process.poll() is not None):
                scan_process = subprocess.Popen([sys.executable, "-m", "factory", "--config", str(config_path), "maintenance"],
                                                cwd=package_root, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                start_new_session=True)
                scan_next_run = time.time() + 60
            supervisor._stop.wait(config.get("poll_seconds", 3))
    finally:
        supervisor.close()


def main():
    parser = argparse.ArgumentParser(prog="software-factory")
    parser.add_argument("--config", required=True, type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--repo", type=Path, required=True)
    init.add_argument("--state-dir", type=Path, required=True)
    sub.add_parser("run")
    check = sub.add_parser("status")
    check.add_argument("--watch", action="store_true")
    task = sub.add_parser("enqueue")
    task.add_argument("--task", required=True)
    task.add_argument("--requirements-file", type=Path, required=True)
    task.add_argument("--acceptance", default="")
    task.add_argument("--runtime", choices=["codex", "opencode"])
    resume = sub.add_parser("resume")
    resume.add_argument("job_id")
    sub.add_parser("maintenance")
    sub.add_parser("checkpoint")
    restore = sub.add_parser("restore-code-into")
    restore.add_argument("--checkpoint", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "init":
        if args.config.exists():
            raise ValueError("config_exists")
        state = args.state_dir.expanduser().resolve()
        config = {"schema_version": 1, "repo": str(args.repo.resolve()), "state_dir": str(state),
                  "base_branch": "main", "runtime": "codex", "poll_seconds": 3,
                  "max_attempts": 3, "max_fix_attempts": 3, "max_active_jobs": 1,
                  "job_timeout_seconds": 1800, "agent_timeout_seconds": 900,
                  "auto_merge": True, "integration_policy": "documentation_only",
                  "quality_gates": [["make", "check"], ["make", "e2e"]],
                  "maintenance_interval_seconds": 86400,
                  "tracker_enabled": True, "tracker_interval_seconds": 30,
                  "bd_executable": shutil.which("bd"), "gh_executable": shutil.which("gh"),
                  "codex_executable": shutil.which("codex"), "opencode_executable": shutil.which("opencode")}
        git(config["repo"], "rev-parse", "main")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json(args.config, config)
        print(json.dumps({"status": "configured", "integration_policy": config["integration_policy"]}))
        return
    config = load(args.config)
    if args.action == "run":
        run(args.config.resolve())
    elif args.action == "status":
        while True:
            print(json.dumps(status(config), sort_keys=True, indent=2), flush=True)
            if not args.watch:
                break
            time.sleep(5)
    elif args.action == "enqueue":
        job = enqueue(config, args.task, args.requirements_file.read_text(), args.runtime, args.acceptance)
        print(json.dumps(public_jobs([job])[0], sort_keys=True))
    elif args.action == "resume":
        print(json.dumps(public_jobs([Store(config["state_dir"]).resume(args.job_id)])[0]))
    elif args.action == "maintenance":
        from .maintenance import scan
        directory = Path(config["state_dir"]) / "maintenance"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(directory / "scan.lock", "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            report = scan(Path(config["repo"]), directory,
                          commands=config.get("maintenance_commands"),
                          environment_python=Path(config["maintenance_python_environment"])
                          if config.get("maintenance_python_environment") else None)
            print(json.dumps({"status": report["status"], "last_run": report["last_run"]}))
    elif args.action == "checkpoint":
        checkpoint = code_checkpoint(config["repo"], Path(config["state_dir"]) / "backups", config["base_branch"])
        print(json.dumps({"checkpoint_id": checkpoint.name, "status": "verified"}))
    elif args.action == "restore-code-into":
        print(json.dumps(restore_code_into(args.checkpoint, args.destination)))


if __name__ == "__main__":
    main()
