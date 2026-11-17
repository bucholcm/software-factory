from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factory.maintenance_tracker import sync_findings


class MaintenanceTrackerTests(unittest.TestCase):
    def test_repeated_scan_is_deduplicated_and_updated_without_raw_output(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"state_dir": folder, "repo": folder}
            Path(folder, "maintenance").mkdir()
            report = {"status": "findings", "revision": "a" * 40, "checks": [
                {"category": "npm_dependencies", "tool": "npm", "status": "findings",
                 "findings": [{"package": "synthetic-package", "id": "CVE-2026-0000"}]}]}
            calls = []
            def bd(config, *args, json_result=False):
                calls.append(args)
                if args[0] == "search":
                    return []
                if args[0] == "create":
                    return {"id": "maintenance-1"}
            with patch("factory.maintenance_tracker.git", return_value="a" * 40), patch("factory.maintenance_tracker.load_report", return_value=report):
                sync_findings(config, bd)
                sync_findings(config, bd)
                self.assertEqual(sum(call[0] == "create" for call in calls), 1)
                self.assertEqual(sum(call[0] == "update" for call in calls), 1)
                report["revision"] = "b" * 40
                sync_findings(config, bd)
                self.assertEqual(sum(call[0] == "create" for call in calls), 1)
                self.assertEqual(sum(call[0] == "update" for call in calls), 2)

    def test_stale_scan_never_creates_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            bd = __import__("unittest.mock").mock.Mock()
            with patch("factory.maintenance_tracker.git", return_value="a" * 40), patch("factory.maintenance_tracker.load_report", return_value={"status": "stale", "checks": []}):
                sync_findings({"state_dir": folder, "repo": folder}, bd)
            bd.assert_not_called()
