# Trigger Phase 1 Conditional Qwen Invocation Design

## Status and scope

This design implements GitHub Issue #69 after the Phase 0 Trigger observer from
Issue #62 and the safety-fact compatibility work from Issue #67. It turns the
existing Trigger decision into a runtime admission decision for cloud work.

This Issue changes only the soil3 Shadow runtime control flow and its tests and
documentation. It does not change Trigger rules or thresholds, `state.v1`,
`strategy.v1`, `gate.v2`, `trace.v1`, Runner, Bridge, Phase3, MQTT,
ActuatorLayer, or execution permissions. It does not deploy or enable the
pipeline timer.

Issue #63 remains the historical comparison and calibration task. Issue #64
remains the longer online validation task. This change does not close or
replace either Issue.

## Current behavior and problem

The Phase 0 runtime performs:

```text
State -> Trigger Shadow record -> Vision/Experience -> Qwen -> Validator
      -> Gate v2 -> Runner dry-run -> Bridge verification -> Episode
```

The Trigger records `CALL_QWEN`, `SKIP_CLOUD`, or `LOCAL_SAFETY_ONLY`, but all
three outcomes still proceed to Qwen. Trigger/context/storage failures are also
caught and ignored, after which Qwen still runs. Re-enabling the five-minute
pipeline timer in this state would therefore restore one model request per
cycle.

## Selected approach

Add one early admission branch inside the existing Shadow runtime immediately
after State and a durable Trigger Shadow record. The evaluator remains the
single source of the three decisions.

```text
create Trace
    -> build and persist State
    -> associate State with Trace
    -> build Decision Context
    -> evaluate Trigger
    -> durably persist trigger_shadow.v1
       |
       +-- SKIP_CLOUD
       |     -> persist no-cloud runtime record
       |     -> return without Vision/Experience/Qwen or downstream artifacts
       |
       +-- LOCAL_SAFETY_ONLY
       |     -> persist local-safety runtime record
       |     -> return without Vision/Experience/Qwen or downstream artifacts
       |
       +-- CALL_QWEN
             -> collect enabled Vision/Experience
             -> existing Qwen/Validator/Gate/Runner/Bridge/Episode flow
```

This placement gives the Trigger control over cloud-related work without
putting it inside the provider, Validator, Gate, or execution chain. It also
ensures the decision evidence exists before a cloud call begins.

### Alternatives rejected

1. A second scheduler or wrapper around the runtime was rejected because it
   would duplicate State generation, Trace creation, Trigger persistence, and
   failure handling.
2. Moving admission logic into Cloud Strategy was rejected because the provider
   would already have been invoked before it could skip itself.
3. Adding a new public protocol was rejected because the existing
   `trigger_shadow.v1` record already contains the decision, reasons, context,
   semantic projection, State reference, and baseline reference needed for the
   runtime branch.

## Runtime admission result

The private helper that starts Trigger evaluation will return an internal
result containing:

- the `TriggerShadowStore` needed only for later Qwen-result finalization;
- the persisted Trigger Shadow artifact path;
- the exact `TriggerDecision` returned by the existing evaluator.

This is an internal Python value, not a stored protocol. Callers must not infer
a decision from a missing path or construct a fallback decision.

The Trigger Shadow record must be written successfully before `CALL_QWEN` is
honored. A context, evaluator, baseline-read, or Trigger-store error produces no
synthetic Trigger decision and takes the fail-closed path described below.

## Branch semantics

### `CALL_QWEN`

The current successful Shadow chain remains unchanged after admission:

- enabled Vision and Experience are collected through their existing public
  interfaces;
- Qwen or the explicitly configured offline fixture is invoked once;
- Validator, Gate v2, dry-run Runner, verification-only Bridge, Trace, and
  Episode retain their existing behavior;
- `phase3_called=false` and `physical_actions_performed=false` remain true
  statements about the cycle;
- the Trigger Shadow record is finalized with the actual provider/Validator/
  Gate result when that information exists.

No Trigger rule, Gate rule, or downstream validation is weakened by admission.

### `SKIP_CLOUD`

This is a normal successful local cycle:

- the State, Trace, Trigger decision, reason codes, Decision Context, semantic
  projection, and baseline reference are retained;
- Vision and Experience are not requested because their only consumer in this
  runtime is the skipped Strategy call;
- Qwen, Validator, Gate, Runner, Bridge, and Episode are not started;
- no audit, Strategy, Gate, Runner, Bridge, or Episode artifact is created;
- the runtime writes and returns a no-cloud cycle record.

The Trigger Shadow `actual_qwen_result` remains `null`. This is the factual
representation of a model that was not called, not an unfinished fabricated
model result.

### `LOCAL_SAFETY_ONLY`

This is also a completed local cycle, but it is distinguished from an ordinary
skip in the runtime record:

- the same factual State and Trigger evidence is retained;
- no Vision, Experience, Qwen, Validator, Gate, Runner, Bridge, or Episode work
  starts;
- no local watering decision is made and no execution module is called;
- the runtime record exposes that cloud work was blocked by local safety facts.

The Trigger does not replace Gate or Phase3. `LOCAL_SAFETY_ONLY` means only that
the Shadow cloud chain stops before Strategy.

## Failure behavior

Trigger admission is fail-closed. The following failures must not call Qwen or
start any later stage:

