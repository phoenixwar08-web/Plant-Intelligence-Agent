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
iteration may continue. The numbers below are still protocol ceilings rather
than tuning knobs: `validator` in the config may tighten each one but never
widen it, and an out-of-range config raises at construction.

| Limit | Protocol ceiling |
| --- | --- |
| actions per strategy | 12 |
| `pump_seconds` per water action | 120 |
| `seconds` per wait action | 86400 |
| summed `pump_seconds` across all actions | 240 |
| summed water + wait time across all actions | 86400 |
| `reason_summary` and `risk_notes` items | 8 |

The two summed ceilings exist because per-action limits alone still accept
twelve repetitions of the largest allowed action.

Unknown fields are rejected at the top level, inside every action object, and
inside `execution`, `model`, and `expected_outcome`. `expected_outcome` accepts
only `soil_moisture` (one string) and `risk_notes` (up to 8 strings), so
execution-shaped keys such as `pump_seconds`, `gpio`, `cmd`, or `relay` cannot
enter the record through a descriptive field. Widening any of these key sets is
a protocol change, not a local edit.

A rejected, unparseable, or broken-config proposal fails closed: the chain
records the reason codes and exits non-zero without reaching any device path.

Example invocation:

```bash
python -m services.soil3.cloud_strategy.service \
  --config /path/to/runtime-config.json \
  --state /path/to/state.v1.json
```

The command exits with status `2` when the provider, parser, or Validator
rejects the proposal. It never falls through to a device-control path.
