# Gate V2 strategy-content binding design

## Status and scope

**Approved design.**

This is a standalone Gate protocol change. It precedes and is intentionally
separate from PR #47 (Phase3 Bridge). It adds a content binding to new
`gate.v2` records without changing the existing implemented `gate.v1` record
shape or its meaning. It does not change `state.v1`, Strategy validation,
Runner behaviour, Phase3, MQTT, ActuatorLayer, or device execution.

## Problem

`gate.v1` records `strategy_id` but not the canonical content fingerprint of
the Strategy that Gate reviewed. Runner already persists
`strategy_sha256 = fingerprint(strategy)`. Therefore an otherwise-valid
Strategy can retain its ID, change content after Gate (for example water from
5 to 6 seconds), create a new dry-run Runner trace, and still appear to match
the Gate by ID alone. A Bridge cannot establish that Gate reviewed the content
in that trace.

## Decision

Add `gate.v2` with one required field:

```text
strategy_sha256: string | null
```

It uses the existing canonical `fingerprint()` implementation from
`services.soil3.cloud_strategy.validator`; no second serialization or hash
algorithm is introduced.

`evaluate_gate_v2()` first normalizes only non-dict inputs in the same way as
the current evaluator and calls the formal `StrategyValidator` locally. If and
only if that validation accepts the complete Strategy, it records
`fingerprint(strategy)` in `strategy_sha256`. This applies to every Gate
decision: `allow`, `allow_with_warning`, and `deny`. If Strategy validation
rejects, `strategy_sha256` is `null`.

All existing Gate safety, budget, decision, and no-actuator behavior is reused
unchanged. `gate.v2` is constructed from that same decision path with the new
schema version and binding field. This keeps the protocol upgrade narrow while
preserving the tested `gate.v1` semantics and output exactly.

## Compatibility and consumer boundary

`gate.v1` remains **implemented** and readable by its existing consumers. No
stored record is migrated and no hidden field is added to it.

Phase3 Bridge will be changed only in its later, rebased PR. It will accept
only `gate.v2`; a `gate.v1` input is rejected fail-closed, with no
`strategy_id` compatibility inference. Before returning an accepted handoff,
Bridge must require:

```text
gate.strategy_sha256 == runner.strategy_sha256 == fingerprint(current_strategy)
```

The existing state binding, formal Strategy validation, complete dry-run trace
validation, and verification-only handoff restrictions remain additional
requirements. A Gate with a null strategy hash cannot satisfy this condition.

## Files and delivery order

The independent Gate PR changes only:

- `services/soil3/cloud_gate/gate_v2.py` and its export;
- `services/soil3/cloud_gate/gate.v2.schema.json`;
- `tests/test_cloud_gate_v2.py`;
- `docs/PROTOCOLS_AND_BOUNDARIES.md` to describe the explicit v1/v2
  compatibility boundary.

It does not modify `services/soil3/phase3_bridge/`, Phase3, MQTT,
ActuatorLayer, Runner, or the frozen `state.v1` protocol.

After the Gate PR is independently reviewed and Owner-merged, PR #47 rebases
onto that `main` and makes its separate, minimal consumer change. No temporary
integration branch is created.

## Test plan

The Gate PR must prove:

1. A valid Strategy records its canonical hash for allow, warning-allow, and
   safety/budget deny decisions.
2. A formally invalid Strategy records `strategy_sha256: null`.
3. `gate.v1` records retain their original schema version and exact shape.
4. The new schema requires the hash field and permits only lowercase SHA-256
   hex or null.

The later Bridge PR must prove:

1. A normal unmodified `gate.v2` → dry-run Runner chain is accepted.
2. Keeping `strategy_id` but changing water from 5 to 6 seconds is rejected.
3. Keeping actions unchanged but modifying another valid Strategy field is
   rejected.
4. A `gate.v1` record is rejected directly, without ID-only fallback.

All tests remain local and dry-run. There is no Phase3 invocation, deployment,
MQTT publish, or hardware smoke test in either PR.