- Decision Context construction failure;
- invalid or unreadable Trigger baseline history;
- evaluator failure or an unrecognized decision;
- Trigger Shadow directory or atomic-write failure;
- failure to read required local inputs used by the existing context builder
  when that failure escapes its current missing-value handling.

State is associated with the newly created Trace before Trigger admission, so a
failing cycle still has a factual State reference. The runtime then persists an
`agent_runtime.v1` failure record when its run store is writable and raises a
stable runtime error. This makes the failure visible to systemd/operations while
preserving the primary rule: no cloud invocation on uncertain admission.

A failure to persist the no-cloud runtime record must also never fall through
to Qwen. It may surface as an I/O error after the already-persisted State and,
when available, Trigger Shadow record.

Trigger finalization after a real Qwen call remains best-effort. A finalization
failure cannot retroactively undo that call; it leaves no eligible new model
baseline, which biases later cycles toward another call rather than toward an
unsafe skip.

## Persistence and observability

No public or frozen schema changes are required.

### Trace

Every cycle continues to create one `trace.v1`. Before admission the runtime
sets only its real `state_ref`.

For `SKIP_CLOUD`, `LOCAL_SAFETY_ONLY`, and Trigger failure:

- Vision and Experience retain their existing `not_requested` associations;
- model provider, model, token, cost, and latency remain `null`;
- Strategy, Gate, Runner, Bridge, and Episode references remain `null`;
- execution remains permanently `phase3_called=false` and
  `physical_actions_performed=false`.

This uses the existing Trace contract without adding Trigger fields to it.

### Trigger Shadow

For successful Trigger evaluation, the existing `trigger_shadow.v1` artifact is
the authoritative observation record for decision, reason codes, Decision
Context, semantic projection, and baseline reference.

Only a `CALL_QWEN` cycle can later populate `actual_qwen_result`. Non-call
records intentionally retain `null`, so `latest_valid_call_baseline()` continues
to select only a finalized, Validator-accepted `CALL_QWEN` record. Skips and
local-safety cycles never advance or replace the last model-decision baseline.

### Runtime cycle record

The internal `agent_runtime.v1` record will make admission visible with the
following fields:

- `trigger_status`: `recorded` or `failed_closed`;
- `trigger_decision`: one of the three decisions, or `null` on Trigger failure;
- `trigger_reason_codes`: the evaluator's exact codes, or a stable local
  failure code without exception text containing secrets;
- `cloud_invocation_status`: `called`, `skipped_trigger`,
  `blocked_local_safety`, or `blocked_trigger_failure`.

For all non-call paths, provider/model and every downstream path/decision/id are
`null`, while stage status fields state why they were not started. No token,
cost, Qwen result, Strategy result, Gate result, Episode, or Feedback status is
invented.

`agent_runtime.v1` is an internal run artifact, not a new public protocol or an
execution input.

## Ordering and partial-write guarantees

The required ordering is:

1. create Trace;
2. atomically persist State;
3. set the Trace State reference;
4. build/evaluate Trigger and atomically persist Trigger Shadow;
5. branch on the returned decision;
6. only for `CALL_QWEN`, enter the existing cloud chain.

This ordering prevents a cloud call from occurring before its admission record
exists. It does not claim a distributed transaction across separate files.
Crash evidence is interpreted conservatively:

- State/Trace without Trigger means admission did not complete;
- a non-call Trigger record without a runtime cycle record still proves that
  no later work was authorized by that decision;
- a `CALL_QWEN` Trigger record with `actual_qwen_result=null` means invocation
  or finalization is unknown, not that a model decision succeeded.

## Test strategy

Tests will be written before implementation and will verify:

1. `CALL_QWEN` invokes `run_chain()` exactly once and preserves the existing
   complete Shadow flow.
2. `SKIP_CLOUD` never calls Vision, Experience, `run_chain()`, Validator/Gate,
   Runner, Bridge, or Episode, and persists the factual local records.
3. `LOCAL_SAFETY_ONLY` has the same no-downstream guarantee and a distinct
   observable status.
4. Trigger/context/evaluator/store failure never calls Qwen or downstream
   components, persists a fail-closed run record when possible, and raises a
   stable error.
5. Non-call Trigger records keep `actual_qwen_result=null` and do not become the
   model baseline.
6. Non-call Trace records contain only the State association plus existing
   `not_requested`/null values and permanent non-execution facts.
7. No non-call path creates Strategy, Gate, Runner, Bridge, Episode, audit,
   Vision, or Experience artifacts.
8. Existing CALL-path provider failure, validation rejection, Gate deny,
   dry-run Runner, Bridge, Trace, and Episode regressions remain valid.

Focused runtime and Trigger tests will run first, followed by the relevant
State, Strategy, Gate, Runner, Bridge, Trace, Episode, Vision, and Experience
regressions. A full suite will run when the local environment permits.

## Documentation and operational boundary

After implementation, `SYSTEM_ARCHITECTURE.md` and the soil3 runtime README will
describe Phase 1 conditional admission and the no-cloud artifacts. The old
Phase 0 statement that Qwen is unconditional will be replaced, not left as a
second conflicting behavior description.

This Issue stops at a Review-ready PR. It does not update production config,
deploy to openEuler, enable `plant-agent-soil3-pipeline.timer`, run a live Qwen
smoke test, or invoke Phase3/MQTT/ActuatorLayer. Those actions require the later
approved deployment and validation step.
