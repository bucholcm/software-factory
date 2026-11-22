# Security policy

## Intended deployment

Software Factory is an early-stage local development supervisor for a single
trusted operator and trusted repositories. It is not a service for accepting
arbitrary repositories or jobs from other users. Only the current `main` branch
is maintained; there is no security response-time guarantee.

The supervisor itself has no HTTP listener. Beads, coding CLIs, GitHub CLI,
scanners and release commands are separate programs with their own permissions,
network behavior and credential storage.

## Trust boundaries

- Treat repository code, quality gates, security gates, project configuration,
  runtime configuration and release adapters as executable code. Review them
  before running the factory. A ticket's text and model output can be hostile.
- Test and release commands run with the supervisor account's OS permissions.
  They are not automatically placed in a container or a separate OS sandbox.
  Use a dedicated account, VM or properly restricted container when working with
  code you do not trust. Do not expose the supervisor to untrusted job submitters.
- Worktrees isolate Git changes. Scope checks restrict which changes can be
  accepted and integrated; they do not confine file reads or child processes.
  Prompt instructions and output redaction are not security isolation.
- Runtime sandbox behavior depends on the installed coding tool. Review its
  filesystem and network policy. OpenCode implementation remains disabled;
  invoking `/factory` in OpenCode does not change the configured worker runtime.
- Local orchestration does not mean offline inference. Coding tools may send
  source, prompts and tool output to the model providers configured by the user.
- Factory-owned Git operations through the command helper disable hooks.
  Put required checks in explicit `quality_gates` and `security_gates`, rather
  than relying on a repository hook to run them.

## Data and recovery

Keep state, credentials, backups and scanner reports outside the source tree.
The ignore rules are a guard against accidental commits, not a secret detector.
Use least-privilege credentials and never put secrets in task descriptions,
command arguments, committed configuration or screenshots.

Factory records can contain task requirements and local paths. Release
checkpoints can contain complete application databases and mutable files.
Restrict access to the private state directory, encrypt sensitive backups and
set a retention policy appropriate to the application. Backups are local;
off-device disaster recovery is not provided.

Restore only checkpoints created by a trusted operator and stored in a trusted
location. Hashes detect corruption; they are not signatures proving who created
an archive. Internal archive symlinks and non-commit revision strings are
rejected. The same OS user can modify the supervisor and its journals, so this
system does not defend against a compromised account.

Application activation requires an explicitly configured adapter that stops
all writers, verifies that boundary and declares schema compatibility. Code
rollback does not rewind data. Keep `source-review` until runtime separation
and recovery behavior have been verified for the actual application.

## Continuous checks

The Security workflow runs Bandit on the factory and scripts, and Gitleaks
against all Git refs available to its full-history checkout. Missing tools,
scanner failures and malformed reports fail the job. Secret matches and raw
scanner output are not printed or uploaded. The scanner binary is version- and
checksum-pinned; Dependabot checks Python tooling and GitHub Action updates.

The SQL update in `factory/store.py` has one targeted Bandit suppression:
column names come only from a fixed allowlist and all values use parameters.
A regression test checks both identifier rejection and value binding. Other
subprocess-related low-severity findings require normal code review; a passing
scanner is not a security guarantee.

Pull-request workflows use GitHub-hosted runners with read-only repository
permissions and no persisted checkout credentials. Do not add production
credentials, self-hosted runners, or privileged `pull_request_target` execution
to untrusted pull-request jobs.

Local checks:

```sh
python -m unittest discover -s tests -v
python -m pip install -r requirements-security.txt
python -m bandit -r factory scripts -ll
# Install the pinned Gitleaks version used in .github/workflows/security.yml.
python -m scripts.scan_secrets
```

## Reporting vulnerabilities

Use GitHub's **Security → Advisories → Report a vulnerability** when private
vulnerability reporting is enabled. Do not put credentials, private application
data or an unpatched exploit in a public issue. If private reporting is
unavailable, request a private contact channel without posting technical details.

## Before making the repository public

- Review the security assessment and run the full-history secret scan again.
- Review commit author names/emails and internal project references in history;
  secret scanning does not decide what personal information you want public.
- Review every image and example for private application data.
- Merge reviewed hardening changes and confirm Tests and Security checks pass.
- Configure branch/ruleset protections for required checks and restrict force
  pushes and deletion, subject to the account's GitHub feature availability.
- When the repository becomes public, enable private vulnerability reporting,
  verify GitHub secret scanning and push protection settings, and check that
  the reporting link works for a researcher.

Preparing these changes does not change repository visibility. Making it public
remains a separate owner decision.
