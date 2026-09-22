# Trace V1 Design

**Issue:** #23 — Day 5 Trace 和实验数据
**Status:** implemented locally, pending independent review
**Date:** 2026-09-22

## Purpose

`trace.v1` is a local, append-safe analysis record for one Shadow decision
attempt.  It correlates records that other modules have already produced and
records only observed model metrics.  It is not a decision input, a Gate
input, an execution authorization, or a command path.

The contract supports the Day 5 order `#23 → #21 → #22`: #23 defines the
store and contract, #21 writes one trace through the Shadow runtime, and #22
uses that runtime to inject failures.

## Non-goals

- Do not copy any State, Vision, Experience, Strategy, Gate, Runner, Bridge,
  Episode, Feedback, or Outcome business object into a trace.
- Do not infer missing facts, calculate a reward, estimate token/cost/latency,
  trigger a model, or make a decision.
- Do not change Phase3, MQTT, ActuatorLayer, `manual_water`, the pump, or any
  existing protocol implementation.
- Do not treat Trace as a trusted admission receipt or as a mitigation for a
  same-permission local file editor.

## Placement and public API

Add an isolated `services/soil3/trace/` package with:

- `trace.v1.schema.json` — JSON Schema describing serialized records.
- `trace_v1.py` — `TraceStore`, validation helpers, `TraceError`, and
  `new_trace_id()`.
- `service.py` — a narrow CLI for create, set decision fields, append a
  feedback reference, set an outcome reference, and read.
- `README.md` — contract and boundary summary.

`TraceStore` is the only writer.  It exposes no generic patch or delete
operation:

1. `create()` creates a new record and returns it.
2. `set_decision(trace_id, ...)` sets selected decision-stage fields once.
3. `append_feedback_refs(trace_id, refs)` appends new feedback references.
4. `set_outcome_ref(trace_id, ref)` changes the outcome from `null` once.
5. `read(trace_id)` returns a deep copy.

The Store generates the identifier: `tr-` followed by 24 lower-case hex
characters.  Records are JSON files under the caller-selected store directory
and use the project atomic JSON write pattern.

## Record shape

The exact schema will require these top-level fields:

```json
{
  "schema_version": "trace.v1",
  "trace_id": "tr-0123456789abcdef01234567",
  "created_at": "2026-09-22T00:00:00Z",
  "updated_at": "2026-09-22T00:00:00Z",
  "decision": {
    "state_ref": null,
    "vision": {"availability": "not_requested", "ref": null},
    "experience": {"availability": "not_requested", "ref": null},
    "model_metrics": {
      "provider": null,
      "model": null,
      "token_usage": null,
      "cost": null,
      "latency": null
    },
    "strategy_ref": null,
    "gate_ref": null,
    "gate_decision": null,
    "gate_reason_codes": null,
    "runner_ref": null,
    "bridge_ref": null,
    "episode_ref": null
  },
  "execution": {
    "phase3_called": false,
    "physical_actions_performed": false
  },
  "feedback_refs": [],
  "outcome_ref": null
}
```

The fields are association metadata, not business-object copies.  In
particular, Strategy actions, State facts, Gate warning codes, Runner steps,
Bridge responses, Episode content, Feedback observations, and Outcome values
never appear in a Trace record.  `gate_reason_codes` is the one analysis
projection permitted from Gate: it is a list of its already-issued machine
codes, not a recomputed reason or a decision input.

### Reference value

Every non-null `*_ref` value has exactly:

```json
{
  "schema_version": "strategy.v1",
  "path": "runtime/strategy/latest.json",
  "sha256": "<64 lower-case hex characters>",
  "record_id": null
}
```

`record_id` is nullable because not every producer has a durable identifier.
When one exists, for example `episode_id` or a Feedback identifier, it is
stored as a string.  `path` is a non-empty local record location; `sha256` is
the producer-record fingerprint supplied by the caller.  Trace validates the
reference shape but does not load, revalidate, or transform its target.

### Availability and metrics

Vision and Experience each use one of `available`, `unavailable`, or
`not_requested`:

