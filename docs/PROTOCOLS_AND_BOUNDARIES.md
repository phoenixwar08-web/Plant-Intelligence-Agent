# Protocols and boundaries

| Protocol | Status | Boundary |
| --- | --- | --- |
| `state.v1` | frozen | Read-only normalized current facts for a decision; built on demand, not by a high-frequency snapshot task. |
| `vision.v1` | implemented | One structured image observation per fixed plant zone; appearance only, never a diagnosis, strategy, or device control. Not enabled: no consumer exists, and no camera or provider request runs unless explicitly configured and separately approved. |
| `strategy.v1` | implemented | Structured proposal bound to one `state.v1` snapshot by `device_code`, that snapshot's `observed_at`, and a `state_sha256` fingerprint the local service computes from it; `state_generated_at` is traceability metadata, not a binding condition, and every timestamp is carried in one RFC 3339 UTC notation the service normalizes to. Only `water(pump_seconds)`, `wait(seconds)`, `observe`, and `stop` actions are allowed, each within a protocol ceiling and within a cumulative ceiling for the whole strategy. It never grants execution or device-control authority, and only a declared projection of `state.v1` is ever sent to a provider. Not enabled and not yet exercised against a live provider: no consumer exists, and the shipped configuration is disabled and carries no provider address. |
| `gate.v1` | planned | Admission decision for a validated strategy. |
| `episode.v1` | planned | Structured care experience record. |
| `feedback.v1` | planned | Factual post-action observations and outcome evidence. |
| `replay_sample.v1` | frozen | Read-only reconstruction of `state.v1` from facts timestamped at or before an inclusive historical cutoff T; future facts are excluded and data-quality limitations remain explicit. |

“Planned” means no public contract is implemented yet. Do not write consumers that assume a planned protocol already exists. Read this document for State or shared-protocol work; it is not mandatory background for every ordinary task.

“Implemented” means the contract has an implementation covered by automated tests, but is not yet frozen: compatibility iteration may continue inside later implementation Issues. A consumer that needs a stable contract must wait for “frozen”. Whether a protocol has been exercised against real devices or a live provider, and whether it is enabled at runtime, are stated per protocol in the Boundary column and in its implementation Issues; neither is implied by this status.

Frozen protocols cannot be changed inside one implementation Issue. Any required change must be an explicit protocol-change proposal, include version/compatibility handling, and identify all producers and consumers before implementation.
