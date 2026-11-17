# Local Software Factory

A separate local development product. BuchOS is its first project adapter.
The supervisor runs independently of chat sessions. Herdr shows agent terminals
and status; Codex CLI executes bounded jobs and independent review sessions.
OpenCode has a read-only planning/review adapter; its implementation sandbox is
not configured in this pilot. Cloud execution is future work.

## Current pilot

The journaled workflow is implementation → deterministic checks → independent
review of the exact commit → fixes/retest → verified code checkpoint → automatic
integration. A killed supervisor adopts its existing worker, rather than starting
a duplicate. Retry, timeout and fix-loop limits make failures visible.

The first rollout automatically integrates **documentation only**. Agent
instructions and skills are excluded from that classification. Source changes
can reach tested/reviewed status, but activation waits for the broader mutable
state recovery milestone. No application database is migrated, restored, or
silently approved by this pilot. This limit is explicit in status/configuration.

Task intake reads Beads and checks GitHub reviews before claiming open,
unassigned tasks labeled `factory-pilot` and `ready-for-development`. It preserves
other agents' work and enforces the global task limit conservatively. The initial
pilot processes one job at a time. Finished work is verified on local `main` before
closing its Bead. It does not push to GitHub in this first rollout.

## Run and inspect

Requires Python 3.11+, Git, authenticated Codex CLI, Beads and GitHub CLI.
Use `python -m factory --help` from this product directory.

```sh
python -m factory --config /private/path/config.json init \
  --repo /path/to/buchos --state-dir /private/path/state
python -m factory --config /private/path/config.json run
python -m factory --config /private/path/config.json status
python -m factory --config /private/path/config.json status --watch
```

`scripts/install_local.py` installs a reviewed `main` revision to a separate
durable directory, optional isolated scanner tools, and a per-user macOS
LaunchAgent. `--start` activates it. Existing installations are never overwritten.
The installed service starts at login and restarts after failure while the Mac is
awake. It does not run while the machine is asleep or powered off.

```sh
python scripts/install_local.py --destination /path/to/software-factory \
  --repo /path/to/buchos --state-dir /private/path/factory-state \
  --install-scanners --start
launchctl bootout gui/$(id -u)/dev.software-factory.buchos
```

The last command stops the supervisor; already-running bounded workers finish
and keep their completion receipts for the next start. To restart, bootstrap the
generated `~/Library/LaunchAgents/dev.software-factory.buchos.plist`.

## Recovery

Each automatic documentation integration first creates a private Git bundle,
validates it, and clones it into a disposable directory to verify recovery of the
previous `main`. An integration journal survives a crash between Git and the
execution database. Code recovery never rewinds application data.

```sh
python -m factory --config /private/path/config.json checkpoint
python -m factory --config /private/path/config.json restore-code-into \
  --checkpoint /private/path/state/backups/CHECKPOINT_ID \
  --destination /new/recovery/directory
```

Restoration creates a new directory, verifies the bundle hash, and checks out the
checkpoint's recorded base revision. It does not overwrite a checkout. Backups
are local protection against software failures; off-device/disaster recovery is
not configured. The SQLite checkpoint primitive is tested with populated WAL
data, but full application-state release/restore is still a separate milestone.

## Maintenance

The service schedules read-only checks daily. A command can trigger them now:

```sh
python -m factory --config /private/path/config.json maintenance
```

Scanners receive a disposable copy of approved tracked HEAD files, not the
working directory, private databases, environment files, or untracked user data.
An explicitly configured application Python interpreter supplies installed
package metadata only. Reports retain normalized package/advisory/rule IDs,
never raw secret matches or source snippets. Missing tools, partial coverage,
scanner failures and stale reports are visible, never reported as clean.

The reusable triage skill is `skills/factory-maintenance/SKILL.md`. Optional
tools include pip-audit, npm, Bandit, Semgrep, Gitleaks and Trivy. Docker checks
are not applicable if a project has no tracked container definitions. Docker
tag/digest update automation still needs an update-manager adapter.

## Verification

```sh
python -m unittest discover -s tests -v
python -m scripts.smoke_runtime
```

The second command spends model tokens on an actual isolated implementation,
independent review and automatic integration. It never opens BuchOS financial
data. Process recovery tests need normal user process-inspection permissions.
