# Runtime usage and cost diagnostics

Feature cost is measured per tracked ticket. Inspect its attempt records for
runtime/model provenance, tokens and price estimates; the job summary includes
all its recorded implementation, review and fix attempts. Project summaries
aggregate tickets without counting retries twice.

Each new model attempt produces a private `usage.json` beside its existing
worker receipts. The collector reads bounded JSONL frames from stdout, drains
stderr, and retains only counters, owned session metadata, timestamps, coverage
and a pricing snapshot. Prompt text, assistant messages, tool output, stderr,
credentials and application data are not written to this journal.

```sh
python -m factory --config /private/project/config.json usage
python -m factory --config /private/project/config.json usage --job PROJECT-TICKET
python -m factory --config /private/project/config.json usage --related
```

Codex/OpenCode skill entry points are `$factory usage [ID]` and
`/factory usage [ID]`. The report never submits work or starts a supervisor.
`status` JSON and the terminal panel include compact per-job totals. The detailed
report contains each attempt and stage; failed attempts/retries count once.
`--related` aggregates configured reliability source state directories, deduped
by canonical state path. A repair job records its source incident/project hashes
without assigning its cost twice to the source project.

## Counters and model provenance

The supported contracts are [Codex exec 0.171 JSONL](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/exec_events.rs)
and [OpenCode 2.4 run JSON](https://github.com/anomalyco/opencode/blob/v2.4.1/packages/cli/src/run/noninteractive.ts).

Codex's final `turn.completed` usage is cumulative for the fresh thread. Later
snapshots replace earlier snapshots; they are not added. Cache reads and writes
are disjoint subsets of reported input; reasoning is a subset of output.
OpenCode reports individual steps: input is fresh input and output excludes
reasoning. Steps are deduplicated by session ID and message ID, including the
final reconciliation that can have a different part ID.

The canonical stored counters are:

- `input_tokens`: fresh input, excluding both cache buckets.
- `cached_input_tokens`: cache reads.
- `cache_write_input_tokens`: cache writes.
- `output_tokens`: output including reasoning.
- `reasoning_output_tokens`: informational subset of output, never added twice.

The report also preserves inclusive `reported_input_total_tokens`. A known
inclusive total can remain available even when an unsupported cache breakdown
makes pricing partial. Missing or unsupported components stay null. Codex's
public exec stream reports usage at completion, so its live token count can
remain unavailable during a long turn; OpenCode can update after each step.

`requested_model` is separate from runtime-reported metadata. Codex exec's
JSONL contract does not expose model/provider and suppresses the human model
banner: those fields remain unknown. OpenCode optionally queries metadata for
**only the session created by that attempt**, validates the returned ID and
project directory, then keeps its selected model/provider. It never exports
messages or searches historical sessions. This session selection is not proof
of the provider-side routed model or the identity used for every step. Missing
metadata is explicit and is not filled from a requested model.

Optional `codex_model` and `opencode_model` settings pass an explicit requested
model to the corresponding CLI. They do not turn the requested identity into an
observed identity, and existing model selection is unchanged when omitted.

## Coverage and persistence

Every attempt has an atomic, mode-0600 snapshot. Stream sizes, pending step
identities and response buffers are bounded. A timeout or incomplete/malformed
stream keeps partial evidence. An unavailable usage snapshot is never represented
as zero tokens or free execution. Failure to persist telemetry blocks execution
with `environment_telemetry_unavailable` rather than silently losing billing
evidence. A killed process can leave an interrupted snapshot; the report marks
it incomplete once the job moves away from that attempt.

Repeated report reads and supervisor restarts do not increment counters. Reports
read one snapshot per attempt and validate its binding to the factory job and
attempt directory. Legacy attempts without usage files are counted as missing;
there is no attempt to recover numbers from private conversation logs. Their
cost stays unknown. Testing stages run deterministic commands and are excluded
from LLM token totals.

## Explicit, versioned price estimates

There are two separate monetary fields:

- `runtime_reported_cost`: what OpenCode reported, which can be endpoint data or
  its internal catalog fallback. A reported zero is preserved. Partial coverage
  has a `known_amount`, not a claim about the whole execution.
- `estimated_cost`: API-equivalent cost from an explicit catalog. It is not an
  invoice or a claim about the actual cost of a Codex/OpenCode subscription.

A catalog is validated with exact decimal strings, source URLs, local effective
dates, currencies and a content hash. Rates multiply disjoint token buckets;
reasoning is already included in output. Cache-write prices replace ordinary
input prices; they are not surcharges. Different currencies remain separate.
The journal freezes the selected catalog and any explicit model assumption for
that attempt. Later configuration/catalog changes do not rewrite its history.

A checked-in [OpenAI Standard/short-context snapshot](pricing/openai-standard-short-2026-11-23.json)
was verified against the [official pricing page](https://developers.openai.com/api/docs/pricing)
on 2026-11-23. It contains GPT-6.2 Sol and Astra. It is an **opt-in estimate
scenario**, not automatic model/tier detection or a default billing assumption.
Its local effective date identifies this catalog version. Cache-write rate is
left unknown because the event does not identify the selected cache TTL. Choose
a separate, explicitly documented rate for that TTL when needed. Rates can
change; adding a new catalog creates a new version.

To enable estimates for future attempts, copy the selected catalog object into
your private configuration's `usage_pricing`. If the runtime does not report the
model, `usage_price_assumptions` can explicitly select a scenario, for example:

```json
{
  "usage_price_assumptions": {
    "codex": {"provider": "openai", "model": "gpt-6.2-sol"}
  }
}
```

This changes the estimate, not execution routing. The report marks the pricing
basis `configured_assumption` and the panel labels the assumed model. Without
an observed identity or an explicit assumption, a catalog alone cannot price an
unknown model.

To inspect a new scenario for existing data without changing its original cost:

```sh
python -m factory --config /private/project/config.json usage --job PROJECT-TICKET \
  --reprice-catalog docs/pricing/openai-standard-short-2026-11-23.json \
  --assume-model codex=openai/gpt-6.2-sol
```

The result adds `recalculated_estimate` separately at attempt/stage/job/project
levels. It never overwrites the original journal or substitutes an estimate for
a runtime-reported zero. There is no automatic price refresh, currency exchange,
budget enforcement or task cancellation in this feature.
