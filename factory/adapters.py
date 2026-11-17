"""Agent runtimes and deterministic test gates, separate from reconciliation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .config import command, git, load, write_json


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
        "review_result": {"type": "string", "enum": ["approved", "changes_requested", "not_applicable"]},
        "question": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"file": {"type": "string"}, "line": {"type": "integer"},
                           "detail": {"type": "string"}}, "required": ["file", "line", "detail"]}},
    }, "required": ["status", "review_result", "findings", "question"],
}


def safe_changed_paths(repo):
    tracked = command(["git", "diff", "--name-only", "-z", "HEAD"], cwd=repo)
    new = command(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=repo)
    if tracked.returncode or new.returncode:
        raise RuntimeError("change_inventory_failed")
    paths = [p for p in (tracked.stdout + new.stdout).split("\0") if p]
    for name in paths:
        parts = Path(name).parts
        if (Path(name).is_absolute() or ".." in parts or any(p.startswith(".env") for p in parts)
                or any(p in {".beads", ".buchos", ".dolt", ".git", "node_modules", ".venv"} for p in parts)
                or Path(name).suffix.lower() in {".sqlite3", ".sqlite", ".db", ".pem", ".key"}
                or (Path(repo) / name).is_symlink()):
            raise RuntimeError("protected_path_changed")
    return sorted(set(paths))


def documentation_path(name):
    path = Path(name)
    return (not path.is_absolute() and ".." not in path.parts and name.endswith(".md")
            and path.name not in {"AGENTS.md", "CLAUDE.md", "SKILL.md"}
            and not any(part in {".agents", ".codex", ".opencode", ".github", "skills"} for part in path.parts))


def is_documentation_change(repo, base):
    files = git(repo, "diff", "--name-only", base, "HEAD").splitlines()
    return bool(files) and all(documentation_path(name) for name in files)


def runtime_environment():
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "TERM", "SHELL",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def builder(config_path):
    def build(job, result_path):
        request = result_path.parent / "request.json"
        write_json(request, {"job": job, "config": str(config_path), "result": str(result_path)})
        return {"argv": [sys.executable, "-m", "factory.adapters", "--request", str(request)],
                "cwd": str(Path(__file__).resolve().parent.parent)}
    return build


def verifier(job, result):
    worktree = job["worktree"]
    if git(worktree, "rev-parse", "HEAD") != result.get("revision", job.get("revision")):
        return False
    if git(worktree, "status", "--porcelain"):
        return False
    if job["stage"] == "testing":
        receipt = Path(job["attempt_dir"]) / "gate-receipt.json"
        value = json.loads(receipt.read_text())
        if value != {"revision": job["revision"], "passed": True}:
            return False
    if job["stage"] == "reviewing":
        if result.get("reviewed_revision") != job["revision"]:
            return False
    return result


def _prompt(job):
    review = job["stage"] == "reviewing"
    lines = [
        "You are an independent reviewer." if review else "Implement this approved task in the isolated worktree.",
        "Read repository instructions. Financial providers stay read-only; imports require human approval.",
        "Never read local private databases, .env, secrets, provider credentials or real financial state.",
        "Do not mutate Beads, commit, push, merge, launch other agents, change permissions, or start services.",
        "The supervisor owns commits, quality gates, independent review and integration.",
        "Return the requested JSON only. Do not place private data or credentials in output.",
        "Use status blocked only for a genuine human product decision. Include the concrete question,",
        "recommendation and alternatives in question. For completed results question must be an empty string.",
        "Repair technical failures yourself within scope or return failed; do not turn them into product questions.",
        "Task: " + job["task_id"], "Requirements: " + job["payload"].get("requirements", ""),
        "Acceptance: " + job["payload"].get("acceptance", ""),
    ]
    if job["payload"].get("allowed_paths"):
        lines += ["You may change only these files: " + ", ".join(job["payload"]["allowed_paths"])]
    if review:
        lines += ["Read-only review of exact SHA " + job["revision"],
                  "Compare against " + job["payload"]["base_revision"] + ". Check requirements, diff, regressions and privacy.",
                  "Do not edit files. Report actionable findings with relative file, line and concise explanation.",
                  "Set review_result approved or changes_requested. Approval requires no actionable findings."]
    elif job["stage"] == "fixing":
        if job.get("outcome", {}).get("blocker") == "quality_gate_failed":
            lines += ["The deterministic quality gate failed. Reproduce the repository quality gates on synthetic data,",
                      "fix the technical failure, and add a relevant regression test. Do not weaken checks."]
        findings = Path(job["payload"]["findings_path"])
        if findings.exists():
            lines += ["Fix these review findings:", findings.read_text()]
    else:
        lines += ["Set review_result not_applicable; findings may be empty."]
    return "\n".join(lines)


def _agent(job, config, result_path):
    review = job["stage"] == "reviewing"
    raw_path = result_path.parent / "native-result.json"
    schema_path = result_path.parent / "schema.json"
    write_json(schema_path, SCHEMA)
    prompt = _prompt(job)
    env = runtime_environment()
    runtime = config.get("review_runtime", job["runtime"]) if review else job["runtime"]
    if runtime == "codex":
        argv = [config["codex_executable"], "exec", "--ephemeral", "--sandbox",
                "read-only" if review else "workspace-write", "-c", 'approval_policy="never"',
                "--output-schema", str(schema_path), "--output-last-message", str(raw_path), "-"]
        try:
            run = subprocess.run(argv, cwd=job["worktree"], input=prompt, text=True, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=config.get("agent_timeout_seconds", 900))
            if run.returncode or not raw_path.exists():
                raise RuntimeError("agent_execution_failed")
            response = json.loads(raw_path.read_text())
        finally:
            raw_path.unlink(missing_ok=True)
    else:
        # OpenCode's planning agent is read-only. Implementation remains disabled
        # until this installation has a verified sandbox profile.
        if not review:
            raise RuntimeError("opencode_implementation_sandbox_unconfigured")
        argv = [config["opencode_executable"], "run", "--agent", "plan", "--format", "json",
                prompt + "\nResponse schema: " + json.dumps(SCHEMA)]
        run = subprocess.run(argv, cwd=job["worktree"], text=True, env=env, capture_output=True,
                             timeout=config.get("agent_timeout_seconds", 900))
        if run.returncode:
            raise RuntimeError("agent_execution_failed")
        texts = []
        for line in run.stdout.splitlines():
            try:
                item = json.loads(line)
                if item.get("type") == "text":
                    texts.append(item.get("part", {}).get("text", ""))
            except ValueError:
                continue
        response = json.loads("".join(texts).strip().removeprefix("```json").removesuffix("```").strip())
    if not isinstance(response, dict) or response.get("status") not in {"completed", "blocked", "failed"}:
        raise RuntimeError("invalid_agent_result")
    if response["status"] != "completed":
        if response["status"] == "blocked":
            question = response.get("question", "")
            if (not isinstance(question, str) or not 10 <= len(question) <= 2000
                    or re.search(r"(?i)(api[_ -]?key|secret|password|bearer|token)\s*[:=]", question)):
                raise RuntimeError("invalid_product_question")
            write_json(Path(job["payload"]["question_path"]), {"question": question})
        write_json(result_path, {"status": response["status"], "blocker": "product_decision_required"
                                if response["status"] == "blocked" else "agent_execution_failed"})
        return
    if review:
        if git(job["worktree"], "status", "--porcelain"):
            raise RuntimeError("reviewer_modified_worktree")
        verdict = response.get("review_result")
        findings = response.get("findings", [])
        if verdict not in {"approved", "changes_requested"} or not isinstance(findings, list):
            raise RuntimeError("invalid_review")
        if verdict == "approved" and findings:
            raise RuntimeError("inconsistent_review")
        safe = []
        for item in findings[:30]:
            path = str(item.get("file", ""))
            detail = str(item.get("detail", ""))[:1500]
            if (not path or Path(path).is_absolute() or ".." in Path(path).parts
                    or not git(job["worktree"], "ls-files", "--", path)
                    or re.search(r"(?i)(api[_ -]?key|secret|password|bearer|token)\s*[:=]", detail)):
                raise RuntimeError("unsafe_review_finding")
            safe.append({"file": path, "line": max(1, int(item.get("line", 1))), "detail": detail})
        write_json(Path(job["payload"]["findings_path"]), safe)
        write_json(result_path, {"status": "completed", "revision": job["revision"],
                                "reviewed_revision": job["revision"], "review_result": verdict})
    else:
        paths = safe_changed_paths(job["worktree"])
        allowed = job["payload"].get("allowed_paths")
        if allowed and any(path not in allowed for path in paths):
            raise RuntimeError("task_scope_exceeded")
        if paths:
            git(job["worktree"], "add", "--", *paths)
            git(job["worktree"], "-c", "user.name=Software Factory", "-c", "user.email=factory@local.invalid",
                "commit", "-m", f"feat: implement {job['task_id']}")
        write_json(result_path, {"status": "completed", "revision": git(job["worktree"], "rev-parse", "HEAD")})


def _gates(job, config, result_path):
    revision = git(job["worktree"], "rev-parse", "HEAD")
    if revision != job["revision"] or git(job["worktree"], "status", "--porcelain"):
        raise RuntimeError("revision_changed")
    if is_documentation_change(job["worktree"], job["payload"]["base_revision"]):
        gates = [["git", "diff", "--check", job["payload"]["base_revision"], "HEAD"]]
    else:
        gates = config["quality_gates"]
    if not gates:
        raise RuntimeError("quality_gates_missing")
    for argv in gates:
        gate_state = result_path.parent / "synthetic-gate-state"
        gate_state.mkdir(mode=0o700, exist_ok=True)
        env = runtime_environment()
        env.update(BUCHOS_STATE_DIR=str(gate_state), BUCHOS_LIFE_WIKI_VAULT_DIR=str(gate_state / "vault"))
        result = subprocess.run(argv, cwd=job["worktree"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env,
                                timeout=config.get("gate_timeout_seconds", 1800))
        if result.returncode:
            raise RuntimeError("quality_gate_failed")
    if git(job["worktree"], "rev-parse", "HEAD") != revision or git(job["worktree"], "status", "--porcelain"):
        raise RuntimeError("gate_changed_worktree")
    write_json(result_path.parent / "gate-receipt.json", {"revision": revision, "passed": True})
    write_json(result_path, {"status": "completed", "revision": revision, "tests_passed": True})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text())
    job, config, result_path = request["job"], load(request["config"]), Path(request["result"])
    try:
        if job["stage"] == "testing":
            _gates(job, config, result_path)
        else:
            _agent(job, config, result_path)
    except Exception as error:
        code = str(error)
        if not re.fullmatch(r"[a-z_]{1,80}", code):
            code = "runtime_execution_failed"
        write_json(result_path, {"status": "failed", "blocker": code})


if __name__ == "__main__":
    main()
