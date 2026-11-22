"""Explicit application release adapters and private, isolated recovery drills.

Code integration never calls this module implicitly. Adapter commands are trusted
project configuration, must be idempotent, and must keep all writers stopped
between ``stop`` and ``start``. A successful ``verify_stopped`` must attest that
boundary, including background writers. Automatic rollback changes code only.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

from .adapters import runtime_environment
from .config import command, git, write_json
from .recovery import checksum, code_checkpoint, restore_code_into, sqlite_checkpoint, checkpoint_member


def _schema(path):
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=15)) as db:
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        version = db.execute("PRAGMA user_version").fetchone()[0]
    return hashlib.sha256(json.dumps([version, schema], sort_keys=True).encode()).hexdigest()


def _inventory(items):
    if not isinstance(items, list):
        raise ValueError("invalid_state_inventory")
    names, paths, result = set(), set(), []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "kind", "path"}:
            raise ValueError("invalid_state_inventory")
        name, kind, raw = item["name"], item["kind"], Path(item["path"]).expanduser()
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", name) or kind not in {"sqlite", "file"}:
            raise ValueError("invalid_state_inventory")
        if not raw.is_absolute() or raw.is_symlink() or any(parent.is_symlink() for parent in raw.parents):
            raise ValueError("unsafe_state_path")
        path = raw.resolve()
        if name in names or path in paths or not path.is_file():
            raise ValueError("invalid_state_inventory")
        names.add(name)
        paths.add(path)
        result.append({"name": name, "kind": kind, "path": str(path)})
    return result


def _copy_file(source, destination):
    before = Path(source).stat()
    with Path(source).open("rb") as src, Path(destination).open("xb") as dst:
        os.chmod(destination, 0o600)
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    after = Path(source).stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise RuntimeError("state_changed_during_checkpoint")


def create_checkpoint(repo, backup_root, inventory, base_branch="main", *, quiesced=False):
    """Snapshot explicit resources, without constructing/migrating an application.

    The standalone API provides per-resource consistency (SQLite online backup
    includes WAL). Only a caller that has stopped and verified *all* writers may
    pass quiesced=True and claim consistency across multiple resources.
    """
    items = _inventory(inventory)
    root = Path(backup_root).expanduser().resolve()
    if root.is_relative_to(Path(repo).resolve()):
        raise ValueError("checkpoint_must_be_outside_repository")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    checkpoint_id = "release-" + uuid.uuid4().hex
    stage = root / ("." + checkpoint_id)
    stage.mkdir(mode=0o700)
    try:
        code = code_checkpoint(repo, stage / "code", base_branch)
        (stage / "state").mkdir(mode=0o700)
        records = []
        for item in items:
            destination = stage / "state" / item["name"]
            if item["kind"] == "sqlite":
                sqlite_checkpoint(item["path"], destination)
            else:
                _copy_file(item["path"], destination)
            record = {**item, "sha256": checksum(destination), "bytes": destination.stat().st_size}
            if item["kind"] == "sqlite":
                record["schema"] = _schema(destination)
            records.append(record)
        manifest = {"schema_version": 1, "checkpoint_id": checkpoint_id,
                    "created_at": time.time(), "verified": False,
                    "consistency": "quiesced" if quiesced else "per_resource",
                    "code": str(code.relative_to(stage)), "inventory": records}
        write_json(stage / "manifest.json", manifest)
        # Rehearse actual restoration into a disposable, independent directory.
        with tempfile.TemporaryDirectory(prefix="factory-state-rehearsal-") as tmp:
            _restore(stage, Path(tmp) / "restored", manifest)
        manifest["verified"] = True
        write_json(stage / "manifest.json", manifest)
        final = root / checkpoint_id
        os.replace(stage, final)
        return final
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _manifest(checkpoint):
    checkpoint = Path(checkpoint).resolve()
    value = json.loads(checkpoint_member(checkpoint, "manifest.json").read_text())
    if value.get("schema_version") != 1 or value.get("verified") is not True or checkpoint.name.startswith("."):
        raise ValueError("checkpoint_not_verified")
    return value


def _restore(checkpoint, destination, manifest):
    if Path(destination).is_symlink():
        raise ValueError("restore_destination_exists")
    checkpoint, destination = Path(checkpoint).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("restore_destination_exists")
    code = checkpoint_member(checkpoint, manifest["code"])
    names = set()
    for item in manifest["inventory"]:
        name = item["name"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", name) or name in names:
            raise ValueError("checkpoint_invalid")
        names.add(name)
        path = checkpoint_member(checkpoint, "state/" + name)
        if item.get("kind") not in {"file", "sqlite"} or checksum(path) != item["sha256"]:
            raise ValueError("checkpoint_not_verified")
        if item["kind"] == "sqlite" and _schema(path) != item["schema"]:
            raise ValueError("checkpoint_not_verified")
    destination.mkdir(parents=True, mode=0o700)
    try:
        result = restore_code_into(code, destination / "code")
        state = destination / "state"
        state.mkdir(mode=0o700)
        for item in manifest["inventory"]:
            source, target = checkpoint_member(checkpoint, "state/" + item["name"]), state / item["name"]
            _copy_file(source, target)
            if checksum(target) != item["sha256"]:
                raise ValueError("checkpoint_not_verified")
            if item["kind"] == "sqlite":
                with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
                    if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise ValueError("checkpoint_not_verified")
        return {"status": "restored", "revision": result["revision"], "scope": "isolated_code_and_state"}
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def restore_into(checkpoint, destination):
    """Restore only into a NEW isolated directory; never overwrite live data."""
    return _restore(checkpoint, destination, _manifest(checkpoint))


@contextmanager
def _locked(config):
    root = Path(config["state_dir"]).resolve() / "release"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with (root / "activation.lock").open("a+") as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("release_already_running") from None
        yield root


def _adapter(config):
    release = config.get("release")
    if not isinstance(release, dict) or not isinstance(release.get("commands"), dict):
        raise ValueError("release_adapter_unconfigured")
    for name in ("stop", "verify_stopped", "start", "health"):
        argv = release["commands"].get(name)
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg for arg in argv):
            raise ValueError("release_adapter_unconfigured")
    _inventory(release.get("inventory"))
    return release


def _artifact(config, revision):
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise ValueError("invalid_release_revision")
    if git(config["repo"], "rev-parse", revision + "^{commit}") != revision:
        raise ValueError("invalid_release_revision")
    root = Path(config["state_dir"]).resolve() / "releases"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact = root / revision
    if not artifact.exists():
        stage = root / ("." + uuid.uuid4().hex)
        try:
            if command(["git", "clone", "--no-hardlinks", "--no-checkout", str(config["repo"]), str(stage)], timeout=300).returncode:
                raise RuntimeError("release_artifact_failed")
            git(stage, "checkout", "--detach", revision)
            os.chmod(stage, 0o700)
            os.replace(stage, artifact)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    if git(artifact, "rev-parse", "HEAD") != revision or git(artifact, "status", "--porcelain"):
        raise RuntimeError("release_artifact_changed")
    return artifact


def _ensure_compatible(config, revision):
    """A code revision must explicitly declare accepted live SQLite schemas."""
    contracts = config["release"].get("compatible_schemas", {}).get(revision, {})
    for item in _inventory(config["release"]["inventory"]):
        if item["kind"] != "sqlite":
            continue
        accepted = contracts.get(item["name"])
        if not isinstance(accepted, list) or not accepted or not all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x) for x in accepted):
            raise ValueError("release_schema_contract_missing")
        if _schema(Path(item["path"])) not in accepted:
            raise ValueError("release_schema_incompatible")


def _run(config, name, revision, runner=None):
    release = config["release"]
    template = release["commands"].get(name)
    if not template and name == "migrate":
        return
    if not isinstance(template, list) or not template or not all(isinstance(x, str) and x for x in template):
        raise ValueError("invalid_release_command")
    artifact = _artifact(config, revision)
    try:
        argv = [arg.format(release=str(artifact), revision=revision) for arg in template]
    except (ValueError, KeyError):
        raise ValueError("invalid_release_command") from None
    timeout = release.get("command_timeout_seconds", 60)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 1800:
        raise ValueError("invalid_release_timeout")
    if runner:
        result = runner(argv, cwd=artifact, timeout=timeout)
    else:
        env = runtime_environment()
        configured_env = release.get("environment", {})
        if not isinstance(configured_env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in configured_env.items()):
            raise ValueError("invalid_release_environment")
        env.update(configured_env)
        result = subprocess.run(argv, cwd=artifact, timeout=timeout, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError("release_" + name + "_failed")


def _save(root, journal, stage, blocker=""):
    journal.update(stage=stage, blocker=blocker, updated_at=time.time())
    write_json(root / "activation.json", journal)


def release_status(config):
    try:
        value = json.loads((Path(config["state_dir"]) / "release/activation.json").read_text())
    except (OSError, ValueError):
        return {"stage": "not_activated", "blocker": ""}
    return {key: value.get(key, "") for key in ("stage", "revision", "previous_revision", "blocker")}


def _rollback(config, root, journal, runner=None):
    _save(root, journal, "rollback_stopping")
    running = journal.get("running_revision") or (journal["revision"] if journal.get("candidate_may_have_run") else journal["previous_revision"])
    _run(config, "stop", running, runner)
    _run(config, "verify_stopped", running, runner)
    if journal.get("checkpoint"):
        manifest = _manifest(journal["checkpoint"])
        for item in manifest["inventory"]:
            if item["kind"] == "sqlite" and _schema(Path(item["path"])) != item["schema"]:
                _save(root, journal, "blocked", "schema_incompatible_with_previous_release")
                return release_status(config)
    elif journal.get("candidate_may_have_run"):
        _save(root, journal, "blocked", "release_checkpoint_missing")
        return release_status(config)
    previous = journal["previous_revision"]
    try:
        _ensure_compatible(config, previous)
    except ValueError:
        _save(root, journal, "blocked", "schema_incompatible_with_previous_release")
        return release_status(config)
    journal["running_revision"] = previous
    _save(root, journal, "rollback_starting")
    _run(config, "start", previous, runner)
    _run(config, "health", previous, runner)
    write_json(root / "active.json", {"revision": previous})
    _save(root, journal, "rolled_back")
    return release_status(config)


def activate(config, revision, runner=None):
    """Activate explicitly configured code; no automatic data restore on failure.

    The caller must authorize and verify the exact tested/reviewed revision.
    ``stop``/``verify_stopped`` are mandatory before any state snapshot or
    migration. A schema-changing failure remains stopped for manual recovery.
    """
    _adapter(config)
    with _locked(config) as root:
        if (root / "activation.json").exists():
            pending = json.loads((root / "activation.json").read_text())
            if pending["stage"] not in {"active", "rolled_back"}:
                raise RuntimeError("release_recovery_required")
        _artifact(config, revision)
        pointer = root / "active.json"
        previous = json.loads(pointer.read_text())["revision"] if pointer.exists() else config["release"].get("initial_revision")
        if not isinstance(previous, str):
            raise ValueError("initial_release_revision_required")
        _artifact(config, previous)
        _ensure_compatible(config, previous)
        journal = {"revision": revision, "previous_revision": previous,
                   "running_revision": previous,
                   "candidate_may_have_run": False, "checkpoint": "", "created_at": time.time()}
        _save(root, journal, "quiescing")
        try:
            _run(config, "stop", previous, runner)
            _run(config, "verify_stopped", previous, runner)
            _save(root, journal, "checkpointing")
            checkpoint = create_checkpoint(config["repo"], root / "backups", config["release"]["inventory"],
                                           previous, quiesced=True)
            journal["checkpoint"] = str(checkpoint)
            # Persist before executing a command that can migrate/write state.
            journal["candidate_may_have_run"] = True
            journal["running_revision"] = revision
            _save(root, journal, "migrating")
            _run(config, "migrate", revision, runner)
            _ensure_compatible(config, revision)
            _save(root, journal, "starting_candidate")
            _run(config, "start", revision, runner)
            _save(root, journal, "checking_health")
            _run(config, "health", revision, runner)
            write_json(pointer, {"revision": revision})
            _save(root, journal, "active")
            return release_status(config)
        except Exception:
            try:
                return _rollback(config, root, journal, runner)
            except Exception:
                _save(root, journal, "blocked", "release_rollback_failed")
                return release_status(config)


def recover(config, runner=None):
    """Conservatively stop an interrupted activation and restore previous CODE.

    A crash after a possible migration is never resolved by restoring old data.
    Schema incompatibility leaves the adapter stopped and records a blocker.
    """
    _adapter(config)
    with _locked(config) as root:
        path = root / "activation.json"
        if not path.exists():
            return release_status(config)
        journal = json.loads(path.read_text())
        if journal["stage"] in {"active", "rolled_back"}:
            return release_status(config)
        try:
            return _rollback(config, root, journal, runner)
        except Exception:
            _save(root, journal, "blocked", "release_rollback_failed")
            return release_status(config)
