import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from factory.adapters import _gates
from factory.config import git, load, write_json
from factory.integration import integrate_ready
from factory.scope import verify_paths
from factory.store import Store
from factory.security import policy, receipt_path


class SourceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic")
        self.git("config", "user.email", "test@local.invalid")
        (self.repo / "app.py").write_text("value = 1\n")
        self.git("add", "app.py")
        self.git("commit", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.config = {"repo": str(self.repo), "state_dir": str(self.root / "state"), "runtime": "codex",
                       "base_branch": "main", "task_policy": "source", "auto_merge": True,
                       "integration_policy": "reviewed_code", "integration_checkout_isolated": True,
                       "quality_gates": [["synthetic-tests"]], "security_gates": [["synthetic-security-scan"]]}
        self.store = Store(self.config["state_dir"])

    def git(self, *args, repo=None):
        subprocess.run(["git", "-C", str(repo or self.repo), *args], check=True, capture_output=True)

    def ready(self):
        wt = self.root / "worktree"
        self.git("worktree", "add", "-b", "factory/source", str(wt))
        (wt / "app.py").write_text("value = 2\n")
        self.git("add", "app.py", repo=wt)
        self.git("commit", "-m", "source", repo=wt)
        revision = git(wt, "rev-parse", "HEAD")
        job = self.store.enqueue("source-1", wt, "factory/source", payload={
            "base_revision": self.base, "allowed_paths": ["app.py"]})
        job = self.store.update(job["id"], stage="ready", revision=revision, reviewed_revision=revision,
                                review_result="approved", tests_passed=True)
        write_json(receipt_path(self.config, job), {"revision": revision, "policy": policy(self.config),
                                                   "passed": True, "created_at": time.time()})
        return job

    def integrate(self):
        integrate_ready(type("Supervisor", (), {"store": self.store})(), self.config)

    def test_reviewed_source_integrates_code_but_preserves_application_state(self):
        job = self.ready()
        private = self.repo / "private.sqlite3"
        private.write_bytes(b"synthetic-later-user-write")
        self.integrate()
        self.assertEqual(self.store.get(job["id"])["stage"], "integrated")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), job["revision"])
        self.assertEqual(private.read_bytes(), b"synthetic-later-user-write")
        self.assertEqual(len(list((self.root / "state/backups").glob("*/manifest.json"))), 1)

    def test_review_only_leaves_source_ready_on_branch(self):
        job = self.ready()
        self.config["integration_policy"] = "documentation_only"
        self.integrate()
        self.assertEqual(self.store.get(job["id"])["stage"], "ready")
        self.assertEqual(self.store.get(job["id"])["blocker"], "integration_policy_gate")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_changed_head_or_missing_approval_cannot_integrate(self):
        job = self.ready()
        self.store.update(job["id"], review_result="changes_requested")
        self.integrate()
        self.assertEqual(self.store.get(job["id"])["stage"], "testing")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_rebase_of_source_invalidates_tests_and_review(self):
        job = self.ready()
        (self.repo / "other.py").write_text("value = 3\n")
        self.git("add", "other.py")
        self.git("commit", "-m", "upstream")
        base = git(self.repo, "rev-parse", "HEAD")
        self.integrate()
        current = self.store.get(job["id"])
        self.assertEqual(current["stage"], "testing")
        self.assertFalse(current["tests_passed"])
        self.assertEqual(current["reviewed_revision"], "")
        self.assertEqual(current["payload"]["base_revision"], base)

    def test_source_security_receipt_is_required_and_bound_to_policy(self):
        job = self.ready()
        self.config["security_gates"] = [["new-security-policy"]]
        self.integrate()
        self.assertEqual(self.store.get(job["id"])["stage"], "testing")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_security_failure_never_produces_success_receipt(self):
        job = self.ready()
        receipt_path(self.config, job).unlink()
        attempt = self.root / "attempt"
        attempt.mkdir()
        original = subprocess.run
        def gates(argv, *args, **kwargs):
            if argv == ["synthetic-tests"]:
                return subprocess.CompletedProcess(argv, 0)
            if argv == ["synthetic-security-scan"]:
                return subprocess.CompletedProcess(argv, 1)
            return original(argv, *args, **kwargs)
        with patch("factory.adapters.subprocess.run", side_effect=gates):
            with self.assertRaisesRegex(RuntimeError, "security_gate_failed"):
                _gates(job, self.config, attempt / "outcome.json")
        self.assertFalse(receipt_path(self.config, job).exists())

    def test_unverified_runtime_checkout_is_not_modified(self):
        job = self.ready()
        self.config.pop("integration_checkout_isolated")
        self.integrate()
        self.assertEqual(self.store.get(job["id"])["blocker"], "integration_checkout_isolation_required")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_scoped_directory_allows_children_but_not_siblings_or_symlink_parents(self):
        (self.repo / "tests").mkdir()
        verify_paths(self.repo, ["tests/one.py"], ["tests"])
        with self.assertRaisesRegex(RuntimeError, "task_scope_exceeded"):
            verify_paths(self.repo, ["tests-other/one.py"], ["tests"])
        (self.repo / "tests/link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "protected_path_changed"):
            verify_paths(self.repo, ["tests/link/one.py"], ["tests"])

    def test_legacy_configuration_remains_documentation_only(self):
        path = self.root / "config.json"
        self.config.pop("task_policy")
        self.config["integration_policy"] = "documentation_only"
        write_json(path, self.config)
        self.assertEqual(load(path)["task_policy"], "documentation_only")
        self.config["integration_policy"] = "reviewed_code"
        write_json(path, self.config)
        with self.assertRaisesRegex(ValueError, "source_task_policy_required"):
            load(path)


if __name__ == "__main__":
    unittest.main()
