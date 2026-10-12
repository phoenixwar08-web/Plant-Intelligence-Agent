# Cloud Invocation Trigger Shadow V1

This package observes whether one soil3 State would justify waking the cloud
model. It is Phase 0 only: every pipeline cycle still calls Qwen exactly as it
did before. No Strategy, Gate, Runner, Bridge, Phase3, MQTT, or actuator code
consumes a Trigger decision.

The Decision Context uses existing facts only:

- current humidity, dynamic `target_low`, `field_capacity`, and effective
  `hard_safety_low`, plus their relative distances;
- canonical openGauss humidity trends at 1h, 3h, and 6h;
- State freshness and normalized Phase3 safety/protection facts;
- the latest durable feedback action receipt and matching factual Feedback,
  when they exist;
- the previous accepted Shadow CALL result as context, without turning it into
  an execution input.

Missing or malformed facts remain explicit. A `command_completed` receipt says
only that the MQTT on/off publication flow completed; it is not confirmed
physical causality. Only `manual_confirmed` or future `device_confirmed`
evidence is projected as a confirmed action.

Legacy Phase3 protection records are interpreted from their factual timing:
pending soak uses its persisted end/duration, while dynamic cooldown uses the
same Phase3 state's last normal-irrigation timestamp. Native/manual action
Feedback uses the Collector's public deterministic Episode binding rather than
guessing a tracking-file location.

The deterministic evaluator has fixed priority:

1. Missing/stale safety inputs, a reached hard safety line, dangerous flags,
   or an OPEN/invalid predictor circuit produce `LOCAL_SAFETY_ONLY`.
2. Normal `pending_soak`, cooldown, response guard, watering guard, or an
   active pump produce `SKIP_CLOUD`.
3. No accepted Shadow CALL baseline, or a changed semantic projection,
   produces `CALL_QWEN`.
4. An unchanged projection produces `SKIP_CLOUD`.

The semantic projection compares dynamic boundary relations, trend direction,
safety/protection state, and confirmed action/Feedback identities. It does not
compare the complete State hash and introduces no plant threshold constant.
Replay calibration, minimum intervals, and maximum-silence heartbeat policy
belong to Issue #63.

One `trigger_shadow.v1` record is stored per Trace under
`agent_chain/trigger_shadow`. It records the suggested decision, reasons,
Decision Context, baseline reference, and a small factual summary of what the
unconditional Qwen/Validator/Gate path actually produced. Token, cost, and
latency fields are present only when a real source supplies them. Every record
declares `phase3_called=false` and `physical_actions_performed=false`.

Trigger collection is best-effort. If context construction or persistence
fails, the existing Qwen pipeline still runs and the runtime records a null
Trigger artifact path. This package does not deploy or enable a production
cloud-call gate.
