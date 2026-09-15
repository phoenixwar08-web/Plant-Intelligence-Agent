# Soil3 Production Baseline

Inspected: 2026-09-15 on the openEuler host reachable through the private `100.*` address.

| Component | Machine path | Observed state | Repository treatment |
| --- | --- | --- | --- |
| Phase1 calibration | `/root/water/phase1_test/water-test-soil3.py` | service inactive | Retained as offline calibration source only. |
| Phase2 predictor | `/root/water/wyc/phase2_predictor/` | no process or shared-memory response observed | Retained as an optional candidate-trajectory predictor. |
| Phase3 controller | `/root/water/phase3/soil3/` | service active | Retained as final safety and MQTT execution authority. |
| IoT event state | `/root/water/wyc/IOT/` | cloud-agent inactive | Only event/state adaptation is retained. |

## Safety boundary

The cloud model may return a `StrategyRequest` only. It has no MQTT publish permission. Every strategy must pass `Validator -> cloud Gate -> Phase3 -> ActionPlan`; Phase3 remains authoritative for sensor, water-path, cooldown, and hard safety protection.

Phase2 is an observed legacy predictor exchanging candidate trajectories through `/dev/shm/pred_request.json` and `/dev/shm/pred_response.json`. It is not the V3 cloud strategy model.

## Deliberately excluded

- runtime JSON, CSV, SQLite, logs, backups, bytecode, and environment files;
- OpenClaw code and watering routes;
- cross-device experience code and all non-soil3 device implementations;
- inactive IoT cloud and embedded command executors.
