# System architecture

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| State | Normalize current sensor, environment, irrigation, and safety facts. |
| Cloud Invocation Trigger | Compare factual Decision Context changes and admit or suppress cloud work; it never decides irrigation or replaces Gate/Phase3. |
| Vision | Convert a captured plant image into structured visual facts. |
| Cloud Strategy | Accept only model-authored decision fields, locally bind them to the current State, and propose structured, non-executing care actions. |
| Validator | Reject malformed, unknown, or invalid strategy content. |
| Gate | Apply the small set of cloud-side admission and exploration constraints. |
| Runner | Persist and progress approved strategy steps; initially dry-run only. |
| Phase3 Bridge | Revalidate Strategy, Gate, and Runner bindings and produce only a zero-argument handoff to Phase3's formal cycle interface. V1 is verification-only. |
| Controlled Execution | Reload the persisted Trace-bound State/Strategy/Gate/Runner/Bridge/Episode chain, reverify it through the public Bridge, consume one exact Owner approval, invoke only the formal Phase3 cycle, and persist an at-most-once factual receipt. |
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
LLM decision fields → local Strategy assembly → Validator → Gate → Runner → Phase3 Bridge → Phase3 → ActuatorLayer → MQTT → ESP32
```

The Bridge-to-Phase3 transition is disabled unless the controlled-execution
adapter reloads the persisted Trace artifacts and open Episode, verifies their
file hashes and record bindings, and obtains a fresh acceptance from the
public `Phase3Bridge().verify(...)` interface. It then requires one unexpired
Owner approval for the exact persisted handoff and invokes only the formal
`DecisionBrain.run_cycle()` class interface. The approval is consumed before
that cycle is invoked; duplicate or interrupted requests cannot invoke the
same approval twice.

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

Issue #62 added the Phase-0 Trigger observer after State. State receives the
existing Phase3 dynamic parameter facts and bounded canonical openGauss history
needed for 1h/3h/6h trends. Issue #69 promotes the same deterministic result to
cloud admission: the runtime durably records `CALL_QWEN`, `SKIP_CLOUD`, or
`LOCAL_SAFETY_ONLY` before branching. Only `CALL_QWEN` collects optional
Vision/Experience and enters Qwen, Validator, Gate, Runner, Bridge, and Episode.
The other decisions retain factual State, Trace, Decision Context, reasons, and
semantic projection without model or downstream artifacts. Trigger admission
failure is fail-closed and cannot fall through to Qwen. `trigger_shadow.v1`
remains analysis evidence outside `trace.v1`; it is not an irrigation decision
or an execution input and cannot call Phase3, MQTT, or ActuatorLayer.
Historical replay and interval/heartbeat calibration remain Issue #63.

State, Vision, Strategy, Episode, Feedback, Trace, and Replay may read facts and create records within their authorized Issue boundaries. They do not bypass Phase3 or become an actuator path.

The Day 6 historical regression service starts from existing
`replay_sample.v1` artifacts and ends at Gate v2. Its deterministic fixture
mode records Strategy distributions, Validator and Gate reason codes, Gate
three-state counts, model/system errors, and reproducible risky-case evidence.
An explicitly selected live-provider mode uses the same Cloud Strategy public
interface but is marked non-deterministic. Both modes use
`exploration_requested=false`, create no budget ledger, and never enter Runner,
Bridge, Episode, Trace, Phase3, MQTT, or ActuatorLayer.
