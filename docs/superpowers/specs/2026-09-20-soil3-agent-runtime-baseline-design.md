# Soil3 agent runtime baseline

## Goal

Run the merged Day1--Day3 proposal chain on openEuler without adding any
actuator path:

```text
real facts -> state.v1 -> strategy.v1 -> gate.v1 -> runner dry-run -> episode.v1
```

The deployment must be based on one versioned `main` release, retain Phase3 as
the sole execution authority, and remain useful when Gate denies because real
safety facts are missing or protective.

## Production baseline and scope

The approved source baseline is the latest `main` commit selected at deployment
time.  It is unpacked below `/root/water/releases/plant-intelligence-release`
with its exact Git revision recorded in a `REVISION` file; new agent services
use that release through `WorkingDirectory` and `PYTHONPATH`.
Existing `/root/water/app/services/soil3/phase3`, Phase3 systemd services,
ActuatorLayer, MQTT services, and `/wyc/plant-agent` are not overwritten or
restarted.

The release includes the existing Day1/Day2 source, `state.v1`, telemetry
adapters, Cloud Strategy, Gate, Runner, and Episode so the new runtime never
imports the board's old `state_id`/`plant_id` Cloud Strategy implementation.

## Runtime services

Two independently removable systemd timer/service pairs are added.

1. `plant-agent-soil3-state.timer` runs
   `plant-agent-soil3-state.service` every five minutes.  The service reads
   existing soil3 sensor, openGauss-health, Phase3 state, and irrigation facts
   through `telemetry.events.build_health_snapshot`, then builds one read-only
   `state.v1`.  It writes the latest snapshot atomically to
   `/root/water/runtime/instances/soil3/agent_chain/state/latest.json`.
2. `plant-agent-soil3-pipeline.timer` runs
   `plant-agent-soil3-pipeline.service` every five minutes.  The pipeline unit
   requires and starts the state service first, then consumes that fresh
   snapshot.  A lock prevents overlapping runs.

The five-minute cadence is a bounded operational refresh rather than a
high-frequency telemetry recorder.  `state.v1` itself remains unchanged and is
not treated as a new source of truth.

Both service pairs are `Type=oneshot`, run as root only because their existing
fact readers require it, have no network/MQTT publish permissions configured,
and write only under the new `agent_chain` runtime directory and its journal.
Stopping and disabling their timers stops the new chain.  Removing their unit
files and release directory rolls it back without touching Phase3 runtime data.

## Data flow and truthfulness

The State service uses only returned facts from the existing sources.  It must
not create `pending_soak`, `cloud_protection`, or any other missing safety flag.
Missing values remain missing in `state.v1`; Gate therefore denies when its
required inputs are unavailable.

The initial Cloud Strategy configuration is a dedicated runtime configuration
with `provider: "offline-fixture"`, no provider URL or secret, and a visible
fixture marker in the strategy audit.  The fixture creates only a valid
proposal-only strategy and is never reported as a provider response.  A real
provider configuration remains disabled until a separately authorized smoke
test.

Gate always receives the actual latest `state.v1` and the generated
`strategy.v1`.  It uses `exploration_requested: false`, has no actuator
permission, and retains its fail-closed behaviour.

Runner is called only after Gate returns `allow` or `allow_with_warning`; it is
always instantiated in `dry_run` mode and has no Phase3 Bridge, MQTT client, or
pump entry point.  On Gate `deny`, Runner is not called.  The pipeline writes a
truthful `skipped_due_to_gate_deny` status rather than manufacturing a dry-run
execution record.

For every pipeline run, Episode embeds the actual state and links the actual
strategy and gate record.  When Runner ran, its dry-run facts are attached as
non-physical facts.  When Gate denied, actions, feedback, and outcome remain
missing and Episode closes with those absences listed in `missing_facts`.

## Runtime layout

```text
/root/water/releases/plant-intelligence-release/
/root/water/runtime/instances/soil3/agent_chain/
  state/latest.json
  strategy/latest.json
  gate/latest.json
  runner/
  episodes/
  audit/
  config/
  locks/
```

No files are written under Phase3 runtime/data/log directories.  The only
record retention owned by this work is the agent-chain runtime directory.

## Deployment and verification

Before enabling timers, deployment records the selected `main` commit and
compares the board's relevant source paths with the release.  It verifies that
the existing Phase3 and MQTT units are active, then installs the release,
runtime configuration, and the two new service/timer pairs without restarting
existing units.

Acceptance requires one actual pipeline record containing a real `state.v1`,
fixture-marked `strategy.v1`, `gate.v1`, and an Episode file.  A Gate `deny`
caused by real missing/protective safety facts is an accepted safety result.  A
Runner record is required only if Gate allowed; otherwise the pipeline skip
record and Episode `missing_facts` prove the chain did not bypass admission.

Tests cover source selection, state fact preservation, fixture labelling, Gate
deny handling, Runner isolation, Episode persistence, systemd unit syntax, and
the deployed openEuler smoke path.  Verification also checks that no new MQTT
publish, pump command, or Phase3 invocation occurred.

## Explicit exclusions

- No Phase3 algorithm, safety, ActuatorLayer, MQTT, ESP32, or Vision scheduler change.
- No direct control route, manual watering route, or physical action.
- No fabricated safety facts, feedback, outcomes, timestamps, or provider success.
- No real-provider call until separately approved.
