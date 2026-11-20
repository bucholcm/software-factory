import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from factory.cli import main
from factory.config import load, write_json


class ProjectCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "project"
        self.other = self.root / "other"
        for repo in (self.repo, self.other):
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Synthetic", "-c", "user.email=test@local.invalid",
                            "commit", "--allow-empty", "-m", "initial"], check=True, capture_output=True)
        self.path = self.root / "config.json"
        write_json(self.path, {"repo": str(self.repo), "state_dir": str(self.root / "state"),
                              "runtime": "codex", "integration_policy": "documentation_only", "base_branch": "main"})

    def call(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["factory", "--config", str(self.path), *map(str, args)]), contextlib.redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def test_submit_refuses_wrong_project_before_opening_state(self):
        with patch("factory.cli.status") as status:
            with self.assertRaisesRegex(ValueError, "configuration_belongs_to_another_project"):
                self.call("submit", "--task", "ticket-1", "--project", self.other)
        status.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_submit_requires_verified_ready_supervisor(self):
        with patch("factory.cli.status", return_value={"status": "unresponsive", "ready": True}), \
                patch("factory.tracker.reconcile") as submit:
            value = self.call("submit", "--task", "ticket-1", "--project", self.repo)
        self.assertEqual(value["status"], "supervisor_not_ready")
        submit.assert_not_called()

    def test_submit_passes_exact_id_to_shared_intake(self):
        with patch("factory.cli.status", return_value={"status": "running", "ready": True}), \
                patch("factory.tracker.reconcile", return_value={"status": "task_claimed", "task_id": "ticket-1"}) as submit:
            value = self.call("submit", "--task", "ticket-1", "--project", self.repo)
        self.assertEqual(value["task_id"], "ticket-1")
        self.assertEqual(submit.call_args.kwargs["task_id"], "ticket-1")

    def test_source_review_mode_preserves_project_and_enables_source(self):
        self.call("set-mode", "source-review")
        config = load(self.path)
        self.assertEqual(config["task_policy"], "source")
        self.assertEqual(config["integration_policy"], "documentation_only")
        self.assertEqual(Path(config["repo"]), self.repo.resolve())

    def test_source_integration_cannot_skip_security_configuration(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "security_gates_unconfigured"):
            self.call("set-mode", "source-integrate")
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
