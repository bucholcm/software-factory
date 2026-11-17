"""BuchOS adapter: Beads stays authoritative; failed review reads block claims."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .config import command, git, write_json


def bd(config, *args, json_result=False):
    result = command([config.get("bd_executable", "bd"), *args, *( ["--json"] if json_result else [])],
                     cwd=config["repo"], timeout=30)
    if result.returncode:
        raise RuntimeError("tracker_operation_failed")
    return json.loads(result.stdout) if json_result else None


def reviews_clear(config):
    remote = git(config["repo"], "remote", "get-url", "origin")
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    if not match:
        raise RuntimeError("github_repository_unavailable")
    repository = "/".join(match.groups())
    result = command([config.get("gh_executable", "gh"), "pr", "list", "--repo", repository,
                      "--author", "@me", "--state", "open", "--limit", "100", "--json", "number"], timeout=30)
    if result.returncode:
        raise RuntimeError("github_reviews_unavailable")
    prs = json.loads(result.stdout)
    if len(prs) >= 100:
        raise RuntimeError("github_review_scan_incomplete")
    owner, name = match.groups()
    for pr in prs:
        cursor = None
        for _ in range(10):
            query = ("query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){"
                     "pullRequest(number:$number){reviewThreads(first:100,after:$cursor){nodes{isResolved}"
                     "pageInfo{hasNextPage endCursor}}}}}")
            argv = [config.get("gh_executable", "gh"), "api", "graphql", "-f", "query=" + query,
                    "-f", "owner=" + owner, "-f", "name=" + name, "-F", "number=" + str(pr["number"])]
            if cursor:
                argv += ["-f", "cursor=" + cursor]
            result = command(argv, timeout=30)
            if result.returncode:
                raise RuntimeError("github_reviews_unavailable")
            threads = json.loads(result.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]
            if any(not thread["isResolved"] for thread in threads["nodes"]):
                return False
            if not threads["pageInfo"]["hasNextPage"]:
                break
            cursor = threads["pageInfo"]["endCursor"]
        else:
            raise RuntimeError("github_review_scan_incomplete")
    return True


def reconcile(config, store, enqueue_function):
    """Only explicitly approved factory-pilot tasks enter this first rollout."""
    marker_path = Path(config["state_dir"]) / "tracker-sync.json"
    try:
        markers = json.loads(marker_path.read_text())
    except (OSError, ValueError):
        markers = {}
    for job in store.jobs():
        fingerprint = ":".join([job["stage"], job["revision"], job["reviewed_revision"], job["blocker"]])
        if markers.get(job["id"]) == fingerprint:
            continue
        details = bd(config, "show", job["task_id"], json_result=True)
        issue = details[0] if isinstance(details, list) else details
        if issue.get("metadata", {}).get("factory_project") != config["state_dir"]:
            continue
        args = ["update", job["task_id"], "--set-metadata", "factory_job_id=" + job["id"],
                "--set-metadata", "factory_stage=" + job["stage"],
                "--set-metadata", "head_revision=" + job["revision"],
                "--set-metadata", "reviewed_revision=" + job["reviewed_revision"],
                "--set-metadata", "review_result=" + job["review_result"],
                "--set-metadata", "reviewer=independent-codex-cli-session",
                "--set-metadata", "factory_blocker=" + job["blocker"]]
        bd(config, *args)
        if job["stage"] == "integrated":
            verified = command(["git", "merge-base", "--is-ancestor", job["revision"], config["base_branch"]], cwd=config["repo"])
            if verified.returncode or job["reviewed_revision"] != job["revision"]:
                raise RuntimeError("integrated_revision_unverified")
            if issue["status"] != "closed":
                bd(config, "close", job["task_id"], "--reason=Factory verified exact tested/reviewed revision " + job["revision"]
                   + " integrated locally after a verified code checkpoint. No application data changed.")
        markers[job["id"]] = fingerprint
        write_json(marker_path, markers)
    # Never claim new work while a factory task still needs attention.
    if any(job["stage"] != "integrated" for job in store.jobs()):
        return {"status": "active_jobs"}
    active = bd(config, "list", "--status=in_progress", json_result=True)
    existing_tasks = {job["task_id"] for job in store.jobs()}
    for issue in active:
        if (issue["id"] not in existing_tasks
                and issue.get("metadata", {}).get("factory_project") == config["state_dir"]):
            job = enqueue_function(config, issue["id"], issue.get("description", ""),
                                   acceptance=issue.get("acceptance_criteria", ""))
            return {"status": "intake_recovered", "task_id": job["task_id"]}
    if len(active) >= 2:
        return {"status": "at_global_capacity"}
    ready = bd(config, "ready", json_result=True)
    candidates = [issue for issue in ready if issue.get("status") == "open" and not issue.get("assignee")
                  and {"ready-for-development", "factory-pilot"}.issubset(set(issue.get("labels", [])))]
    if not candidates:
        return {"status": "no_approved_pilot_tasks"}
    if not reviews_clear(config):
        return {"status": "github_review_findings_pending"}
    issue = sorted(candidates, key=lambda value: (value.get("priority", 4), value["id"]))[0]
    bd(config, "update", issue["id"], "--claim", "--set-metadata", "factory_project=" + config["state_dir"])
    job = enqueue_function(config, issue["id"], issue.get("description", ""),
                           acceptance=issue.get("acceptance_criteria", ""))
    bd(config, "update", issue["id"], "--set-metadata", "factory_job_id=" + job["id"],
       "--set-metadata", "branch=" + job["branch"], "--set-metadata", "worktree=" + job["worktree"])
    return {"status": "task_claimed", "task_id": issue["id"]}
