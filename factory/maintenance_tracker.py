"""Deduplicate sanitized maintenance checks into Beads triage work."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import git, write_json
from .maintenance import load_report


def sync_findings(config, bd):
    directory = Path(config["state_dir"]) / "maintenance"
    report = load_report(directory, revision=git(config["repo"], "rev-parse", "HEAD"))
    if report.get("status") in {"stale", "error"} and not report.get("checks"):
        return
    if report.get("stale") or report.get("status") == "stale":
        return
    index_path = directory / "beads-index.json"
    try:
        index = json.loads(index_path.read_text())
    except (OSError, ValueError):
        index = {}
    groups = {}
    for check in report.get("checks", []):
        if check["status"] not in {"findings", "error", "unavailable"}:
            continue
        group = groups.setdefault((check["category"], check["tool"]), {"statuses": set(), "errors": set(), "findings": set()})
        group["statuses"].add(check["status"])
        if check.get("error_code"):
            group["errors"].add(check["error_code"])
        for item in check.get("findings", []):
            group["findings"].add((item.get("package", ""), item.get("id", "")))
    for (category, tool), group in sorted(groups.items()):
        key = hashlib.sha256((category + ":" + tool).encode()).hexdigest()
        details = {"category": category, "tool": tool, "statuses": sorted(group["statuses"]),
                   "errors": sorted(group["errors"]), "findings": sorted(group["findings"]),
                   "revision": report["revision"]}
        fingerprint = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
        previous = index.get(key, {})
        if previous.get("fingerprint") == fingerprint:
            continue
        task_id = previous.get("task_id")
        if not task_id:
            existing = bd(config, "search", key, json_result=True)
            matching = [item for item in existing if item.get("metadata", {}).get("factory_maintenance_key") == key]
            if matching:
                task_id = matching[0]["id"]
            else:
                created = bd(config, "create", "--type=chore", "--priority=2",
                             "--title=Factory maintenance: " + category + " (" + tool + ")",
                             "--description=Investigate the latest sanitized read-only maintenance report. "
                             "Findings require technical triage, not automatic assumptions of exploitability. "
                             "Use the factory-maintenance skill; prepare bounded fixes through tests and independent review. "
                             "Do not suppress scanners to pass checks. Factory maintenance key: " + key,
                             "--labels=maintenance,security-triage",
                             "--metadata=" + json.dumps({"factory_maintenance_key": key}), json_result=True)
                task_id = created["id"]
        summary = {**details, "findings": details["findings"][:40], "finding_count": len(details["findings"])}
        bd(config, "update", task_id, "--set-metadata", "factory_scan=" + json.dumps(summary, sort_keys=True))
        index[key] = {"task_id": task_id, "fingerprint": fingerprint}
        write_json(index_path, index)
