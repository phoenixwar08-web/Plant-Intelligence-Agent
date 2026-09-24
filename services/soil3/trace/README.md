# Trace V1

`trace.v1` is a local, analysis-only record for one Shadow decision attempt.
It correlates already-produced records through references and records only
model metrics supplied with an explicit source. It does not copy business
objects, infer missing values, calculate reward, decide, authorize execution,
or issue a device command.

## Lifecycle

- `create()` creates a Store-generated `tr-...` identifier and explicit empty
  associations. Its execution value is permanently
  `{"phase3_called": false, "physical_actions_performed": false}`.
- `set_decision()` accepts only named decision associations. Each can move from
  its defined empty value once; an identical retry is a no-op and a different
  value is refused.
- `append_feedback_refs()` appends only new Feedback references in caller
  order. It never edits, removes, reorders, or duplicates an existing fact.
- `set_outcome_ref()` changes the Outcome reference from `null` once; a
  different later value is refused.
- `read()` returns a deep copy.

All rejected updates validate before writing, so the stored JSON remains
unchanged on failure.

## References and metrics

References contain only `schema_version`, `path`, `sha256`, and optional
`record_id`. Trace never loads or embeds their target records. Vision and
Experience absence is explicit as `unavailable` or `not_requested` with a
null reference.

Token usage, cost, and latency are each `null` unless their respective actual
sources provide a numeric value: `provider_response.usage`,
`provider_response.billing`, and `runtime_monotonic_clock`. Trace never
estimates, aggregates, or converts these values.

## CLI

Every command requires an explicit `--store-dir`; no default points to runtime
or production data.

```text
python -m services.soil3.trace.service --store-dir <dir> create
python -m services.soil3.trace.service --store-dir <dir> read --trace-id <id>
python -m services.soil3.trace.service --store-dir <dir> set-decision --trace-id <id> --updates <object.json>
python -m services.soil3.trace.service --store-dir <dir> append-feedback --trace-id <id> --refs <array.json>
python -m services.soil3.trace.service --store-dir <dir> set-outcome --trace-id <id> --ref <object.json>
```

Refusals print `{"error": "...", "reasons": ["..."]}` to stderr and exit
with status 2.
