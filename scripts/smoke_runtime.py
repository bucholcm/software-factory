"""An actual isolated model/runtime pipeline smoke test; no application data."""
import json
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
    package = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="factory-live-smoke-") as folder:
        root = Path(folder)
        repo = root / "repo"
        repo.mkdir()
        for args in (["init", "-b", "main"], ["config", "user.name", "Factory Smoke"],
                     ["config", "user.email", "smoke@local.invalid"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        (repo / "README.md").write_text("# Synthetic factory runtime test\n")
        for args in (["add", "README.md"], ["commit", "-m", "synthetic initial"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        path = root / "config.json"
        subprocess.run([__import__("sys").executable, "-m", "factory", "--config", str(path), "init", "--repo", str(repo),
                        "--state-dir", str(root / "state")], cwd=package, check=True, capture_output=True)
        config = load(path)
        config.update(tracker_enabled=False, poll_seconds=1, agent_timeout_seconds=240, job_timeout_seconds=300)
        write_json(path, config)
        job = enqueue(config, "synthetic-runtime-smoke", "Create docs/probe.md containing a short sentence confirming an isolated local software-factory runtime smoke test. Do not change other files.", acceptance="docs/probe.md exists and contains only generic synthetic information.")
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
                    assert (repo / "docs/probe.md").is_file()
                    print(json.dumps({"status": "passed", "reviewed_revision": current["reviewed_revision"], "checkpoint_count": len(list((root / "state/backups").iterdir()))}), flush=True)
                    return
                if current["stage"] in {"blocked", "failed"}:
                    raise RuntimeError("smoke_pipeline_" + current["blocker"])
                time.sleep(1)
            raise RuntimeError("smoke_pipeline_timeout")


if __name__ == "__main__":
    main()
