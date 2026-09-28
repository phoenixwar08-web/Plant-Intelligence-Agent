"""CLI for read-only soil3 historical Strategy and Gate regression."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from services.soil3.cloud_gate.gate_v1 import GatePolicy

from .batch_v1 import load_batch_manifest, run_batch


def _load_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{name} must contain a JSON object") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return value


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_markdown(report: dict[str, Any]) -> str:
    statistics = report["statistics"]
    cases = statistics["cases"]
    strategy = statistics["strategy"]
    lines = [
        "# soil3 Historical Regression Report",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Deterministic: `{str(report['deterministic']).lower()}`",
        f"- Summary SHA-256: `{report['summary_sha256']}`",
        f"- Cases: {cases['total']} total, {cases['completed']} completed, {cases['system_error']} system errors",
        f"- Strategy: {strategy['accepted']} accepted, {strategy['rejected']} rejected",
        "",
        "## Gate decisions",
        "",
    ]
    decisions = statistics["gate_decisions"]
    for decision in ("allow", "allow_with_warning", "deny"):
        lines.append(f"- `{decision}`: {decisions.get(decision, 0)}")
    lines.extend(["", "## Risky cases", ""])
    risky_cases = report["risky_cases"]
    if not risky_cases:
        lines.append("No risky cases were retained by the objective rules.")
    else:
        lines.extend(
            [
                "| Case | Sample | Risk codes | Validator / Gate evidence |",
                "| --- | --- | --- | --- |",
            ]
        )
        for case in risky_cases:
            evidence = [
                *case["validator_reason_codes"],
                *case["gate_reason_codes"],
                *case["gate_warning_codes"],
            ]
            if case.get("error_code"):
                evidence.append(case["error_code"])
            lines.append(
                "| {case} | {sample} | {risks} | {evidence} |".format(
                    case=case["case_id"],
                    sample=case.get("sample_id") or "unavailable",
                    risks=", ".join(case["risk_codes"]),
                    evidence=", ".join(evidence) or "none",
                )
            )
    if not report["deterministic"]:
        lines.extend(
            [
                "",
                "> This live-provider run is non-deterministic. Use fixture mode for reproducible acceptance evidence.",
            ]
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, *, session: Any = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Batch soil3 replay samples through Strategy, Validator, and Gate v2"
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--strategy-config", required=True, type=Path)
    parser.add_argument("--prompt", required=True, type=Path)
    parser.add_argument("--gate-policy", required=True, type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    parser.add_argument(
        "--mode", choices=("fixture", "live-provider"), default="fixture"
    )
    args = parser.parse_args(argv)

    manifest = load_batch_manifest(args.manifest, mode=args.mode)
    strategy_config = _load_object(args.strategy_config, name="strategy config")
    gate_policy = GatePolicy.from_dict(
        _load_object(args.gate_policy, name="Gate policy")
    )
    prompt = args.prompt.read_text(encoding="utf-8")
    report = run_batch(
        manifest,
        strategy_config=strategy_config,
        prompt=prompt,
        gate_policy=gate_policy,
        session=session,
    )
    _atomic_write_text(
        args.json_output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write_text(args.markdown_output, render_markdown(report))
    return report


if __name__ == "__main__":
    main()
