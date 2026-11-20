"""Install a reviewed local product revision and a per-user macOS service.

Run only after tests and independent review. This script does not merge or
restore application data. It never overwrites an existing product or service.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factory.config import git, write_json
from factory.service import repair_service


def run(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError("installation_step_failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--name", default="buchos")
    parser.add_argument("--install-scanners", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--non-implementation-issue", action="append", default=[])
    args = parser.parse_args()
    os.umask(0o077)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", args.name):
        raise ValueError("invalid_service_name")
    source = Path(__file__).resolve().parent.parent
    destination = args.destination.expanduser().resolve()
    state = args.state_dir.expanduser().resolve()
    repo = args.repo.expanduser().resolve()
    label = "dev.software-factory." + args.name
    plist_path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    if destination.exists() or plist_path.exists() or (state / "config.json").exists():
        raise ValueError("existing_installation_requires_explicit_upgrade")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    python = sys.executable
    tools = state / "tools"
    if args.install_scanners:
        run([python, "-m", "venv", str(tools)])
        run([str(tools / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
             "pip-audit>=2,<3", "bandit>=1.7,<2", "semgrep>=1,<2"])
        result = subprocess.run([str(tools / "bin/python"), "-m", "pip", "list", "--format=json"],
                                capture_output=True, text=True, check=True)
        (state / "scanner-versions.json").write_text(result.stdout)
        if not shutil.which("gitleaks") and shutil.which("brew"):
            run([shutil.which("brew"), "install", "gitleaks"])
    run(["git", "clone", "--no-hardlinks", "--branch", "main", str(source), str(destination)])
    run(["git", "remote", "remove", "origin"], cwd=destination)
    config_path = state / "config.json"
    run([python, "-m", "factory", "--config", str(config_path), "init", "--repo", str(repo), "--state-dir", str(state)], cwd=destination)
    config = json.loads(config_path.read_text())
    config["non_implementation_issue_ids"] = args.non_implementation_issue
    app_python = repo / ".venv/bin/python"
    config["maintenance_python_environment"] = str(app_python) if app_python.is_file() else None
    if args.install_scanners:
        config["maintenance_commands"] = {"python": [str(tools / "bin/python"), "-m", "pip_audit"],
                                           "code": [str(tools / "bin/python"), "-m", "bandit"],
                                           "javascript_code": [str(tools / "bin/semgrep")]}
    write_json(config_path, config)
    result = repair_service(destination, config_path, expected_revision=git(destination, "rev-parse", "HEAD"),
                            python=python, name=args.name, activate=args.start)
    print(json.dumps({**result, "product": str(destination), "config": str(config_path), "state": str(state)}))


if __name__ == "__main__":
    main()
