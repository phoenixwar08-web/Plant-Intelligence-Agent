# soil3 Feedback Collector

This sidecar records factual plant-response observations after an explicit real-action receipt. It does not decide, command, publish MQTT, call `manual_water`, or enable Experience.

## Action evidence

- Native Phase3 records `command_completed` only after its existing MQTT on/off flow. This is command evidence, not proof that the pump physically ran.
- Controlled execution reuses its verified Trace/Episode binding and records the command outcome separately.
- Manual watering must be recorded as a fact only:

  ```bash
  python3 -m services.soil3.feedback_collector.service manual-record \
    --receipt-dir /root/water/runtime/instances/soil3/agent_chain/feedback-actions \
    --state-file /root/water/runtime/instances/soil3/phase3/system_state.json \
    --action-id act-<24-lowercase-hex> \
    --confirmed-by <owner> \
    --reference-action-at 2026-10-09T10:00:00Z \
    --pump-seconds 5
  ```

  This command writes no actuator command and never imports Phase3 or `manual_water`.

The Collector scans only durable receipts with `command_completed`, `manual_confirmed`, or `device_confirmed` evidence. It never infers an action from Shadow Episodes, logs, `pending_soak`, or nearby timestamps. A native action creates its own Episode; an explicitly bound controlled action reuses its existing Episode.

## Collection schedule

Each action has one persistent tracking record and four factual windows: 30 minutes, 2–3 hours, 6–12 hours, and 24 hours. A missed window is marked `missed`, never backfilled. After the final window (or its expiry), the Collector generates the existing `feedback.v1` Outcome and closes the Episode. A long outage therefore produces an explicit no-observation Outcome rather than a fabricated late measurement.

Soil observations are built in memory from the canonical telemetry health adapter and `StateBuilder`; CSV is not used as a soil fallback. When `vision_enabled` is true, public Vision manifests and every referenced observation are hash-validated before their facts are saved. Any Vision dependency, capture, configuration, or validation failure is stored as missing Vision (`null`) while soil collection continues.

Only Outcomes for Episodes containing exactly one `manual_confirmed` or `device_confirmed` action can later be classified as causal Experience. `command_completed` remains factual feedback but not a confirmed causal sample.

## Configuration and scheduling

The timer is intentionally independent of the State/pipeline timer:

```bash
python3 -m services.soil3.feedback_collector.service collect \
  --config /root/water/runtime/instances/soil3/agent_chain/config/feedback-collector.json
```

The JSON config must explicitly name `receipt_dir`, `tracking_dir`, `episode_dir`, `feedback_dir`, and `phase3_state_path`. It may name `sensor_log_path`, `irrigation_trials_path`, `service_unit`, `parameters`, and `vision_enabled`; do not enable Experience as part of this feature. The systemd units are templates only in this repository: this Issue does not deploy them, contact the board, or perform a real watering action.
