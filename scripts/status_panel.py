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


def render(snapshot, project, *, color=False):
    def tint(text, code):
        return f"\033[{code}m{text}\033[0m" if color else text

    lines = [tint("LOCAL SOFTWARE FACTORY", "1;36"), project, "─" * 64]
    healthy = snapshot.get("status") == "running" and snapshot.get("ready") is True
    health = "● RUNNING / READY" if healthy else f"● {snapshot.get('status', 'unknown').upper()}"
    lines += [tint(health, "1;32" if healthy else "1;33"), ""]
    task_policy = snapshot.get("task_policy", "documentation_only")
    integration = snapshot.get("integration_policy", "documentation_only")
    mode = ("SOURCE INTEGRATE" if integration == "reviewed_code" else "SOURCE REVIEW") if task_policy == "source" else "DOCUMENTATION"
    lines += [f"Mode         {mode}",
              f"Phase        {snapshot.get('phase', 'unknown')}",
              f"Workers      {snapshot.get('active', 0)} active",
              "Activation   explicit release command", ""]
    updated = snapshot.get("updated_at")
    if isinstance(updated, (int, float)):
        lines += [f"Heartbeat    {max(0, int(time.time() - updated))}s ago", ""]
    lines += [tint("JOBS", "1;37")]
    jobs = snapshot.get("jobs", [])
    if not jobs:
        lines += ["No jobs submitted. Ready for a project ticket."]
    for job in jobs[-6:]:
        lines += [f"{job['task_id']}  ·  {job['stage']}",
                  f"  Tests: {'passed' if job.get('tests_passed') else 'pending'}  |  Review: {job.get('review_result') or 'pending'}"]
        if job.get("blocker"):
            lines += [tint("  Blocker: " + job["blocker"], "33")]
    lines += ["", tint("PIPELINE", "1;37"),
              "Implement → Test → Review → Fix → Checkpoint → Integrate",
              "", "─" * 64,
              "Codex       $factory TICKET  /  $factory status",
              "OpenCode    /factory TICKET  /  /factory status", "",
              datetime.now().strftime("%H:%M:%S") + "  ·  Ctrl+C closes this view, not the supervisor"]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    try:
        while True:
            config = load(args.config)
            snapshot = status(config)
            color = sys.stdout.isatty() and not args.plain
            if color and not args.once:
                print("\033[2J\033[H", end="")
            print(render(snapshot, Path(config["repo"]).name, color=color), flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
