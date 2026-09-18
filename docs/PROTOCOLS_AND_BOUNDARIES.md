# Protocols and boundaries

| Protocol | Status | Boundary |
| --- | --- | --- |
| `state.v1` | frozen | Read-only normalized current facts for a decision; built on demand, not by a high-frequency snapshot task. |
| `vision.v1` | implemented | One structured image observation per fixed plant zone; appearance only, never a diagnosis, strategy, or device control. Not enabled: no consumer exists, and no camera or provider request runs unless explicitly configured and separately approved. |
| `strategy.v1` | planned | Structured proposal; never direct device control. |
| `gate.v1` | planned | Admission decision for a validated strategy. |
| `episode.v1` | planned | Structured care experience record. |
| `feedback.v1` | planned | Factual post-action observations and outcome evidence. |
| `replay_sample.v1` | frozen | Read-only reconstruction of `state.v1` from facts timestamped at or before an inclusive historical cutoff T; future facts are excluded and data-quality limitations remain explicit. |

“Planned” means no public contract is implemented yet. Do not write consumers that assume a planned protocol already exists. Read this document for State or shared-protocol work; it is not mandatory background for every ordinary task.

“Implemented” means the contract exists, is covered by tests, and has been verified against real data, but is not yet frozen: compatibility iteration may continue inside later implementation Issues. A consumer that needs a stable contract must wait for “frozen”.

Frozen protocols cannot be changed inside one implementation Issue. Any required change must be an explicit protocol-change proposal, include version/compatibility handling, and identify all producers and consumers before implementation.
