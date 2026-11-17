from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from factory.maintenance import load_report, scan


class FakeRunner:
    def __init__(self, outputs=None):
        self.outputs = outputs or {}
        self.calls = []
        self.snapshots = []

    def __call__(self, argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        self.calls.append(argv)
        self.snapshots.append(sorted(str(path.relative_to(kwargs["cwd"])) for path in Path(kwargs["cwd"]).rglob("*") if path.is_file()))
        self.assert_safe(kwargs)
        key = (argv[0], argv[1] if argv[0] in {"npm", "trivy"} else "")
        value = self.outputs.get(key, FileNotFoundError())
        if isinstance(value, Exception):
            raise value
        code, payload = value
        if argv[0] == "gitleaks":
            Path(argv[argv.index("--report-path") + 1]).write_text(json.dumps(payload))
            return subprocess.CompletedProcess(argv, code, "PRIVATE untrusted stdout", "PRIVATE STDERR TOKEN")
        return subprocess.CompletedProcess(argv, code, json.dumps(payload), "PRIVATE STDERR TOKEN")

    @staticmethod
    def assert_safe(kwargs):
        assert kwargs["shell"] is False
        assert kwargs["timeout"] > 0
        assert kwargs["capture_output"] is True


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.output = Path(self.temporary.name) / "reports"
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic")
        self.git("config", "user.email", "synthetic@example.invalid")

    def git(self, *argv):
        return subprocess.run(["git", *argv], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, files):
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", "--", *files)
        self.git("commit", "-qm", "Synthetic fixture")

    def category(self, report, category):
        return [check for check in report["checks"] if check["category"] == category]

    def test_python_vulnerabilities_are_allowlisted_and_private_files_never_scanned(self):
        self.commit({"requirements.txt": "example==1.0\n", "main.py": "print('synthetic')\n", ".env": "PRIVATE tracked secret", "data.sqlite3": "PRIVATE database"})
        (self.repo / "private.py").write_text("PRIVATE untracked source")
        runner = FakeRunner({("pip-audit", ""): (1, {"dependencies": [{"name": "example", "version": "1.0", "vulns": [{"id": "CVE-2025-12345", "description": "PRIVATE SOURCE TOKEN"}]}]}),
                             ("bandit", ""): (0, {"results": [], "errors": []})})
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(report["status"], "findings")
        check = self.category(report, "python_resolved")[0]
        self.assertEqual(check["findings"], [{"package": "example", "id": "CVE-2025-12345"}])
        self.assertNotIn("PRIVATE", (self.output / "maintenance.json").read_text())
        for files in runner.snapshots:
            self.assertNotIn(".env", files)
            self.assertNotIn("data.sqlite3", files)
            self.assertNotIn("private.py", files)
        self.assertIn("--no-deps", runner.calls[0])
        self.assertIn("--disable-pip", runner.calls[0])

    def test_npm_root_and_app_locks_audited_without_scripts_and_outdated_reported(self):
        self.commit({"package.json": "{}", "package-lock.json": "{}", "app/package.json": "{}", "app/package-lock.json": "{}"})
        runner = FakeRunner({("npm", "audit"): (0, {"vulnerabilities": {}}),
                             ("npm", "outdated"): (1, {"example": {"latest": "2", "current": "1", "path": "PRIVATE PATH"}})})
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(len(self.category(report, "npm_dependencies")), 2)
        self.assertEqual(len(self.category(report, "npm_outdated")), 2)
        self.assertTrue(all(check["status"] == "findings" for check in self.category(report, "npm_outdated")))
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertTrue(all("--ignore-scripts" in call and "--package-lock-only" in call for call in runner.calls if call[0] == "npm"))

    def test_npm_advisory_url_is_reduced_to_stable_id(self):
        self.commit({"package.json": "{}", "package-lock.json": "{}"})
        runner = FakeRunner({("npm", "audit"): (1, {"vulnerabilities": {"example": {"via": [{"url": "https://github.com/advisories/GHSA-abcd-1234-5678?token=PRIVATE", "title": "PRIVATE source"}]}}}),
                             ("npm", "outdated"): (0, {})})
        check = self.category(scan(self.repo, self.output, runner=runner), "npm_dependencies")[0]
        self.assertEqual(check["findings"], [{"package": "example", "id": "GHSA-abcd-1234-5678"}])

    def test_absent_docker_is_not_applicable_and_missing_code_scanner_unavailable(self):
        self.commit({"main.py": "pass\n"})
        report = scan(self.repo, self.output, runner=FakeRunner())
        self.assertEqual(self.category(report, "docker_image")[0]["status"], "not_applicable")
        self.assertEqual(self.category(report, "code_security")[0]["status"], "unavailable")
        self.assertEqual(report["status"], "unavailable")

    def test_docker_inventory_images_and_configuration_are_scanned(self):
        self.commit({"Dockerfile": "FROM python:3.11 AS build\nFROM build AS final\n", "compose.yaml": "services:\n  app:\n    image: alpine:3\n"})
        runner = FakeRunner({("trivy", "config"): (0, {"Results": []}),
                             ("trivy", "image"): (1, {"Results": [{"Vulnerabilities": [{"PkgName": "openssl", "VulnerabilityID": "CVE-2025-12345", "Description": "PRIVATE"}]}]})})
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(self.category(report, "docker_inventory")[0]["count"], 2)
        self.assertEqual(len(self.category(report, "docker_image")), 2)
        self.assertEqual({call[-1] for call in runner.calls if call[1] == "image"}, {"python:3.11", "alpine:3"})
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_unresolved_images_are_incomplete_not_clean(self):
        self.commit({"Dockerfile": "ARG BASE\nFROM ${BASE}\n"})
        runner = FakeRunner({("trivy", "config"): (0, {"Results": []})})
        check = self.category(scan(self.repo, self.output, runner=runner), "docker_image")[0]
        self.assertEqual(check["status"], "unavailable")
        self.assertEqual(check["error_code"], "unresolved_image_reference")

    def test_scanner_errors_timeouts_and_invalid_results_cannot_be_clean(self):
        self.commit({"main.py": "pass\n"})
        for value, code in [((2, {"results": [], "errors": []}), "scan_failed"),
                            ((1, {"results": [], "errors": []}), "invalid_scan_result"),
                            ((0, {"results": [], "errors": ["PRIVATE"]}), "invalid_scan_result"),
                            (subprocess.TimeoutExpired(["bandit"], 1, output="PRIVATE"), "scan_timeout")]:
            with self.subTest(code=code):
                report = scan(self.repo, self.output, runner=FakeRunner({("bandit", ""): value}))
                self.assertEqual(report["status"], "error")
                self.assertEqual(self.category(report, "code_security")[0]["error_code"], code)
                self.assertNotIn("PRIVATE", json.dumps(report))

    def test_failed_rescan_replaces_prior_clean_report(self):
        self.commit({"package.json": "{}", "package-lock.json": "{}"})
        clean = scan(self.repo, self.output, runner=FakeRunner({("npm", "audit"): (0, {"vulnerabilities": {}}), ("npm", "outdated"): (0, {}), ("gitleaks", ""): (0, [])}))
        self.assertEqual(clean["status"], "clean")
        scan(self.repo, self.output, runner=FakeRunner({("npm", "audit"): (2, {})}))
        self.assertEqual(load_report(self.output)["status"], "error")

    def test_age_or_revision_mismatch_marks_report_stale(self):
        self.commit({"main.py": "pass\n"})
        report = scan(self.repo, self.output, runner=FakeRunner())
        self.assertEqual(load_report(self.output, revision="a" * 40)["status"], "stale")
        report["last_run"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        (self.output / "maintenance.json").write_text(json.dumps(report))
        self.assertEqual(load_report(self.output)["status"], "stale")
        self.assertEqual(load_report(self.output, max_age_seconds=3 * 86400)["status"], "unavailable")

    def test_manifest_directives_cannot_read_outside_snapshot(self):
        self.commit({"requirements.txt": "-r /PRIVATE/secret.txt\n"})
        runner = FakeRunner()
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(report["status"], "unavailable")
        self.assertFalse([call for call in runner.calls if call[0] == "pip-audit"])
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_declared_pinned_dependencies_are_audited_and_unpinned_are_visible(self):
        self.commit({"pyproject.toml": '[project]\nname="synthetic"\nversion="1"\ndependencies=["example==1.0"]\n'})
        runner = FakeRunner({("pip-audit", ""): (0, {"dependencies": [{"name": "example", "vulns": []}]})})
        self.assertEqual(self.category(scan(self.repo, self.output, runner=runner), "python_declared")[0]["status"], "clean")
        self.commit({"pyproject.toml": '[project]\nname="synthetic"\ndependencies=["example>=1"]\n'})
        runner = FakeRunner()
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(report["status"], "unavailable")
        self.assertFalse([call for call in runner.calls if call[0] == "pip-audit"])

    def test_inventory_failure_overwrites_prior_report_without_error_text(self):
        self.commit({"README.md": "synthetic"})
        scan(self.repo, self.output, runner=FakeRunner())
        def failed(*args, **kwargs):
            raise OSError("PRIVATE")
        report = scan(self.repo, self.output, runner=failed)
        self.assertEqual(report["status"], "error")
        self.assertEqual(load_report(self.output)["status"], "error")
        self.assertNotIn("PRIVATE", (self.output / "maintenance.json").read_text())

    def test_resolved_lock_uses_pinned_packages_without_loading_local_project(self):
        self.commit({"uv.lock": 'version=1\n[[package]]\nname="synthetic-local"\nversion="1"\nsource={editable="."}\n[[package]]\nname="example"\nversion="1.2"\nsource={registry="https://registry.invalid"}\n'})
        runner = FakeRunner({("pip-audit", ""): (0, {"dependencies": [{"name": "example", "vulns": []}]})})
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(self.category(report, "python_resolved")[0]["status"], "clean")
        self.assertEqual(len([call for call in runner.calls if call[0] == "pip-audit"]), 1)
        self.assertEqual(self.category(report, "python_transitive")[0]["status"], "clean")

    def test_skipped_dependency_or_empty_trivy_payload_is_never_clean(self):
        self.commit({"requirements.txt": "example==1.0\n", "Dockerfile": "FROM scratch\n"})
        runner = FakeRunner({("pip-audit", ""): (0, {"dependencies": [{"name": "example", "skip_reason": "PRIVATE"}]}),
                             ("trivy", "config"): (0, {})})
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(report["status"], "error")
        self.assertTrue(all(check["status"] == "error" for check in report["checks"] if check["category"] in {"python_resolved", "docker_configuration"}))

    def test_direct_dependency_audit_does_not_claim_transitive_coverage(self):
        self.commit({"pyproject.toml": '[project]\nname="synthetic"\nversion="1"\ndependencies=["example==1.0"]\n'})
        runner = FakeRunner({("pip-audit", ""): (0, {"dependencies": [{"name": "example", "vulns": []}]}),
                             ("gitleaks", ""): (0, [])})
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(self.category(report, "python_declared")[0]["status"], "clean")
        self.assertEqual(self.category(report, "python_transitive")[0]["status"], "unavailable")
        self.assertEqual(self.category(report, "python_transitive")[0]["error_code"], "complete_dependency_inventory_required")
        self.assertEqual(self.category(report, "python_versions")[0]["status"], "unavailable")
        self.assertEqual(report["status"], "unavailable")

    def test_docker_updates_remain_explicitly_incomplete_despite_clean_trivy(self):
        self.commit({"Dockerfile": "FROM scratch\n"})
        runner = FakeRunner({("trivy", "config"): (0, {"Results": []}), ("gitleaks", ""): (0, [])})
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(self.category(report, "docker_updates")[0]["status"], "unavailable")
        self.assertEqual(report["status"], "unavailable")

    def test_secret_scan_reports_only_redacted_rule_identifiers_and_deletes_temp_report(self):
        self.commit({"main.py": "pass\n"})
        runner = FakeRunner({("gitleaks", ""): (1, [{"RuleID": "generic-api-key", "Secret": "REDACTED", "Match": "PRIVATE source", "File": "PRIVATE path"}])})
        report = scan(self.repo, self.output, runner=runner)
        check = self.category(report, "secret_scan")[0]
        self.assertEqual(check["status"], "findings")
        self.assertEqual(check["findings"], [{"id": "GITLEAKS-generic-api-key"}])
        call = next(call for call in runner.calls if call[0] == "gitleaks")
        self.assertIn("--redact=100", call)
        self.assertIn("--no-git", call)
        self.assertFalse(Path(call[call.index("--report-path") + 1]).exists())
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertNotIn("PRIVATE", (self.output / "maintenance.json").read_text())

    def test_secret_scanner_missing_or_unredacted_results_never_produce_clean(self):
        self.commit({"main.py": "pass\n"})
        report = scan(self.repo, self.output, runner=FakeRunner())
        self.assertEqual(self.category(report, "secret_scan")[0]["status"], "unavailable")
        runner = FakeRunner({("gitleaks", ""): (1, [{"RuleID": "generic-api-key", "Secret": "PRIVATE"}])})
        report = scan(self.repo, self.output, runner=runner)
        self.assertEqual(self.category(report, "secret_scan")[0]["status"], "error")
        self.assertNotIn("PRIVATE", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
