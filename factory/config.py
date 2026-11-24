"""Explicit project configuration and bounded, private operational records."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def command(argv, cwd=None, timeout=60):
    if argv and argv[0] == "git":
        # Factory-owned Git operations must not execute repository hooks on the host.
        argv = ["git", "-c", "core.hooksPath=" + os.devnull, *argv[1:]]
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def git(repo, *args):
    result = command(["git", "-C", str(repo), *args])
    if result.returncode:
        raise RuntimeError("git_operation_failed")
    return result.stdout.strip()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    config = json.loads(Path(path).read_text())
    for field in ("repo", "state_dir"):
        config[field] = str(Path(config[field]).expanduser().resolve())
    if config.get("runtime") not in {"codex", "opencode"}:
        raise ValueError("invalid_runtime")
    if config.get("opencode_implementation_sandbox") is not None:
        # Legacy verified flags did not establish an enforced sandbox.
        raise ValueError("invalid_opencode_implementation_sandbox")
    config.setdefault("task_policy", "documentation_only")
    if config["task_policy"] not in {"documentation_only", "source"}:
        raise ValueError("unsupported_task_policy")
    if config.get("integration_policy") not in {"documentation_only", "reviewed_code", "disabled"}:
        raise ValueError("unsupported_integration_policy")
    if config["integration_policy"] == "reviewed_code" and config["task_policy"] != "source":
        raise ValueError("source_task_policy_required")
    if "environment" in config:
        from .environment import validate_profile
        validate_profile(config["environment"])
    sources = config.get("reliability_sources", [])
    paths = [config[k] for k in ("reliability_tracker_repo", "reliability_repair_config", "reliability_factory_repo") if k in config]
    if (not isinstance(sources, list) or any(not isinstance(s, str) or not Path(s).is_absolute() for s in sources)
            or any(not isinstance(s, str) or not Path(s).is_absolute() for s in paths)):
        raise ValueError("invalid_reliability_configuration")
    interval = config.get("reliability_interval_seconds", 60)
    if type(interval) not in {int, float} or not 30 <= interval <= 86400:
        raise ValueError("invalid_reliability_interval")
    from .pricing import freeze_catalog, estimate_cost
    freeze_catalog(config)
    assumptions = config.get('usage_price_assumptions', {})
    if not isinstance(assumptions, dict) or set(assumptions) - {'codex', 'opencode'}:
        raise ValueError('invalid_usage_price_assumptions')
    for assumption in assumptions.values():
        if not isinstance(assumption, dict) or set(assumption) != {'provider', 'model'}:
            raise ValueError('invalid_usage_price_assumptions')
        estimate_cost([], None, assumption)
    for field in ('codex_model', 'opencode_model'):
        value = config.get(field)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,159}', value)
                                  or value.startswith(('sk-', 'ghp_', 'github_pat_')) or '..' in value.split('/')):
            raise ValueError('invalid_runtime_model')
    return config
