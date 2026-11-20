"""Read-only discovery of factory configuration for the caller's Git project."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


def repository(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        raise ValueError("not_a_git_project")
    return Path(result.stdout.strip()).resolve()


def discover(project: Path, *, config: Path | None = None, home: Path | None = None) -> dict:
    project = project.expanduser().resolve()
    common = repository(project)
    home = (home or Path.home()).resolve()
    # Only known factory locations; never recursively scan the user's files.
    roots = [home / "Library/Application Support/SoftwareFactory",
             home / ".local/state/software-factory"]
    candidates = [config.expanduser().resolve()] if config else [common.parent / ".software-factory/config.json"]
    if not config:
        for root in roots:
            candidates.extend(root.glob("*/config.json"))
            candidates.extend(root.glob("projects/*/config.json"))
    matches = []
    for path in sorted(set(candidates)):
        if not path.is_file():
            if config:
                raise ValueError("configuration_not_found")
            continue
        try:
            value = json.loads(path.read_text())
            if repository(Path(value["repo"]).expanduser()) != common:
                if config:
                    raise ValueError("configuration_belongs_to_another_project")
                continue
            matches.append(str(path))
        except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
            if config:
                raise ValueError("configuration_invalid_or_project_mismatch") from None
    return {"project": str(project), "git_common_dir": str(common),
            "factory_root": str(Path(__file__).resolve().parents[3]),
            "configurations": matches,
            "status": "configured" if len(matches) == 1 else "choose_configuration" if matches else "configuration_required"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--action", choices=["inspect", "status", "submit", "run", "set-mode"], default="inspect")
    parser.add_argument("--task")
    parser.add_argument("--mode", choices=["documentation", "source-review", "source-integrate"])
    args = parser.parse_args()
    try:
        resolved = discover(args.project, config=args.config)
        if args.action == "inspect":
            print(json.dumps(resolved, indent=2))
            return
        if len(resolved["configurations"]) != 1:
            print(json.dumps(resolved, indent=2))
            sys.exit(1)
        argv = [sys.executable, "-m", "factory", "--config", resolved["configurations"][0], args.action]
        if args.action == "submit":
            if not args.task:
                parser.error("submit requires --task")
            argv += ["--task", args.task, "--project", resolved["project"]]
        if args.action == "set-mode":
            if not args.mode:
                parser.error("set-mode requires --mode")
            argv += [args.mode]
        sys.exit(subprocess.run(argv, cwd=resolved["factory_root"]).returncode)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
