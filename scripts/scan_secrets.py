"""Fail-closed, redacted full-history secret scan for public-release checks."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile


def scan(repo, executable="gitleaks"):
    with tempfile.TemporaryDirectory(prefix="factory-secret-scan-") as folder:
        report = Path(folder) / "redacted.json"
        report.touch(mode=0o600)
        try:
            result = subprocess.run(
                [executable, "git", str(Path(repo).resolve()), "--log-opts=--all", "--redact=100",
                 "--no-banner", "--report-format=json", "--report-path", str(report)],
                capture_output=True, text=True, timeout=180,
            )
        except FileNotFoundError:
            return {"status": "error", "error": "scanner_missing"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "scan_timeout"}
        except OSError:
            return {"status": "error", "error": "scanner_failed"}
        if result.returncode not in {0, 1}:
            return {"status": "error", "error": "scanner_failed"}
        try:
            if report.stat().st_size > 16 * 1024 * 1024:
                raise ValueError
            values = json.loads(report.read_text())
            if not isinstance(values, list):
                raise ValueError
            rules = set()
            for value in values:
                if (not isinstance(value, dict) or value.get("Secret") != "REDACTED"
                        or not isinstance(value.get("RuleID"), str)
                        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value["RuleID"])):
                    raise ValueError
                rules.add(value["RuleID"])
            if bool(values) != (result.returncode == 1):
                raise ValueError
        except (OSError, ValueError, TypeError):
            return {"status": "error", "error": "invalid_scanner_report"}
        # Never forward scanner stdout/stderr, raw matches, source or file paths.
        return {"status": "findings" if values else "clean", "count": len(values), "rules": sorted(rules)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = scan(args.repo)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["status"] == "clean" else 1)


if __name__ == "__main__":
    main()