- `available` requires a reference.
- `unavailable` and `not_requested` require `ref: null`.

This describes the actual runtime event without inventing an observation or a
retrieval result.

`model_metrics` is one set-once value.  `provider` and `model` are strings
only when a real model invocation identifies them; otherwise they remain
`null`.  Each numeric metric is either `null` or a source-bearing object:

```json
{"source": "provider_response.usage", "value": 123, "unit": "tokens"}
{"source": "provider_response.billing", "value": 0.0125, "unit": "CNY"}
{"source": "runtime_monotonic_clock", "value": 842, "unit": "ms"}
```

The Store accepts only `provider_response.usage` for token usage,
`provider_response.billing` for cost, and `runtime_monotonic_clock` for
latency, alongside a finite non-negative numeric value and each metric's fixed
unit. It performs no calculation, estimation, currency conversion,
aggregation, or fallback. A caller that has no observed value leaves the
metric `null`.

## Update invariants

Decision-stage fields are independent set-once fields.  They are initialized
to their explicit empty value and may transition from that empty value to one
valid supplied value exactly once.  Repeating the identical value is a no-op;
supplying a different value is refused.  The decision-stage fields are:

- `state_ref`, `vision`, `experience`, `model_metrics`;
- `strategy_ref`, `gate_ref`, `gate_decision`, `gate_reason_codes`;
- `runner_ref`, `bridge_ref`, `episode_ref`.

The Store validates all proposed changes before writing.  A request that
contains one invalid, conflicting, or unknown field fails as a whole and the
serialized file remains byte-for-byte unchanged.

`feedback_refs` is append-only.  Each request must contain one or more valid
Feedback references; it adds only identities not already present.  A duplicate
reference is refused rather than re-counting a later observation in an
experiment.  Existing entries cannot be edited, reordered, or removed.

`outcome_ref` is initialized to `null`, may become one valid Outcome reference
once, and follows the same identical-retry/no-op and conflicting-value/refusal
rule as a decision field.

`execution` is written at creation as exactly
`{"phase3_called": false, "physical_actions_performed": false}` and is never
accepted by any update operation.

## Failure behavior

Malformed IDs, schema versions, references, availability pairs, metric source
objects, Gate decisions, reason-code lists, unknown update keys, and invalid
state transitions raise `TraceError` with machine-readable reasons.  Missing
facts are represented by the explicit initial empty values; they are never
repaired or inferred.  Persistence errors fail loudly.  No rejected operation
writes a partial record.

## Regression matrix

Before implementation, tests must be added first and observed failing against
the absent module.  The completed suite must cover:

1. Store creation: ID/timestamp/schema shape, initial explicit empties, and
   fixed execution booleans.
2. Schema consistency: required fields, enums, patterns, and
   `additionalProperties: false` match Store output.
3. Reference-only persistence: record JSON contains no embedded Strategy
   actions, State facts, Gate payload, Runner steps, or Feedback/Outcome
   business object.
4. Decision set-once semantics: initial write, identical retry, conflicting
   update, multi-field atomic refusal, and byte-for-byte persistence check.
5. Vision/Experience availability pairing and explicit missing/not-requested
   representation.
6. Model metric null handling, valid source-bearing values, and rejection of
   calculated-looking/malformed/negative/unknown-unit values.
7. Feedback append-only semantics, duplicate refusal, order preservation, and
   byte-for-byte refusal behavior.
8. Outcome `null → ref` once-only behavior and conflicting update refusal.
9. CLI round trip plus structured refusal output.
10. Import/boundary inspection showing the Trace package does not import or
    invoke Phase3, MQTT, ActuatorLayer, or pump control.

## Integration contract for later Issues

Issue #21 creates one Trace at Shadow runtime entry and supplies actual
references as each stage completes.  It cannot alter `trace.v1` or its
invariants.  It leaves Feedback and Outcome empty while the Episode is open.
Later feedback/outcome handling appends only references for the same `trace_id`.
Issue #22 consumes the resulting runtime to test failures; it adds no new
attack model for same-permission local JSON editors.
