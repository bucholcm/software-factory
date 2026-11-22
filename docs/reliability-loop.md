# Project environments and the reliability loop

The factory prepares dependencies before launching a model. A project can
configure an explicit `environment` profile in its private configuration:

```json
{
  "environment": {
    "python": "/absolute/path/to/python3.13",
    "minimum_python": [3, 11],
    "venv": ".venv",
    "required_executables": ["node", "npm", "tesseract"],
    "fingerprint_files": ["pyproject.toml", "Makefile", "package-lock.json", "app/package-lock.json"],
    "bootstrap_commands": [
      ["{venv_python}", "-m", "pip", "install", "-e", ".[dev]"],
      ["make", "install"],
      ["npm", "ci", "--ignore-scripts"],
      ["npx", "playwright", "install", "chromium"]
    ],
    "checks": [
      ["{venv_python}", "-I", "-c", "import pytest, pypdf, xlrd, yfinance, exchange_calendars"],
      ["node", "-e", "require('./app/node_modules/typescript'); require('@playwright/test')"]
    ]
  }
}
```

This example describes BuchOS, including its OCR dependency. Select dependencies
and checks for each project. Existing configurations without a profile retain
their existing behavior; an absent profile is not evidence of a prepared
environment. Bootstrap commands are trusted configuration, run as argv without
a shell, with the supervisor account's permissions and network access. Never
load them automatically from a task or model response. They must not start the
application, migrate data, or contact providers.

A fresh virtual environment uses the configured interpreter. An incompatible
existing environment is preserved and blocks with `environment_python_mismatch`.
The virtual environment must be ignored by Git. Required system tools are
checked before invoking a model; the factory does not install global packages
automatically. Dependency commands run before implementation and testing, outside
the model's restricted network sandbox. Models receive instructions to use
prepared dependencies and targeted offline checks.

A private receipt fingerprints the profile and explicitly listed tracked
manifests. Dependency probes run again even when the receipt matches. Deleted
or broken dependencies invalidate reuse, and changed manifests trigger a new
bootstrap. A per-worktree lock covers preparation across process restarts.
Bootstrap that changes the source inventory fails rather than silently
including generated changes in a commit.

Environment failures block at the current stage, without entering the code-fix
loop. Once repaired, resume the same job. For a stopped, failed or blocked job:

```sh
python -m factory --config /private/project/config.json prepare-environment JOB_ID
python -m factory --config /private/project/config.json resume JOB_ID
```

For an unchanged revision stranded in testing/fixing after an environment
failure, `resume JOB_ID --retest` starts with deterministic tests. It refuses
dirty or changed revisions and outstanding review findings, invalidates old
test/review claims, and still requires fresh independent review. This avoids
spending a model session to repair code that already passes after setup repair.

Only the supervisor's completed gates and independent exact-revision review can
advance the job. Running a command manually does not create those receipts.

## Operational feedback

Every failed adapter attempt records an incident in the private execution
SQLite database. Records contain stage, a bounded error code, command index,
exit code, project revision and counters. They exclude raw output, task text,
source excerpts, credentials and application data. A process error, missing
result and a model-reported failure have distinct codes. Job status includes
safe diagnostic fields, so a failed command can be identified in the configured
argv list without logging its output.

A completion replay after a restart does not increment the count twice. A new
failed attempt increments the same incident. Verified success at the same
job/stage records recovery; this does not automatically close a general repair
issue or prove prevention of recurrence.

```sh
python -m factory --config /private/project/config.json reliability
python -m factory --config /private/project/config.json reliability --sync
```

Configure `reliability_tracker_repo` to a local Beads repository for repair
triage. Configure `reliability_sources` with other factory **state directories**
to aggregate their incidents; do not point it at application databases.
Export searches for the incident key before creating an issue, including after
loss of the local index. Recurrence reopens a closed issue when recovery has not
been observed. An unavailable or older source database is reported explicitly.
Issues carry the `factory-reliability` label and no implementation-readiness
labels: a red application test alone is not proof of a factory defect.

## Automatic preparation of factory fixes

For bounded automatic implementation, create a separate factory configuration:

```json
{
  "role": "factory-repair",
  "repo": "/path/to/software-factory",
  "state_dir": "/private/factory-repair-state",
  "base_branch": "main",
  "runtime": "codex",
  "task_policy": "source",
  "integration_policy": "disabled",
  "auto_merge": false,
  "tracker_enabled": false,
  "max_active_jobs": 1,
  "codex_executable": "/absolute/path/to/codex",
  "quality_gates": [["/absolute/path/to/python3.13", "-m", "unittest", "discover", "-s", "tests", "-v"]],
  "reliability_repair_paths": ["factory", "tests", "docs", "skills/factory"]
}
```

Start its supervisor through your normal durable service mechanism. Point the
project supervisor's `reliability_repair_config` to that private configuration.
Set `reliability_factory_repo` to the canonical factory source checkout as well;
dispatch verifies the target belongs to that Git repository and is separate
from the application repository. Include `repo` and `state_dir` as usual in
the project configuration.
With this setting or a tracker configured, the project supervisor runs a
separate feedback process every 60 seconds (minimum configurable interval 30s).
The feedback process cannot stall the project's reconciliation heartbeat.

An environment/infrastructure incident can dispatch one corrective job to this
separate supervisor. Ordinary quality/security gate failures and model-reported
failures require at least three observations before automatic dispatch. The
repair prompt contains only sanitized incident metadata. The repair supervisor
must be running and ready, reserve one implementation slot, and use explicit
allowed paths. It performs normal implementation, deterministic tests and
independent review. The same incident cannot spawn a new repair repeatedly;
failed repairs require investigation instead of blind resubmission.

```sh
python -m factory --config /private/project/config.json reliability --dispatch
```

The operational loop is:

1. Observe a project failure and preserve safe evidence.
2. Deduplicate an incident and create/update its repair issue.
3. Prepare a bounded factory fix in a separate worktree.
4. Run regression/full tests and independent review of the exact revision.
5. Review and merge the delivered branch, then stage the immutable runtime.
6. Validate the candidate against the affected project before activation.
7. Resume the existing project job and observe recovery or recurrence.

Steps 1–4 can run automatically with the configurations above. Merge and runtime
activation remain controlled rollout steps; the loop never edits or replaces a
running factory. A reviewed branch continues to reserve the repair slot until
its delivery has been reconciled. This is deliberate backpressure, not an
unlimited background stream of speculative self-modifications. Tracker sync
failures are persisted in `reliability/sync-status.json`; local evidence remains
available for retry.
