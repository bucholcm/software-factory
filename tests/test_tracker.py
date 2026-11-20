import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factory.store import Store
from factory.tracker import reconcile
from factory.config import write_json


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"repo": self.tmp.name, "state_dir": str(Path(self.tmp.name) / "state"), "base_branch": "main"}
        self.store = Store(self.config["state_dir"])
        self.candidate = {"id": "pilot-1", "status": "open", "description": "Write documentation.",
                          "acceptance_criteria": "Useful docs", "labels": ["factory-pilot", "ready-for-development"],
                          "metadata": {"factory_paths": ["docs/pilot.md"]}}
        self.calls = []

    def fake_bd(self, config, *args, json_result=False):
        self.calls.append(args)
        if args[0] == "list":
            return []
        if args[0] == "ready":
            return [self.candidate]
        if args[0] == "show":
            return [{"id": args[1], "status": "in_progress", "metadata": {"factory_project": self.config["state_dir"]}}]
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

    def test_independent_pilot_work_continues_while_another_job_is_blocked(self):
        job = self.store.enqueue("blocked-other", self.tmp.name, "factory/other", payload={"allowed_paths": ["docs/other.md"]})
        self.store.update(job["id"], stage="blocked", blocker="external_service_unavailable", resume_stage="implementing")
        with patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear", return_value=True):
            result = reconcile(self.config, self.store, self.fake_enqueue)
        self.assertEqual(result["status"], "task_claimed")
        self.assertEqual(self.store.get(job["id"])["stage"], "blocked")

    def test_overlapping_scopes_do_not_start_while_blocked(self):
        job = self.store.enqueue("blocked-other", self.tmp.name, "factory/other", payload={"allowed_paths": ["docs/pilot.md"]})
        self.store.update(job["id"], stage="blocked", blocker="external_service_unavailable", resume_stage="implementing")
        with patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear", return_value=True):
            result = reconcile(self.config, self.store, self.fake_enqueue)
        self.assertEqual(result["status"], "pilot_scope_or_independence_required")
        self.assertFalse(any("--claim" in call for call in self.calls))

    def test_product_question_survives_sync_and_resumes_after_answer(self):
        question_path = Path(self.tmp.name) / "question.json"
        write_json(question_path, {"question": "Which label should the document use? Recommend Local factory; alternative Development factory."})
        job = self.store.enqueue("question-task", self.tmp.name, "factory/question", payload={
            "requirements": "Write docs.", "question_path": str(question_path), "allowed_paths": ["docs/question.md"]})
        self.store.update(job["id"], stage="blocked", blocker="product_decision_required", resume_stage="implementing")
        issue = {"id": "question-task", "status": "in_progress", "metadata": {"factory_project": self.config["state_dir"]}, "dependencies": []}
        decision = {"id": "decision-1", "status": "open", "close_reason": ""}
        calls = []
        def fake(config, *args, json_result=False):
            calls.append(args)
            if args[0] == "show":
                return [issue if args[1] == issue["id"] else decision]
            if args[0] == "search":
                return []
            if args[0] == "create":
                return decision
            if args[:2] == ("dep", "add"):
                issue["dependencies"] = [{"id": decision["id"], "dependency_type": "blocks"}]
            if args[0] in {"list", "ready"}:
                return []
            return None
        with patch("factory.tracker.bd", side_effect=fake):
            reconcile(self.config, self.store, self.fake_enqueue)
            waiting = self.store.get(job["id"])
            self.assertEqual(waiting["stage"], "blocked")
            self.assertEqual(waiting["payload"]["decision_id"], "decision-1")
            decision.update(status="closed", close_reason="Use Local factory as the label.")
            reconcile(self.config, self.store, self.fake_enqueue)
        resumed = self.store.get(job["id"])
        self.assertEqual(resumed["stage"], "implementing")
        self.assertIn("Use Local factory as the label.", resumed["payload"]["requirements"])
        self.assertEqual(sum(call[0] == "create" for call in calls), 1)
        self.assertIn(("dep", "add", "question-task", "decision-1"), calls)

    def test_source_intake_is_explicit_and_directory_scope_is_preserved(self):
        self.candidate["metadata"]["factory_paths"] = ["src/module.py", "tests"]
        with patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear", return_value=True):
            denied = reconcile(self.config, self.store, self.fake_enqueue)
            self.assertEqual(denied["status"], "pilot_scope_or_independence_required")
            self.config["task_policy"] = "source"
            with patch.object(self, "fake_enqueue", wraps=self.fake_enqueue) as submit:
                result = reconcile(self.config, self.store, submit, task_id="pilot-1")
                self.assertEqual(result["status"], "task_claimed")
                self.assertEqual(submit.call_args.kwargs["allowed_paths"], ["src/module.py", "tests"])

    def test_exact_ticket_cannot_start_another_candidate(self):
        with patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear") as reviews:
            result = reconcile(self.config, self.store, self.fake_enqueue, task_id="another-1")
        self.assertEqual(result["status"], "ticket_not_ready_or_approved")
        reviews.assert_not_called()
        self.assertFalse(any("--claim" in call for call in self.calls))

    def test_existing_ticket_is_idempotent(self):
        job = self.store.enqueue("pilot-1", self.tmp.name, "factory/pilot-1")
        with patch("factory.tracker.bd") as tracker:
            result = reconcile(self.config, self.store, self.fake_enqueue, task_id="pilot-1")
        self.assertEqual(result["job_id"], job["id"])
        tracker.assert_not_called()

    def test_source_scope_rejects_private_or_ambiguous_paths(self):
        self.config["task_policy"] = "source"
        for path in (".", "../src", "/src", "src/../secrets", ".env", "data.sqlite3", ".beads", "a//b"):
            with self.subTest(path=path), patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear") as reviews:
                self.candidate["metadata"]["factory_paths"] = [path]
                result = reconcile(self.config, self.store, self.fake_enqueue, task_id="pilot-1")
                self.assertEqual(result["status"], "pilot_scope_or_independence_required")
                reviews.assert_not_called()

    def test_external_review_reserves_parent_directory_scope(self):
        self.config["task_policy"] = "source"
        self.candidate["metadata"]["factory_paths"] = ["src/module.py"]
        def fake(config, *args, **kwargs):
            if args == ("list", "--status=in_review"):
                return [{"id": "external", "metadata": {"factory_paths": ["src"]}}]
            return self.fake_bd(config, *args, **kwargs)
        with patch("factory.tracker.bd", side_effect=fake), patch("factory.tracker.reviews_clear") as reviews:
            result = reconcile(self.config, self.store, self.fake_enqueue)
        self.assertEqual(result["status"], "pilot_scope_or_independence_required")
        reviews.assert_not_called()

    def test_external_active_task_reserves_its_scope(self):
        self.config["task_policy"] = "source"
        self.candidate["metadata"]["factory_paths"] = ["src/module.py"]
        for scopes in (["src"], None):
            with self.subTest(scopes=scopes):
                def fake(config, *args, **kwargs):
                    if args == ("list", "--status=in_progress"):
                        return [{"id": "external", "metadata": {"factory_paths": scopes}}]
                    return self.fake_bd(config, *args, **kwargs)
                with patch("factory.tracker.bd", side_effect=fake), patch("factory.tracker.reviews_clear") as reviews:
                    result = reconcile(self.config, self.store, self.fake_enqueue)
                self.assertEqual(result["status"], "pilot_scope_or_independence_required")
                reviews.assert_not_called()

    def test_manual_e2e_review_does_not_block_independent_work(self):
        def fake(config, *args, **kwargs):
            if args == ("list", "--status=in_review"):
                return [{"id": "manual", "labels": ["manual-e2e"]}]
            return self.fake_bd(config, *args, **kwargs)
        with patch("factory.tracker.bd", side_effect=fake), patch("factory.tracker.reviews_clear", return_value=True):
            self.assertEqual(reconcile(self.config, self.store, self.fake_enqueue)["status"], "task_claimed")

    def test_exact_submit_does_not_resume_other_answered_decision(self):
        job = self.store.enqueue("other-1", self.tmp.name, "factory/other", payload={"allowed_paths": ["docs/other.md"]})
        self.store.update(job["id"], stage="blocked", blocker="product_decision_required", resume_stage="implementing")
        with patch("factory.tracker.bd", side_effect=self.fake_bd), patch("factory.tracker.reviews_clear", return_value=True), \
                patch("factory.tracker.sync_product_decision") as resume:
            result = reconcile(self.config, self.store, self.fake_enqueue, task_id="pilot-1")
        self.assertEqual(result["status"], "task_claimed")
        self.assertEqual(self.store.get(job["id"])["stage"], "blocked")
        resume.assert_not_called()


if __name__ == "__main__":
    unittest.main()
