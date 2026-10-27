# Cloud Invocation Trigger V1

This package decides whether one soil3 State justifies waking the cloud model.
In Phase 1 the runtime persists the Trigger admission before any optional cloud
context is collected. `CALL_QWEN` enters the existing Vision / Experience / Qwen
chain; `SKIP_CLOUD` and `LOCAL_SAFETY_ONLY` stop before those calls while keeping
the factual Trigger and runtime records. A malformed or failed Trigger
admission fails closed and does not call Qwen, Vision, or Experience.

The Trigger is not an irrigation decision or execution authority. It does not
replace Strategy validation, Gate, Runner, Bridge, Phase3, MQTT, or the actuator
boundary. Runner and Bridge remain shadow / dry-run when the cloud chain runs.

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
   produces a `CALL_QWEN` candidate.
4. An unchanged projection produces `SKIP_CLOUD`.
5. A CALL candidate is admitted only when the latest durable local CALL
   admission is at least `trigger_min_cloud_interval_seconds` old. During the
   interval it becomes `SKIP_CLOUD` with `cloud_min_interval_active`.

The semantic projection compares dynamic boundary relations, trend direction,
safety/protection state, and confirmed action/Feedback identities. It does not
compare the complete State hash and introduces no plant threshold constant.
Trend direction uses a fixed `+/-0.1` percentage-point deadband matching the
canonical soil sensor resolution. Values inside that band remain `stable` so
measurement quantization cannot repeatedly admit Qwen. This is only a cloud
invocation noise filter; it does not change State, Gate, or Phase3 thresholds.
The initial ordinary cloud-admission interval is 900 seconds. It is separate
from the semantic baseline: the baseline still advances only after Validator
acceptance, while every durably persisted CALL admission starts the interval,
including provider failure, Validator rejection, or interruption before
finalization. Admission check-and-persist is serialized across local threads
and processes; the lock is released before optional context collection or Qwen
invocation. This prevents restart/retry storms without treating a failed model
result as a semantic baseline. Local safety decisions and normal Phase3
protection skips keep their higher priority. Replay calibration and a future
maximum-silence heartbeat policy remain separate work.

One `trigger_shadow.v1` record is stored per Trace under
`agent_chain/trigger_shadow`. It records the suggested decision, reasons,
Decision Context, baseline reference, and a small factual summary of what the
Qwen/Validator/Gate path actually produced when cloud admission occurred.
Token, cost, and latency fields are present only when a real source supplies
them. Every record declares `phase3_called=false` and
`physical_actions_performed=false`.

Trigger admission is fail-closed. If context construction, evaluation, or
persistence fails, the runtime records `blocked_trigger_failure` and the cloud
pipeline does not start.
