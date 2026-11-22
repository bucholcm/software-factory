"""Create a synthetic Beads project for showing the workflow in Scotty.

Requires Git and bd. The destination must not exist. This never starts the
factory, calls a model, connects a Git remote or touches another Beads database.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess


def create(destination):
    destination = Path(destination).expanduser().absolute()
    destination.mkdir(parents=True, exist_ok=False)
    # Ignore inherited database/Git routing so this demo cannot target a real project.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "SHELL", "TERM"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["XDG_CONFIG_HOME"] = str(destination / ".demo-config")

    def run(argv):
        if argv[0] == "git":
            argv = ["git", "-c", "core.hooksPath=" + os.devnull, *argv[1:]]
        return subprocess.run(argv, cwd=destination, env=environment, check=True, capture_output=True, text=True).stdout

    def bd(*args):
        return run(["bd", "--actor=Factory Demo", *args])

    run(["git", "init", "-b", "main"])
    run(["git", "config", "user.name", "Factory Demo"])
    run(["git", "config", "user.email", "demo@local.invalid"])
    (destination / "README.md").write_text("# Factory workflow demo\n\nSynthetic tasks only. No model workers or private application data.\n")
    run(["git", "add", "README.md"])
    run(["git", "commit", "-m", "Initialize synthetic workflow demo"])
    bd("init", "--prefix=demo", "--non-interactive", "--skip-hooks", "--skip-agents")
    bd("prime")

    def issue(identifier, title, kind="task", *, parent=None, paths=None, labels=None, description="Synthetic workflow example."):
        args = ["create", "--title=" + title, "--type=" + kind, "--priority=1", "--json",
                "--description=" + description, "--acceptance=Demonstrate native Beads relationships without executing a factory job.",
                "--metadata=" + json.dumps({"synthetic_demo": True, **({"factory_paths": paths} if paths else {})})]
        if parent:
            args += ["--parent=" + parent]
        else:
            args += ["--id=" + identifier]
        if labels:
            args += ["--labels=" + ",".join(labels)]
        result = json.loads(bd(*args))
        if result["id"] != identifier:
            raise RuntimeError("demo_issue_identity_mismatch")

    epic, decision = "demo-a11", "demo-d01"
    settings, preview, connect, validation = [epic + "." + str(n) for n in range(1, 5)]
    issue(epic, "Ship a local project feature", "epic")
    issue(decision, "Decide the settings label", "decision", labels=["needs-product-decision"],
          description="Choose the visible settings label. Recommendation: Project settings. Alternative: Workspace settings. Blocks only the settings-dependent work.")
    issue(settings, "Implement settings panel", parent=epic,
          paths=["src/settings.py", "tests/test_settings.py"], labels=["factory-pilot", "ready-for-development"])
    issue(preview, "Add preview validation", parent=epic,
          paths=["src/preview.py", "tests/test_preview.py"], labels=["factory-pilot", "ready-for-development"],
          description="Independent work: its files and contracts do not overlap the blocked settings task.")
    issue(connect, "Connect preview to settings", parent=epic,
          paths=["src/integration.py", "tests/test_integration.py"], labels=["factory-pilot", "ready-for-development"])
    issue(validation, "Owner validates the completed flow", parent=epic, labels=["manual-e2e"],
          description="Periodic human product validation. Does not consume an implementation slot or block independent work.")
    for dependent, prerequisite in ((settings, decision), (connect, settings), (connect, preview), (validation, connect)):
        bd("dep", "add", dependent, prerequisite)
    bd("dep", "relate", decision, preview)
    bd("dep", "cycles")
    ready = json.loads(bd("ready", "--limit=0", "--json"))
    # Decisions may themselves be ready; only preview is eligible implementation.
    executable = [item["id"] for item in ready if {"factory-pilot", "ready-for-development"}.issubset(item.get("labels", []))]
    if executable != [preview]:
        raise RuntimeError("demo_dependency_validation_failed")
    manifest = {"project": str(destination.resolve()), "epic": epic, "decision": decision,
                "settings": settings, "preview": preview, "connect": connect, "validation": validation,
                "ready_implementation": executable, "factory_started": False}
    (destination / "demo-workflow.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(create(args.destination), indent=2))


if __name__ == "__main__":
    main()
