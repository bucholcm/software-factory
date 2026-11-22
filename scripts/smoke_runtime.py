"""An actual isolated model/runtime pipeline smoke test; no application data."""
import json
import argparse
from pathlib import Path
import subprocess
import tempfile
import time

from factory.adapters import builder, verifier
from factory.cli import enqueue
from factory.config import load, write_json
from factory.integration import integrate_ready
from factory.supervisor import Supervisor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--environment", action="store_true", help="Prepare a fresh explicit Python environment before the model")
    parser.add_argument("--security-python", type=Path, help="Python with Bandit installed, required for --source")
    options = parser.parse_args()
    if options.source and not options.security_python:
        parser.error("--source requires --security-python (an interpreter with Bandit installed)")
    package = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="factory-live-smoke-") as folder:
        root = Path(folder)
        repo = root / "repo"
        repo.mkdir()
        for args in (["init", "-b", "main"], ["config", "user.name", "Factory Smoke"],
                     ["config", "user.email", "smoke@local.invalid"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        (repo / "README.md").write_text("# Synthetic factory runtime test\n")
        (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n.venv/\n")
        for args in (["add", "README.md", ".gitignore"], ["commit", "-m", "synthetic initial"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        path = root / "config.json"
        subprocess.run([__import__("sys").executable, "-m", "factory", "--config", str(path), "init", "--repo", str(repo),
                        "--state-dir", str(root / "state")], cwd=package, check=True, capture_output=True)
        config = load(path)
        config.update(tracker_enabled=False, poll_seconds=1, agent_timeout_seconds=240, job_timeout_seconds=300)
        if options.environment:
            config["environment"] = {"python": __import__("sys").executable,
                                     "fingerprint_files": ["README.md"],
                                     "checks": [["{venv_python}", "-I", "-c", "import unittest"]]}
        if options.source:
            config.update(task_policy="source", integration_policy="reviewed_code",
                          integration_checkout_isolated=True,
                          security_gates=[[str(options.security_python.expanduser().absolute()), "-m", "bandit", "-q", "calculator.py"]],
                          quality_gates=[[__import__("sys").executable, "-m", "unittest", "discover", "-s", "tests"]])
        write_json(path, config)
        requirements = ("Create calculator.py defining add(a, b) returning the sum. Create tests/test_calculator.py with unittest cases for positive, negative and zero integers. Do not change other files."
                        if options.source else "Create docs/probe.md containing a short sentence confirming an isolated local software-factory runtime smoke test. Do not change other files.")
        job = enqueue(config, "synthetic-runtime-smoke", requirements,
                      acceptance="The requested files exist, are correct and contain only synthetic information.",
                      allowed_paths=["calculator.py", "tests"] if options.source else ["docs/probe.md"])
        if options.restart:
            with Supervisor(config, builder(path), verifier) as first:
                first.tick()
                owned = first.store.get(job["id"])
                assert owned["pid"] and owned["attempt"] == 1
            print(json.dumps({"restart": "supervisor_recreated_with_existing_worker", "pid": owned["pid"]}), flush=True)
        with Supervisor(config, builder(path), verifier) as supervisor:
            deadline = time.time() + 600
            previous = None
            while time.time() < deadline:
                supervisor.tick()
                integrate_ready(supervisor, config)
                current = supervisor.store.get(job["id"])
                if current["stage"] != previous:
                    print(json.dumps({"stage": current["stage"], "blocker": current["blocker"]}), flush=True)
                    previous = current["stage"]
                if current["stage"] == "integrated":
                    assert (repo / ("calculator.py" if options.source else "docs/probe.md")).is_file()
                    print(json.dumps({"status": "passed", "reviewed_revision": current["reviewed_revision"], "checkpoint_count": len(list((root / "state/backups").iterdir()))}), flush=True)
                    return
                if current["stage"] in {"blocked", "failed"}:
                    raise RuntimeError("smoke_pipeline_" + current["blocker"])
                time.sleep(1)
            raise RuntimeError("smoke_pipeline_timeout")


if __name__ == "__main__":
    main()
