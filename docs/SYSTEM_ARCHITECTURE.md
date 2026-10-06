# System architecture

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| State | Normalize current sensor, environment, irrigation, and safety facts. |
| Vision | Convert a captured plant image into structured visual facts. |
| Cloud Strategy | Propose structured, non-executing care actions. |
| Validator | Reject malformed, unknown, or invalid strategy content. |
| Gate | Apply the small set of cloud-side admission and exploration constraints. |
| Runner | Persist and progress approved strategy steps; initially dry-run only. |
| Phase3 Bridge | Revalidate Strategy, Gate, and Runner bindings and produce only a zero-argument handoff to Phase3's formal cycle interface. V1 is verification-only. |
| Phase3 | Final safety and irrigation decision authority. |
| ActuatorLayer | Phase3-owned actuator interface. |
| Feedback | Record subsequent factual observations at defined times. |
| Episode | Link state, strategy, gate result, actions, feedback, and outcome. |
| Trace | Correlate existing Shadow records and sourced experiment metrics; never decide or execute. |
| Experience Retrieval | Read closed Episodes and return separately ranked, explainable successful and failed analogues; never generate a strategy. |
| Replay | Read historical facts into reproducible samples. |
| Historical Regression | Batch existing Replay samples through public Strategy, Validator, and Gate v2 interfaces and summarize read-only experiment evidence. |

## Formal control chain

```text
LLM → Strategy → Validator → Gate → Runner → Phase3 Bridge → Phase3 → ActuatorLayer → MQTT → ESP32
```

The following paths are prohibited:

```text
LLM → MQTT
LLM → manual_water
LLM → ESP32
```

The soil3 Shadow runtime can explicitly request Vision and Experience after
State and before Strategy. Available validated Vision facts and the public
`vision_run.v1` manifest reference, plus the read-only Experience Retrieval
result and its runtime artifact reference, are associated through one
`trace.v1` and supplied to Cloud Strategy as supporting facts. Disabled inputs
are `not_requested`; requested inputs without facts are `unavailable`. Missing
facts stay null and are never inferred. Strategy, Gate v2, dry-run Runner,
verification-only Bridge, and an open Episode then continue on the same trace.
This integration always records `phase3_called=false` and
`physical_actions_performed=false`.

State, Vision, Strategy, Episode, Feedback, Trace, and Replay may read facts and create records within their authorized Issue boundaries. They do not bypass Phase3 or become an actuator path.

The Day 6 historical regression service starts from existing
`replay_sample.v1` artifacts and ends at Gate v2. Its deterministic fixture
mode records Strategy distributions, Validator and Gate reason codes, Gate
three-state counts, model/system errors, and reproducible risky-case evidence.
An explicitly selected live-provider mode uses the same Cloud Strategy public
interface but is marked non-deterministic. Both modes use
`exploration_requested=false`, create no budget ledger, and never enter Runner,
Bridge, Episode, Trace, Phase3, MQTT, or ActuatorLayer.
