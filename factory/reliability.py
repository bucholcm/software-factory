"""Durable project feedback. Only codes, counters and revisions leave a job.

Incidents create repair work, never mutate running factory code or auto-approve
a fix. The normal exact-revision tests/review/checkpoint boundary still applies.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .config import write_json
from .worker import sanitize_result


def observe(store, job, result, *, verified_delivery=False):
    result = sanitize_result(result)
    if result["status"] == "completed":
        # Recovery is evidence about this job/stage, not proof of a general fix.
        with store.connect() as db:
            now = time.time()
            db.execute("UPDATE reliability_affected_jobs SET recovered_at=? WHERE job_id=? AND (stage=? OR ?) AND recovered_at IS NULL",
                       (now, job["id"], job["stage"], verified_delivery))
            db.execute("""UPDATE reliability_incidents SET recovered_at=?
                WHERE signature IN (SELECT signature FROM reliability_affected_jobs WHERE job_id=? AND (stage=? OR ?))
                AND NOT EXISTS (SELECT 1 FROM reliability_affected_jobs AS affected
                    WHERE affected.signature=reliability_incidents.signature AND affected.recovered_at IS NULL)""",
                       (now, job["id"], job["stage"], verified_delivery))
        return
    if result.get("blocker") == "product_decision_required":
        return
    diagnostic = result.get("diagnostic", {})
    key = hashlib.sha256(json.dumps([job["stage"], result["blocker"], diagnostic], sort_keys=True).encode()).hexdigest()
    now = time.time()
    with store.connect() as db:
        inserted = db.execute("INSERT OR IGNORE INTO reliability_observations(job_id,attempt_dir) VALUES(?,?)",
                              (job["id"], job.get("attempt_dir", ""))).rowcount
        if not inserted:
            return
        db.execute("""INSERT INTO reliability_affected_jobs(signature,job_id,stage) VALUES(?,?,?)
            ON CONFLICT(signature,job_id) DO UPDATE SET recovered_at=NULL""", (key, job["id"], job["stage"]))
        db.execute("""INSERT INTO reliability_incidents
            (signature,stage,blocker,diagnostic,occurrences,first_seen,last_seen,last_job_id,revision)
            VALUES(?,?,?,?,1,?,?,?,?) ON CONFLICT(signature) DO UPDATE SET
            occurrences=occurrences+1,last_seen=excluded.last_seen,recovered_at=NULL,
            last_job_id=excluded.last_job_id,revision=excluded.revision""",
            (key, job["stage"], result["blocker"], json.dumps(diagnostic, sort_keys=True), now, now, job["id"], job.get("revision", "")))


def report(config):
    sources = [config["state_dir"], *config.get("reliability_sources", [])]
    incidents, unavailable = [], 0
    for source in sorted(set(str(Path(s).expanduser().resolve()) for s in sources)):
        path = Path(source) / "execution.sqlite3"
        try:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute("SELECT signature,stage,blocker,diagnostic,occurrences,first_seen,last_seen,recovered_at,revision FROM reliability_incidents").fetchall()
            valid = []
            for row in rows:
                item = dict(row)
                item["diagnostic"] = sanitize_result({"status": "failed", "diagnostic": json.loads(item["diagnostic"]) }).get("diagnostic", {})
                # Source databases are inputs; reject any unrecognized free text.
                if (item["stage"] not in {"implementing", "fixing", "testing", "reviewing"}
                        or not re.fullmatch(r"[a-z_]{1,64}", item["blocker"])
                        or not re.fullmatch(r"[0-9a-f]{64}", item["signature"])
                        or (item["revision"] and not re.fullmatch(r"[0-9a-f]{40}", item["revision"]))):
                    raise ValueError("invalid_reliability_record")
                if type(item["occurrences"]) is not int or not 1 <= item["occurrences"] <= 2**53:
                    raise ValueError("invalid_reliability_count")
                for field in ("first_seen", "last_seen", "recovered_at"):
                    value = item[field]
                    if field == "recovered_at" and value is None:
                        continue
                    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1e12:
                        raise ValueError("invalid_reliability_timestamp")
                item["project_key"] = hashlib.sha256(source.encode()).hexdigest()[:16]
                valid.append(item)
            incidents.extend(valid)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            unavailable += 1
    return {"incidents": incidents, "unavailable_sources": unavailable}


def sync(config, bd):
    target = config.get("reliability_tracker_repo")
    if not target:
        return {"status": "local_only", **report(config)}
    directory = Path(config["state_dir"]) / "reliability"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A CLI invocation and the supervisor may export concurrently.
    from .config import git
    common = Path(git(target, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    with (common / "factory-reliability-sync.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report_value = report(config)
        index_path = directory / "beads-index.json"
        try:
            index = json.loads(index_path.read_text())
        except (OSError, ValueError):
            index = {}
        target_config = {**config, "repo": target}
        exported = 0
        groups = {}
        for item in report_value["incidents"]:
            groups.setdefault(item["signature"], []).append(item)
        for key, items in sorted(groups.items()):
            summary = {"stage": items[0]["stage"], "blocker": items[0]["blocker"],
                       "diagnostic": items[0]["diagnostic"],
                       "occurrences": sum(i["occurrences"] for i in items),
                       "projects": len(items), "last_seen": max(i["last_seen"] for i in items),
                       "currently_recovered": all(i["recovered_at"] is not None for i in items)}
            fingerprint = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
            previous = index.get(key, {})
            if previous.get("fingerprint") == fingerprint:
                continue
            # Search even after a lost local index / crash after remote creation.
            found = bd(target_config, "search", key, json_result=True)
            matches = [i for i in found if i.get("metadata", {}).get("factory_reliability_key") == key]
            if matches:
                issue = matches[0]
                task_id = issue["id"]
                if issue.get("status") == "closed" and not summary["currently_recovered"]:
                    bd(target_config, "update", task_id, "--status=open")
            else:
                issue = bd(target_config, "create", "--type=bug", "--priority=1",
                           "--title=Factory reliability: " + summary["blocker"],
                           "--description=Repair the factory mechanism behind this sanitized runtime incident. "
                           "Reproduce in a disposable project, add a regression test, run full factory checks and independent exact-revision review. "
                           "Validate a candidate runtime against the affected project before controlled rollout. "
                           "Do not change application requirements, weaken gates or edit a running runtime. Reliability key: " + key,
                           "--labels=factory-reliability",
                           "--metadata=" + json.dumps({"factory_reliability_key": key}), json_result=True)
                task_id = issue["id"]
            bd(target_config, "update", task_id, "--set-metadata", "factory_reliability=" + json.dumps(summary, sort_keys=True))
            index[key] = {"task_id": task_id, "fingerprint": fingerprint}
            write_json(index_path, index)
            exported += 1
    return {"status": "synced", "updated": exported, "unavailable_sources": report_value["unavailable_sources"]}


def dispatch(config):
    """Send bounded repair work to a separate, explicitly configured factory.

    Its checkout, execution state and supervisor are separate from application
    projects. Source integration is disabled: delivery ends at exact-SHA review.
    """
    path = config.get("reliability_repair_config")
    if not path:
        return {"status": "repair_dispatch_unconfigured"}
    from .config import load
    from .cli import enqueue, status
    from .store import Store
    target = load(path)
    if (target.get("role") != "factory-repair" or target.get("task_policy") != "source"
            or target.get("integration_policy") != "disabled"
            or Path(target["state_dir"]).resolve() == Path(config["state_dir"]).resolve()
            or target.get("max_active_jobs", 1) != 1):
        raise ValueError("unsafe_reliability_repair_configuration")
    from .config import git
    factory_repo = config.get("reliability_factory_repo")
    if not factory_repo or not Path(factory_repo).is_absolute():
        raise ValueError("reliability_factory_repository_required")
    common = lambda repo: Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if common(target["repo"]) != common(factory_repo) or common(target["repo"]) == common(config["repo"]):
        raise ValueError("repair_requires_separate_factory_repository")
    allowed = target.get("reliability_repair_paths")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("reliability_repair_scope_required")
    from .scope import scopes_valid
    if not scopes_valid(allowed):
        raise ValueError("invalid_reliability_repair_scope")
    health = status(target)
    if health.get("status") != "running" or health.get("ready") is not True:
        return {"status": "repair_supervisor_not_ready"}
    store = Store(target["state_dir"])
    with (store.state_dir / "reliability-dispatch.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Ready branches still reserve their scope until a human resolves them.
        if any(j["stage"] != "integrated" for j in store.jobs()):
            return {"status": "repair_capacity_reserved"}
        for item in sorted(report(config)["incidents"], key=lambda x: x["last_seen"]):
            if item["recovered_at"] is not None:
                continue
            task_id = "factory-reliability-" + item["signature"][:24]
            if any(j["task_id"] == task_id for j in store.jobs()):
                continue  # No blind resume or duplicate models for the same incident.
            # Ordinary failing project tests are not themselves proof of a
            # factory defect. Escalate their repeated failure, not each red test.
            if item["blocker"] in {"quality_gate_failed", "security_gate_failed", "agent_reported_failure"} and item["occurrences"] < 3:
                continue
            evidence = {k: item[k] for k in ("stage", "blocker", "diagnostic", "occurrences", "signature")}
            job = enqueue(target, task_id,
                          "Improve software-factory reliability for this sanitized project incident: " + json.dumps(evidence, sort_keys=True) +
                          ". Inspect the factory mechanism first; reproduce with synthetic data. "
                          "If the cause is only a missing project dependency, improve preflight/bootstrap and safe recovery; "
                          "do not fabricate an application code defect. Do not modify any live service, installation, "
                          "private project configuration or application state. Do not suppress failing checks.",
                          acceptance="A synthetic regression reproduces the incident and passes with the fix. "
                          "Full factory checks and independent exact-revision review pass. "
                          "Deliver a reviewed branch; installation remains a separate controlled rollout.",
                          allowed_paths=allowed,
                          origin={'incident_signature': item['signature'], 'project_key': item['project_key']})
            return {"status": "repair_dispatched", "job_id": job["id"], "task_id": task_id}
    return {"status": "no_new_repair_work"}
