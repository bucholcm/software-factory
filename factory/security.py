"""Exact-candidate receipts for explicitly configured security commands."""
import hashlib
import json
from pathlib import Path
import time


def policy(config):
    commands = config.get("security_gates")
    if (not isinstance(commands, list) or not commands or any(
            not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg for arg in argv)
            for argv in commands)):
        raise RuntimeError("security_gates_unconfigured")
    return hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest()


def receipt_path(config, job):
    return Path(config["state_dir"]) / "security" / (job["id"] + ".json")


def verified(config, job):
    expected = policy(config)
    try:
        value = json.loads(receipt_path(config, job).read_text())
        age = time.time() - value["created_at"]
        return (value["revision"] == job["revision"] and value["policy"] == expected
                and value["passed"] is True and 0 <= age <= config.get("security_max_age_seconds", 3600))
    except (OSError, ValueError, KeyError, TypeError):
        return False
