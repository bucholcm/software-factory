from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from factory.config import git, write_json
from factory.release import activate, create_checkpoint, recover, restore_into, _schema


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="factory-release-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic Release Test")
        self.git("config", "user.email", "test@local.invalid")
        (self.repo / "code.py").write_text("VERSION = 1\n")
        self.git("add", "code.py")
        self.git("commit", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.git("switch", "-c", "candidate")
        (self.repo / "code.py").write_text("VERSION = 2\n")
        self.git("commit", "-am", "candidate")
        self.candidate = git(self.repo, "rev-parse", "HEAD")
        self.git("switch", "main")
        self.database = self.root / "synthetic.sqlite3"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO items(value) VALUES('before')")
            db.commit()
        self.mutable = self.root / "synthetic-candidates.json"
        self.mutable.write_text('{"candidate":"synthetic"}')
        self.inventory = [{"name": "database", "kind": "sqlite", "path": str(self.database)},
                          {"name": "candidates", "kind": "file", "path": str(self.mutable)}]
        schema = _schema(self.database)
        self.config = {"repo": str(self.repo), "state_dir": str(self.root / "factory"), "base_branch": "main",
                       "release": {"inventory": self.inventory, "initial_revision": self.base,
                                   "compatible_schemas": {self.base: {"database": [schema]}, self.candidate: {"database": [schema]}},
                                   "commands": {name: [name, "{revision}", "{release}"] for name in ("stop", "verify_stopped", "start", "health")}}}

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def rows(self, path=None):
        with closing(sqlite3.connect(path or self.database)) as db:
            return db.execute("SELECT value FROM items ORDER BY id").fetchall()

    def test_populated_wal_and_file_checkpoint_rehearses_isolated_restore(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO items(value) VALUES('wal-only')")
            writer.commit()
            self.assertGreater(Path(str(self.database) + "-wal").stat().st_size, 0)
            checkpoint = create_checkpoint(self.repo, self.root / "backups", self.inventory)
            writer.execute("INSERT INTO items(value) VALUES('later-write')")
            writer.commit()
        restored = self.root / "restore"
        receipt = restore_into(checkpoint, restored)
        self.assertEqual(receipt["scope"], "isolated_code_and_state")
        self.assertEqual((restored / "code/code.py").read_text(), "VERSION = 1\n")
        self.assertEqual(self.rows(restored / "state/database"), [("before",), ("wal-only",)])
        self.assertEqual(self.rows(), [("before",), ("wal-only",), ("later-write",)])
        self.assertEqual((restored / "state/candidates").read_text(), self.mutable.read_text())
        manifest = json.loads((checkpoint / "manifest.json").read_text())
        self.assertTrue(manifest["verified"])
        self.assertEqual(manifest["consistency"], "per_resource")
        self.assertEqual((checkpoint / "state/database").stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, "restore_destination_exists"):
            restore_into(checkpoint, restored)

    def test_failed_snapshot_never_publishes_checkpoint(self):
        with patch("factory.release._copy_file", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                create_checkpoint(self.repo, self.root / "backups", self.inventory)
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_interrupted_hidden_snapshot_and_tampered_data_refuse_restore(self):
        checkpoint = create_checkpoint(self.repo, self.root / "backups", self.inventory)
        hidden = checkpoint.with_name("." + checkpoint.name)
        checkpoint.rename(hidden)
        with self.assertRaisesRegex(ValueError, "checkpoint_not_verified"):
            restore_into(hidden, self.root / "restore")
        hidden.rename(checkpoint)
        (checkpoint / "state/candidates").write_text("modified")
        with self.assertRaisesRegex(ValueError, "checkpoint_not_verified"):
            restore_into(checkpoint, self.root / "restore")
        self.assertFalse((self.root / "restore").exists())

    def test_rejects_symlinks_duplicates_and_in_repository_backups(self):
        alias = self.root / "alias.json"
        alias.symlink_to(self.mutable)
        for inventory in ([{"name": "alias", "kind": "file", "path": str(alias)}], self.inventory * 2):
            with self.assertRaises(ValueError):
                create_checkpoint(self.repo, self.root / "backups", inventory)
        with self.assertRaisesRegex(ValueError, "checkpoint_must_be_outside_repository"):
            create_checkpoint(self.repo, self.repo / "backups", self.inventory)

    def test_activation_stops_and_verifies_before_snapshot_and_migration(self):
        calls = []
        self.config["release"]["commands"]["migrate"] = ["migrate", "{revision}"]
        def runner(argv, **kwargs):
            calls.append(argv[0])
            if argv[0] == "migrate":
                manifest = next((Path(self.config["state_dir"]) / "release/backups").glob("release-*/manifest.json"))
                self.assertTrue(json.loads(manifest.read_text())["verified"])
                self.assertEqual(json.loads(manifest.read_text())["consistency"], "quiesced")
            return subprocess.CompletedProcess(argv, 0)
        result = activate(self.config, self.candidate, runner)
        self.assertEqual(result["stage"], "active")
        self.assertEqual(calls, ["stop", "verify_stopped", "migrate", "start", "health"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_release_checkpoint_records_previous_code_when_main_already_integrated(self):
        self.git("merge", "--ff-only", self.candidate)
        activate(self.config, self.candidate, lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0))
        journal = json.loads((Path(self.config["state_dir"]) / "release/activation.json").read_text())
        destination = self.root / "previous-release-restore"
        result = restore_into(journal["checkpoint"], destination)
        self.assertEqual(result["revision"], self.base)
        self.assertEqual((destination / "code/code.py").read_text(), "VERSION = 1\n")

    def test_failed_health_rolls_back_code_and_preserves_post_release_writes(self):
        calls = []
        def runner(argv, **kwargs):
            name, revision = argv[:2]
            calls.append((name, revision))
            if (name, revision) == ("start", self.candidate):
                with closing(sqlite3.connect(self.database)) as db:
                    db.execute("INSERT INTO items(value) VALUES('after-candidate-start')")
                    db.commit()
                self.mutable.write_text('{"candidate":"later"}')
            return subprocess.CompletedProcess(argv, int((name, revision) == ("health", self.candidate)))
        result = activate(self.config, self.candidate, runner)
        self.assertEqual(result["stage"], "rolled_back")
        self.assertEqual(calls[-2:], [("start", self.base), ("health", self.base)])
        self.assertEqual(self.rows(), [("before",), ("after-candidate-start",)])
        self.assertEqual(self.mutable.read_text(), '{"candidate":"later"}')

    def test_schema_changing_failure_stays_stopped_instead_of_unsafe_rollback(self):
        calls = []
        self.config["release"]["commands"]["migrate"] = ["migrate", "{revision}"]
        def runner(argv, **kwargs):
            calls.append((argv[0], argv[1]))
            if argv[0] == "migrate":
                with closing(sqlite3.connect(self.database)) as db:
                    db.execute("ALTER TABLE items ADD COLUMN new_value TEXT")
            return subprocess.CompletedProcess(argv, 0)
        result = activate(self.config, self.candidate, runner)
        self.assertEqual(result["stage"], "blocked")
        self.assertEqual(result["blocker"], "schema_incompatible_with_previous_release")
        self.assertFalse(any(name == "start" for name, _ in calls))
        self.assertEqual(calls[-2:], [("stop", self.candidate), ("verify_stopped", self.candidate)])

    def test_interrupt_after_candidate_start_recovers_previous_code_once(self):
        calls = []
        def crash(argv, **kwargs):
            calls.append((argv[0], argv[1]))
            if argv[:2] == ["start", self.candidate]:
                raise SystemExit("synthetic supervisor crash")
            return subprocess.CompletedProcess(argv, 0)
        with self.assertRaises(SystemExit):
            activate(self.config, self.candidate, crash)
        with self.assertRaisesRegex(RuntimeError, "release_recovery_required"):
            activate(self.config, self.candidate, crash)
        def runner(argv, **kwargs):
            calls.append((argv[0], argv[1]))
            return subprocess.CompletedProcess(argv, 0)
        result = recover(self.config, runner)
        self.assertEqual(result["stage"], "rolled_back")
        self.assertEqual(calls[-4:], [("stop", self.candidate), ("verify_stopped", self.candidate), ("start", self.base), ("health", self.base)])
        before = len(calls)
        recover(self.config, runner)
        self.assertEqual(len(calls), before)

    def test_stop_verification_failure_never_snapshots_or_migrates(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv[0])
            return subprocess.CompletedProcess(argv, int(argv[0] == "verify_stopped"))
        result = activate(self.config, self.candidate, runner)
        self.assertEqual(result["stage"], "blocked")
        self.assertNotIn("start", calls)
        self.assertFalse((Path(self.config["state_dir"]) / "release/backups").exists())

    def test_crash_after_starting_rollback_stops_previous_revision_on_recovery(self):
        running = self.base
        calls = []
        crash_once = True
        def runner(argv, **kwargs):
            nonlocal running, crash_once
            name, revision = argv[:2]
            calls.append((name, revision))
            if name == "stop" and running == revision:
                running = None
            if name == "verify_stopped" and running is not None:
                return subprocess.CompletedProcess(argv, 1)
            if name == "start":
                running = revision
                if revision == self.base and crash_once:
                    crash_once = False
                    raise SystemExit("crash while restarting previous release")
            return subprocess.CompletedProcess(argv, int((name, revision) == ("health", self.candidate)))
        with self.assertRaises(SystemExit):
            activate(self.config, self.candidate, runner)
        self.assertEqual(running, self.base)
        result = recover(self.config, runner)
        self.assertEqual(result["stage"], "rolled_back")
        self.assertEqual(calls[-4:], [("stop", self.base), ("verify_stopped", self.base), ("start", self.base), ("health", self.base)])

    def test_sigkill_during_candidate_start_reconciles_from_durable_journal(self):
        config_path = self.root / "config.json"
        write_json(config_path, self.config)
        code = """
import json, os, signal, subprocess, sys
from factory.release import activate
config = json.load(open(sys.argv[1]))
def runner(argv, **kwargs):
    if argv[:2] == ['start', sys.argv[2]]:
        os.kill(os.getpid(), signal.SIGKILL)
    return subprocess.CompletedProcess(argv, 0)
activate(config, sys.argv[2], runner)
"""
        child = subprocess.run([sys.executable, "-c", code, str(config_path), self.candidate], timeout=15,
                               capture_output=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(child.returncode, -9)
        journal_path = Path(self.config["state_dir"]) / "release/activation.json"
        self.assertEqual(json.loads(journal_path.read_text())["stage"], "starting_candidate")
        result = recover(self.config, lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0))
        self.assertEqual(result["stage"], "rolled_back")

    def test_initial_previous_revision_and_schema_contract_are_required(self):
        self.config["release"].pop("initial_revision")
        with self.assertRaisesRegex(ValueError, "initial_release_revision_required"):
            activate(self.config, self.candidate)
        self.config["release"]["initial_revision"] = self.base
        self.config["release"].pop("compatible_schemas")
        with self.assertRaisesRegex(ValueError, "release_schema_contract_missing"):
            activate(self.config, self.candidate)

    def test_missing_adapter_never_claims_activation(self):
        with self.assertRaisesRegex(ValueError, "release_adapter_unconfigured"):
            activate({"repo": str(self.repo), "state_dir": str(self.root / "state")}, self.candidate)


if __name__ == "__main__":
    unittest.main()
