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
import uuid

from .adapters import builder, verifier
from .config import command, git, load, write_json
from .integration import integrate_ready
from .recovery import code_checkpoint, restore_code_into
from .store import Store
from .supervisor import Supervisor
from .worker import group_exists, process_identity, process_matches


def public_jobs(jobs):
    fields = ("id", "task_id", "runtime", "stage", "attempt", "pid", "branch", "worktree", "revision", "reviewed_revision",
              "review_result", "tests_passed", "blocker", "updated_at", "created_at", "started_at", "next_run", "fix_count")
    values = [{key: job[key] for key in fields} for job in jobs]
    for value, job in zip(values, jobs):
        from .worker import sanitize_result
        diagnostic = sanitize_result(job.get("outcome", {})).get("diagnostic")
        if diagnostic:
            value["diagnostic"] = diagnostic
        value["decision_id"] = job.get("payload", {}).get("decision_id", "")
        value["next_action"] = ("answer_product_decision" if value["decision_id"] and value["stage"] == "blocked"
                                else "enable_code_integration_or_keep_branch" if value["blocker"] == "integration_policy_gate"
                                else "repair_project_environment_then_resume" if value["blocker"].startswith("environment_")
                                else "await_runtime_recovery" if value["blocker"]
                                else "await_task" if value["stage"] == "integrated" else value["stage"])
    return values


