import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.adapters import _agent
from factory.config import load


def run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def completed_response(**extra):
    payload = {"status": "completed", "summary": "synthetic", "review_result": "not_applicable"}
    payload.update(extra)
    return json.dumps(payload)


class OpenCodeImplementationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run(self.repo, "init", "-b", "main")
        run(self.repo, "config", "user.name", "Test")
        run(self.repo, "config", "user.email", "test@local.invalid")
        (self.repo / "NOTES.md").write_text("probe\n")
        run(self.repo, "add", "NOTES.md")
        run(self.repo, "commit", "-m", "initial")
        self.base = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()

    def job(self, stage="implementing"):
        return {
            "id": "job-1",
            "task_id": "test-task",
            "stage": stage,
            "runtime": "opencode",
            "worktree": str(self.repo),
            "revision": self.base,
            "payload": {
                "requirements": "synthetic requirements",
                "acceptance": "synthetic acceptance",
                "allowed_paths": ["NOTES.md"],
                "question_path": str(self.attempt / "question.json"),
                "findings_path": str(self.attempt / "findings.json"),
                "base_revision": self.base,
            },
        }

    def config(self, **extra):
        base = {"opencode_executable": "opencode"}
        base.update(extra)
        return base

    def test_implementation_stays_disabled_without_verified_sandbox(self):
        with self.assertRaisesRegex(RuntimeError, "opencode_implementation_sandbox_unconfigured"):
            _agent(self.job(), self.config(), self.attempt / "result.json")

    def test_implementation_uses_auto_build_agent_and_archived_flags(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            (self.repo / "NOTES.md").write_text("probe\nsandbox-probe-ok\n")
            return subprocess.CompletedProcess(argv, 0, stdout=completed_response(), stderr="")

        result_path = self.attempt / "result.json"
        with patch("factory.adapters.run_agent", side_effect=fake_run):
            _agent(self.job(), self.config(opencode_implementation_sandbox="verified"), result_path)

        argv = captured["argv"]
        self.assertIn("--auto", argv)
        self.assertIn("build", argv)
        self.assertIn("--archived", argv)
        self.assertNotIn("plan", argv)
        result = json.loads(result_path.read_text())
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(result["revision"], self.base)
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True
        ).stdout
        self.assertEqual(status, "")
        log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "-1", "--pretty=%s"], capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(log, "feat: implement test-task")

    def test_implementation_model_flag_is_passed_when_configured(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout=completed_response(), stderr="")

        with patch("factory.adapters.run_agent", side_effect=fake_run):
            _agent(
                self.job(),
                self.config(opencode_implementation_sandbox="verified", opencode_model="deepseek/deepseek-chat"),
                self.attempt / "result.json",
            )
        self.assertIn("--model", captured["argv"])
        self.assertIn("deepseek/deepseek-chat", captured["argv"])

    def test_review_branch_stays_read_only_plan_agent(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                argv, 0, stdout=completed_response(review_result="approved", findings=[]), stderr=""
            )

        with patch("factory.adapters.run_agent", side_effect=fake_run):
            _agent(self.job(stage="reviewing"), self.config(), self.attempt / "result.json")

        argv = captured["argv"]
        self.assertIn("plan", argv)
        self.assertNotIn("--auto", argv)

    def test_config_rejects_unknown_sandbox_value(self):
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({
            "repo": str(self.repo),
            "state_dir": str(self.root / "state"),
            "runtime": "opencode",
            "integration_policy": "documentation_only",
            "opencode_implementation_sandbox": "maybe",
        }))
        with self.assertRaisesRegex(ValueError, "invalid_opencode_implementation_sandbox"):
            load(bad)

        good = self.root / "good.json"
        good.write_text(json.dumps({
            "repo": str(self.repo),
            "state_dir": str(self.root / "state"),
            "runtime": "opencode",
            "integration_policy": "documentation_only",
            "opencode_implementation_sandbox": "verified",
        }))
        self.assertEqual(load(good).get("opencode_implementation_sandbox"), "verified")


if __name__ == "__main__":
    unittest.main()
