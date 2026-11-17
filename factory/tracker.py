"""BuchOS adapter: Beads stays authoritative; failed review reads block claims."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .config import command, git, write_json
from .adapters import documentation_path


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


def issue_by_id(config, task_id):
    value = bd(config, "show", task_id, json_result=True)
    return value[0] if isinstance(value, list) else value


def implementation_tasks(config, items):
    excluded = set(config.get("non_implementation_issue_ids", []))
    return [item for item in items if item["id"] not in excluded
            and item.get("issue_type") not in {"epic", "decision", "tracking"}
            and not {"agent-review", "manual-e2e", "tracking"}.intersection(item.get("labels", []))]


def sync_product_decision(config, store, job, issue):
    if job["stage"] != "blocked" or job["blocker"] != "product_decision_required":
        return job
    payload = dict(job["payload"])
    decision_id = payload.get("decision_id") or issue.get("metadata", {}).get("factory_decision_id")
    if not decision_id:
        question = json.loads(Path(payload["question_path"]).read_text())["question"]
        if not isinstance(question, str) or not 10 <= len(question) <= 2000:
            raise RuntimeError("invalid_product_question")
        # Search by immutable job identity to recover a crash after creation.
        matches = bd(config, "search", job["id"], json_result=True)
        decisions = [item for item in matches if item.get("issue_type") == "decision"
                     and item.get("metadata", {}).get("factory_job_id") == job["id"]
                     and item["id"] not in payload.get("answered_decision_ids", [])]
        if decisions:
            decision_id = decisions[0]["id"]
        else:
            created = bd(config, "create", "--type=decision", "--priority=1",
                         "--title=Product decision for " + job["task_id"],
                         "--description=" + question + "\nFactory job: " + job["id"],
                         "--labels=needs-product-decision,factory-question",
                         "--metadata=" + json.dumps({"factory_job_id": job["id"], "factory_project": config["state_dir"]}),
                         json_result=True)
            decision_id = created["id"]
        payload["decision_id"] = decision_id
        job = store.update(job["id"], payload=payload)
    dependencies = issue.get("dependencies", [])
    if not any((item.get("id") or item.get("depends_on_id")) == decision_id for item in dependencies):
        bd(config, "dep", "add", job["task_id"], decision_id)
    bd(config, "update", job["task_id"], "--set-metadata", "factory_decision_id=" + decision_id, "--status=open")
    decision = issue_by_id(config, decision_id)
    answer = decision.get("close_reason", "").strip()
    if decision.get("status") != "closed" or not answer or answer.lower() in {"closed", "completed", "done"}:
        return job
    # Other native blockers still apply after this one decision is answered.
    issue = issue_by_id(config, job["task_id"])
    for dependency in issue.get("dependencies", []):
        if dependency.get("dependency_type", dependency.get("type")) == "blocks":
            blocker = issue_by_id(config, dependency.get("id") or dependency.get("depends_on_id"))
            if blocker["status"] != "closed":
                return job
    if len(implementation_tasks(config, bd(config, "list", "--status=in_progress", json_result=True))) >= 2:
        return job
    payload["requirements"] += "\nAccepted product decision: " + answer
    payload["answered_decision_ids"] = [*payload.get("answered_decision_ids", []), decision_id]
    payload.pop("decision_id", None)
    store.update(job["id"], payload=payload)
    bd(config, "update", job["task_id"], "--claim", "--unset-metadata", "factory_decision_id")
    return store.resume(job["id"])


def reconcile(config, store, enqueue_function):
    """Only explicitly approved factory-pilot tasks enter this first rollout."""
    if config.get("maintenance_tracker_enabled", True):
        report_path = Path(config["state_dir"]) / "maintenance" / "maintenance.json"
        if report_path.exists():
            from .maintenance_tracker import sync_findings
            sync_findings(config, bd)
    marker_path = Path(config["state_dir"]) / "tracker-sync.json"
    try:
        markers = json.loads(marker_path.read_text())
    except (OSError, ValueError):
        markers = {}
    for job in store.jobs():
        fingerprint = ":".join([job["stage"], job["revision"], job["reviewed_revision"], job["blocker"]])
        if markers.get(job["id"]) == fingerprint and not (job["stage"] == "blocked" and job["blocker"] == "product_decision_required"):
            continue
        issue = issue_by_id(config, job["task_id"])
        if issue.get("metadata", {}).get("factory_project") != config["state_dir"]:
            continue
        job = sync_product_decision(config, store, job, issue)
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
    active = bd(config, "list", "--status=in_progress", json_result=True)
    existing_tasks = {job["task_id"] for job in store.jobs()}
    for issue in active:
        if (issue["id"] not in existing_tasks
                and issue.get("metadata", {}).get("factory_project") == config["state_dir"]):
            job = enqueue_function(config, issue["id"], issue.get("description", ""),
                                   acceptance=issue.get("acceptance_criteria", ""),
                                   allowed_paths=issue.get("metadata", {}).get("factory_paths", []))
            return {"status": "intake_recovered", "task_id": job["task_id"]}
    implementation_active = implementation_tasks(config, active)
    if len(implementation_active) >= 2:
        return {"status": "at_global_capacity"}
    ready = bd(config, "ready", json_result=True)
    candidates = [issue for issue in ready if issue.get("status") == "open" and not issue.get("assignee")
                  and {"ready-for-development", "factory-pilot"}.issubset(set(issue.get("labels", [])))]
    if not candidates:
        return {"status": "no_approved_pilot_tasks"}
    pending = [job for job in store.jobs() if job["stage"] != "integrated"]
    assessed = []
    for candidate in candidates:
        paths = candidate.get("metadata", {}).get("factory_paths", [])
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and documentation_path(path) for path in paths):
            continue
        if any(not job["payload"].get("allowed_paths")
               or any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
                      for a in paths for b in job["payload"]["allowed_paths"]) for job in pending):
            continue
        assessed.append(candidate)
    if not assessed:
        return {"status": "pilot_scope_or_independence_required"}
    if not reviews_clear(config):
        return {"status": "github_review_findings_pending"}
    issue = sorted(assessed, key=lambda value: (value.get("priority", 4), value["id"]))[0]
    bd(config, "update", issue["id"], "--claim", "--set-metadata", "factory_project=" + config["state_dir"])
    job = enqueue_function(config, issue["id"], issue.get("description", ""),
                           acceptance=issue.get("acceptance_criteria", ""),
                           allowed_paths=issue["metadata"]["factory_paths"])
    bd(config, "update", issue["id"], "--set-metadata", "factory_job_id=" + job["id"],
       "--set-metadata", "branch=" + job["branch"], "--set-metadata", "worktree=" + job["worktree"])
    return {"status": "task_claimed", "task_id": issue["id"]}
