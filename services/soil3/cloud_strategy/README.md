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

Example invocation:

```bash
python -m services.soil3.cloud_strategy.service \
  --config /path/to/runtime-config.json \
  --state /path/to/state.v1.json
```

The command exits with status `2` when the provider, parser, or Validator
rejects the proposal. It never falls through to a device-control path.
