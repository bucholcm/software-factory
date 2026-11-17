"""Serialized, exact-revision integration for the documentation pilot."""
from __future__ import annotations

import json
from pathlib import Path
import time

from .adapters import is_documentation_change
from .config import command, git, write_json
from .recovery import code_checkpoint


def integrate_ready(supervisor, config):
    for job in supervisor.store.jobs():
        if job["stage"] != "ready":
            continue
        if not config.get("auto_merge") or config["integration_policy"] == "disabled":
            continue
        # Broader activation requires the mutable-state recovery milestone.
        if not is_documentation_change(job["worktree"], job["payload"]["base_revision"]):
            supervisor.store.update(job["id"], blocker="state_recovery_gate_pending")
            continue
        repo = config["repo"]
        journal_path = Path(config["state_dir"]) / "integrations" / (job["id"] + ".json")
        if journal_path.exists():
            previous = json.loads(journal_path.read_text())
            if (previous.get("revision") == job["revision"]
                    and previous.get("stage") in {"checkpoint_ready", "integrated"}
                    and git(repo, "rev-parse", config["base_branch"]) == job["revision"]):
                previous["stage"] = "integrated"
                write_json(journal_path, previous)
                supervisor.store.update(job["id"], stage="integrated", blocker="")
                supervisor.store.event(job["id"], "integrated", "integration_recovered")
                continue
        if (git(repo, "rev-parse", "HEAD") != git(repo, "rev-parse", config["base_branch"])
                or git(repo, "symbolic-ref", "--short", "HEAD") != config["base_branch"]
                or git(repo, "diff", "--name-only") or git(repo, "diff", "--cached", "--name-only")):
            supervisor.store.update(job["id"], blocker="main_checkout_busy")
            continue
        if git(job["worktree"], "rev-parse", "HEAD") != job["reviewed_revision"] or not job["tests_passed"]:
            supervisor.store.update(job["id"], stage="testing", reviewed_revision="", review_result="",
                                    tests_passed=False, blocker="", attempt=0)
            continue
        base = git(repo, "rev-parse", config["base_branch"])
        ancestor = command(["git", "merge-base", "--is-ancestor", base, job["revision"]], cwd=repo)
        if ancestor.returncode:
            rebased = command(["git", "rebase", config["base_branch"]], cwd=job["worktree"], timeout=120)
            if rebased.returncode:
                command(["git", "rebase", "--abort"], cwd=job["worktree"])
                supervisor.store.update(job["id"], stage="blocked", resume_stage="implementing", blocker="rebase_conflict")
            else:
                payload = dict(job["payload"], base_revision=base)
                supervisor.store.update(job["id"], stage="testing", revision=git(job["worktree"], "rev-parse", "HEAD"),
                                        reviewed_revision="", review_result="", tests_passed=False, attempt=0, blocker="",
                                        payload=payload)
            continue
        checkpoint = code_checkpoint(repo, Path(config["state_dir"]) / "backups", config["base_branch"])
        journal = {"job_id": job["id"], "base_revision": base, "revision": job["revision"],
                   "checkpoint_id": checkpoint.name, "stage": "checkpoint_ready", "created_at": time.time()}
        write_json(journal_path, journal)
        # Refuse a concurrent base change after checkpoint creation.
        if git(repo, "rev-parse", config["base_branch"]) != base:
            continue
        if command(["git", "merge", "--ff-only", job["revision"]], cwd=repo).returncode:
            supervisor.store.update(job["id"], blocker="integration_failed")
            continue
        if git(repo, "rev-parse", "HEAD") != job["revision"]:
            raise RuntimeError("integration_revision_mismatch")
        journal["stage"] = "integrated"
        write_json(journal_path, journal)
        supervisor.store.update(job["id"], stage="integrated", blocker="")
        supervisor.store.event(job["id"], "integrated", "exact_revision_integrated")
