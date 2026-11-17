import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factory.store import Store
from factory.tracker import reconcile


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"repo": self.tmp.name, "state_dir": str(Path(self.tmp.name) / "state"), "base_branch": "main"}
        self.store = Store(self.config["state_dir"])
        self.candidate = {"id": "pilot-1", "status": "open", "description": "Write documentation.",
                          "acceptance_criteria": "Useful docs", "labels": ["factory-pilot", "ready-for-development"]}
        self.calls = []

    def fake_bd(self, config, *args, json_result=False):
        self.calls.append(args)
        if args[0] == "list":
            return []
        if args[0] == "ready":
            return [self.candidate]
        return None

    def fake_enqueue(self, config, task_id, requirements, **kwargs):
        return {"task_id": task_id, "id": "job1", "branch": "factory/" + task_id, "worktree": "/synthetic/worktree"}

    def test_github_read_failure_cannot_claim(self):
        with patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                reconcile(self.config, self.store, self.fake_enqueue)
        self.assertFalse(any(call[0] == "update" for call in self.calls))

    def test_unresolved_review_prevents_new_claim(self):
        with patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear", return_value=False):
            result = reconcile(self.config, self.store, self.fake_enqueue)
        self.assertEqual(result["status"], "github_review_findings_pending")
        self.assertFalse(any(call[0] == "update" for call in self.calls))

    def test_claim_and_ownership_marker_share_one_update(self):
        with patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear", return_value=True):
            result = reconcile(self.config, self.store, self.fake_enqueue)
        self.assertEqual(result["status"], "task_claimed")
        claim = next(call for call in self.calls if "--claim" in call)
        self.assertIn("factory_project=" + self.config["state_dir"], claim)

    def test_foreign_or_nonpilot_task_never_claimed(self):
        for change in ({"assignee": "another-agent"}, {"labels": ["ready-for-development"]}):
            with self.subTest(change=change):
                original = dict(self.candidate)
                self.candidate.update(change)
                self.calls.clear()
                with patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear", return_value=True):
                    result = reconcile(self.config, self.store, self.fake_enqueue)
                self.assertEqual(result["status"], "no_approved_pilot_tasks")
                self.assertFalse(any(call[0] == "update" for call in self.calls))
                self.candidate = original

    def test_restart_recovers_owned_claim_before_capacity_or_new_work(self):
        owned = {"id": "pilot-interrupted", "metadata": {"factory_project": self.config["state_dir"]}, "description": "Approved docs."}
        with patch("factory.tracker.bd", return_value=[owned]), patch("factory.tracker.reviews_clear") as reviews:
            result = reconcile(self.config, self.store, self.fake_enqueue)
        self.assertEqual(result, {"status": "intake_recovered", "task_id": "pilot-interrupted"})
        reviews.assert_not_called()

    def test_global_slot_limit_prevents_new_claim(self):
        with patch("factory.tracker.bd", return_value=[{"id": "foreign-a"}, {"id": "foreign-b"}]), patch("factory.tracker.reviews_clear") as reviews:
            result = reconcile(self.config, self.store, self.fake_enqueue)
        self.assertEqual(result["status"], "at_global_capacity")
        reviews.assert_not_called()


if __name__ == "__main__":
    unittest.main()
