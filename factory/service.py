"""Versioned runtime artifacts and explicitly activated local macOS services.

The launcher imports committed code from its private runtime artifact, not from
a working directory in Documents. Project access is still checked by the
supervisor; this module never changes macOS privacy permissions.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

from .config import git, load, write_json
from .worker import process_matches


SHA = re.compile(r"[0-9a-f]{40}")


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("service_directory_is_symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write_bytes(path, content, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _git_bytes(source, *args):
    result = subprocess.run(["git", "-C", str(source), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("runtime_source_unavailable")
    return result.stdout


def _verify_artifact(path, revision, expected_files=None):
    if path.is_symlink() or not path.is_dir():
        raise ValueError("runtime_artifact_invalid")
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("runtime_artifact_invalid")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("revision") != revision:
        raise ValueError("runtime_artifact_revision_mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or "factory/__init__.py" not in files or "factory/__main__.py" not in files:
        raise ValueError("runtime_artifact_invalid")
    if expected_files is not None and files != expected_files:
        raise ValueError("runtime_artifact_source_mismatch")
    expected = set(files) | {"manifest.json"}
    actual = set()
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValueError("runtime_artifact_invalid")
        if item.is_file():
            actual.add(item.relative_to(path).as_posix())
    if actual != expected:
        raise ValueError("runtime_artifact_changed")
    for name, checksum in files.items():
        relative = PurePosixPath(name)
        if len(relative.parts) != 2 or relative.parts[0] != "factory" or relative.suffix != ".py":
            raise ValueError("runtime_artifact_invalid")
        if _hash((path / name).read_bytes()) != checksum:
            raise ValueError("runtime_artifact_changed")
        if (path / name).stat().st_mode & 0o077:
            raise ValueError("runtime_artifact_not_private")
    if manifest_path.stat().st_mode & 0o077 or path.stat().st_mode & 0o077:
        raise ValueError("runtime_artifact_not_private")
    return path


def stage_runtime(source, state_dir, revision=None):
    """Copy only committed regular Python module blobs, with a verified manifest."""
    source = Path(source).resolve()
    head = git(source, "rev-parse", "HEAD")
    revision = revision or head
    if not isinstance(revision, str) or not SHA.fullmatch(revision) or revision != head:
        raise ValueError("runtime_source_revision_mismatch")
    root = _private_directory(Path(state_dir).resolve() / "runtimes")
    destination = root / revision
    contents = {}
    for entry in _git_bytes(source, "ls-tree", "-rz", revision, "--", "factory").split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split()
        name = raw_name.decode("utf-8")
        relative = PurePosixPath(name)
        if relative.suffix != ".py":
            continue
        if len(relative.parts) != 2 or relative.parts[0] != "factory" or mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("runtime_source_entry_invalid")
        content = _git_bytes(source, "cat-file", "blob", object_id)
        if len(content) > 4 * 1024 * 1024:
            raise ValueError("runtime_module_too_large")
        contents[name] = content
    files = {name: _hash(content) for name, content in contents.items()}
    if "factory/__init__.py" not in files or "factory/__main__.py" not in files:
        raise ValueError("runtime_source_incomplete")
    if destination.exists() or destination.is_symlink():
        return _verify_artifact(destination, revision, files)
    staging = Path(tempfile.mkdtemp(prefix="." + revision + "-", dir=root))
    try:
        modules = _private_directory(staging / "factory")
        for name, content in contents.items():
            _write_bytes(modules / PurePosixPath(name).name, content)
        write_json(staging / "manifest.json", {"schema_version": 1, "revision": revision, "files": files})
        _verify_artifact(staging, revision)
        os.replace(staging, destination)
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return destination
    except Exception:
        shutil.rmtree(staging)
        raise


def service_label(name):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", name):
        raise ValueError("invalid_service_name")
    return "dev.software-factory." + name


def service_spec(artifact, config_path, python, name="buchos", home=None):
    artifact, config_path = Path(artifact).resolve(), Path(config_path).resolve()
    home = Path(home or Path.home()).resolve()
    state = config_path.parent
    return {"Label": service_label(name),
            "ProgramArguments": [str(python), "-m", "factory", "--config", str(config_path), "run"],
            "WorkingDirectory": str(artifact), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "StandardOutPath": str(state / "service.out.log"), "StandardErrorPath": str(state / "service.err.log"),
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + str(home / ".local/bin"),
                                     "HOME": str(home), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                                     "PYTHONPATH": str(artifact)}}


def _heartbeat(state):
    try:
        path = Path(state) / "status.json"
        if path.stat().st_size > 1024 * 1024:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def wait_for_ready(state_dir, *, not_before, previous_startup_id="", timeout=30,
                   process_verifier=process_matches, monotonic=time.monotonic, sleeper=time.sleep):
    """An old status file or merely live launchctl process is never readiness."""
    deadline = monotonic() + max(0, timeout)
    while True:
        value = _heartbeat(state_dir)
        startup = value.get("startup_id", "")
        started, updated = value.get("started_at"), value.get("updated_at")
        numeric = all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)
                      for item in (started, updated))
        if (value.get("ready") is True and value.get("status") == "running"
                and isinstance(startup, str) and re.fullmatch(r"[0-9a-f]{32}", startup)
                and startup != previous_startup_id and numeric
                and started >= not_before - 0.01 and updated >= max(started, not_before - 0.01)
                and -0.01 <= time.time() - updated <= 10
                and process_verifier(value.get("pid"), value.get("process_identity", ""))):
            return {key: value[key] for key in ("startup_id", "started_at", "updated_at", "pid")}
        if monotonic() >= deadline:
            raise RuntimeError("service_ready_not_observed")
        sleeper(min(0.25, max(0, deadline - monotonic())))


def _launchctl(runner, *args):
    return runner(["launchctl", *args], cwd="/", capture_output=True, text=True, timeout=15, check=False)


def _owned_definition(content, label, config_path):
    try:
        value = plistlib.loads(content)
        arguments = value["ProgramArguments"]
        index = arguments.index("--config")
        return (value["Label"] == label and len(arguments) == 6
                and arguments[1:4] == ["-m", "factory", "--config"]
                and arguments[index + 1] == str(config_path) and arguments[5] == "run")
    except (ValueError, KeyError, TypeError, IndexError, plistlib.InvalidFileException):
        return False


def repair_service(source, config_path, *, expected_revision, python, name="buchos", activate=False,
                   home=None, runner=subprocess.run, readiness=wait_for_ready, process_verifier=process_matches,
                   ready_timeout=30):
    """Prepare a repair; activation replaces only this exact owned service.

    The caller must first stop an existing supervisor and its Herdr restart loop.
    Configuration, execution records, tools, and application state are untouched.
    """
    source, config_path = Path(source).resolve(), Path(config_path).resolve()
    home = Path(home or Path.home()).resolve()
    config = load(config_path)
    state = Path(config["state_dir"])
    artifact = stage_runtime(source, state, expected_revision)
    spec = service_spec(artifact, config_path, python, name, home)
    label = spec["Label"]
    directory = _private_directory(state / "service")
    candidate = directory / (label + "-" + expected_revision + ".plist")
    content = plistlib.dumps(spec, sort_keys=True)
    _write_bytes(candidate, content)
    result = {"status": "prepared", "service": label, "revision": expected_revision,
              "runtime": str(artifact), "candidate": str(candidate)}
    if not activate:
        return result
    previous = _heartbeat(state)
    if process_verifier(previous.get("pid"), previous.get("process_identity", "")):
        raise RuntimeError("existing_supervisor_running")
    plist_path = home / "Library/LaunchAgents" / (label + ".plist")
    if plist_path.is_symlink():
        raise ValueError("service_definition_is_symlink")
    old = plist_path.read_bytes() if plist_path.exists() else None
    old_mode = stat.S_IMODE(plist_path.stat().st_mode) if old is not None else 0o600
    if old is not None and not _owned_definition(old, label, config_path):
        raise ValueError("service_definition_not_owned")
    domain = "gui/" + str(os.getuid())
    target = domain + "/" + label
    disabled_result = _launchctl(runner, "print-disabled", domain)
    if disabled_result.returncode:
        raise RuntimeError("service_state_unavailable")
    match = re.search(r'"' + re.escape(label) + r'"\s*=>\s*(true|false)', disabled_result.stdout)
    was_disabled = bool(match and match.group(1) == "true")
    was_loaded = _launchctl(runner, "print", target).returncode == 0
    if was_loaded and old is None:
        raise RuntimeError("service_definition_missing")
    if old is not None:
        backup = directory / ("previous-" + _hash(old) + ".plist")
        if backup.exists() and backup.read_bytes() != old:
            raise RuntimeError("service_backup_changed")
        if not backup.exists():
            _write_bytes(backup, old)
    journal_path = directory / ("activation-" + uuid.uuid4().hex + ".json")
    journal = {"schema_version": 1, "service": label, "revision": expected_revision,
               "previous_loaded": was_loaded, "previous_disabled": was_disabled,
               "previous_plist_sha256": _hash(old) if old is not None else None, "status": "prepared"}
    write_json(journal_path, journal)
    try:
        if was_loaded and _launchctl(runner, "bootout", target).returncode:
            raise RuntimeError("service_stop_failed")
        if plist_path.parent.is_symlink():
            raise ValueError("service_directory_is_symlink")
        plist_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_bytes(plist_path, content)
        if _launchctl(runner, "enable", target).returncode:
            raise RuntimeError("service_enable_failed")
        not_before = time.time()
        if _launchctl(runner, "bootstrap", domain, str(plist_path)).returncode:
            raise RuntimeError("service_bootstrap_failed")
        ready = readiness(state, not_before=not_before, previous_startup_id=previous.get("startup_id", ""),
                          timeout=ready_timeout, process_verifier=process_verifier)
        native = _launchctl(runner, "print", target)
        native_pid = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", native.stdout, re.M)
        if native.returncode or native_pid is None or int(native_pid.group(1)) != ready["pid"]:
            raise RuntimeError("service_process_mismatch")
        journal.update(status="ready", startup_id=ready["startup_id"], pid=ready["pid"])
        write_json(journal_path, journal)
        return {**result, "status": "ready", "startup_id": ready["startup_id"], "pid": ready["pid"]}
    except Exception as error:
        rollback_ok = True
        try:
            if _launchctl(runner, "print", target).returncode == 0:
                if _launchctl(runner, "bootout", target).returncode:
                    raise RuntimeError("service_rollback_stop_failed")
            if old is None:
                plist_path.unlink(missing_ok=True)
            else:
                _write_bytes(plist_path, old, old_mode)
            if was_loaded:
                if _launchctl(runner, "enable", target).returncode or _launchctl(runner, "bootstrap", domain, str(plist_path)).returncode:
                    raise RuntimeError("service_rollback_start_failed")
            if _launchctl(runner, "disable" if was_disabled else "enable", target).returncode:
                raise RuntimeError("service_rollback_state_failed")
        except Exception:
            rollback_ok = False
        code = str(error)
        if not re.fullmatch(r"[a-z_]{1,64}", code):
            code = "service_activation_failed"
        journal.update(status="failed", error_code=code, rollback_complete=rollback_ok)
        write_json(journal_path, journal)
        raise RuntimeError(code if rollback_ok else "service_rollback_incomplete") from None
