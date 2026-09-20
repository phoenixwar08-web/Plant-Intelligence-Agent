# Cloud Gate V1 Design

## Scope

Implement GitHub Issue #15 only. Cloud Gate accepts a `state.v1` snapshot and a
`strategy.v1` record, decides whether the proposal is admissible, and records a
bounded exploration-budget reservation. It creates records only; it cannot
control a pump or enter the Phase3, ActuatorLayer, MQTT, or ESP32 paths.

## Design

The module lives at `services/soil3/cloud_gate/` and is split by responsibility:

* `gate_v1.py` defines the `gate.v1` record, policy validation, and deterministic
  state/strategy admission rules.
* `budget.py` owns an explicit local JSON ledger. A reservation is identified by
  `strategy_id` plus `state_sha256`; repeating the same request returns the prior
  reservation and never consumes the budget again.
* `service.py` is a CLI boundary. It requires explicit paths for the input state,
  strategy, policy configuration, ledger, and output record. It writes the output
  atomically and has no default production path.
* `gate.v1.schema.json` describes the public decision record. The protocol is
  added to the protocol table as `implemented`, not frozen.

The Gate independently calls the existing `StrategyValidator` with the supplied
state. A caller cannot claim that an invalid proposal was already validated. The
Gate reads the strategy only after that check passes and never changes it.

## gate.v1 record

Every result has these required fields:

* `schema_version: "gate.v1"`, UUID `gate_id`, and RFC 3339 UTC `decided_at`.
* `device_code`, `strategy_id`, `state_observed_at`, and `state_sha256` copied
  from the validated binding.
* `decision`: `allow`, `allow_with_warning`, or `deny`.
* Deterministic `reason_codes` and `warning_codes`; an allow result has neither,
  a warning result has warning codes, and a deny result has reason codes.
* A `budget` object that states whether exploration was requested, the requested
  water seconds, the amount reserved, the remaining budget, and the stable
  reservation identity when one exists.
* `execution` fixed to `{"mode":"admission_only","actuator_commands_allowed":false}`.

No field authorizes a device action. A future Runner may interpret only a
non-deny record as dry-run input; it must never infer real execution authority.

## Admission policy

Policy configuration is required, validated, and local. It supplies two soil3
freshness boundaries: `warning_age_seconds: 900` and
`deny_age_seconds: 18000`. The code validates that both are finite non-negative
numbers and `warning_age_seconds <= deny_age_seconds`; an invalid policy fails
closed.

The Gate returns `deny` without reserving budget when any of these applies:

* the state is not `state.v1` for `soil3`, or the strategy fails the existing
  validator/binding check;
* soil humidity, soil age, phase3-state age, observed time, or safety flags are
  missing or malformed;
* soil or Phase3 state age exceeds the deny boundary;
* the current irrigation state says the pump is active;
* an active Phase3-provided protection flag is present (`pending_soak`,
  `water_delivery_suspect`, `reservoir_empty_suspect`, `low_wet_recovery_suspect`,
  `sensor_fault`, `dynamic_cooldown`, `watering_trigger_guard`,
  `recent_response_guard`, `hard_safety_low_guard`, or `cloud_protection`);
* the predictor circuit reports `OPEN`, an unrecognised state, or a malformed
  flag value;
* an exploration request exceeds the remaining configured budget, or the ledger
  cannot be read or locked.

The Gate returns `allow_with_warning` for a valid proposal when soil or
Phase3-state age is at least the warning boundary but does not exceed the deny
boundary, or when the predictor circuit reports `HALF_OPEN`. It returns `allow`
only when all gate facts are fresh and there are no warnings. The Gate does not
calculate Phase3 watering thresholds or reinterpret numeric safety limits; it
only consumes the factual safety signals already carried by `state.v1`.

## Exploration budget

Exploration is an explicit boolean request outside `strategy.v1`, avoiding any
change to that Day 2 contract. An exploration request costs the sum of validated
`water.pump_seconds` actions. Non-exploration requests have a requested and
reserved amount of zero.

The policy defines a finite, non-negative `max_exploration_water_seconds`. The
ledger tracks reservations per `device_code` and rolling window beginning at
`window_started_at`; the policy supplies `window_seconds`. Expired reservations
are ignored when calculating the current window. The example configuration sets
the budget to zero, which is fail-closed until an operator supplies a deliberate
non-zero local policy. Tests use small explicit budgets.

Ledger writes use a sidecar exclusive lock and an fsync-plus-replace temporary
file write. Failure to acquire the lock or parse the current file returns a deny
result and leaves the ledger unchanged. Reopening the same ledger after a
restart must preserve earlier reservations and their idempotency identity.

## Tests and exclusions

Tests drive the public Gate service and ledger, covering allow, warning, invalid
strategy binding, absent facts, stale data, active safety protections, malformed
policy, budget exhaustion, repeated requests, and persistence across a new
ledger instance. They also verify the CLI has explicit paths and atomic output.

Out of scope: Phase3 source, MQTT, `manual_water`, actuator code, production
configuration, databases, historical data, live provider calls, Runner, and
Episode. No frozen protocol changes are made.
