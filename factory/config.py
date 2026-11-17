"""Explicit project configuration and bounded, private operational records."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile


def command(argv, cwd=None, timeout=60):
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
    if config.get("integration_policy") not in {"documentation_only", "disabled"}:
        raise ValueError("unsupported_integration_policy")
    return config
