import json
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.adapters import _agent, _extract_agent_json
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

    def test_legacy_flag_cannot_launch_implementation_or_bootstrap(self):
        for stage in ("implementing", "fixing"):
            for flag in (None, "verified", "maybe"):
                with self.subTest(stage=stage, flag=flag), patch("factory.adapters.run_agent") as agent, patch("factory.adapters.prepare") as bootstrap:
                    with self.assertRaisesRegex(RuntimeError, "opencode_implementation_sandbox_unconfigured"):
                        _agent(self.job(stage), self.config(opencode_implementation_sandbox=flag), self.attempt / "result.json")
                    agent.assert_not_called()
                    bootstrap.assert_not_called()
        self.assertFalse((self.attempt / "usage.json").exists())
        self.assertFalse((self.attempt / "result.json").exists())

    def test_model_flag_remains_available_for_review(self):
        with patch("factory.adapters.run_agent", return_value=subprocess.CompletedProcess([], 0,
                   stdout=completed_response(review_result="approved", findings=[]))) as agent:
            _agent(self.job("reviewing"), self.config(opencode_model="provider/model"), self.attempt / "result.json")
        self.assertIn("--model", agent.call_args.args[0])
        self.assertIn("provider/model", agent.call_args.args[0])

    def test_legacy_probe_cannot_invoke_executable_or_claim_verification(self):
        canary = self.root / "called"
        executable = self.root / "fake-opencode"
        executable.write_text("#!/bin/sh\ntouch " + str(canary) + "\n")
        executable.chmod(0o700)
        script = Path(__file__).resolve().parents[1] / "scripts/verify_opencode_sandbox.py"
        result = subprocess.run([sys.executable, str(script), "--executable", str(executable)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("opencode_implementation_sandbox_unconfigured", result.stderr)
        self.assertFalse(canary.exists())

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

        for flag in ("verified", True, {}, []):
            config = json.loads(bad.read_text())
            config["opencode_implementation_sandbox"] = flag
            bad.write_text(json.dumps(config))
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, "invalid_opencode_implementation_sandbox"):
                load(bad)

    def test_json_strings_with_braces_escapes_and_nesting(self):
        for summary in ("Missing closing } in code", "literal { marker", 'quoted \" and slash \\ and }', "unicode żółć { }\nline"):
            value = {"status": "completed", "summary": summary, "review_result": "not_applicable",
                     "findings": [{"detail": summary, "nested": {"value": "}"}}]}
            payload = json.dumps(value)
            for text in (payload, "Completed. " + payload, "```json\n" + payload + "\n```", "Status. ```json\n" + payload + "\n```"):
                with self.subTest(text=text):
                    self.assertEqual(_extract_agent_json(text), value)
        with self.assertRaises(ValueError):
            _extract_agent_json("{" * 65537)

    def test_opencode_prose_and_fence_answers_are_extracted(self):
        payload = '{"status": "completed", "summary": "synthetic", "review_result": "not_applicable"}'
        shapes = {
            "clean": payload,
            "fenced": "```json\n" + payload + "\n```",
            "prose_then_fence": "Short status line first.```json\n" + payload + "\n```",
            "prose_then_inline": "Done, appended the line. " + payload,
        }
        for label, text in shapes.items():
            with self.subTest(label=label):
                self.assertEqual(_extract_agent_json(text), json.loads(payload))
        with self.assertRaises(ValueError):
            _extract_agent_json("no json here at all")
        with self.assertRaises(ValueError):
            _extract_agent_json("")


if __name__ == "__main__":
    unittest.main()
