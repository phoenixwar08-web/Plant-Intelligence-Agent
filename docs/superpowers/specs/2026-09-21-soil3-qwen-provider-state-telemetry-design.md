# Soil3 Qwen provider and production state telemetry

## Goal

Extend the existing soil3 proposal-only runtime so it can produce a real
Qwen-backed `strategy.v1` and map real soil humidity/freshness into the
existing frozen `state.v1` contract:

```text
openGauss telemetry -> state.v1 -> Qwen strategy.v1 -> gate.v1
  -> runner dry-run or skip -> episode.v1
```

This work remains shadow-mode only.  Phase3 remains the sole physical control
authority, and this runtime receives no Phase3 Bridge, MQTT publisher, or pump
entry point.

## Provider modes and provenance

The runtime accepts exactly two explicit modes:

1. `offline_fixture` is retained for tests and an intentional operational
   rollback.
2. `qwen_dashscope` calls the configured Qwen Model Studio workspace through
   the existing OpenAI-compatible Cloud Strategy client.

There is no automatic fallback from Qwen to the fixture.  A failed Qwen
request must remain a failed Qwen request in the run audit and Episode; it must
not be represented as a fixture result or a successful provider result.  Every
strategy and pipeline record carries the selected provider mode, provider name,
and model so the source is independently traceable.

The configured endpoint is an explicit workspace endpoint, supplied at runtime
only.  The model identifier is `qwen3.8-Flash`.  The normal strategy prompt,
`strategy.v1` builder, and validator remain the only proposal construction and
validation path.  Qwen output has no execution permission.

## Secret handling

The API key is never written to Git, a runtime JSON configuration, an Episode,
an audit record, a command line, or a journal message.  On openEuler it is held
only in a root-owned, mode-0600 runtime secret file.  The pipeline systemd
service reads that file through an `EnvironmentFile` and supplies only the
environment-variable name to the provider configuration.

The secret file is created during post-merge deployment, not by the source
tree.  Disabling the pipeline timer and removing that runtime secret returns
the service to a non-provider state without changing Phase3.

## Read-only soil telemetry adapter

`soil_sensor_readings` in openGauss is the canonical soil3 soil-telemetry
source already used by Phase3.  The telemetry adapter adds a narrowly scoped,
read-only query for the latest row with valid soil humidity, temperature, EC,
and receive time.  It maps the actual humidity and receive time into the
existing snapshot data that `state.v1` already consumes.

The adapter never supplies a fallback humidity or timestamp.  On an empty
result, database/read error, null value, or invalid physical range it returns
no soil reading.  `state.v1` then continues to contain missing soil humidity
and freshness, causing Gate to deny as designed.  No `state.v1` field, schema,
or validator changes are permitted.

Tests inject the database reader and cover a valid mapped reading, no row,
database failure, null fields, and invalid physical values.  They also prove
the adapter uses no write SQL.

## Safety facts deliberately left missing

`cloud_protection` has no current production producer and remains absent.
Phase3 persists `pending_soak` only while an actual soak is pending; absence is
not a positive inactive fact.  This Issue does not translate either absence to
`false`, does not change Phase3, and does not change Gate.

The current Phase3 state also exposes `dynamic_cooldown` and
`hard_safety_low_guard` in structures that Gate cannot currently interpret as
an explicit active/inactive fact.  They remain unchanged and continue to
produce a fail-closed decision.  Current `predictor_circuit: OPEN` and active
`recent_response_guard` are likewise real grounds for denial, not defects to
work around in this Issue.

## Runtime operation and deployment

Source changes are reviewed in one Issue branch and must be owner-merged to
`main` before board deployment.  The existing state timer remains read-only.
For the first real-provider smoke, deployment stops only the new pipeline
timer, preserves the running state timer and all Phase3/MQTT services, installs
the root-only secret and Qwen runtime configuration, and invokes one isolated
pipeline run.

The smoke is accepted only when it records a genuine Qwen-backed validated
`strategy.v1`, the actual `state.v1`, Gate's actual decision, and an Episode.
Gate deny skips Runner.  If Gate permits Runner, it remains `dry_run` and its
record must state that no physical actions occurred.  A provider failure,
schema failure, or validation failure leaves the timer disabled and restores
the explicit `offline_fixture` configuration; no silent fallback occurs.

Only after a successful smoke does deployment enable the existing five-minute
pipeline timer in `qwen_dashscope` mode and verify a scheduled shadow run.
Both timer pairs remain independently stoppable and removable.  No Phase3,
MQTT, actuator, or production sensor configuration is restarted or modified.

## Acceptance evidence

The implementation and post-merge deployment provide:

- a redacted real Qwen `strategy.v1` sample and provider provenance;
- a real `state.v1` sample showing mapped soil humidity and receive-time based
  freshness, or an explicit missing result if the live query has no valid row;
- Gate's actual decision and reasons, without seeking an allow result;
- a persisted Episode that links the real state, strategy, gate, and
  Runner-or-skip outcome;
- service/audit evidence that provider mode was `qwen_dashscope` and that no
  MQTT publish, Phase3 Bridge call, or physical action occurred.

## Explicit exclusions

- No frozen `state.v1` protocol change.
- No Gate rule or validator relaxation.
- No `cloud_protection` or `pending_soak` producer implementation.
- No Phase3 algorithm, bridge, ActuatorLayer, MQTT, ESP32, Vision, database
  schema, or historical-data change.
- No implicit provider fallback and no fabrication of safety facts.
