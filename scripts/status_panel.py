"""Compact, read-only presentation of factory status for a terminal pane.

Run from the product checkout: python -m scripts.status_panel --config CONFIG.
The status API inspects the supervisor and its execution journal; this view
does not submit tasks or display requirements, agent output or application data.
"""
import argparse
from datetime import datetime
from pathlib import Path
import sys
import time

from factory.cli import status
from factory.config import load
from factory.progress import summary


def duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s" if hours else f"{minutes:02d}m {seconds:02d}s"


def combine(projects):
    """Merge read-only views; one active repair must remain visible next to a blocked project."""
    if len(projects) == 1:
        name, snapshot = projects[0]
        return snapshot, name
    jobs, services = [], []
    for name, snapshot in projects:
        jobs.extend({**job, "project": name} for job in snapshot.get("jobs", []))
        services.append({"name": name, "status": snapshot.get("status", "unknown"),
                         "ready": snapshot.get("ready") is True})
    healthy = all(s["status"] == "running" and s["ready"] for s in services)
    heartbeats = [snapshot.get("updated_at") for _, snapshot in projects]
    value = {"status": "running" if healthy else "degraded", "ready": healthy,
             "phase": f"monitoring {len(projects)} services", "jobs": jobs,
             "activity": summary(jobs), "projects": services}
    if all(type(t) in {int, float} for t in heartbeats):
        value["updated_at"] = min(heartbeats)
    return value, " + ".join(name for name, _ in projects)


def render(snapshot, project, *, color=False, now=None):
    now = time.time() if now is None else now
    def tint(text, code):
        return f"\033[{code}m{text}\033[0m" if color else text

    lines = [tint("LOCAL SOFTWARE FACTORY", "1;36"), project, "─" * 64]
    healthy = snapshot.get("status") == "running" and snapshot.get("ready") is True
    health = "● RUNNING / READY" if healthy else f"● {snapshot.get('status', 'unknown').upper()}"
    lines += [tint(health, "1;32" if healthy else "1;33"), ""]
    activity = snapshot.get("activity", {})
    state = activity.get("state", "unknown")
    marker = "|/-\\"[int(now) % 4]
    labels = {"working": f"{marker} WORK IN PROGRESS · {activity.get('working', 0)} live worker(s)",
              "waiting": "WAITING / RECONCILING", "stalled": "ACTION NEEDED · blocked or failed work",
              "ready": "DELIVERY READY · reviewed branch", "idle": "IDLE · no work running"}
    lines += [tint(labels.get(state, "Activity unavailable"), "1;36" if state == "working" else "1;33"), ""]
    task_policy = snapshot.get("task_policy", "documentation_only")
    integration = snapshot.get("integration_policy", "documentation_only")
    mode = ("SOURCE INTEGRATE" if integration == "reviewed_code" else "SOURCE REVIEW") if task_policy == "source" else "DOCUMENTATION"
    if snapshot.get("projects"):
        mode = "PER SERVICE"
        lines += [f"Service      {s['name']}: {s['status']} / {'ready' if s['ready'] else 'not ready'}" for s in snapshot["projects"]]
    lines += [f"Mode         {mode}",
              f"Coordinator  {snapshot.get('phase', 'unknown')}",
              f"Workers      {sum(bool(job.get('progress', {}).get('worker_alive') or job.get('progress', {}).get('owned_group_alive')) for job in snapshot.get('jobs', []))} verified live" if activity else f"Workers      {snapshot.get('active', 0)} reported",
              "Activation   explicit release command", ""]
    updated = snapshot.get("updated_at")
    if isinstance(updated, (int, float)):
        lines += [f"Heartbeat    {max(0, int(now - updated))}s ago", ""]
    jobs = snapshot.get("jobs", [])
    lines += [tint(f"JOBS ({min(len(jobs), 4)}/{len(jobs)} shown)", "1;37")]
    if not jobs:
        lines += ["No jobs submitted. Ready for a project ticket."]
    def priority(job):
        activity = job.get("progress", {}).get("activity", "")
        return (0 if activity.startswith(("working", "orphaned_worker")) else 1 if activity in {"queued", "retry_wait", "reconciling", "supervisor_unavailable"}
                else 2 if activity.startswith(("blocked", "failed")) else 3 if activity == "ready_for_delivery" else 4)
    for job in sorted(reversed(jobs), key=priority)[:4]:
        progress = job.get("progress", {})
        gate_states = {item["name"]: item["state"] for item in progress.get("milestones", [])}
        tests_label = "passed" if gate_states.get("tests") == "passed" else "pending"
        review_label = "approved" if gate_states.get("review") == "passed" else "pending"
        if review_label != "approved" and job.get("review_result"):
            if job.get("reviewed_revision") != job.get("revision"):
                review_label = "stale"
            elif job["review_result"] == "changes_requested":
                review_label = "changes requested"
        lines += [f"{job['task_id']}  ·  {job['stage']}",
                  f"  {job.get('project', project)} · {progress.get('activity', 'activity unknown')} · {job.get('runtime', 'unknown')} · attempt {job.get('attempt', 0)}",
                  f"  Attempt: {duration(progress.get('attempt_elapsed_seconds'))} | Total incl. waits: {duration(progress.get('elapsed_seconds'))}",
                  f"  Tests: {tests_label}  |  Review: {review_label}"]
        milestones = progress.get("milestones", [])
        if milestones:
            marks = {"passed": "✓", "current": "▶", "pending": "·"}
            trail = " → ".join(marks[item["state"]] + " " + item["name"] for item in milestones)
            lines += [f"  [{progress['verified_steps']}/{progress['total_steps']} verified] {trail}",
                      f"  Fix cycles: {progress.get('fix_cycles', 0)} | Attempts started: {progress.get('attempts_started', 0)}"]
        if progress.get("retry_in_seconds") is not None:
            lines += [f"  Retry in: {duration(progress['retry_in_seconds'])}"]
        lines += ["  Tokens: not collected", ""]
        if job.get("blocker"):
            lines += [tint("  Blocker: " + job["blocker"], "33"), ""]
        if progress.get("activity", "").startswith("orphaned_worker"):
            lines += [tint("  Wrapper is gone; its owned process group is still alive.", "33")]
        if progress.get("activity", "").endswith("supervisor_unavailable") and (progress.get("worker_alive") or progress.get("owned_group_alive")):
            lines += [tint("  Worker is alive; supervisor heartbeat is unavailable.", "33")]
    lines += ["", tint("PIPELINE", "1;37"),
              "Implement → Test → Review → Fix → Checkpoint → Integrate",
              "", "─" * 64,
              "Codex       $factory TICKET  /  $factory status",
              "OpenCode    /factory TICKET  /  /factory status", "",
              datetime.fromtimestamp(now).strftime("%H:%M:%S") + "  ·  Ctrl+C closes this view, not the supervisor"]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, action="append", help="Repeat to monitor related project/repair services together")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    try:
        while True:
            projects = []
            for path in dict.fromkeys(args.config):
                config = load(path)
                name = config.get("role") or Path(config["repo"]).name
                projects.append((name, status(config)))
            snapshot, name = combine(projects)
            color = sys.stdout.isatty() and not args.plain
            if color and not args.once:
                print("\033[2J\033[H", end="")
            print(render(snapshot, name, color=color), flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