def status(config):
    path = Path(config["state_dir"]) / "status.json"
    try:
        value = json.loads(path.read_text())
        alive = process_matches(value.get("pid"), value.get("process_identity", ""))
        if not alive:
            value["status"] = "stopped"
        elif time.time() - value.get("updated_at", 0) > config.get("heartbeat_stale_seconds", 120):
            value["status"] = "unresponsive"
        elif value.get("status") not in {"starting", "blocked", "stopped"}:
            value["status"] = "running" if value.get("ready", True) else "starting"
    except (OSError, ValueError, TypeError):
        value = {"status": "stopped"}
    store = Store(config["state_dir"])
    jobs = store.jobs()
    value["jobs"] = public_jobs(jobs)
    from .progress import project, timelines, summary
    timeline = timelines(store)
    now = time.time()
    for public, job in zip(value["jobs"], jobs):
        live = bool(job.get("pid") and process_matches(job["pid"], job.get("process_identity", "")))
        group_live = bool(not live and job.get("pid") and job.get("attempt_dir") and job.get("process_identity")
                          and process_identity(job["pid"]) is None and group_exists(job["pid"]))
        public["progress"] = project(job, value, timeline.get(job["id"], {}), now=now, worker_alive=live, group_alive=group_live)
    value["activity"] = summary(value["jobs"])
    value.setdefault("task_policy", config.get("task_policy", "documentation_only"))
    value.setdefault("integration_policy", config["integration_policy"])
    value["activation_policy"] = "explicit_release_command"
    from .release import release_status
    value["release"] = release_status(config)
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
    status_path = Path(config["state_dir"]) / "status.json"
    startup = {"startup_id": uuid.uuid4().hex, "started_at": time.time(), "pid": os.getpid(),
               "process_identity": process_identity(os.getpid()) or ""}
    announced = False
    def heartbeat(snapshot=None, *, ready=False, phase="initializing", state="starting", error=None):
        value = {**(snapshot or {}), **startup, "ready": ready, "status": state,
                 "phase": phase, "updated_at": time.time()}
        if error:
            value["startup_error"] = error
        write_json(status_path, value)
    def announce_start():
        nonlocal announced
        heartbeat()
        announced = True
    try:
        supervisor = Supervisor(config, builder(config_path), verifier, startup_callback=announce_start)
    except Exception:
        if announced:
            heartbeat(phase="startup_failed", state="blocked", error="initialization_failed")
        raise
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: supervisor.stop())
    package_root = Path(__file__).resolve().parent.parent
    scan_process = None
    scan_next_run = 0
    tracker_next_run = 0
    tracker_status = {"status": "not_checked"}
    feedback_process = None
    feedback_next_run = 0
    has_reconciled = False
    try:
        while not supervisor._stop.is_set():
            refreshed = load(config_path)
            if refreshed["repo"] != config["repo"] or refreshed["state_dir"] != config["state_dir"]:
                raise RuntimeError("running_project_configuration_changed")
            for key in ("task_policy", "integration_policy", "auto_merge"):
                if key in refreshed:
                    config[key] = refreshed[key]
            heartbeat({"jobs": public_jobs(supervisor.store.jobs())}, ready=has_reconciled,
                      phase="reconciling", state="running")
            snapshot = supervisor.tick()
            has_reconciled = True
            snapshot["jobs"] = public_jobs(supervisor.store.jobs())
            snapshot["tracker"] = tracker_status
            snapshot["integration_policy"] = config["integration_policy"]
            snapshot["task_policy"] = config.get("task_policy", "documentation_only")
            snapshot["activation_policy"] = "explicit_release_command"
            snapshot["auto_merge"] = config["auto_merge"]
            heartbeat(snapshot, ready=True, phase="checking_tracker", state="running")
            if config.get("tracker_enabled") and time.time() >= tracker_next_run:
                try:
                    from .tracker import reconcile
                    tracker_status = reconcile(config, supervisor.store, enqueue)
                except Exception:
                    tracker_status = {"status": "tracker_or_github_unavailable"}
                tracker_next_run = time.time() + config.get("tracker_interval_seconds", 30)
            snapshot["tracker"] = tracker_status
            heartbeat(snapshot, ready=True, phase="integrating", state="running")
            try:
                integrate_ready(supervisor, config)
                snapshot["integration_error"] = ""
            except Exception:
                snapshot["integration_error"] = "integration_requires_attention"
            snapshot["jobs"] = public_jobs(supervisor.store.jobs())
            snapshot["integration_policy"] = config["integration_policy"]
            snapshot["auto_merge"] = config["auto_merge"]
            heartbeat(snapshot, ready=True, phase="idle", state="running")
            if ((refreshed.get("reliability_tracker_repo") or refreshed.get("reliability_repair_config")) and time.time() >= feedback_next_run
                    and (feedback_process is None or feedback_process.poll() is not None)):
                feedback_process = subprocess.Popen(
                    [sys.executable, "-m", "factory", "--config", str(config_path), "reliability", "--sync"],
                    cwd=package_root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                feedback_next_run = time.time() + max(30, float(refreshed.get("reliability_interval_seconds", 60)))
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
        heartbeat({"jobs": public_jobs(supervisor.store.jobs())}, phase="stopped", state="stopped")


def main():
    parser = argparse.ArgumentParser(prog="software-factory")
    parser.add_argument("--config", required=True, type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--repo", type=Path, required=True)
    init.add_argument("--state-dir", type=Path, required=True)
    sub.add_parser("run")
    mode = sub.add_parser("set-mode")
    mode.add_argument("mode", choices=["documentation", "source-review", "source-integrate"])
    submit = sub.add_parser("submit")
    submit.add_argument("--task", required=True)
    submit.add_argument("--project", required=True, type=Path)
    check = sub.add_parser("status")
    check.add_argument("--watch", action="store_true")
    task = sub.add_parser("enqueue")
    task.add_argument("--task", required=True)
    task.add_argument("--requirements-file", type=Path, required=True)
    task.add_argument("--acceptance", default="")
    task.add_argument("--runtime", choices=["codex", "opencode"])
    task.add_argument("--allowed-path", action="append", default=[])
    resume = sub.add_parser("resume")
    resume.add_argument("job_id")
    resume.add_argument("--retest", action="store_true", help="Recheck the unchanged failed revision after environment repair; still requires independent review")
    sub.add_parser("maintenance")
    feedback = sub.add_parser("reliability")
    feedback.add_argument("--sync", action="store_true")
    feedback.add_argument("--dispatch", action="store_true")
    environment = sub.add_parser("prepare-environment")
    environment.add_argument("job_id")
    sub.add_parser("checkpoint")
    sub.add_parser("checkpoint-state")
    activation = sub.add_parser("activate")
    activation.add_argument("--revision", required=True)
    sub.add_parser("recover-release")
    restore_state = sub.add_parser("restore-state-into")
    restore_state.add_argument("--checkpoint", type=Path, required=True)
    restore_state.add_argument("--destination", type=Path, required=True)
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
                  "auto_merge": True, "integration_policy": "documentation_only", "task_policy": "documentation_only",
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
    elif args.action == "set-mode":
        if args.mode == "source-integrate":
            from .security import policy
            policy(config)
            if config.get("integration_checkout_isolated") is not True:
                raise ValueError("integration_checkout_isolation_required")
        config.update(task_policy="documentation_only" if args.mode == "documentation" else "source",
                      integration_policy="reviewed_code" if args.mode == "source-integrate" else "documentation_only",
                      auto_merge=True)
        write_json(args.config, config)
        print(json.dumps({"status": "configuration_updated", "mode": args.mode,
                          "task_policy": config["task_policy"], "integration_policy": config["integration_policy"]}))
    elif args.action == "submit":
        from .tracker import reconcile
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", args.task):
            raise ValueError("invalid_task_id")
        common = lambda repo: git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if Path(common(args.project)).resolve() != Path(common(config["repo"])).resolve():
            raise ValueError("configuration_belongs_to_another_project")
        health = status(config)
        if health.get("status") != "running" or not health.get("ready"):
            print(json.dumps({"status": "supervisor_not_ready", "task_id": args.task}))
            return
        print(json.dumps(reconcile(config, Store(config["state_dir"]), enqueue, task_id=args.task), sort_keys=True))
    elif args.action == "status":
        while True:
            print(json.dumps(status(config), sort_keys=True, indent=2), flush=True)
            if not args.watch:
                break
            time.sleep(5)
    elif args.action == "enqueue":
        job = enqueue(config, args.task, args.requirements_file.read_text(), args.runtime, args.acceptance, args.allowed_path)
        print(json.dumps(public_jobs([job])[0], sort_keys=True))
    elif args.action == "resume":
        print(json.dumps(public_jobs([Store(config["state_dir"]).resume(args.job_id, retest=args.retest)])[0]))
    elif args.action == "prepare-environment":
        from .environment import prepare
        from .adapters import runtime_environment
        job = Store(config["state_dir"]).get(args.job_id)
        if config.get("environment") is None:
            raise ValueError("environment_profile_unconfigured")
        from .worker import group_exists
        if (not job or job["stage"] not in {"failed", "blocked"}
                or (job["pid"] and (process_matches(job["pid"], job["process_identity"]) or group_exists(job["pid"])))):
            raise ValueError("environment_repair_requires_stopped_job")
        prepare(job, config, None, runtime_environment())
        print(json.dumps({"status": "environment_ready", "job_id": job["id"]}))
    elif args.action == "reliability":
        from .reliability import report, sync, dispatch
        from .tracker import bd
        if args.sync or args.dispatch:
            path = Path(config["state_dir"]) / "reliability" / "sync-status.json"
            try:
                value = sync(config, bd)
                if args.dispatch or config.get("reliability_repair_config"):
                    value["repair"] = dispatch(config)
            except Exception:
                write_json(path, {"status": "tracker_unavailable", "updated_at": time.time()})
                raise RuntimeError("reliability_tracker_unavailable") from None
            write_json(path, {**value, "updated_at": time.time()})
        else:
            value = report(config)
        print(json.dumps(value, sort_keys=True, indent=2))
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
    elif args.action == "checkpoint-state":
        from .release import create_checkpoint
        inventory = config.get("release", {}).get("inventory")
        checkpoint = create_checkpoint(config["repo"], Path(config["state_dir"]) / "release/backups", inventory, config["base_branch"])
        print(json.dumps({"checkpoint_id": checkpoint.name, "status": "verified", "consistency": "per_resource"}))
    elif args.action == "restore-state-into":
        from .release import restore_into
        print(json.dumps(restore_into(args.checkpoint, args.destination)))
    elif args.action == "activate":
        from .release import activate
        matches = [job for job in Store(config["state_dir"]).jobs()
                   if job["stage"] == "integrated" and job["revision"] == args.revision
                   and job["reviewed_revision"] == args.revision and job["review_result"] == "approved"
                   and job["tests_passed"]]
        ancestor = command(["git", "merge-base", "--is-ancestor", args.revision, config["base_branch"]], cwd=config["repo"])
        if not matches or ancestor.returncode:
            raise ValueError("activation_requires_verified_integrated_revision")
        print(json.dumps(activate(config, args.revision)))
    elif args.action == "recover-release":
        from .release import recover
        print(json.dumps(recover(config)))
    elif args.action == "restore-code-into":
        print(json.dumps(restore_code_into(args.checkpoint, args.destination)))


if __name__ == "__main__":
    main()
