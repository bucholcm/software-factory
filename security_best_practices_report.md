# Public-release security assessment

Date: 2026-11-22. Baseline: `93f25b8`. Hardening branch:
`security/public-release-prep`.

## Executive summary

The reviewed repository is a Python CLI/supervisor, not a web application.
Full-history and working-tree Gitleaks scans found no secret matches. Manual
review identified a documentation-policy bypass and several recovery/host
execution hardening opportunities. The fixes and regression tests are included
in this branch. Repository visibility remains private.

This prepares the code for review as a trusted-local-project pilot. It is not
an independent audit, a guarantee of security, or authorization to host
untrusted repositories. Owner review of historical author metadata, protected
branch configuration and public vulnerability-reporting setup remains necessary
before publication. See [SECURITY.md](SECURITY.md).

## High severity

### 1. Agent command templates could take the lighter documentation path — fixed

Impact: an agent command or instruction template classified as documentation
could bypass the source-specific tests/security integration path and change
behavior when consumed by a coding tool.

Previously, most `.md` paths qualified as documentation, including command
templates under `integrations/` and additional tool instruction directories.
[factory/adapters.py:41](factory/adapters.py#L41) now permits ordinary top-level
documentation and `docs/`, while excluding behavior directories such as agent
commands, prompts and rules. Other Markdown is treated as source. This is a
conservative policy, not a general prompt-injection defense.

Regression: actual committed OpenCode command template and representative
Claude/Cursor/prompt paths do not qualify as documentation.

## Medium severity

### 2. Restore followed internal symlink ancestors — fixed

Direct-member checks did not cover a symlink replacing an archive's `state/`
or `code/` directory. A substituted checkpoint could read outside its archive
root while still satisfying a member hash. Checksums do not authenticate an
archive's author.

[factory/recovery.py:27](factory/recovery.py#L27) validates relative archive
paths and rejects symlinks at every internal path component. Code and state
restore share this check. Code manifests also require a full commit hash before
Git runs, preventing option-shaped revision strings from becoming arguments.

Regressions cover manifest/bundle symlinks, internal directory symlinks, rejected
revision strings and restoration into existing destinations.

### 3. Implicit Git hooks could run during supervisor-owned operations — hardened

Locally configured hooks can execute arbitrary commands when worktrees are
created or commits/checkouts/merges run. The factory should run its configured
gates explicitly rather than trigger additional host commands implicitly.

[factory/config.py:11](factory/config.py#L11) disables hooks in the shared
factory-owned Git helper. A regression arms a real checkout hook, verifies the
factory operation does not execute it, and verifies an ordinary user Git
command still can. User-configured gates/release commands remain trusted code.

### 4. Integration-journal replay did not revalidate its backup — fixed

Recovery could mark an already-merged job integrated based on the journal and
Git revision without noticing that its checkpoint had been corrupted afterward.
[factory/integration.py](factory/integration.py) now verifies the recorded
bundle hash and checks the manifest's base revision against the journal before
accepting replay. A regression corrupts the backup after the merge and confirms
replay refuses it. This does not automatically rewind the code or application
data.

## Low severity and release hygiene

### 5. Credential-path and control-character handling — hardened

[factory/scope.py:5](factory/scope.py#L5) now rejects control characters and
additional common credential locations. `.gitignore` also covers environment
files, private keys, SQLite databases, bundles and private state/report folders.
These patterns supplement secret scanning; they do not identify every secret.

### 6. Continuous security checks and disclosure guidance — added

The Security workflow adds medium/high Bandit checks and a redacted all-ref
Gitleaks scan. Its Python wrapper fails closed for a missing/failed scanner,
malformed report, inconsistent exit status or unredacted finding. A synthetic
positive control confirmed a generated fake credential is detected without
printing its value. Reports remain in private temporary directories and are
not uploaded as artifacts.

Actions are SHA-pinned and the Gitleaks release binary is checksum-pinned.
Dependabot and CODEOWNERS provide update/review routing. They do not replace
required-check protections in GitHub settings.

## Reviewed static-analysis finding

Bandit reported B608 for the dynamic SQL update in
[factory/store.py:100](factory/store.py#L100). Field identifiers are validated
against the fixed `FIELDS` allowlist and values are bound parameters. This is
not a demonstrated SQL injection. A targeted `nosec B608` annotation records
that decision, and a regression verifies identifier rejection and value binding.
No scanner categories were globally disabled.

## Evidence and limitations

- Gitleaks 8.30.1: zero matches in all local Git refs at the baseline and zero
  matches in the audited working tree. Local refs can be re-scanned with
  `python -m scripts.scan_secrets` before publication.
- Bandit 1.9.4 after fixes: zero unsuppressed medium/high findings; low-severity
  subprocess/import findings remain visible for review.
- Regression suite includes existing worker restart, WAL, schema-compatibility
  and data-preserving rollback cases plus the new security boundary/scanner cases.
- All 134 local deterministic tests passed after the changes. Python compilation
  and `git diff --check` also passed. GitHub CI results are recorded on the pull
  request; they should be checked again at the eventual merge revision.
- Images use a synthetic project and contain no portfolio data. No real
  application databases, credentials or provider state were read for this audit.
- Git history contains author identity/email metadata and internal project/ticket
  references. No history was rewritten; the owner should review that information
  before changing visibility. The baseline inventory found three distinct machine-derived
  author/committer email identities and no historical private-key/database/bundle
  file paths. This is a privacy inventory, not a claim that all history is public-safe.
  New preparation commits use the owner's GitHub no-reply address; published
  historical identities were not rewritten.
- Agent prompts, review verdicts and worktree scopes are not isolation from a
  malicious repository. Test/release commands run as the supervisor account.
- Local orchestration can still use remote model providers through configured
  CLIs. No claim of offline inference or provider-independent data residency is
  made.
- Hash-verified backups are not signed archives. Same-account tampering and a
  compromised host remain outside the supported trust model.
- The installed native service is version-pinned separately from this branch;
  repository fixes do not silently upgrade a running installation.

The bundled security skill has Python web-framework guidance but no dedicated
CLI/supervisor reference. This assessment therefore used direct source review,
synthetic regression tests and primary documentation for Git hooks, Python
subprocesses and GitHub security settings:
[Git hooks](https://git-scm.com/docs/githooks),
[Python subprocess security](https://docs.python.org/3/library/subprocess.html#security-considerations),
[GitHub secret scanning](https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning),
[private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).
