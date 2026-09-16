# Soil3 Production Paths and Boundary

The Linux production project root is `/root/water`. These are current functional locations, not a task schedule or authorization to change production files.

| Purpose | Current machine path | Repository treatment |
| --- | --- | --- |
| Phase1 calibration source | `/root/water/app/services/soil3/phase1/` | Offline calibration source only. |
| Phase2 predictor source | `/root/water/app/services/soil3/phase2_predictor/` | Candidate-trajectory predictor. |
| Phase3 controller source | `/root/water/app/services/soil3/phase3/` | Final safety and MQTT execution authority. |
| Soil3 telemetry source | `/root/water/app/services/soil3/telemetry/` | Observation and event adaptation only. |
| Runtime state | `/root/water/runtime/instances/soil3/phase3/` | Not source code; preserve unless explicitly authorized. |
| Long-term data | `/root/water/data/soil3/phase3/` | Not source code; preserve unless explicitly authorized. |
| Logs | `/root/water/logs/soil3/phase3/` | Operational output; do not treat as source. |

## Safety boundary

The cloud model may return a `StrategyRequest` only. It has no MQTT publish permission. Every strategy must pass `Validator -> cloud Gate -> Phase3 -> ActionPlan`; Phase3 remains authoritative for sensor, water-path, cooldown, and hard safety protection.

Phase2 is an observed legacy predictor exchanging candidate trajectories through `/dev/shm/pred_request.json` and `/dev/shm/pred_response.json`. It is not the V3 cloud strategy model.

## Deliberately excluded

- runtime JSON, CSV, SQLite, logs, backups, bytecode, and environment files;
- OpenClaw code and watering routes;
- cross-device experience code and all non-soil3 device implementations;
- inactive IoT cloud and embedded command executors.
