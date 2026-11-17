---
name: factory-maintenance
description: Triage software-factory maintenance reports for dependency vulnerabilities, outdated packages, container findings, and code security rules; prepare bounded fixes through the normal tested delivery pipeline.
---

Use `factory.maintenance.scan(repo, output_dir)` for a new report and
`load_report(output_dir, revision=current_sha)` to check freshness. The report
contains package/advisory or rule IDs only. Raw scanner output, source excerpts,
credentials, financial state, and private paths must stay out of reports,
tracker entries, agent prompts, and logs.

Treat `error`, `unavailable`, and `stale` as incomplete coverage. Missing tools,
unpinned Python requirements, unsupported lock formats, unresolved container
images, and registry failures need an explicit follow-up; they are not a clean
scan. Docker `not_applicable` means no tracked Docker configuration was found.
The Python adapter audits pinned requirements without installing dependencies;
it does not claim to audit the current virtual environment.

For each finding, verify the exact affected package, advisory ID or rule ID,
current dependency path and fixed version using maintained advisory/vendor
sources. A scanner finding requires investigation, not automatic suppression.
Outdated does not necessarily mean vulnerable. Keep unavailable scans separate
from verified vulnerabilities.

Search Beads before creating work. Deduplicate by repository, package and
advisory/rule ID; update existing work when the same issue recurs. Record the
scanned revision, verified impact, bounded remediation and acceptance checks.
Use native dependencies for real blockers. Avoid copying advisory prose or
scanner stderr into Beads.

Deliver fixes through the factory's ordinary isolated implementation, required
tests, independent review, integration and recovery checks. Prefer the smallest
compatible dependency update; preserve lockfile consistency and run the affected
E2E scenarios. Do not run `npm audit fix --force`, install scanners, relax checks,
or suppress a finding as part of triage. Broader upgrades require their own
explicit scope. Rescan the resulting exact revision and close only verified
findings; new code invalidates an earlier clean scan.
