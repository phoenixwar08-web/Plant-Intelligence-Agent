# Issue #67 Safety Fact Contract Compatibility

## Scope

Issue #67 aligns the read-only State, Trigger, and Gate interpretation of two
Phase3 safety facts. It does not change `state.v1`, Phase3, Qwen scheduling,
Runner, Bridge, MQTT, ActuatorLayer, or any physical execution path.

## Shared contract classification

The existing `PHASE3_SAFETY_FLAG_KEYS` remains the complete set of known facts
that State may preserve from Phase3. A second shared classification separates
facts that must be present from facts whose absence has an established normal
meaning:

- `pending_soak` is optional when absent because Phase3 persists it only while
  a soak task exists.
- `cloud_protection` is optional when absent because the repository has no
  production producer for this reserved fact.
- every other existing Phase3 safety fact remains required.

Optional does not mean trusted-safe. If either optional fact is present, its
value is validated and an active or malformed value keeps the existing
fail-closed behavior.

State continues to copy only facts that Phase3 actually supplied. It does not
write `false`, synthesize an inactive object, or otherwise change the frozen
`state.v1` output contract.

## `pending_soak` semantics

The compatibility rule follows the persisted Phase3 contract:

- missing or `null` means no persisted soak task, but is treated as inactive
  only when `phase3_state_age_sec` is finite, non-negative, and no greater
  than the existing Gate policy `deny_age_seconds`; unavailable or stale
  Phase3 state keeps the fact missing;
- a persisted object must contain finite `pump_end_time_wall`, `water_sec`, and
  `pre_humidity` values plus a finite, non-negative `soak_duration`, matching
  the fields Phase3 requires to restore `PendingSoak`;
- malformed scalar or object values remain invalid and fail closed;
- a valid object remains active for Trigger and Gate while it is persisted,
  including after its nominal deadline.

The final point matches Phase3 settlement. At the deadline Phase3 marks the
in-memory sentinel ready, but the next `run_cycle()` must still read the sensor,
settle the soak, and remove `pending_soak` from `system_state.json`. Trigger or
Gate must not release protection before that deletion. A stale or unavailable
Phase3 state still fails through the existing freshness checks, so an old
missing/null value cannot become a safe default.

## `cloud_protection` semantics

Repository and production-state inspection show no implementation that
produces `cloud_protection`; historical design documents call it reserved.
Therefore:

- absence is not a missing-required-fact failure;
- a present inactive boolean/object remains inactive;
- a present active value remains a blocking safety fact;
- a present malformed value remains fail-closed.

This removes only the impossible mandatory-presence requirement. It does not
weaken sensor faults, water-path protection, predictor circuit, hard-safety,
freshness, pump, cooldown, response, or watering-guard checks.

## Consumer behavior

Trigger and Gate consume the same known/required/optional classification.
Trigger continues to prioritize unavailable/stale required facts and dangerous
facts as `LOCAL_SAFETY_ONLY`, then normal active protection as `SKIP_CLOUD`.
Gate v1 and Gate v2 continue to deny active, malformed, stale, or missing
required safety facts. Gate v2 retains Gate v1 admission semantics and its
strategy content binding.

No consumer invents a Qwen call, baseline, Gate allow, or execution permission.

## Verification

Tests are written before production changes and cover:

- missing and `null` pending soak with fresh versus missing/stale Phase3 state;
- valid pending soak before, at, and after its nominal deadline;
- malformed pending soak objects and scalar values;
- missing, inactive, active, and malformed cloud protection;
- normal Trigger `CALL_QWEN` and `SKIP_CLOUD` paths;
- dangerous Trigger `LOCAL_SAFETY_ONLY` paths;
- Gate v1 and Gate v2 allow/deny parity for the compatibility cases;
- all other required safety facts remaining fail-closed;
- State preserving source facts without synthesizing either optional fact.

Focused State/Trigger/Gate tests and the related runtime and fault-safety
regressions must pass. No openEuler deployment or live Qwen/Phase3/MQTT action
is part of this Issue.
