# Changelog

## 0.1.3 — 2026-11-24

- Collect bounded, private Codex/OpenCode token diagnostics per attempt, stage,
  task and project, including failed attempts and retries. Missing historical
  data and unreported model identities remain explicit.
- Add versioned, optional API-equivalent cost estimates and read-only
  recalculation, separate from runtime-reported costs.
- Show token and cost summaries in status reports and terminal panels; provide
  `factory usage` through the CLI and Codex/OpenCode entry points.
- Fix a completion-receipt race that could block successful work after a
  supervisor restart or a fast worker exit.
- Parse OpenCode JSON responses containing quoted braces, escapes and prose.
- Disable the unisolated native OpenCode implementation path and reject the
  former `verified` flag. The legacy canary probe can no longer enable it.
  OpenCode can submit factory work to a Codex executor; planning review remains
  available. An enforced native OpenCode implementation sandbox is future work.
- Align Python package metadata with the release version.

Runtime module contents match the independently reviewed `20ce10f` deployed to
both local services. New token collection applies to future attempts; previous
attempts without records are not backfilled. API-equivalent prices are estimates,
not subscription invoices. Codex JSONL currently does not expose observed model
identity, so pricing an unknown model requires an explicit scenario.

## 0.1.2 — 2026-11-22

Previous GitHub release, at `0fa9670`.
