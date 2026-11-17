"""Private, durable execution records; project tasks remain in their tracker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager


STAGES = frozenset({"implementing", "testing", "reviewing", "fixing", "ready", "blocked", "failed", "integrated"})
FIELDS = frozenset({"stage", "resume_stage", "attempt", "fix_count", "next_run", "pid", "process_identity", "runtime_session_id", "branch", "worktree", "revision", "reviewed_revision", "review_result", "tests_passed", "blocker", "attempt_dir", "started_at", "updated_at", "outcome", "payload"})


class Store:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.path = self.state_dir / "execution.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL,
                    runtime TEXT NOT NULL, stage TEXT NOT NULL,
                    resume_stage TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    fix_count INTEGER NOT NULL DEFAULT 0,
                    next_run REAL NOT NULL DEFAULT 0,
                    pid INTEGER, process_identity TEXT NOT NULL DEFAULT '',
                    runtime_session_id TEXT NOT NULL DEFAULT '',
                    branch TEXT NOT NULL, worktree TEXT NOT NULL,
                    revision TEXT NOT NULL DEFAULT '',
                    reviewed_revision TEXT NOT NULL DEFAULT '',
                    review_result TEXT NOT NULL DEFAULT '',
                    tests_passed INTEGER NOT NULL DEFAULT 0,
                    blocker TEXT NOT NULL DEFAULT '',
                    attempt_dir TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    outcome TEXT NOT NULL DEFAULT '{}',
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL, stage TEXT NOT NULL,
                    code TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _job(row):
        if row is None:
            return None
        value = dict(row)
        value["tests_passed"] = bool(value["tests_passed"])
        value["outcome"] = json.loads(value["outcome"])
        value["payload"] = json.loads(value["payload"])
        return value

    def enqueue(self, task_id: str, worktree: str | Path, branch: str, runtime: str = "codex", payload: dict | None = None) -> dict:
        if not task_id or len(task_id) > 160 or runtime not in {"codex", "opencode", "fake"}:
            raise ValueError("invalid_job")
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("invalid_job_payload")
        payload_json = json.dumps(payload or {}, sort_keys=True)
        if len(payload_json) > 100000:
            raise ValueError("job_payload_too_large")
        now = time.time()
        with self.connect() as db:
            db.execute("""INSERT OR IGNORE INTO jobs
                (id,task_id,runtime,stage,branch,worktree,created_at,updated_at,payload)
                VALUES(?,?,?,'implementing',?,?,?,?,?)""",
                (uuid.uuid4().hex, task_id, runtime, branch, str(Path(worktree).resolve()), now, now, payload_json))
            row = db.execute("SELECT * FROM jobs WHERE task_id=?", (task_id,)).fetchone()
        return self._job(row)

    def get(self, job_id: str) -> dict | None:
        with self.connect() as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            return [self._job(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at,id")]

    def update(self, job_id: str, **fields) -> dict:
        if not fields or set(fields) - FIELDS:
            raise ValueError("invalid_job_fields")
        if "stage" in fields and fields["stage"] not in STAGES:
            raise ValueError("invalid_stage")
        fields["updated_at"] = time.time()
        for key in ("outcome", "payload"):
            if key in fields:
                fields[key] = json.dumps(fields[key], sort_keys=True)
        assignments = ",".join(f"{name}=?" for name in fields)
        with self.connect() as db:
            changed = db.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id)).rowcount
            if not changed:
                raise KeyError("unknown_job")
        return self.get(job_id)

    def event(self, job_id: str, stage: str, code: str):
        # These are internal event codes, never arbitrary agent or shell output.
        if not code or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in code):
            raise ValueError("invalid_event_code")
        with self.connect() as db:
            db.execute("INSERT INTO events(job_id,stage,code,created_at) VALUES(?,?,?,?)", (job_id, stage, code, time.time()))

    def resume(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job is None or job["stage"] not in {"blocked", "failed"}:
            raise ValueError("job_not_resumable")
        if job["pid"]:
            from .worker import group_exists, process_matches
            if process_matches(job["pid"], job["process_identity"]) or group_exists(job["pid"]):
                raise ValueError("job_still_running")
        stage = job["resume_stage"] or "implementing"
        if stage not in {"implementing", "testing", "reviewing", "fixing"}:
            raise ValueError("invalid_resume_stage")
        return self.update(job_id, stage=stage, attempt=0, blocker="", next_run=0, pid=None,
                           process_identity="", attempt_dir="", started_at=0)
