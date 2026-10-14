# Trigger Shadow Phase 0 Design

## Scope

Issue #62 adds an observation-only Cloud Invocation Trigger beside the existing
soil3 Shadow runtime. It also completes the existing `state.v1` producer inputs
needed by that Trigger. It does not condition, delay, or suppress Qwen calls.

The runtime remains:

```text
State -> Trigger Shadow record -> Qwen Strategy -> Validator -> gate.v2
      -> Runner dry-run -> Bridge verification-only -> Episode
```

The Trigger has no execution authority. `strategy.v1`, `gate.v2`, Trace,
Runner, Bridge, Phase3, MQTT, and ActuatorLayer are unchanged.

## State input completion

### Dynamic parameters

The existing `StateBuilder.build_from_health_snapshot(snapshot, parameters)`
interface remains unchanged. A new read-only State adapter loads:

- `FC`, `TARGET_LOW`, and `K_P` from the Phase3 `evolving_params.json` path
  explicitly supplied by `RuntimeConfig`;
- `HARD_SAFETY_LOW` from the same health snapshot's
  `system_state.hard_safety_low_guard.effective_low` fact.

The adapter never instantiates Phase3 `ConfigManager`, writes a lock file,
persists defaults, derives thresholds, or falls back to constants. Each value
is accepted only when it is a finite number. A missing or malformed source
becomes `None`, which `StateBuilder` preserves as a missing State fact.

### Historical telemetry

The openGauss adapter gains an optional history query. Its upper bound is the
snapshot timestamp, so a future row cannot enter State. Its lower bound is six
hours plus the existing `StateBuilder` 30-minute horizon tolerance. Valid rows
are bounded by selecting the newest configured number of rows, then returned
in ascending timestamp order. Invalid rows are omitted; query
failure returns no canonical facts and never falls back to CSV.

`build_health_snapshot` keeps its current latest-only behavior by default.
The agent runtime explicitly requests the historical window. This avoids
changing other callers while allowing the unchanged StateBuilder trend logic
to produce `humidity_1h`, `humidity_3h`, and `humidity_6h`.

## Decision Context

`build_decision_context` projects only factual State and durable local records:

- current humidity;
- FC, TARGET_LOW, and effective hard safety low;
- distances to those three dynamic boundaries;
- 1h, 3h, and 6h humidity trends;
- normalized Phase3 safety/protection facts;
- State soil and Phase3 freshness;
- the latest durable action receipt, including its evidence level;
- the latest matching factual Feedback window when available;
- the previous simulated Shadow CALL baseline.

Unknown facts remain `None` or an explicit unavailable status. A
`command_completed` receipt may be observed, but only `manual_confirmed` or
`device_confirmed` evidence is treated as causal post-action context.
Native/manual receipt association reuses the Collector's public deterministic
action-to-Episode resolver, so Feedback lookup does not guess a tracking path.
For real Phase3 records that predate an explicit `active` member, pending soak
uses its persisted deadline and dynamic cooldown combines its duration with
the same Phase3 state's last normal irrigation timestamp. Missing or malformed
timing facts remain invalid rather than being normalized to inactive.

The semantic projection contains dynamic-boundary relations, trend directions,
normalized protection/safety states, and confirmed action/Feedback identities.
It intentionally does not compare the complete State hash.

## Deterministic evaluator

Evaluation priority is fixed:

1. Return `LOCAL_SAFETY_ONLY` when a required core fact is unavailable,
   telemetry/Phase3 state is beyond the existing Gate deny age, the effective
   hard safety line is reached, a dangerous safety flag is active or malformed,
   or the predictor circuit is OPEN/invalid.
2. Return `SKIP_CLOUD` while `pending_soak`, normal `dynamic_cooldown`,
   `recent_response_guard`, or `watering_trigger_guard` is active.
3. Return `CALL_QWEN` when no valid Shadow CALL baseline exists.
4. Return `CALL_QWEN` when the semantic projection differs from the last
   simulated Shadow CALL baseline.
5. Otherwise return `SKIP_CLOUD` with `semantic_context_unchanged`.

These are invocation-observation rules, not irrigation rules. No fixed plant
moisture zone or watering threshold is introduced. Numeric tuning and
heartbeat calibration are intentionally deferred to Issue #63 replay.

## Shadow persistence

Each pipeline trace owns one `trigger_shadow.v1` JSON artifact under
`runtime_root/trigger_shadow/<trace_id>.json`. The initial write contains the
State reference, decision, reason codes, Decision Context snapshot, baseline
reference, and an `actual_qwen_result` of `None`.

After the existing cloud chain runs, the artifact is finalized once with a
small factual summary:

- provider/model identity when present;
- Validator accepted flag and reason codes;
- Strategy artifact reference when accepted;
- whether a water action was proposed and total proposed seconds;
- Gate artifact reference, decision, and reason codes when Gate ran;
- real token/cost/latency metrics only when their existing source supplied
  them.

The record references rather than copies State, Strategy, Gate, Episode, or raw
provider output. `trace.v1` is not modified.

The simulated baseline advances only when the Trigger decision was
`CALL_QWEN` and the real Qwen cycle produced a Validator-accepted Strategy.
Actual Qwen results from `SKIP_CLOUD` and `LOCAL_SAFETY_ONLY` cycles remain
available for comparison but do not advance the counterfactual baseline.

## Failure boundary

Trigger context construction, evaluation, baseline lookup, and artifact writes
are best-effort observations. Their exceptions are caught at the runtime
boundary and cannot skip or delay `run_chain`. When possible, the run record
contains the Trigger artifact path; otherwise it records no Trigger path and
the existing cloud chain proceeds unchanged.

Provider/Validator failure still finalizes the Trigger artifact before the
runtime raises its existing failure. Gate deny remains a normal recorded cloud
outcome. Trigger never imports or invokes Phase3, MQTT, ActuatorLayer, or
`manual_water`.

## Verification

Tests prove real parameter mapping, missing-fact preservation, historical
trend construction without look-ahead, all three Trigger decisions, baseline
semantics, artifact finalization, and unconditional Qwen invocation. Existing
State, runtime, strategy, Gate, Runner, Bridge, Episode, Trace, and Feedback
regressions remain green.
