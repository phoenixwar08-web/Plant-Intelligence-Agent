# soil3 Episode V1

This module implements the structured care-experience record from Issue #17:

```text
state.v1 snapshot + strategy + gate result + executed actions + feedback + outcome
                              -> one episode.v1 record
```

An episode is a record, not an actor. It grants no execution, publishes no
MQTT, never calls `manual_water`, does not import Phase3, and does not create
strategies. It depends only on the frozen `state.v1` data contract; the Runner
(Issue #16), Gate (Issue #15), and Feedback/Outcome (Issue #18) are separate
modules, and none of them needs to exist for this store to work. Visual
feedback collection timing belongs to Issue #30, not here.

## Lifecycle

| Operation | Effect | Refused when |
| --- | --- | --- |
| `create(initial_state)` | Opens one episode (`status: open`) from a real `state.v1` record; computes the binding hash locally | input is not a soil3 `state.v1` object |
| `update(episode_id, ...)` | Attaches caller-supplied sections; `strategy` / `gate_result` / `outcome` set-once, `executed_actions` / `feedback` append-only | episode is closed; any supplied section fails validation; a set-once section already holds a different value |
| `read(episode_id)` | Returns the stored record with its lifecycle status, as a deep copy | id is malformed or unknown |
| `close(episode_id, outcome=None)` | Sets `status: closed` and `closed_at` exactly once; finalizes `missing_facts` | episode is already closed; a differing `outcome` was already set |

A closed episode is immutable. Both successful and failed episodes are kept;
nothing is deleted or repaired by this module.

## Binding and no-fabrication rules

- `state_binding.state_sha256` is computed by the store from the embedded
  snapshot, under the same canonical encoding `strategy.v1` binds with (the
  hashing and timestamp helpers are imported from the Cloud Strategy Validator
  so the two cannot drift). A caller cannot choose which state an episode is
  linked to.
- A supplied `strategy` carrying a `state_sha256` or `state_observed_at` that
  does not match the embedded snapshot is refused
  (`strategy_state_sha256_mismatch`, `strategy_state_observed_at_mismatch`).
- Structured reason and confidence survive storage: `reason_summary` and
  `confidence` on the strategy, `decision` and `reason_codes` on the gate
  result, and the outcome's independent evaluations are preserved exactly as
  supplied. No unified reward is computed.
- `gate.v1` is implemented, but this Issue does not make Episode a Gate
  consumer, so `gate_result` remains a caller's structured payload with only
  envelope checks. `feedback.v1` remains planned and is stored with the same
  envelope checks (object / list of objects, decision vocabulary when present).
  This module writes no
  consumer that assumes a planned contract exists.
- A section nobody supplied stays `null`/empty and is listed in
  `missing_facts` at close. Missing facts are stated, never invented —
  including timestamps: an unparseable caller timestamp is left exactly as the
  producer wrote it.
- The model's hidden thought process is never recorded. Payloads carrying
  reasoning-shaped keys (`chain_of_thought`, `hidden_reasoning`,
  `raw_reasoning`, `reasoning_trace`, `thinking`) are refused outright, so a
  reasoning leak fails loudly instead of entering the experience record.
- Refused operations change nothing on disk: validation runs before any write,
  and writes are atomic.

## Storage

One JSON file per episode (`ep-<24 hex>.json`) under a caller-chosen
directory; episode ids are pattern-checked before any filesystem access, so a
crafted id cannot escape the store directory. Single-writer access per
directory is assumed. The module has no default directory and never infers,
creates, or edits production runtime/data/log paths.

## CLI

```bash
python -m services.soil3.episode.service --store-dir /path/to/episodes create \
  --state /path/to/state_v1.json
python -m services.soil3.episode.service --store-dir /path/to/episodes update \
  --episode-id ep-xxxx \
  --strategy strategy.json --gate-result gate.json \
  --executed-actions actions.json --feedback feedback.json
python -m services.soil3.episode.service --store-dir /path/to/episodes read \
  --episode-id ep-xxxx
python -m services.soil3.episode.service --store-dir /path/to/episodes close \
  --episode-id ep-xxxx --outcome outcome.json
```

Every payload input is a caller-supplied JSON file; the CLI reads facts, it
never produces them. Errors print `{"error": code, "reasons": [...]}` on stderr
and exit non-zero.

`episode.v1` is marked `implemented` in `docs/PROTOCOLS_AND_BOUNDARIES.md`:
the contract exists and is covered by `tests/test_episode_v1.py`, but it is not
frozen, so compatibility iteration may continue inside later Issues.
