# soil3 Feedback Collector design (Issue #60)

## Status and scope

This is the proposed design for Issue #60.  It adds a factual, delayed
feedback collection path for real watering events:

```text
durably recorded action receipt
  -> Episode binding
  -> 30min / 2-3h / 6-12h / 24h factual observations
  -> feedback.v1
  -> feedback.v1 Outcome + closed Episode
  -> eligible Experience Retrieval input
```

It reuses `feedback.v1`, `FeedbackStore`, `episode.v1`, and `EpisodeStore`.
It must not alter `state.v1`, `strategy.v1`, `gate.v2`, Runner, Bridge,
Phase3 decision rules, MQTT messages, or actuator calls.  The Collector has
no execution capability: it reads action receipts and telemetry/state facts,
then writes Feedback and Episode facts only.  `experience.enabled` remains
false.

## Truth boundary

`Phase3` currently proves only that its MQTT `on` and `off` publish workflow
completed.  That is not proof that a pump physically ran or water reached the
plant.  The design therefore distinguishes command evidence from confirmed
physical-action evidence at every point.

| Evidence level | Meaning | May collect factual Feedback | May become causal Experience sample |
| --- | --- | --- | --- |
| `intent_only` | action was durably prepared before any command | no | no |
| `command_incomplete` | the process could have reached MQTT, but completion is unknown | no | no |
| `command_completed` | MQTT `on` and `off` publish workflow completed | yes | no |
| `manual_confirmed` | an operator recorded a real watering fact | yes | yes |
| `device_confirmed` | a future independent device acknowledgement proves execution | yes | yes |

The Collector never upgrades an evidence level from plant response, a log
timestamp, `pending_soak`, or a nearby Shadow episode.  A good soil response
after an unconfirmed command remains a useful observation but not confirmed
causal evidence.

## Action receipt sidecar

### Receipt shape

The sidecar is an internal, append-safe persistence contract named
`feedback_action_receipt.v1`.  One file is stored per `action_id` under the
Phase3 runtime-owned receipt directory.  The filename is
`act-<24-lowercase-hex>.json`; the id is generated exactly once before the
action command.  A representative final receipt is:

```json
{
  "schema_version": "feedback_action_receipt.v1",
  "action_id": "act-0123456789abcdef01234567",
  "device_code": "soil3",
  "action_source": "phase3_native",
  "created_at": "2026-10-08T12:00:00Z",
  "reference_action_at": "2026-10-08T12:00:08Z",
  "command": {"kind": "water", "pump_seconds": 8.0},
  "command_status": "command_completed",
  "execution_evidence": {
    "level": "command_completed",
    "physical_action_confirmed": false,
    "mqtt_on_published_at": "2026-10-08T12:00:00Z",
    "mqtt_off_published_at": "2026-10-08T12:00:08Z"
  },
  "episode_binding": {"mode": "create_independent", "episode_id": null},
  "initial_state": {"schema_version": "state.v1"},
  "trace_id": null,
  "source_receipt_ref": null
}
```

`initial_state` is the existing `state.v1` snapshot built immediately before
the receipt is prepared.  Its real `observed_at` and missing facts are kept
unchanged; no action time or state fact is synthesized.  Its only purpose is
to create a real-action Episode with an honest baseline.

### Producers

1. **Native Phase3** creates a write-ahead receipt before its existing MQTT
   `on` publish.  It does not alter Phase3 duration choice, safety checks,
   MQTT payloads, or actuator sequencing.  After the existing call returns,
   it updates the same receipt to `command_completed`; exceptions update the
   same receipt to an incomplete/failed command state.
2. **Controlled Execution** creates one write-ahead receipt whose stable
   action id is deterministically derived from its one-shot approval id.  It
   preserves the existing Trace/Episode binding and updates the same receipt
   after the formal Phase3 cycle returns.  A returned positive action duration
   still proves only `command_completed` unless later independently confirmed.
3. **Manual confirmation CLI** creates a receipt only.  It requires an
   operator-supplied stable action id, action time, actor, and water duration;
   repeating exactly the same command is idempotent and a mismatching reuse of
   the id is refused.  It never imports or invokes `manual_water`, Phase3,
   MQTT, or an actuator.

### Crash safety and idempotency

The first receipt write uses create-if-absent and is flushed before MQTT is
allowed.  If it cannot be made durable, Phase3 returns before publishing
`on`; this is a traceability availability guard, not a change to its decision
or pump protocol.  An action therefore cannot begin without a durable id.

There is no atomic transaction spanning a filesystem and MQTT.  A process
crash after `on` can leave `intent_only` or `command_incomplete`; that file is
intentionally retained as an unresolved action, never replayed and never
silently treated as no action.  This prevents a real action from being lost
forever while refusing to claim completion without proof.

Later receipt updates are atomic replacement of the same file.  A restart or
duplicate scan sees the same `action_id`, so it cannot create a second action
event.  No recovery path derives a new receipt from `pending_soak`, logs, or
timestamps.

## Episode binding and provenance

The action fact is appended through the existing `EpisodeStore`:

