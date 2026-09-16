# Protocols and boundaries

| Protocol | Status | Boundary |
| --- | --- | --- |
| `state.v1` | frozen | Read-only normalized current facts for a decision; built on demand, not by a high-frequency snapshot task. |
| `vision.v1` | planned | Structured image observation. |
| `strategy.v1` | planned | Structured proposal; never direct device control. |
| `gate.v1` | planned | Admission decision for a validated strategy. |
| `episode.v1` | planned | Structured care experience record. |
| `feedback.v1` | planned | Factual post-action observations and outcome evidence. |
| `replay_sample.v1` | planned | Read-only reconstruction of historical facts at time T. |

“Planned” means no public contract is implemented yet. Do not write consumers that assume a planned protocol already exists. Read this document for State or shared-protocol work; it is not mandatory background for every ordinary task.

Frozen protocols cannot be changed inside one implementation Issue. Any required change must be an explicit protocol-change proposal, include version/compatibility handling, and identify all producers and consumers before implementation.
