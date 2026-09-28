# soil3 historical Strategy and Gate regression

This module implements the read-only Day 6 regression path from Issue #25:

```text
replay_sample.v1/state.v1 -> Cloud Strategy -> StrategyValidator -> gate.v2
```

It does not call Runner, Bridge, Episode, Trace, Phase3, MQTT, ActuatorLayer,
`manual_water`, or a pump. It never requests exploration and never constructs
or writes a Gate budget ledger.

## Input manifest

The manifest is an ordered list of existing Replay samples. Relative paths are
resolved from the manifest file:

```json
{
  "schema_version": "historical_regression_manifest.v1",
  "cases": [
    {
      "case_id": "2026-09-16-baseline",
      "replay_sample": "samples/replay-20260916.json",
      "fixture_response": "responses/replay-20260916.txt",
      "labels": ["baseline"]
    }
  ]
}
```

`case_id` values must be unique. Each Replay file must contain a soil3
`state.v1`. Fixture mode requires one captured model response per case. Labels
are descriptive metadata only and do not affect Strategy or Gate decisions.

## Reproducible fixture run

```powershell
python -m services.soil3.historical_regression.service `
  --manifest D:\read-only\regression-manifest.json `
  --strategy-config config\cloud_strategy.example.json `
  --prompt services\soil3\cloud_strategy\prompts\strategy_v1.txt `
  --gate-policy config\cloud_gate.example.json `
  --json-output D:\reports\day6-regression.json `
  --markdown-output D:\reports\day6-regression.md
```

Fixture mode is the default acceptance mode. Its stable digest excludes
upstream timestamps and random run/Gate identifiers while retaining sample
digests, validation results, action summaries, Gate decisions, reason codes,
and error codes. Re-running unchanged inputs therefore produces the same
statistics, risky-case list, and `summary_sha256`.

## Optional live-provider run

Add `--mode live-provider` to call the provider configured by the existing
Cloud Strategy configuration. The manifest then does not require
`fixture_response`. The resulting report is explicitly marked
non-deterministic. Provider failures and invalid outputs are recorded through
the existing Cloud Strategy reason codes; no fallback response is invented.

Live mode is for observation, not the reproducible acceptance baseline. It
requires the provider credential environment variable already named by the
Strategy configuration.

## Reports

The JSON and Markdown reports contain:

- Strategy accepted/rejected and Validator reason-code counts;
- accepted Strategy action types, action counts, and proposed pump seconds;
- Gate `allow`, `allow_with_warning`, and `deny` counts;
- Gate reason and warning codes;
- model/provider and case-local system errors;
- an ordered, reproducible list of objectively risky cases.

Risk classification does not infer severity, reward, remediation, Feedback,
or Outcome. It retains water proposals rejected by Validator, water proposals
that Gate denies or warns about, safety/data/freshness/binding Gate denies, and
model/system errors that prevent a safe admission conclusion.

Malformed case inputs are recorded and the next case continues. Invalid
batch-level manifest, Strategy config, prompt, or Gate policy inputs stop the
run before case processing. Input files are opened read-only; only the two
explicitly named output paths are atomically replaced.
