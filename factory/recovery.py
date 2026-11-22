"""Verified code checkpoints and isolated SQLite recovery rehearsals."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing

from .config import command, git, write_json


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checkpoint_member(checkpoint, name):
    """Keep archive reads beneath their root without following internal symlinks."""
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("checkpoint_invalid")
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or str(relative) != name or name == ".":
        raise ValueError("checkpoint_invalid")
    path = Path(checkpoint).resolve()
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise ValueError("checkpoint_invalid")
    return path


def verified_code_manifest(checkpoint):
    manifest = json.loads(checkpoint_member(checkpoint, "manifest.json").read_text())
    if (manifest.get("schema_version") != 1 or manifest.get("verified") is not True
            or manifest.get("scope") != "committed_code"
            or not isinstance(manifest.get("base_revision"), str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", manifest["base_revision"])
            or checksum(checkpoint_member(checkpoint, "code.bundle")) != manifest.get("bundle_sha256")):
        raise ValueError("checkpoint_not_verified")
    return manifest


def sqlite_checkpoint(source, destination):
    """No application constructors: those may migrate before a backup exists."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file() or source == destination or destination.exists():
        raise ValueError("invalid_checkpoint_path")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise RuntimeError("checkpoint_integrity_failed")
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {"sha256": checksum(destination), "bytes": destination.stat().st_size}


def code_checkpoint(repo, backup_root, base_branch="main"):
    backup_root = Path(backup_root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    checkpoint_id = f"{int(time.time())}-{uuid.uuid4().hex[:12]}"
    staging = backup_root / ("." + checkpoint_id)
    staging.mkdir(mode=0o700)
    bundle = staging / "code.bundle"
    try:
        result = command(["git", "bundle", "create", str(bundle), "--all"], cwd=repo, timeout=300)
        if result.returncode:
            raise RuntimeError("code_checkpoint_failed")
        os.chmod(bundle, 0o600)
        if command(["git", "bundle", "verify", str(bundle)], cwd=repo).returncode:
            raise RuntimeError("code_checkpoint_invalid")
        manifest = {"schema_version": 1, "checkpoint_id": checkpoint_id,
                    "created_at": time.time(), "base_branch": base_branch,
                    "base_revision": git(repo, "rev-parse", base_branch),
                    "bundle_sha256": checksum(bundle), "scope": "committed_code",
                    "verified": False}
        # A real clone checks that the archive is independently recoverable.
        with tempfile.TemporaryDirectory(prefix="factory-restore-check-") as folder:
            if command(["git", "clone", "--no-checkout", str(bundle), folder], timeout=300).returncode:
                raise RuntimeError("code_restore_rehearsal_failed")
            if command(["git", "-C", folder, "cat-file", "-e", manifest["base_revision"] + "^{commit}"]).returncode:
                raise RuntimeError("checkpoint_revision_missing")
        manifest["verified"] = True
        write_json(staging / "manifest.json", manifest)
        final = backup_root / checkpoint_id
        os.replace(staging, final)
        return final
    except Exception:
        shutil.rmtree(staging)
        raise


def restore_code_into(checkpoint, destination):
    if Path(destination).is_symlink():
        raise ValueError("restore_destination_exists")
    checkpoint, destination = Path(checkpoint).resolve(), Path(destination).resolve()
    manifest = verified_code_manifest(checkpoint)
    if destination.exists():
        raise ValueError("restore_destination_exists")
    result = command(["git", "clone", str(checkpoint / "code.bundle"), str(destination)], timeout=300)
    if result.returncode:
        raise RuntimeError("code_restore_failed")
    git(destination, "checkout", "--detach", manifest["base_revision"])
    return {"status": "restored", "revision": manifest["base_revision"], "scope": "committed_code"}
