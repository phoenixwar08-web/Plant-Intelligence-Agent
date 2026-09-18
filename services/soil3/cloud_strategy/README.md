# soil3 Cloud Strategy V1

This module implements the non-executing proposal chain from Issue #13:

```text
state.v1 -> Cloud LLM -> strategy.v1 -> Validator
```

It does not publish MQTT, call `manual_water`, import Phase3, or create an
`ActionPlan`. A validated strategy remains only a proposal for later Gate,
Runner, and Phase3 processing.

Use `config/cloud_strategy.example.json` as the configuration template. Supply
the API key only through the configured environment variable. Runtime state,
audit JSONL, credentials, and real provider addresses do not belong in Git.

## Validator ceilings

`strategy.v1` is marked `implemented` in `docs/PROTOCOLS_AND_BOUNDARIES.md`: the
contract exists and is covered by tests, but it is not frozen, so compatibility
iteration may continue. The ceilings below are protocol bounds, not tuning knobs.
Each row says whether runtime config may tighten it, or whether it is a protocol
constant with no config knob at all.

| Limit | Ceiling | Config may tighten? |
| --- | --- | --- |
| actions per strategy | 12 | yes: `validator.max_actions` |
| `pump_seconds` per water action | 120 | yes: `validator.max_pump_seconds` |
| `seconds` per wait action | 86400 | yes: `validator.max_wait_seconds` |
| summed `pump_seconds` across all actions | 240 | yes: `validator.max_total_pump_seconds` |
| summed water + wait time across all actions | 86400 | yes: `validator.max_total_seconds` |
| `reason_summary` and `risk_notes` items | 8 | no: protocol constant |
| unknown keys in `execution`, `model`, `expected_outcome` | rejected | no: protocol constant |

The two summed ceilings exist because per-action limits alone still accept
twelve repetitions of the largest allowed action. A config value outside its
protocol bound raises at construction, so tightening is a deliberate act and
widening is impossible.

Unknown fields are rejected at the top level, inside every action object, and
inside `execution`, `model`, and `expected_outcome`. `expected_outcome` accepts
only `soil_moisture` (one string) and `risk_notes` (up to 8 strings), so
execution-shaped keys such as `pump_seconds`, `gpio`, `cmd`, or `relay` cannot
enter the record through a descriptive field. Widening any of these key sets is
a protocol change, not a local edit.

## Binding to a state.v1 snapshot

`state.v1` writes no identifier column, so a proposal names the snapshot it came
from with three fields: `device_code` (always `soil3`), that snapshot's
`observed_at`, and `state_sha256` — a SHA-256 of the snapshot under a
key-order-independent JSON encoding.
`state_generated_at` is required and is reported in every audit record for
traceability, but it is deliberately not a binding condition of its own: it is
part of the hashed content, so a strategy that names a different snapshot's
`generated_at` is already caught by `state_sha256_mismatch`.

The hash is what makes the binding unambiguous: `state.v1` declares no uniqueness
for `(device_code, observed_at)`, so two snapshots of one device carrying the
same observation moment can only be told apart by their content.

`state_sha256` is attached by this service, never by the model. The prompt tells
the provider not to write it, and a value that arrives anyway is overwritten from
the snapshot actually loaded before the Validator runs; the Validator recomputes
it independently and reports `invalid_state_sha256` or `state_sha256_mismatch`. An
attempted override stays visible in the audit copy of the raw response and in the
hash of the complete response.

### One timestamp form

`strategy.v1` carries exactly one timestamp notation: RFC 3339 in UTC with a
trailing `Z`, the form the `state.v1` producer itself writes for `generated_at`.
Schema and Validator accept only that form for `state_observed_at`,
`state_generated_at`, and `created_at`, so an equivalent instant written another
way is a malformed record, not a binding mismatch.

The state side stays lenient because `state.v1` copies `observed_at` through
untouched and a real record can hold an ISO string with a `Z`, another offset, or
a numeric epoch. This service therefore rewrites the notation at both boundaries:
on the way out, in the projection sent to the provider, and on the way in, on the
parsed reply, before the Validator sees it. Values are compared as instants after
that rewrite, and a timestamp that cannot be parsed is left alone so the Validator
reports it instead of the service inventing one. A state whose `observed_at` is
missing or unparseable is rejected rather than guessed at. The chain's own audit
record is the one place that still mirrors the snapshot as loaded, notation
included, because it reports what the source said.

The fixtures in `tests/test_cloud_strategy.py` are built by calling
`StateBuilder` from `services/soil3/state/state_v1.py`, so a change in the
producer's shape breaks these tests instead of quietly diverging.

## What leaves the machine, and what is kept

The model receives a projection, not the state file: the scalar identity fields
plus declared keys under `soil`, `air`, `trends`, `irrigation`, `safety` (its
numeric thresholds and the known Phase3 flags only) and `data_quality`.
`extensions`, `fact_sources`, `source_timestamps`, `vision`, and any flag name
outside the declared list are never forwarded; `state.v1` may carry runtime paths
in those places and `MODEL_INPUT_SAFETY_FLAGS` is checked against the producer's
own flag list by test.

Each run is written to the audit JSONL with the projection it sent, a SHA-256
fingerprint of the full state, and the model's raw text truncated to
`AUDIT_RAW_RESPONSE_MAX_CHARS` with the character count and a SHA-256 of the
complete response. The unvalidated response is exactly the artifact that can
carry smuggled keys, so it is fingerprinted rather than stored without limit.

A rejected, unparseable, broken-validator-config, or broken-provider-config
proposal fails closed: the chain records a reason code
(`invalid_provider_config`, `invalid_validator_config`, `invalid_model_json`,
`provider_disabled`, `missing_api_key`, ...) and exits non-zero without
reaching any device path. Config defects are reason codes, never tracebacks.

The prompt file is `prompts/strategy_v1.txt` for the `strategy.v1` schema family;
its `prompt_version` label is `strategy-prompt.v2`, which is the revision of the
text itself. Proposals carrying any other `prompt_version` are rejected, so audit
records can tell prompt revisions apart. `strategy-prompt.v2` has no consumer and
no released history yet, so the wording that lands with this PR is what v2 means;
the label is bumped when a further revision has to coexist with records of this one.

Example invocation:

```bash
python -m services.soil3.cloud_strategy.service \
  --config /path/to/runtime-config.json \
  --state /path/to/state.v1.json
```

The command exits with status `2` when the provider, parser, or Validator
rejects the proposal. It never falls through to a device-control path.
