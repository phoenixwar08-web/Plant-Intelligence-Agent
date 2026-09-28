# Day 6 Historical Replay Regression Design

## Goal

Use existing read-only soil3 historical replay samples to batch exercise the
public `State -> Strategy -> Validator -> Gate v2` path. Produce reproducible
per-case evidence, aggregate statistics, and a deterministic list of risky
cases without entering Runner, Bridge, Phase3, MQTT, ActuatorLayer, or any
physical-control path.

## Scope and non-goals

This change adds a batch regression service, command-line entry point, tests,
and usage documentation. It consumes existing `replay_sample.v1`, Cloud
Strategy, Strategy Validator, and Gate v2 interfaces.

It must not:

- modify `state.v1`, `replay_sample.v1`, `strategy.v1`, or `gate.v2`;
- rebuild or mutate historical source data;
- write a Gate budget ledger or request exploration;
- invoke Runner, Bridge, Episode, Phase3, MQTT, ActuatorLayer, or a pump;
- require production access, openEuler deployment, or same-Day hardware work;
- invent missing model responses, safety facts, measurements, or outcomes.

## Considered approaches

### Selected: manifest-driven fixture baseline with optional live provider

A batch manifest names existing replay samples and, in deterministic fixture
mode, one captured model response per sample. Each case runs through the same
public Strategy, Validator, and Gate functions used by the application. This
keeps acceptance reproducible while retaining an explicit optional live mode
for observing real provider behavior.

### Rejected: rebuild Replay inside the batch runner

Reading CSV, watering history, State history, and parameter history directly
would duplicate the Day 2 Replay input workflow and couple statistical
regression to historical-source parsing. Issue #25 starts from the already
defined replay sample boundary.

### Rejected: reuse the complete Shadow runtime

The Shadow runtime continues through Runner, Bridge, Trace, and Episode. Those
components are outside Issue #25 and add stateful artifacts that are not needed
to validate the requested four-stage historical path.

## Inputs

The CLI accepts a JSON batch manifest with a top-level version and ordered
cases. Each case contains:

```json
{
  "case_id": "stable-human-readable-id",
  "replay_sample": "relative/or/absolute/replay_sample.json",
  "fixture_response": "relative/or/absolute/model-response.txt",
  "labels": ["optional", "descriptive", "labels"]
}
```

Rules:

- paths relative to the manifest are resolved from the manifest directory;
- `case_id` values are unique and non-empty;
- the replay artifact must be a `replay_sample.v1` object containing a
  `state.v1` object for soil3;
- fixture mode requires `fixture_response` for every case;
- live mode ignores fixture-response content and uses the existing configured
  Cloud Strategy provider;
- labels are copied only as case metadata and never affect decisions.

The Strategy configuration, prompt, and Gate policy are existing inputs. No
secret or provider response is embedded in the manifest.

## Processing model

Cases run in manifest order. For every case the service:

1. validates and loads the replay sample without modifying it;
2. calls the public Cloud Strategy `run_chain()` with either captured fixture
   content or the configured live provider;
3. records the formal Strategy validation result returned by that chain;
4. when a validated Strategy exists, calls public `evaluate_gate_v2()` with
   `exploration_requested=false` and no budget ledger;
5. classifies objective statistics and risky-case reasons;
6. records a bounded case result and continues to the next case.

An invalid Strategy does not get passed to Gate because no formal
`strategy.v1` object exists to admit. Its Validator rejection remains visible
in the case result. A malformed replay input or unexpected case-local failure
is recorded as a system error and does not abort later cases. Failure to load
the batch-level manifest, Strategy configuration, prompt, or Gate policy is a
batch error and fails before processing begins.

## Deterministic and live modes

`fixture` is the default and the acceptance mode. It supplies the exact
captured text to the existing `run_chain(fixture_content=...)` boundary. Given
unchanged inputs, normalized report content is reproducible.