```json
{
  "kind": "water",
  "action_id": "act-...",
  "action_source": "phase3_native",
  "executed_at": "2026-10-08T12:00:08Z",
  "pump_seconds": 8.0,
  "execution_evidence": {
    "level": "command_completed",
    "physical_action_confirmed": false
  },
  "receipt_ref": {"schema_version": "feedback_action_receipt.v1", "path": "...", "sha256": "..."}
}
```

* Native Phase3 receipts create a new independent Episode from the receipt's
  snapshot.  They never attach to a five-minute Shadow Episode.
* Controlled receipts append this action fact to their already verified,
  explicitly bound open Episode.
* Manual receipts create an independent Episode from the factual snapshot
  captured at confirmation.  A manually supplied historical action time that
  cannot be honestly associated with that snapshot stays collectable but is
  not causal-Experience eligible.

`feedback.v1` already binds each Feedback record to its Episode and carries
`reference_action_at`; the Outcome carries the same episode id.  Source and
evidence are retained in the parent Episode's unique action fact rather than
duplicated into a new Feedback protocol.  This preserves the existing
`feedback.v1` contract and keeps provenance auditable for both Feedback and
Outcome.

## Collector lifecycle

The separate Collector scans only durable receipt files with
`command_completed`, `manual_confirmed`, or `device_confirmed` evidence.  It
keeps collector-owned scheduling state per action id under the agent runtime
root.  A single-instance lock prevents two timer invocations from writing the
same window concurrently.

| Window | Due target | Accepted interval | On late/missed recovery |
| --- | --- | --- | --- |
| `30min` | 30 min | 15-60 min | mark missed after 60 min |
| `2-3h` | 150 min | 90-240 min | mark missed after 240 min |
| `6-12h` | 540 min | 300-840 min | mark missed after 840 min |
| `24h` | 1440 min | 1080-1800 min | mark missed after 1800 min, then finalize |

For a due window the Collector builds an in-memory, read-only `state.v1`
snapshot using the existing canonical telemetry/state adapters.  It writes a
soil observation only from those returned facts.  Unavailable or stale source
facts remain null/missing; there is no CSV fallback and no backdated reading.

If Vision is enabled, the Collector calls only the public Vision API and
accepts facts only after validating the public `vision_run.v1` manifest and
its referenced observations.  A capture/configuration/validation failure
produces `observations.vision = null`; it does not stop soil Feedback or the
rest of the schedule.

For every successfully stored `feedback.v1` record, the Collector immediately
uses `FeedbackStore.attach_to_episode(..., finalize=False)`.  After the 24h
window has either been recorded or marked missed, it finalizes once through
the existing store.  Missing windows remain explicit in the existing Outcome
and an already closed Episode is never reopened.

## Assessments and Experience eligibility

The Collector records raw soil and Vision facts first.  Its minimal
deterministic assessment is deliberately limited to existing `feedback.v1`
vocabulary:

* with a numeric humidity plus numeric `target_low` and `field_capacity`,
  `sustained_dry` is true at/below `target_low`; `sustained_wet` is true
  at/above `field_capacity`; otherwise each is false;
* `recovery` is `good` only when neither condition is true, `poor` when either
  is true, and `unknown` when the required facts are missing or unusable;
* `rewater_needed` mirrors a known `sustained_dry`; `visual_recovery` remains
  `unknown` unless a future approved comparator supplies an evidence-backed
  comparison; and data quality is `unknown` when the state cannot support the
  assessment.

This does not calculate a reward or claim that an action caused the observed
state.  The Outcome remains the existing aggregation of independent facts.

`ExperienceRetriever` will recognize feedback Outcomes conservatively: a
non-conflicted `feedback.v1` Outcome whose latest known recovery is `good`
maps to success; `poor` maps to failure; all other values are unclassified.
It must additionally require exactly one matching Episode action whose
evidence level is `manual_confirmed` or `device_confirmed`.  A
`command_completed` record can close with factual Feedback but is excluded
from causal retrieval.

## Service boundary and operations

New, independent units:

* `plant-agent-soil3-feedback-collector.service` (`Type=oneshot`)
* `plant-agent-soil3-feedback-collector.timer` (`OnBootSec` plus a five-minute
  `OnUnitActiveSec`, `Persistent=true`)

They do not start, restart, or block the State or Shadow pipeline timers.
The service has read access to telemetry, Phase3 receipt files, and optional
Vision artifacts, and write access only to its collector state plus the
existing Agent runtime Feedback/Episode directories.  It has no MQTT, GPIO,
or actuator permission.

## Acceptance tests

Focused tests will prove:

1. no receipt means no Feedback, including ordinary Shadow Episodes;
2. native, controlled, and manual sources get one stable action id and no
   duplicate tracking across restarts/scans;
3. a crash at each write-ahead boundary retains one unresolved receipt and
   never causes replay;
4. native creates an independent Episode while controlled reuses its verified
   Episode;
5. all four timing windows collect true State facts once, missed windows stay
   missing, and Vision unavailable remains null;
6. 24h finalization closes exactly once;
7. only confirmed actions with non-conflicted `good`/`poor` Feedback Outcomes
   enter Experience Retrieval; command-only records do not;
8. the manual CLI has no `manual_water`, Phase3, MQTT, or actuator call path;
   and
9. existing Shadow pipeline behavior remains unchanged.

