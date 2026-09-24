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
| Controlled Execution | Consume one short-lived Owner approval bound to an accepted Bridge request, call only the supplied zero-argument Phase3 cycle, and persist an at-most-once factual receipt. |
| Phase3 | Final safety and irrigation decision authority. |
| ActuatorLayer | Phase3-owned actuator interface. |
| Feedback | Record subsequent factual observations at defined times. |
| Episode | Link state, strategy, gate result, actions, feedback, and outcome. |
| Trace | Correlate existing Shadow records and sourced experiment metrics; never decide or execute. |
| Experience Retrieval | Read closed Episodes and return separately ranked, explainable successful and failed analogues; never generate a strategy. |
| Replay | Read historical facts into reproducible samples. |

## Formal control chain

```text
LLM → Strategy → Validator → Gate → Runner → Phase3 Bridge → Phase3 → ActuatorLayer → MQTT → ESP32
```

The Bridge-to-Phase3 transition is disabled unless the controlled-execution
adapter receives one unexpired Owner approval for the exact Bridge request.
The approval is consumed before the zero-argument Phase3 cycle is invoked;
duplicate or interrupted requests cannot invoke the same approval twice.

The following paths are prohibited:

```text
LLM → MQTT
LLM → manual_water
LLM → ESP32
```

The soil3 Shadow runtime associates State, explicit Vision/Experience
availability, Strategy, Gate v2, dry-run Runner, verification-only Bridge, and
an open Episode through one `trace.v1`. Missing facts stay explicit. This
integration never calls Phase3 and never performs a physical action.

State, Vision, Strategy, Episode, Feedback, Trace, and Replay may read facts and create records within their authorized Issue boundaries. They do not bypass Phase3 or become an actuator path.