`live-provider` is opt-in. It calls the existing provider through `run_chain()`
and is explicitly marked non-deterministic in the report. Provider exceptions,
invalid JSON, and Validator rejects are measured using the existing Strategy
chain result rather than a second model-integration implementation.

Run timestamps and randomly assigned upstream identifiers are retained only in
the per-run evidence where necessary. The reproducibility comparison and
summary digest are computed from stable case IDs, input digests, validation
outcomes, Gate decisions/reasons, action summaries, and error codes, not from
volatile timestamps or UUIDs.

## Gate policy and side-effect boundary

The batch service uses `evaluate_gate_v2(state, strategy, policy, False,
None)`. Exploration is never requested, no `BudgetLedger` is constructed, and
no reservation can be written. Gate remains admission-only and its execution
metadata must still say `actuator_commands_allowed=false`.

The new module must not import Runner, Bridge, Phase3, MQTT, ActuatorLayer, or
manual-water modules. Tests enforce this boundary and verify that historical
inputs are byte-identical before and after a run.

## Outputs

One run writes two derived artifacts:

- a machine-readable JSON report containing run metadata, input digests,
  ordered case results, aggregate statistics, risky cases, and a stable summary
  digest;
- a Markdown rendering of the same facts for Review and experiment inspection.

No Strategy, State, or Gate business object is treated as a new protocol.
Case results reference the source sample and summarize the already returned
records instead of copying entire historical State objects into the report.

Aggregate statistics include:

- total, completed, and system-error case counts;
- Strategy accepted/rejected counts and Validator reason-code counts;
- action-type counts, action-count distribution, and proposed pump-duration
  totals derived only from accepted Strategies;
- Gate `allow`, `allow_with_warning`, and `deny` counts;
- Gate reason-code and warning-code counts;
- model/provider failure-code counts;
- risky-case counts grouped by deterministic reason.

Unavailable categories are reported as zero; no missing value is inferred.

## Risky-case rules

The service does not calculate reward, severity, or a subjective danger score.
A case is retained in the reproducible risky-case list when any of these
objective rules applies:

- the model proposed a water action but formal validation rejected the
  Strategy;
- an accepted Strategy contains a water action and Gate returns `deny` or
  `allow_with_warning`;
- Gate denies because of a safety flag, missing/unavailable safety fact,
  missing sensor fact, freshness failure, or Strategy binding failure;
- Strategy generation/validation or case processing ends in a model or system
  error, so no safe admission conclusion can be formed.

Each entry contains the case ID, replay sample ID and digest, rule codes,
Validator/Gate reason codes, and source artifact references. It contains no
invented remediation or outcome.

## Error handling

- Invalid manifest structure is a batch-level validation error.
- Missing/unreadable case files, invalid replay artifacts, and unexpected
  case-local exceptions are recorded under that case and processing continues.
- Provider failure, empty response, invalid JSON, and Validator rejection use
  the existing Strategy chain failure and reason codes.
- Gate is called only with a formally accepted Strategy. A Gate exception is a
  case-local system error.
- Output files are written atomically after processing. Existing output files
  are replaced only when the caller explicitly names them.

## Verification

Focused tests cover:

- manifest validation and relative path resolution;
- repeated fixture runs producing the same stable statistics, risky-case list,
  and summary digest;
- accepted and rejected Strategies;
- all three Gate decisions;
- invalid JSON, provider failure representation, and case-local system errors;
- continuation after a failed case;
- correct Strategy/action and reason-code distributions;
- objective risky-case classification;
- byte-identical historical inputs after execution;
- no import or call path to Runner, Bridge, Episode, Phase3, MQTT,
  ActuatorLayer, or physical control.

Relevant Replay, Cloud Strategy, Validator, and Gate regressions run after the
focused suite. No deployment or real-device demonstration is part of Issue
#25; Day-level deployment remains gated on all Day 6 Issues being merged.
