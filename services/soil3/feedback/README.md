# soil3 Feedback V1

This module implements the multi-timescale factual feedback and Outcome record
from Issue #18:

```text
episode.v1 (Day 3) + factual observations at 30min / 2-3h / 6-12h / 24h
                     -> feedback.v1 records -> one aggregated outcome payload
```

A feedback record is a fact, not an actor. The module grants no execution,
publishes no MQTT, never calls `manual_water`, does not import or modify
Phase3, fabricates no observation or timestamp, and does not depend on the
Experience or Bridge work. It builds on the Day-3 `episode.v1` contract:
every record names its episode, and the optional attach step writes into an
episode under episode.v1's own rules. Collecting observations (scheduling
captures or reads) is not this module's job — visual-feedback collection
timing belongs to Issue #30; this store records what callers hand it.

## Windows

One record covers one observation window of one episode. All four windows are
representable:

| Window | Nominal meaning | Plausibility range after `reference_action_at` |
| --- | --- | --- |
| `30min` | about 30 minutes after the action | 15–60 min |
| `2-3h` | 2–3 hours after | 90–240 min |
| `6-12h` | 6–12 hours after | 300–840 min |
| `24h` | about 24 hours after | 1080–1800 min |

`reference_action_at` (for example the watering's `executed_at`) is an optional
caller fact. The ranges are generous so ordinary scheduler jitter passes; an
observation outside its range — or before its reference — is still stored, but
flagged in `contradictions`. When no reference time is supplied, or either
time is unparseable, the timing check is skipped instead of guessed.

## Recorded facts

- `observations.soil` / `observations.vision`: caller-supplied structured
  facts, envelope-checked only (object or null). The facts belong to their
  producers (state/sensors, vision); this module never reshapes or completes
  them.
- `assessments`, kept independent — no unified reward is computed anywhere:
  `recovery` (good/partial/poor/none/unknown), `sustained_dry`,
  `sustained_wet`, `rewater_needed` (true/false/unknown),
  `visual_recovery` (improved/unchanged/worse/unknown), and `data_quality`
  (good/degraded/poor/unknown).

## Missing data and contradictions

- An assessment nobody supplied stays `"unknown"`; an observation nobody
  supplied stays `null` and is listed in `missing_observations`. Absence is
  stated, never filled in.
- Contradictory data is preserved and flagged, never reconciled, repaired, or
  deleted. Store-computed flags: `sustained_dry_and_wet` (both true in one
  record), `assessment_without_soil_observation` /
  `assessment_without_vision_observation` (a known assessment with no
  observation backing it), `observed_at_before_reference`, and
  `observed_at_outside_window`.
- `feedback_id`, `created_at`, `contradictions`, and `missing_observations`
  are store-computed; a caller payload carrying them (or any unknown field) is
  refused. Reasoning-shaped keys (`chain_of_thought`, `hidden_reasoning`,
  `raw_reasoning`, `reasoning_trace`, `thinking`) are refused anywhere in a
  payload, recursively — the same vocabulary episode.v1 refuses.
- `observed_at` is required and must be parseable, because window placement
  and ordering depend on it; refusing beats inventing a time. An unparseable
  `reference_action_at` string is kept exactly as the producer wrote it.
- Refused operations change nothing on disk: validation runs before any write,
  and writes are atomic. Records are write-once: the store never edits or
  deletes one.

## Operations

| Operation | Effect | Refused when |
| --- | --- | --- |
| `record(payload)` | Stores one window observation; computes id, contradictions, missing list | payload fails validation |
| `read(feedback_id)` | Returns the stored record, as a deep copy | id is malformed or unknown, file is corrupt |
| `list_for_episode(episode_id)` | All records of one episode, oldest observation first | id is malformed; a record file is corrupt (fails loudly, never silently skipped) |
| `outcome(episode_id)` | Aggregates records into one outcome payload (returned, not written) | id is malformed |
| `attach_to_episode(episode_id, store)` | Appends the records to the episode's `feedback` and sets its `outcome` once | episode has no records, is unknown, or is closed |

### Outcome aggregation rules

For each assessment key, the value from the **latest window that reported a
known value** wins (`24h` over `6-12h` over `2-3h` over `30min`); a key nobody
ever reported stays `"unknown"` with a `null` source, and `assessment_sources`
states which window each value came from. Windows with no record are listed in
`windows_missing`; repeated records within one window are ordered by
`observed_at` and the latest contributes, while **all** records' contradiction
flags are unioned into the outcome, so aggregation cannot hide a conflict. The
outcome keeps the independent evaluations exactly as recorded and computes no
unified reward. An episode with no records at all yields an all-`unknown`
outcome — the honest statement that nothing was observed.

### Attach semantics

`attach_to_episode` writes through `EpisodeStore.update`, so episode.v1's own
rules apply: feedback entries append, the outcome is set-once, and a closed
episode is immutable. Attach is idempotent — records already on the episode
(by `feedback_id`) are not appended twice, and an outcome the episode already
holds is never rewritten. Because the outcome is set-once, attach it after the
last window you intend to record (normally `24h`); an outcome attached earlier
cannot be refreshed by this module.

## Storage

One JSON file per record (`fb-<24 hex>.json`) under a caller-chosen directory;
ids are pattern-checked before any filesystem access, so a crafted id cannot
escape the store directory. Single-writer access per directory is assumed. The
module has no default directory and never infers, creates, or edits production
runtime/data/log paths.

## CLI

```bash
python -m services.soil3.feedback.service --store-dir /path/to/feedback record \
  --input observation.json
python -m services.soil3.feedback.service --store-dir /path/to/feedback read \
  --feedback-id fb-xxxx
python -m services.soil3.feedback.service --store-dir /path/to/feedback list \
  --episode-id ep-xxxx
python -m services.soil3.feedback.service --store-dir /path/to/feedback outcome \
  --episode-id ep-xxxx
python -m services.soil3.feedback.service --store-dir /path/to/feedback attach \
  --episode-id ep-xxxx --episode-store-dir /path/to/episodes
```

Every payload input is a caller-supplied JSON file; the CLI reads facts, it
never produces them. Errors print `{"error": code, "reasons": [...]}` on stderr
and exit non-zero.

`feedback.v1` is marked `implemented` in `docs/PROTOCOLS_AND_BOUNDARIES.md`:
the contract exists and is covered by `tests/test_feedback_v1.py`, but it is
not frozen, so compatibility iteration may continue inside later Issues. No
producer writes feedback records yet, and no consumer reads outcomes yet.
