"""Explicit, repeatable project bootstrap outside the model's network sandbox.

Only configured commands run. No application state or raw command output is
retained. Missing prerequisites block work before spending a model attempt.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .config import command, git, write_json
from .scope import verify_paths


class ExecutionFailure(RuntimeError):
    def __init__(self, code, *, phase, command_index=None, exit_code=None):
        super().__init__(code)
        self.diagnostic = {"phase": phase}
        if command_index is not None:
            self.diagnostic["command_index"] = command_index
        if exit_code is not None:
            self.diagnostic["exit_code"] = exit_code


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) - {
        "python", "minimum_python", "venv", "required_executables",
        "bootstrap_commands", "checks", "fingerprint_files", "timeout_seconds",
    }:
        raise ValueError("invalid_environment_profile")
    if profile.get("python") and (not isinstance(profile["python"], str) or not Path(profile["python"]).is_absolute()):
        raise ValueError("environment_python_absolute_required")
    minimum = profile.get("minimum_python", [3, 11])
    if not isinstance(minimum, list) or len(minimum) != 2 or any(type(n) is not int or n < 0 for n in minimum):
        raise ValueError("invalid_environment_python_version")
    venv = profile.get("venv", ".venv")
    # Keep dependency directories outside task source scopes, and never target
    # a tracked source directory as a virtual environment.
    if not isinstance(venv, str) or not re.fullmatch(r"\.venv(?:-[A-Za-z0-9_-]+)?", venv):
        raise ValueError("invalid_environment_venv")
    for key in ("bootstrap_commands", "checks"):
        commands = profile.get(key, [])
        if not isinstance(commands, list) or any(
            not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv)
            for argv in commands
        ):
            raise ValueError("invalid_environment_commands")
    if profile.get("bootstrap_commands") and not profile.get("checks"):
        raise ValueError("environment_checks_required")
    if profile.get("bootstrap_commands") and not profile.get("fingerprint_files"):
        raise ValueError("environment_manifests_required")
    for key in ("required_executables", "fingerprint_files"):
        if not isinstance(profile.get(key, []), list) or any(not isinstance(x, str) or not x for x in profile.get(key, [])):
            raise ValueError("invalid_environment_inventory")
    if any(not re.fullmatch(r"[A-Za-z0-9_.+-]+", x) for x in profile.get("required_executables", [])):
        raise ValueError("invalid_environment_executable")
    timeout = profile.get("timeout_seconds", 900)
    if type(timeout) not in {int, float} or not 1 <= timeout <= 3600:
        raise ValueError("invalid_environment_timeout")


def _run(argv, repo, env, profile, phase, index):
    try:
        result = subprocess.run(argv, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=profile.get("timeout_seconds", 900))
    except FileNotFoundError:
        raise ExecutionFailure("environment_executable_missing", phase=phase, command_index=index) from None
    except subprocess.TimeoutExpired:
        raise ExecutionFailure("environment_command_timeout", phase=phase, command_index=index) from None
    except OSError:
        raise ExecutionFailure("environment_command_unavailable", phase=phase, command_index=index) from None
    return result.returncode


def source_fingerprint(repo):
    changed = git(repo, "diff", "--name-only", "HEAD").splitlines()
    new = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    new = [name for name in new if name]
    verify_paths(repo, changed + new)
    digest = hashlib.sha256(git(repo, "diff", "--binary", "HEAD").encode())
    digest.update(git(repo, "diff", "--cached", "--binary", "HEAD").encode())
    digest.update(git(repo, "rev-parse", "HEAD").encode())
    digest.update(git(repo, "rev-parse", "--abbrev-ref", "HEAD").encode())
    for name in sorted(new):
        digest.update(name.encode())
        digest.update((Path(repo) / name).read_bytes())
    return digest.hexdigest()


def prepare(job, config, attempt_path, env):
    profile = config.get("environment")
    if profile is None:
        return env
    validate_profile(profile)
    repo = Path(job["worktree"]).resolve()
    # One lock/receipt per worktree; survives attempts and supervisor restarts.
    key = hashlib.sha256(str(repo).encode()).hexdigest()
    directory = Path(config["state_dir"]) / "environments" / key
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (directory / "prepare.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for index, name in enumerate(profile.get("required_executables", [])):
            if not shutil.which(name, path=env.get("PATH", os.defpath)):
                raise ExecutionFailure("environment_executable_missing", phase="preflight", command_index=index)
        venv = repo / profile.get("venv", ".venv")
        if venv.is_symlink() or (venv.exists() and not venv.is_dir()):
            raise ExecutionFailure("environment_venv_unsafe", phase="preflight")
        before = source_fingerprint(repo)
        def execute(argv, phase, index):
            try:
                return _run(argv, repo, env, profile, phase, index)
            finally:
                if source_fingerprint(repo) != before:
                    raise ExecutionFailure("environment_modified_source", phase=phase)
        # Gitignored dependency artifacts must never enter a source commit.
        if git(repo, "ls-files", "--", venv.name):
            raise ExecutionFailure("environment_venv_tracked", phase="preflight")
        python = profile.get("python")
        if (python or venv.exists()) and command(["git", "check-ignore", "-q", "--", venv.name + "/.factory-environment"], cwd=repo).returncode:
            raise ExecutionFailure("environment_venv_not_ignored", phase="preflight")
        if python and not venv.exists():
            if execute([python, "-m", "venv", str(venv)], "venv", 0):
                raise ExecutionFailure("environment_venv_creation_failed", phase="venv")
        venv_python = venv / "bin" / "python3"
        if python or venv.exists():
            minimum = profile.get("minimum_python", [3, 11])
            probe = "import sys; raise SystemExit(0 if sys.version_info[:2] >= " + repr(tuple(minimum)) + " else 1)"
            if execute([str(venv_python), "-I", "-c", probe], "python", 0):
                raise ExecutionFailure("environment_python_mismatch", phase="python")
            env = {**env, "VIRTUAL_ENV": str(venv), "PATH": str(venv / "bin") + os.pathsep + env.get("PATH", os.defpath)}
        manifests = profile.get("fingerprint_files", [])
        verify_paths(repo, manifests)
        hashes = {}
        for name in manifests:
            if not git(repo, "ls-files", "--", name) or not (repo / name).is_file():
                raise ExecutionFailure("environment_manifest_missing", phase="preflight")
            hashes[name] = hashlib.sha256((repo / name).read_bytes()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps({"profile": profile, "files": hashes}, sort_keys=True).encode()).hexdigest()
        receipt_path = directory / "receipt.json"
        try:
            cached = json.loads(receipt_path.read_text()).get("fingerprint") == fingerprint
        except (OSError, ValueError, AttributeError):
            cached = False
        def expand(argv):
            return [arg.replace("{venv_python}", str(venv_python)).replace("{python}", python or "python3") for arg in argv]
        def checks():
            return all(execute(expand(argv), "environment_check", i) == 0
                       for i, argv in enumerate(profile.get("checks", [])))
        if cached and checks():
            return env
        receipt_path.unlink(missing_ok=True)
        for index, argv in enumerate(profile.get("bootstrap_commands", [])):
            code = execute(expand(argv), "bootstrap", index)
            if code:
                raise ExecutionFailure("environment_bootstrap_failed", phase="bootstrap", command_index=index, exit_code=code)
        if not checks():
            raise ExecutionFailure("environment_check_failed", phase="environment_check")
        write_json(receipt_path, {"fingerprint": fingerprint, "ready": True})
        return env
