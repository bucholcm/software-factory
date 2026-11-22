"""Read-only progress from verified gates, journal timestamps and live workers."""
from __future__ import annotations

import math


ACTIVE = frozenset({"implementing", "testing", "reviewing", "fixing"})
WORKING = frozenset({"working", "working_supervisor_unavailable", "orphaned_worker", "orphaned_worker_supervisor_unavailable"})


def timestamp(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value > 0 else None


def project(job, snapshot, timeline, *, now, worker_alive=False, group_alive=False):
    stage = job["stage"]
    healthy = snapshot.get("status") == "running" and snapshot.get("ready") is True
    next_run = timestamp(job.get("next_run"))
    process_alive = worker_alive or group_alive
    if stage in ACTIVE:
        if worker_alive:
            activity = "working" if healthy else "working_supervisor_unavailable"
        elif group_alive:
            activity = "orphaned_worker" if healthy else "orphaned_worker_supervisor_unavailable"
        elif not healthy:
            activity = "supervisor_unavailable"
        elif next_run and next_run > now:
            activity = "retry_wait"
        elif job.get("attempt_dir"):
            activity = "reconciling"
        else:
            activity = "queued"
    elif stage in {"blocked", "failed"}:
        activity = stage + ("_worker_alive" if process_alive else "")
    elif stage == "integrated":
        activity = "completed"
    elif stage == "ready":
        activity = "ready_for_delivery"
    else:
        activity = "unknown"

    effective_stage = job.get("resume_stage") if stage in {"blocked", "failed"} else stage
    implemented = bool(job.get("revision")) and effective_stage not in {"implementing", "fixing"}
    tested = implemented and job.get("tests_passed") is True
    reviewed = tested and job.get("review_result") == "approved" and job.get("reviewed_revision") == job.get("revision")
    milestones = []
    for name, complete, current in (("implementation", implemented, effective_stage in {"implementing", "fixing"}),
                                    ("tests", tested, effective_stage == "testing"),
                                    ("review", reviewed, effective_stage == "reviewing")):
        milestones.append({"name": name, "state": "passed" if complete else "current" if current else "pending"})
    if stage == "ready" and not reviewed:
        activity = "awaiting_verification"

    start = timestamp(job.get("created_at")) or timestamp(timeline.get("first_started_at"))
    # Repeated ready-state policy updates must not keep increasing delivery time.
    end = None
    if reviewed and stage in {"ready", "integrated"}:
        end = timestamp(timeline.get("integrated_at" if stage == "integrated" else "delivered_at")) or timestamp(job.get("updated_at"))
    attempt_start = timestamp(job.get("started_at"))
    return {
        "activity": activity,
        "worker_alive": bool(worker_alive),
        "owned_group_alive": bool(group_alive),
        "elapsed_seconds": max(0, int((end or now) - start)) if start else None,
        "elapsed_includes_waiting": True,
        "attempt_elapsed_seconds": max(0, int(now - attempt_start)) if attempt_start and process_alive else None,
        "retry_in_seconds": max(0, math.ceil(next_run - now)) if activity == "retry_wait" else None,
        "attempts_started": timeline.get("attempts_started", 0),
        "fix_cycles": job.get("fix_count", 0),
        "verified_steps": sum(item["state"] == "passed" for item in milestones),
        "total_steps": len(milestones),
        "milestones": milestones,
        "tokens": None,
        "token_status": "not_collected",
    }


def timelines(store):
    with store.connect() as db:
        rows = db.execute("""SELECT job_id,
            MIN(CASE WHEN code='worker_started' THEN created_at END) AS first_started_at,
            MAX(CASE WHEN stage='ready' AND code='stage_advanced' THEN created_at END) AS delivered_at,
            MAX(CASE WHEN stage='integrated' AND code IN ('exact_revision_integrated','integration_recovered') THEN created_at END) AS integrated_at,
            SUM(CASE WHEN code='worker_started' THEN 1 ELSE 0 END) AS attempts_started
            FROM events GROUP BY job_id""").fetchall()
    return {row["job_id"]: dict(row) for row in rows}


def summary(jobs):
    activities = [job["progress"]["activity"] for job in jobs]
    working = sum(state in WORKING for state in activities)
    stalled = sum(state.startswith(("blocked", "failed")) for state in activities)
    waiting = sum(state in {"queued", "retry_wait", "reconciling", "supervisor_unavailable", "awaiting_verification"} for state in activities)
    ready = activities.count("ready_for_delivery")
    state = "working" if working else "waiting" if waiting else "stalled" if stalled else "ready" if ready else "idle"
    return {"state": state, "working": working, "waiting": waiting, "stalled": stalled, "ready": ready}
