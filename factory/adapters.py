"""Agent runtimes and deterministic test gates, separate from reconciliation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .config import command, git, load, write_json
from .scope import verify_paths
from .security import policy as security_policy, receipt_path
from .environment import ExecutionFailure, prepare
from .usage import Collector, opencode_session_model, run_agent


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
    verify_paths(repo, paths)
    return sorted(set(paths))


def documentation_path(name):
    path = Path(name)
    ordinary = name in {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "SUPPORT.md", "CODE_OF_CONDUCT.md"} or name.startswith("docs/")
    return (ordinary and not path.is_absolute() and ".." not in path.parts and name.endswith(".md")
            and path.name not in {"AGENTS.md", "CLAUDE.md", "SKILL.md"}
            and not any(part in {".agents", ".codex", ".opencode", ".github", ".claude", ".cursor",
                                 ".windsurf", "skills", "agents", "commands", "command", "prompts", "rules"}
                        for part in path.parts))


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
        "The supervisor prepares configured dependencies before this session. Do not install or upgrade dependencies.",
        "Use targeted offline checks; report missing tools as a failure, not as a product decision.",
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
        if job.get("outcome", {}).get("blocker") in {"quality_gate_failed", "security_gate_failed"}:
            lines += ["The deterministic quality gate failed. Reproduce the relevant failure with focused offline checks on synthetic data,",
                      "fix the technical failure, and add a relevant regression test. Do not weaken checks or run dependency installers.",
                      "The supervisor will rerun the full configured quality gates after your result."]
        findings = Path(job["payload"]["findings_path"])
        if findings.exists():
            lines += ["Fix these review findings:", findings.read_text()]
    else:
        lines += ["Set review_result not_applicable; findings may be empty."]
    return "\n".join(lines)


def _extract_agent_json(text):
    """Codex enforces --output-schema; OpenCode has no such enforcement, so a
    model may answer with prose around the JSON or wrap it in a code fence.
    Extract the JSON object tolerantly: strict field validation follows, so
    only this extraction step may be forgiving."""
    candidate = (text or "").strip()
    if not candidate:
        raise ValueError("agent_json_not_found")
    if candidate.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", candidate)
        stripped = stripped.removesuffix("```").strip()
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    for block in reversed(re.findall(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL)):
        try:
            return json.loads(block)
        except ValueError:
            continue
    depth = 0
    start = -1
    for index, char in enumerate(candidate):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        return json.loads(candidate[start:index + 1])
                    except ValueError:
                        start = -1
    raise ValueError("agent_json_not_found")


def _agent(job, config, result_path):
    review = job["stage"] == "reviewing"
    raw_path = result_path.parent / "native-result.json"
    schema_path = result_path.parent / "schema.json"
    write_json(schema_path, SCHEMA)
    prompt = _prompt(job)
    if job["stage"] == "fixing":
        prompt += "\nConfigured quality gate argv (supervisor context; use focused offline checks instead of installer-bearing commands): " + json.dumps(config.get("quality_gates", []))
        prompt += "\nConfigured security gate argv (supervisor context): " + json.dumps(config.get("security_gates", []))
        if job.get("outcome", {}).get("diagnostic"):
            from .worker import sanitize_result
            prompt += "\nSafe gate failure context: " + json.dumps(sanitize_result(job["outcome"]).get("diagnostic", {}))
    env = runtime_environment()
    env = prepare(job, config, result_path.parent, env)
    runtime = config.get("review_runtime", job["runtime"]) if review else job["runtime"]
    if runtime == "codex":
        requested_model = config.get("codex_model")
        collector = Collector(job, runtime, result_path.parent / 'usage.json', config, requested_model)
        argv = [config["codex_executable"], "exec", "--ephemeral", "--json", "--sandbox",
                "read-only" if review else "workspace-write", "-c", 'approval_policy="never"',
                "--output-schema", str(schema_path), "--output-last-message", str(raw_path), "-"]
        if requested_model:
            argv[2:2] = ['--model', requested_model]
        try:
            run = run_agent(argv, cwd=job["worktree"], prompt=prompt, env=env, collector=collector,
                            timeout=config.get("agent_timeout_seconds", 900))
            if run.returncode:
                raise ExecutionFailure("agent_process_failed", phase="agent", exit_code=run.returncode)
            if not raw_path.exists():
                raise ExecutionFailure("agent_result_missing", phase="agent")
            if raw_path.stat().st_size > 65536:
                raise ExecutionFailure("agent_result_invalid", phase="agent")
            try:
                response = json.loads(raw_path.read_text())
            except ValueError:
                raise ExecutionFailure("agent_result_invalid", phase="agent") from None
        except FileNotFoundError:
            raise ExecutionFailure("agent_executable_missing", phase="agent") from None
        except subprocess.TimeoutExpired:
            raise ExecutionFailure("agent_timeout", phase="agent") from None
        finally:
            raw_path.unlink(missing_ok=True)
    else:
        # OpenCode has no OS-level sandbox comparable to Codex `--sandbox`.
        # Implementation stays disabled until the operator verifies the profile
        # with scripts/verify_opencode_sandbox.py and sets
        # opencode_implementation_sandbox to "verified" in the config. After
        # that, the deterministic verify_paths check, the independent review
        # stage and the configured gates remain the scope enforcement.
        if not review and config.get("opencode_implementation_sandbox") != "verified":
            raise RuntimeError("opencode_implementation_sandbox_unconfigured")
        requested_model = config.get('opencode_model')
        collector = Collector(job, runtime, result_path.parent / 'usage.json', config, requested_model)
        if review:
            argv = [config["opencode_executable"], "run", "--agent", "plan", "--format", "json",
                    prompt + "\nResponse schema: " + json.dumps(SCHEMA)]
        else:
            # Non-interactive implementation: --auto approves only permissions
            # that are not explicitly denied, and --archived keeps scripted
            # factory sessions out of the operator's session lists.
            argv = [config["opencode_executable"], "run", "--auto", "--agent", "build", "--format", "json",
                    "--archived", prompt + "\nResponse schema: " + json.dumps(SCHEMA)]
        if requested_model:
            argv[2:2] = ['--model', requested_model]
        try:
            run = run_agent(argv, cwd=job["worktree"], prompt='', env=env, collector=collector,
                            timeout=config.get("agent_timeout_seconds", 900))
        except FileNotFoundError:
            raise ExecutionFailure("agent_executable_missing", phase="agent") from None
        except subprocess.TimeoutExpired:
            raise ExecutionFailure("agent_timeout", phase="agent") from None
        opencode_session_model(collector, config['opencode_executable'], job['worktree'], env)
        if run.returncode:
            raise ExecutionFailure("agent_process_failed", phase="agent", exit_code=run.returncode)
        try:
            response = _extract_agent_json(run.stdout)
        except ValueError:
            raise ExecutionFailure("agent_result_invalid", phase="agent") from None
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
                                if response["status"] == "blocked" else "agent_reported_failure"})
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
        verify_paths(job["worktree"], paths, allowed)
        if paths:
            git(job["worktree"], "add", "--", *paths)
            git(job["worktree"], "-c", "user.name=Software Factory", "-c", "user.email=factory@local.invalid",
                "commit", "-m", f"feat: implement {job['task_id']}")
        committed = git(job["worktree"], "diff", "--name-only", "--no-renames",
                        job["payload"].get("base_revision", "HEAD"), "HEAD").splitlines()
        verify_paths(job["worktree"], committed, allowed)
        write_json(result_path, {"status": "completed", "revision": git(job["worktree"], "rev-parse", "HEAD")})


def _gates(job, config, result_path):
    revision = git(job["worktree"], "rev-parse", "HEAD")
    if revision != job["revision"] or git(job["worktree"], "status", "--porcelain"):
        raise RuntimeError("revision_changed")
    documentation = is_documentation_change(job["worktree"], job["payload"]["base_revision"])
    if documentation:
        gates = [["git", "diff", "--check", job["payload"]["base_revision"], "HEAD"]]
    else:
        gates = config["quality_gates"]
    if not gates:
        raise RuntimeError("quality_gates_missing")
    security = None
    if not documentation and (config.get("security_gates") or config.get("integration_policy") == "reviewed_code"):
        security = security_policy(config)
    commands = [(argv, "quality_gate_failed") for argv in gates]
    if security:
        commands += [(argv, "security_gate_failed") for argv in config["security_gates"]]
    env = prepare(job, config, result_path.parent, runtime_environment())
    for index, (argv, failure) in enumerate(commands):
        gate_state = result_path.parent / "synthetic-gate-state"
        gate_state.mkdir(mode=0o700, exist_ok=True)
        env = dict(env)
        env.update(BUCHOS_STATE_DIR=str(gate_state), BUCHOS_LIFE_WIKI_VAULT_DIR=str(gate_state / "vault"))
        try:
            result = subprocess.run(argv, cwd=job["worktree"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=env,
                                    timeout=config.get("gate_timeout_seconds", 1800))
        except FileNotFoundError:
            raise ExecutionFailure("environment_executable_missing", phase="quality_gate" if failure == "quality_gate_failed" else "security_gate",
                                   command_index=index) from None
        except subprocess.TimeoutExpired:
            raise ExecutionFailure("gate_timeout", phase="quality_gate" if failure == "quality_gate_failed" else "security_gate",
                                   command_index=index) from None
        if result.returncode:
            raise ExecutionFailure(failure, phase="quality_gate" if failure == "quality_gate_failed" else "security_gate",
                                   command_index=index, exit_code=result.returncode)
    if git(job["worktree"], "rev-parse", "HEAD") != revision or git(job["worktree"], "status", "--porcelain"):
        raise RuntimeError("gate_changed_worktree")
    if security:
        write_json(receipt_path(config, job), {"revision": revision, "policy": security, "passed": True, "created_at": time.time()})
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
        result = {"status": "failed", "blocker": code}
        if isinstance(error, ExecutionFailure):
            result["diagnostic"] = error.diagnostic
        write_json(result_path, result)


if __name__ == "__main__":
    main()
