---
name: factory-reliability
description: Close the software-factory operational feedback loop across projects: diagnose incidents, repair the factory in an isolated branch, validate exact revisions and prepare controlled rollout.
---

Use this workflow for requests to improve factory reliability and for
`factory-reliability` issues. Read `docs/reliability-loop.md` in the factory
checkout. Do not stop after diagnosing a project or restarting its job.

1. Read the project's factory config, safe job status, reliability report and
   existing Beads repair issues. Preserve current ownership. Deduplicate work.
2. Separate application defects, missing project dependencies, runtime failures
   and factory mechanism defects. Never infer success from a live supervisor.
3. Reproduce the mechanism on synthetic data in a disposable project/worktree.
   Repair factory preflight, bootstrap, lifecycle, diagnostics or recovery in
   an isolated factory branch. Keep project-specific setup in trusted config.
4. Add a meaningful regression, run full factory checks and arrange independent
   review of the exact revision. Do not weaken quality gates or copy raw agent
   output, private state or prompts into evidence or tracker entries.
5. Validate the candidate against the affected project's synthetic gates. Keep
   factory runtime activation separate from application integration/activation.
   Never edit an installed runtime in place. Stage the reviewed immutable SHA,
   preserve the previous runtime and use the existing service readiness/rollback
   mechanism for an authorized activation. No automatic merge.
6. Resume the existing project job, preserving branch/revision and required
   full tests and independent review. Observe a fresh result. Record recovery
   in the originating issue with the factory revision and project gate results.
   Do not close the general defect merely because one retry passed.

When automatic repair dispatch is configured, its supervisor must use the
`factory-repair` role, one job at a time, explicit file scopes and disabled
integration. A failed or review-pending repair is a concrete item to resolve,
not a reason to create duplicate work or deploy unreviewed code.
