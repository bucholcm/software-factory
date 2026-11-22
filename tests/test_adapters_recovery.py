import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing

from factory.adapters import _agent, is_documentation_change, safe_changed_paths, verifier
from factory.config import git, write_json
from factory.integration import integrate_ready
from factory.recovery import code_checkpoint, restore_code_into, sqlite_checkpoint
from factory.store import Store


def run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run(self.repo, "init", "-b", "main")
        run(self.repo, "config", "user.name", "Test")
        run(self.repo, "config", "user.email", "test@local.invalid")
        (self.repo / "README.md").write_text("initial\n")
        run(self.repo, "add", "README.md")
        run(self.repo, "commit", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def commit_file(self, name, value):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        run(self.repo, "add", name)
        run(self.repo, "commit", "-m", "change")

    def test_verified_code_restore_is_independent_and_excludes_untracked_private_data(self):
        (self.repo / "private.sqlite3").write_bytes(b"private-do-not-export")
        checkpoint = code_checkpoint(self.repo, self.root / "backups")
        self.commit_file("README.md", "changed\n")
        restored = self.root / "restored"
        result = restore_code_into(checkpoint, restored)
        self.assertEqual(result["revision"], self.base)
        self.assertEqual((restored / "README.md").read_text(), "initial\n")
        self.assertFalse((restored / "private.sqlite3").exists())
        self.assertEqual((self.repo / "private.sqlite3").read_bytes(), b"private-do-not-export")

    def test_corrupted_bundle_cannot_restore(self):
        checkpoint = code_checkpoint(self.repo, self.root / "backups")
        with (checkpoint / "code.bundle").open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "checkpoint_not_verified"):
            restore_code_into(checkpoint, self.root / "restored")

    def test_sqlite_wal_checkpoint_and_later_writes_preserved(self):
        source = self.root / "source.sqlite3"
        with closing(sqlite3.connect(source)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE transactions(id INTEGER, value TEXT)")
            db.execute("INSERT INTO transactions VALUES(1, 'synthetic')")
            db.commit()
            snapshot = self.root / "snapshot.sqlite3"
            result = sqlite_checkpoint(source, snapshot)
            db.execute("INSERT INTO transactions VALUES(2, 'later')")
            db.commit()
            self.assertEqual(db.execute("SELECT count(*) FROM transactions").fetchone()[0], 2)
        with closing(sqlite3.connect(snapshot)) as restored:
            self.assertEqual(restored.execute("SELECT count(*) FROM transactions").fetchone()[0], 1)
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertTrue(result["sha256"])
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            sqlite_checkpoint(source, snapshot)

    def test_documentation_gate_excludes_agent_instructions(self):
        self.commit_file("docs/pilot.md", "hello\n")
        self.assertTrue(is_documentation_change(self.repo, self.base))
        self.commit_file("AGENTS.md", "instructions\n")
        self.assertFalse(is_documentation_change(self.repo, self.base))

    def test_documentation_gate_rejects_nested_agent_behavior_changes(self):
        for name in ("docs/AGENTS.md", ".opencode/agents/reviewer.md", "nested/.agents/example.md", "docs/CLAUDE.md"):
            with self.subTest(name=name):
                base = git(self.repo, "rev-parse", "HEAD")
                self.commit_file(name, "instructions\n")
                self.assertFalse(is_documentation_change(self.repo, base))

    def test_failed_native_agent_output_is_removed(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        result = attempt / "outcome.json"
        job = {"task_id": "test", "stage": "implementing", "runtime": "codex", "worktree": str(self.repo), "payload": {}}
        def failure(*args, **kwargs):
            (attempt / "native-result.json").write_text("synthetic-private-response")
            return subprocess.CompletedProcess(args[0], 1)
        with patch("factory.adapters.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "agent_process_failed"):
                _agent(job, {"codex_executable": "codex"}, result)
        self.assertFalse((attempt / "native-result.json").exists())

    def test_changed_private_paths_and_symlinks_are_rejected(self):
        for name in (".env", "private.sqlite3"):
            path = self.repo / name
            path.write_text("synthetic")
            with self.assertRaises(RuntimeError):
                safe_changed_paths(self.repo)
            path.unlink()
        (self.repo / "alias.md").symlink_to(self.repo / "README.md")
        with self.assertRaises(RuntimeError):
            safe_changed_paths(self.repo)

    def test_test_gate_cannot_pass_without_matching_receipt(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        job = {"stage": "testing", "worktree": str(self.repo), "revision": self.base, "attempt_dir": str(attempt)}
        result = {"status": "completed", "revision": self.base, "tests_passed": True}
        write_json(attempt / "gate-receipt.json", {"revision": "0" * 40, "passed": True})
        self.assertFalse(verifier(job, result))
        write_json(attempt / "gate-receipt.json", {"revision": self.base, "passed": True})
        self.assertEqual(verifier(job, result), result)

    def test_automatic_documentation_integration_leaves_live_state_untouched(self):
        state = self.root / "factory-state"
        store = Store(state)
        worktree = self.root / "worktree"
        run(self.repo, "worktree", "add", "-b", "factory/test", str(worktree), "main")
        (worktree / "docs").mkdir()
        (worktree / "docs/pilot.md").write_text("pilot\n")
        run(worktree, "add", "docs/pilot.md")
        run(worktree, "commit", "-m", "documentation")
        revision = git(worktree, "rev-parse", "HEAD")
        (self.repo / "private.sqlite3").write_bytes(b"post-release-synthetic-state")
        job = store.enqueue("test-docs", worktree, "factory/test", payload={"base_revision": self.base})
        store.update(job["id"], stage="ready", revision=revision, reviewed_revision=revision,
                     review_result="approved", tests_passed=True)
        supervisor = type("Stub", (), {"store": store})()
        config = {"repo": str(self.repo), "state_dir": str(state), "base_branch": "main",
                  "auto_merge": True, "integration_policy": "documentation_only"}
        integrate_ready(supervisor, config)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), revision)
        self.assertEqual(store.get(job["id"])["stage"], "integrated")
        self.assertEqual((self.repo / "private.sqlite3").read_bytes(), b"post-release-synthetic-state")
        manifest = next((state / "backups").glob("*/manifest.json"))
        self.assertTrue(json.loads(manifest.read_text())["verified"])
        original_manifest = json.loads(manifest.read_text())
        journal_path = state / "integrations" / (job["id"] + ".json")
        journal = json.loads(journal_path.read_text())
        journal["stage"] = "checkpoint_ready"
        write_json(journal_path, journal)
        store.update(job["id"], stage="ready")
        integrate_ready(supervisor, config)
        self.assertEqual(store.get(job["id"])["stage"], "integrated")
        self.assertEqual(len(list((state / "backups").glob("*/manifest.json"))), 1)
        self.assertEqual(json.loads(manifest.read_text()), original_manifest)
        self.assertEqual(json.loads(journal_path.read_text())["base_revision"], self.base)

    def test_rebase_rechecks_documentation_against_current_main(self):
        state = self.root / "factory-state"
        store = Store(state)
        worktree = self.root / "worktree"
        run(self.repo, "worktree", "add", "-b", "factory/rebase", str(worktree), "main")
        (worktree / "docs").mkdir()
        (worktree / "docs/pilot.md").write_text("pilot\n")
        run(worktree, "add", "docs/pilot.md")
        run(worktree, "commit", "-m", "documentation")
        revision = git(worktree, "rev-parse", "HEAD")
        self.commit_file("app.py", "print('upstream')\n")
        current_main = git(self.repo, "rev-parse", "HEAD")
        job = store.enqueue("test-rebase", worktree, "factory/rebase", payload={"base_revision": self.base})
        store.update(job["id"], stage="ready", revision=revision, reviewed_revision=revision,
                     review_result="approved", tests_passed=True)
        supervisor = type("Stub", (), {"store": store})()
        config = {"repo": str(self.repo), "state_dir": str(state), "base_branch": "main",
                  "auto_merge": True, "integration_policy": "documentation_only"}
        integrate_ready(supervisor, config)
        rebased = store.get(job["id"])
        self.assertEqual(rebased["stage"], "testing")
        self.assertEqual(rebased["payload"]["base_revision"], current_main)
        self.assertEqual(rebased["reviewed_revision"], "")
        self.assertTrue(is_documentation_change(worktree, current_main))


if __name__ == "__main__":
    unittest.main()
