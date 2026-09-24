# Day 5 Shadow fault and safety matrix

This matrix records the repeatable, simulation-only acceptance suite for Issue
#22. It does not authorize or perform a real Phase3, MQTT, ActuatorLayer, or
pump action.

| Fault injected | Required result |
| --- | --- |
| Soil data older than the Gate deny limit | Gate deny; Runner and Bridge absent |
| Soil sensor missing | Gate deny; no fabricated humidity |
| Phase3 safety facts incomplete | Gate deny with missing-safety reasons |
| Vision unavailable / Experience not requested | Explicit null references in `trace.v1` |
| Qwen timeout or provider failure | Strategy rejected; Gate, Runner, Bridge, Episode absent |
| Invalid model JSON | Strategy rejected; no fixture fallback |
| StrategyValidator rejection | No Gate, Runner, Bridge, or successful Episode path |
| Gate deny | Runner and Bridge absent |
| Gate/Strategy content hash mismatch | Runner and Bridge absent |
| Runner/Strategy hash mismatch | Bridge response rejected |
| Runner mode changed from `dry_run` | Bridge response rejected |
| Invalid Trace update | Stop before Strategy; no downstream calls |
| Restart during wait | Resume persisted dry-run state |
| Duplicate request | Do not duplicate the dry-run water result |

Every test asserts `phase3_called=false` and
`physical_actions_performed=false` in the artifacts it reaches. The suite also
uses an AST import check to prevent the Shadow runtime and tests from importing
Phase3, Phase1 actuator code, or `paho.mqtt`.

Run the focused suite:

```bash
python -m unittest tests.test_day5_fault_safety
```

Run the related Day 5 regression:

```bash
python -m unittest \
  tests.test_day5_fault_safety \
  tests.test_soil3_agent_runtime \
  tests.test_trace_v1 \
  tests.test_cloud_gate_v2 \
  tests.test_strategy_runner_v1 \
  tests.test_phase3_bridge_v1
```
