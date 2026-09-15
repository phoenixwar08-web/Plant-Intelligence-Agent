# Plant Intelligence Agent — soil3 production baseline

This is a soil3-only, source-controlled reconstruction of the openEuler production system inspected on 2026-09-15. It contains safe source and deployment templates only: no credentials, runtime state, logs, backups, camera URLs, or production databases.

## Structure

- `services/soil3/phase1/`: offline calibration source; not a normal watering executor.
- `services/soil3/phase2_predictor/`: optional legacy candidate-trajectory predictor.
- `services/soil3/phase3/`: final safety controller and the only pump authority.
- `services/soil3/telemetry/`: event/state adaptation without cloud command execution.
- `ops/`: sanitized systemd and cron templates.

## Execution boundary

```text
StrategyRequest -> Validator -> cloud Gate -> Phase3 -> ActionPlan -> MQTT
```

Only Phase3 may publish a real pump command. The cloud model, future vision service, Phase2 predictor, and telemetry modules must not bypass it. Vision and cloud-model services are independent from OpenClaw.

## Verification

```powershell
python -m compileall services tests
pytest -q
```

See [the production baseline](docs/PRODUCTION_BASELINE.md) before deploying any component.
