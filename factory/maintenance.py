"""Read-only maintenance checks against an exact, tracked Git revision.

Scanner output is untrusted. Only package names and stable advisory/rule IDs
cross the scanner boundary; stderr, source excerpts and exception text do not.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import tomllib
from typing import Callable, Mapping, Sequence


TIMEOUT = 120
REPORT_FILE = "maintenance.json"
_PACKAGE = re.compile(r"(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._+-]{0,127}", re.I)
_IDENTIFIER = re.compile(r"(?:CVE-\d{4}-\d{4,}|GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}|PYSEC-\d{4}-\d+|NPM-\d+|B\d{3}|AVD-[A-Z0-9-]+|GITLEAKS-[a-z0-9_-]{1,64}|SEMGREP-[a-z0-9_.-]{1,200})", re.I)
_SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_TOOLS = {"python": ("pip-audit",), "npm": ("npm",), "container": ("trivy",), "code": ("bandit",), "secrets": ("gitleaks",), "javascript_code": ("semgrep",)}
_STATUSES = {"clean", "findings", "unavailable", "not_applicable", "error"}


def _run(runner: Callable, argv: Sequence[str], cwd: Path):
    return runner(list(argv), cwd=str(cwd), capture_output=True, text=True,
                  timeout=TIMEOUT, check=False, shell=False)


def _check(category: str, tool: str, status: str, findings=(), *, count=None, error_code=None):
    items = sorted({(item.get("package", ""), item.get("id", "")) for item in findings})
    result = {"category": category, "status": status, "tool": tool,
              "count": len(items) if count is None else count,
              "findings": [{k: v for k, v in (("package", package), ("id", identifier)) if v}
                           for package, identifier in items]}
    if error_code:
        result["error_code"] = error_code
    return result


def _finding(package=None, identifier=None):
    item = {}
    if isinstance(package, str) and _PACKAGE.fullmatch(package):
        item["package"] = package
    if isinstance(identifier, str) and _IDENTIFIER.fullmatch(identifier):
        item["id"] = identifier
    return item


def _parse_python(payload):
    dependencies = payload["dependencies"] if isinstance(payload, dict) else payload
    if not isinstance(dependencies, list):
        raise ValueError
    findings = []
    for dependency in dependencies:
        if "skip_reason" in dependency or "vulns" not in dependency:
            raise ValueError
        for vulnerability in dependency.get("vulns", []):
            item = _finding(dependency.get("name"), vulnerability.get("id"))
            if not item.get("id"):
                raise ValueError
            findings.append(item)
    return findings


def _parse_npm(payload):
    vulnerabilities = payload["vulnerabilities"]
    if not isinstance(vulnerabilities, dict):
        raise ValueError
    findings = []
    for name, vulnerability in vulnerabilities.items():
        if not isinstance(vulnerability, dict):
            raise ValueError
        identifiers = []
        for via in vulnerability.get("via", []):
            if isinstance(via, dict):
                # npm's advisory URL carries a GHSA; never retain the URL itself.
                match = re.search(r"GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}", str(via.get("url", "")), re.I)
                if match:
                    identifiers.append(match.group())
                elif isinstance(via.get("source"), int):
                    identifiers.append(f"NPM-{via['source']}")
        if not identifiers:
            identifiers = [None]  # Transitive vulnerability reference.
        for identifier in identifiers:
            item = _finding(name, identifier)
            if not item:
                raise ValueError
            findings.append(item)
    return findings


def _parse_outdated(payload):
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError
    result = []
    for name, value in payload.items():
        if not isinstance(value, dict) or not isinstance(value.get("latest"), str):
            raise ValueError
        item = _finding(name)
        if not item:
            raise ValueError
        result.append(item)
    return result


def _parse_trivy(payload):
    results = payload["Results"]
    if not isinstance(results, list):
        raise ValueError
    findings = []
    for result in results:
        for key, field in (("Vulnerabilities", "VulnerabilityID"), ("Misconfigurations", "AVDID")):
            for vulnerability in result.get(key, []):
                if key == "Misconfigurations" and vulnerability.get("Status") == "PASS":
                    continue
                item = _finding(vulnerability.get("PkgName"), vulnerability.get(field))
                if not item.get("id"):
                    raise ValueError
                findings.append(item)
    return findings


def _parse_bandit(payload):
    if payload.get("errors"):
        raise ValueError
    results = payload["results"]
    if not isinstance(results, list):
        raise ValueError
    findings = [_finding(identifier=item.get("test_id")) for item in results]
    if any(not item for item in findings):
        raise ValueError
    return findings


def _parse_semgrep(payload):
    if payload.get("errors"):
        raise ValueError
    results = payload["results"]
    if not isinstance(results, list):
        raise ValueError
    findings = [_finding(identifier=f"SEMGREP-{item['check_id']}") for item in results]
    if any(not item for item in findings):
        raise ValueError
    return findings


def _python_packages(payload, *, outdated=False):
    if not isinstance(payload, list):
        raise ValueError
    result = []
    for package in payload:
        if not isinstance(package, dict):
            raise ValueError
        # Editable project metadata is not a registry identity. Even when pip
        # unexpectedly returns it despite --exclude-editable, never audit a
        # namesake registry distribution or expose its local filesystem path.
        source = package.get("direct_url", {})
        if ("editable_project_location" in package
                or (isinstance(source, dict) and str(source.get("url", "")).startswith("file:"))):
            continue
        name, version = package["name"], package["version"]
        versions = [version, package["latest_version"]] if outdated else [version]
        if not isinstance(name, str) or not _PACKAGE.fullmatch(name) or any(
            not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.!+_-]*", value) for value in versions
        ):
            raise ValueError
        result.append((name, version))
    return result


def _environment_checks(snapshot, runner, audit_command, interpreter):
    # The caller explicitly trusts this interpreter. Never discover or execute
    # an interpreter from an arbitrary scanned repository.
    command = [str(interpreter), "-I", "-m", "pip", "list", "--format=json", "--disable-pip-version-check", "--exclude-editable"]
    checks = []
    for outdated in (False, True):
        category = "python_versions" if outdated else "python_transitive"
        try:
            result = _run(runner, [*command, "--outdated"] if outdated else command, snapshot)
        except FileNotFoundError:
            checks.append(_check(category, "pip", "unavailable", error_code="environment_python_missing"))
            continue
        except subprocess.TimeoutExpired:
            checks.append(_check(category, "pip", "error", error_code="scan_timeout"))
            continue
        except OSError:
            checks.append(_check(category, "pip", "error", error_code="scan_failed"))
            continue
        if result.returncode:
            checks.append(_check(category, "pip", "error", error_code="scan_failed"))
            continue
        try:
            packages = _python_packages(json.loads(result.stdout), outdated=outdated)
        except (ValueError, TypeError, KeyError, AttributeError):
            checks.append(_check(category, "pip", "error", error_code="invalid_environment_inventory"))
            continue
        if outdated:
            findings = [_finding(name) for name, _ in packages]
            checks.append(_check(category, "pip", "findings" if findings else "clean", findings))
        else:
            path = snapshot / "factory-environment-requirements.txt"
            path.write_text("\n".join(sorted({f"{name}=={version}" for name, version in packages})) + "\n")
            checks.append(_scanner(category, "pip-audit", [*audit_command, "--requirement", path.name, "--format", "json",
                                   "--progress-spinner", "off", "--disable-pip", "--no-deps"], snapshot, runner, _parse_python))
    return checks


def _scanner(category, tool, argv, cwd, runner, parser):
    try:
        result = _run(runner, argv, cwd)
    except FileNotFoundError:
        return _check(category, tool, "unavailable", error_code="tool_missing")
    except subprocess.TimeoutExpired:
        return _check(category, tool, "error", error_code="scan_timeout")
    except OSError:
        return _check(category, tool, "error", error_code="scan_failed")
    if result.returncode not in (0, 1):
        return _check(category, tool, "error", error_code="scan_failed")
    try:
        payload = json.loads(result.stdout)
        if isinstance(payload, dict) and "error" in payload:
            raise ValueError
        findings = parser(payload)
        # Exit 1 is documented for findings, never accept an unexplained failure.
        if result.returncode and not findings:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        return _check(category, tool, "error", error_code="invalid_scan_result")
    return _check(category, tool, "findings" if findings else "clean", findings)


def _bandit_scanner(snapshot, runner, command):
    # Bandit plugins can write diagnostics before the JSON on stdout. A
    # dedicated private report keeps those diagnostics out of the parser and
    # prevents source excerpts from being retained in operational records.
    with tempfile.TemporaryDirectory(prefix="factory-bandit-") as temporary:
        destination = Path(temporary) / "bandit.json"
        destination.touch(mode=0o600)
        argv = [*command, "-r", ".", "-f", "json", "--ignore-nosec", "--output", str(destination)]
        try:
            result = _run(runner, argv, snapshot)
        except FileNotFoundError:
            return _check("code_security", "bandit", "unavailable", error_code="tool_missing")
        except subprocess.TimeoutExpired:
            return _check("code_security", "bandit", "error", error_code="scan_timeout")
        except OSError:
            return _check("code_security", "bandit", "error", error_code="scan_failed")
        try:
            if destination.stat().st_size > 16 * 1024 * 1024:
                raise ValueError
            payload = json.loads(destination.read_text())
            if not isinstance(payload, dict) or "error" in payload:
                raise ValueError
            errors = payload.get("errors", [])
            if not isinstance(errors, list):
                raise ValueError
            findings = _parse_bandit({**payload, "errors": []})
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
            code = "scan_failed" if result.returncode not in (0, 1) else "invalid_scan_result"
            return _check("code_security", "bandit", "error", error_code=code)
        if result.returncode not in (0, 1):
            return _check("code_security", "bandit", "error", findings, error_code="scan_failed")
        if errors:
            # Valid findings remain useful, but skipped/invalid source files
            # mean this is incomplete coverage, even when findings are empty.
            return _check("code_security", "bandit", "error", findings, error_code="scan_incomplete")
        if result.returncode and not findings:
            return _check("code_security", "bandit", "error", error_code="invalid_scan_result")
        return _check("code_security", "bandit", "findings" if findings else "clean", findings)


def _secret_scanner(snapshot, runner, command):
    # Gitleaks writes only its fully redacted report to a private temporary file.
    # stdout/stderr are captured in memory and are never forwarded or logged.
    with tempfile.TemporaryDirectory(prefix="factory-redacted-") as temporary:
        destination = Path(temporary) / "gitleaks.json"
        destination.touch(mode=0o600)
        argv = [*command, "detect", "--source", str(snapshot), "--no-git",
                "--redact=100", "--report-format=json", "--report-path", str(destination),
                "--exit-code", "1", "--no-banner"]
        try:
            result = _run(runner, argv, snapshot)
        except FileNotFoundError:
            return _check("secret_scan", "gitleaks", "unavailable", error_code="tool_missing")
        except subprocess.TimeoutExpired:
            return _check("secret_scan", "gitleaks", "error", error_code="scan_timeout")
        except OSError:
            return _check("secret_scan", "gitleaks", "error", error_code="scan_failed")
        if result.returncode not in (0, 1):
            return _check("secret_scan", "gitleaks", "error", error_code="scan_failed")
        try:
            payload = json.loads(destination.read_text())
            if not isinstance(payload, list):
                raise ValueError
            findings = []
            for item in payload:
                # The scanner must honor --redact before its report is accepted.
                if item.get("Secret") != "REDACTED":
                    raise ValueError
                finding = _finding(identifier=f"GITLEAKS-{item['RuleID']}")
                if not finding:
                    raise ValueError
                findings.append(finding)
            if result.returncode and not findings:
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return _check("secret_scan", "gitleaks", "error", error_code="invalid_scan_result")
        return _check("secret_scan", "gitleaks", "findings" if findings else "clean", findings)


def _is_docker(name):
    path = PurePosixPath(name)
    return (path.name == "Dockerfile" or path.name.startswith("Dockerfile.")
            or path.name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"})


def _approved(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts):
        return False
    return (path.suffix in _SOURCE_SUFFIXES or _is_docker(name)
            or path.name in {"pyproject.toml", "uv.lock", "poetry.lock", "package.json", "package-lock.json", "npm-shrinkwrap.json"}
            or (path.name.startswith("requirements") and path.suffix == ".txt"))


def _snapshot(repo, target, runner):
    revision = _run(runner, ["git", "rev-parse", "HEAD"], repo)
    sha = revision.stdout.strip()
    if revision.returncode or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError
    files = _run(runner, ["git", "ls-tree", "-r", "--name-only", "-z", sha], repo)
    if files.returncode:
        raise ValueError
    names = []
    for name in files.stdout.split("\0"):
        if not name or not _approved(name):
            continue
        result = _run(runner, ["git", "show", f"{sha}:{name}"], repo)
        if result.returncode:
            raise ValueError
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(result.stdout, encoding="utf-8")
        names.append(name)
    return sha, names


def _python_checks(snapshot, names, runner, command):
    checks = []
    transitive_checks = []
    manifests = [name for name in names if PurePosixPath(name).name.startswith("requirements") and name.endswith(".txt")]
    if "pyproject.toml" in names:
        try:
            project = tomllib.loads((snapshot / "pyproject.toml").read_text())["project"]
            requirements = project.get("dependencies", [])
            groups = project.get("optional-dependencies", {})
            if not isinstance(groups, dict) or any(not isinstance(group, list) for group in groups.values()):
                raise ValueError
            requirements += [dep for group in groups.values() for dep in group]
            if not isinstance(requirements, list) or any(not isinstance(dep, str) or "\n" in dep or "\r" in dep or dep.lstrip().startswith("-") for dep in requirements):
                raise ValueError
            generated = snapshot / "factory-declared-requirements.txt"
            generated.write_text("\n".join(requirements) + "\n")
            if requirements:
                manifests.append(generated.name)
            else:
                checks.append(_check("python_declared", "pip-audit", "not_applicable"))
        except (ValueError, KeyError, TypeError):
            checks.append(_check("python_declared", "pip-audit", "error", error_code="invalid_manifest"))
    for lock_name in ("uv.lock", "poetry.lock"):
        if lock_name not in names:
            continue
        try:
            packages = tomllib.loads((snapshot / lock_name).read_text())["package"]
            if not isinstance(packages, list):
                raise ValueError
            requirements = []
            for package in packages:
                source = package.get("source", {})
                if isinstance(source, dict) and ("editable" in source or "virtual" in source):
                    continue  # Local project, not a registry dependency.
                name, version = package["name"], package["version"]
                if not isinstance(name, str) or not _PACKAGE.fullmatch(name) or not isinstance(version, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.!+_-]*", version):
                    raise ValueError
                requirements.append(f"{name}=={version}")
            generated = snapshot / f"factory-{lock_name}-requirements.txt"
            generated.write_text("\n".join(sorted(set(requirements))) + "\n")
            if requirements:
                manifests.append(generated.name)
            else:
                checks.append(_check("python_resolved", "pip-audit", "not_applicable"))
                transitive_checks.append(checks[-1])
        except (ValueError, TypeError, KeyError, AttributeError):
            checks.append(_check("python_resolved", "pip-audit", "error", error_code="invalid_manifest"))
    if not manifests and not checks:
        checks.append(_check("python_dependencies", "pip-audit", "not_applicable"))
    for name in manifests:
        try:
            # Requirements can contain arbitrary local paths, include directives,
            # index credentials and VCS URLs. Only pinned registry requirements
            # are safe for the no-install adapter.
            lines = (snapshot / name).read_text().splitlines()
            dependencies = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
            if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*(?:\[[a-zA-Z0-9,._-]+\])?==[a-zA-Z0-9][a-zA-Z0-9.!+_-]*", dep) for dep in dependencies):
                checks.append(_check("python_dependencies", "pip-audit", "unavailable", error_code="pinned_requirements_required"))
                continue
        except (OSError, UnicodeError):
            checks.append(_check("python_dependencies", "pip-audit", "error", error_code="invalid_manifest"))
            continue
        # --disable-pip prevents dependency installation/build execution. An
        # unpinned or unsupported requirement fails visibly rather than resolves.
        category = "python_declared" if name == "factory-declared-requirements.txt" else "python_resolved"
        checks.append(_scanner(category, "pip-audit", [*command, "--requirement", name, "--format", "json",
                               "--progress-spinner", "off", "--disable-pip", "--no-deps"], snapshot, runner, _parse_python))
        if name.startswith(("factory-uv.lock-", "factory-poetry.lock-")):
            transitive_checks.append(checks[-1])
    applicable = any(name.endswith(".py") or PurePosixPath(name).name in {"pyproject.toml", "uv.lock", "poetry.lock"}
                     or PurePosixPath(name).name.startswith("requirements") for name in names)
    if transitive_checks:
        for check in transitive_checks:
            checks.append(_check("python_transitive", "pip-audit", check["status"], check["findings"], error_code=check.get("error_code")))
    elif applicable:
        checks.append(_check("python_transitive", "pip-audit", "unavailable", error_code="complete_dependency_inventory_required"))
    else:
        checks.append(_check("python_transitive", "pip-audit", "not_applicable"))
    checks.append(_check("python_versions", "python", "unavailable" if applicable else "not_applicable",
                         error_code="update_checker_not_configured" if applicable else None))
    return checks


def _execute(snapshot, names, runner, commands, environment_python=None):
    checks = _python_checks(snapshot, names, runner, commands["python"])
    if environment_python is not None:
        checks = [check for check in checks if check["category"] not in {"python_transitive", "python_versions"}]
        checks.extend(_environment_checks(snapshot, runner, commands["python"], environment_python))
    manifests = [name for name in names if PurePosixPath(name).name == "package.json"]
    if not manifests:
        checks.append(_check("npm_dependencies", "npm", "not_applicable"))
    for name in manifests:
        directory = PurePosixPath(name).parent
        cwd = snapshot / directory
        if any(str(directory / lock) in names for lock in ("package-lock.json", "npm-shrinkwrap.json")):
            checks.append(_scanner("npm_dependencies", "npm", [*commands["npm"], "audit", "--package-lock-only", "--json", "--ignore-scripts"], cwd, runner, _parse_npm))
        else:
            checks.append(_check("npm_dependencies", "npm", "unavailable", error_code="lockfile_missing"))
        checks.append(_scanner("npm_outdated", "npm", [*commands["npm"], "outdated", "--json", "--ignore-scripts", "--package-lock-only"], cwd, runner, _parse_outdated))
    docker = [name for name in names if _is_docker(name)]
    checks.append(_check("docker_inventory", "inventory", "clean" if docker else "not_applicable", count=len(docker)))
    if docker:
        checks.append(_check("docker_updates", "renovate", "unavailable", error_code="update_checker_not_configured"))
        checks.append(_scanner("docker_configuration", "trivy", [*commands["container"], "config", "--format", "json", "--exit-code", "1", "."], snapshot, runner, _parse_trivy))
        images = set()
        unresolved = False
        for name in docker:
            aliases = set()
            for line in (snapshot / name).read_text().splitlines():
                match = re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line, re.I)
                compose = re.match(r"\s*image:\s*['\"]?([^\s'\"#]+)", line)
                if match or compose:
                    image = (match or compose).group(1)
                    if image.lower() not in aliases and image != "scratch":
                        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@+-]*", image):
                            images.add(image)
                        else:
                            unresolved = True
                    if match and match.group(2):
                        aliases.add(match.group(2).lower())
        for image in sorted(images):
            checks.append(_scanner("docker_image", "trivy", [*commands["container"], "image", "--format", "json", "--exit-code", "1", image], snapshot, runner, _parse_trivy))
        if unresolved:
            checks.append(_check("docker_image", "trivy", "unavailable", error_code="unresolved_image_reference"))
    else:
        checks.append(_check("docker_updates", "renovate", "not_applicable"))
        checks.append(_check("docker_configuration", "trivy", "not_applicable"))
        checks.append(_check("docker_image", "trivy", "not_applicable"))
    if any(name.endswith(".py") for name in names):
        checks.append(_bandit_scanner(snapshot, runner, commands["code"]))
    else:
        checks.append(_check("code_security", "bandit", "not_applicable"))
    if any(PurePosixPath(name).suffix in _SOURCE_SUFFIXES - {".py"} for name in names):
        checks.append(_scanner("code_security_javascript", "semgrep", [*commands["javascript_code"], "scan", "--config=p/security-audit",
                               "--json", "--quiet", "--metrics=off", "."], snapshot, runner, _parse_semgrep))
    checks.append(_secret_scanner(snapshot, runner, commands["secrets"]))
    return checks


def scan(repo: Path, output_dir: Path, *, runner=subprocess.run,
         commands: Mapping[str, Sequence[str]] | None = None,
         environment_python: Path | None = None) -> dict:
    """Run scanners on tracked HEAD, atomically replacing the sanitized report.

    ``runner`` has subprocess.run's interface. ``commands`` can replace the
    executable prefix for python/npm/container/code/secrets/javascript_code adapters.
    ``environment_python`` explicitly trusts an interpreter for isolated pip
    inventory and outdated metadata queries; it is never auto-discovered.
    No shell is used.
    Audit tools may contact advisory registries; this function never installs
    packages or executes project scripts. Missing/incomplete checks stay visible.
    """
    report = {"schema_version": 1, "revision": None,
              "last_run": datetime.now(timezone.utc).isoformat(), "checks": []}
    configured = dict(_TOOLS)
    configured.update(commands or {})
    try:
        with tempfile.TemporaryDirectory(prefix="factory-maintenance-") as temporary:
            snapshot = Path(temporary)
            report["revision"], names = _snapshot(Path(repo), snapshot, runner)
            report["checks"] = _execute(snapshot, names, runner, configured, environment_python)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        report["checks"] = [_check("inventory", "git", "error", error_code="inventory_failed")]
    statuses = {check["status"] for check in report["checks"]}
    report["status"] = ("error" if "error" in statuses else "findings" if "findings" in statuses
                        else "unavailable" if "unavailable" in statuses else "clean")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".maintenance-", dir=output_dir)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_dir / REPORT_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return report


def load_report(output_dir: Path, *, revision: str | None = None, max_age_seconds: float = 86400) -> dict:
    """Load a sanitized report; old/revision-mismatched results are never current."""
    try:
        report = json.loads((Path(output_dir) / REPORT_FILE).read_text())
        timestamp = datetime.fromisoformat(report["last_run"])
        stale = (datetime.now(timezone.utc) - timestamp).total_seconds() > max_age_seconds
        if timestamp > datetime.now(timezone.utc) or (revision is not None and report["revision"] != revision):
            stale = True
        if (report["schema_version"] != 1 or report["status"] not in _STATUSES
                or not isinstance(report["checks"], list)):
            raise ValueError
        # Do not relay arbitrary fields from a modified report file.
        checks = []
        for check in report["checks"]:
            if check["status"] not in _STATUSES:
                raise ValueError
            if not all(re.fullmatch(r"[a-z_\-]+", check[key]) for key in ("category", "tool")):
                raise ValueError
            findings = [_finding(item.get("package"), item.get("id")) for item in check["findings"]]
            code = check.get("error_code")
            if code and not re.fullmatch(r"[a-z_]+", code):
                raise ValueError
            checks.append(_check(check["category"], check["tool"], check["status"], findings,
                                 count=int(check["count"]), error_code=code))
        sha = report["revision"]
        if sha is not None and not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError
        return {"schema_version": 1, "revision": sha, "last_run": timestamp.isoformat(),
                "status": "stale" if stale else report["status"], "checks": checks}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"schema_version": 1, "revision": None, "last_run": None, "status": "unavailable", "checks": []}
