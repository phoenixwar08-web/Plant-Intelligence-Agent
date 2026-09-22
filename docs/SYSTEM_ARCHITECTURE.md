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
| Phase3 | Final safety and irrigation decision authority. |
| ActuatorLayer | Phase3-owned actuator interface. |
| Feedback | Record subsequent factual observations at defined times. |
| Episode | Link state, strategy, gate result, actions, feedback, and outcome. |
| Trace | Correlate existing Shadow records and sourced experiment metrics; never decide or execute. |
| Experience Retrieval | Read closed Episodes and return separately ranked, explainable successful and failed analogues; never generate a strategy. |
| Replay | Read historical facts into reproducible samples. |

## Formal control chain

```text
LLM → Strategy → Validator → Gate → Runner → Phase3 → ActuatorLayer → MQTT → ESP32
```

The following paths are prohibited:

```text
LLM → MQTT
LLM → manual_water
LLM → ESP32
```

State, Vision, Strategy, Episode, Feedback, Trace, and Replay may read facts and create records within their authorized Issue boundaries. They do not bypass Phase3 or become an actuator path.
