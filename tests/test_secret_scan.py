import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from scripts.scan_secrets import scan


class SecretScanTests(unittest.TestCase):
    def scanner(self, findings, code):
        def run(argv, **kwargs):
            Path(argv[argv.index("--report-path") + 1]).write_text(json.dumps(findings))
            return subprocess.CompletedProcess(argv, code, stdout="synthetic-private-output", stderr="synthetic-private-error")
        return run

    def test_findings_are_reduced_to_rule_ids_without_raw_fields(self):
        finding = {"RuleID": "generic-api-key", "Secret": "REDACTED", "Match": "synthetic-private-match",
                   "File": "private-path", "Commit": "synthetic-private-commit"}
        with patch("scripts.scan_secrets.subprocess.run", side_effect=self.scanner([finding], 1)):
            result = scan(Path.cwd())
        self.assertEqual(result, {"status": "findings", "count": 1, "rules": ["generic-api-key"]})
        self.assertNotIn("private", json.dumps(result))

    def test_missing_or_failed_scanner_does_not_report_clean(self):
        with patch("scripts.scan_secrets.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(scan(Path.cwd())["status"], "error")
        with patch("scripts.scan_secrets.subprocess.run", side_effect=self.scanner([], 2)):
            self.assertEqual(scan(Path.cwd())["status"], "error")

    def test_unredacted_or_inconsistent_report_is_an_error(self):
        for values, code in (([{"RuleID": "test", "Secret": "unredacted"}], 1), ([], 1),
                             ([{"RuleID": "test", "Secret": "REDACTED"}], 0)):
            with self.subTest(values=values, code=code), patch("scripts.scan_secrets.subprocess.run", side_effect=self.scanner(values, code)):
                self.assertEqual(scan(Path.cwd())["status"], "error")

    def test_success_requires_zero_exit_and_empty_valid_report(self):
        with patch("scripts.scan_secrets.subprocess.run", side_effect=self.scanner([], 0)):
            self.assertEqual(scan(Path.cwd()), {"status": "clean", "count": 0, "rules": []})


if __name__ == "__main__":
    unittest.main()
