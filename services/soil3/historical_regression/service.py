"""CLI for read-only soil3 historical Strategy and Gate regression."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_output_paths(
    *,
    json_output: Path,
    markdown_output: Path,
    input_paths: list[Path],
) -> None:
    resolved_json = json_output.resolve()
    resolved_markdown = markdown_output.resolve()
    if resolved_json == resolved_markdown:
        raise ValueError("output paths must be distinct")
    resolved_inputs = {path.resolve() for path in input_paths}
    if resolved_json in resolved_inputs or resolved_markdown in resolved_inputs:
        raise ValueError("output path collides with input")


def _append_counter(
    lines: list[str], values: dict[str, int], *, indent: str = ""
) -> None:
    if not values:
        lines.append(indent + "- None.")
        return
    for key, count in sorted(values.items()):
        lines.append(f"{indent}- `{key}`: {count}")


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
        "## Input digests",
        "",
    ]
    for name, digest in sorted(report["input_digests"].items()):
        lines.append(f"- `{name}`: `{digest}`")

    lines.extend(["", "## Validator reason codes", ""])
    _append_counter(lines, statistics["validator_reason_codes"])
    action_stats = statistics["actions"]
    lines.extend(
        [
            "",
            "## Action distribution",
            "",
            f"- Proposed water seconds from accepted Strategies: {action_stats['proposed_water_seconds']}",
            "- Action types:",
        ]
    )
    _append_counter(lines, action_stats["type_counts"], indent="  ")
    lines.append("- Action counts per accepted Strategy:")
    _append_counter(lines, action_stats["count_distribution"], indent="  ")

    lines.extend(["", "## Gate decisions", ""])
    decisions = statistics["gate_decisions"]
    for decision in ("allow", "allow_with_warning", "deny"):
        lines.append(f"- `{decision}`: {decisions.get(decision, 0)}")
    for title, key in (
        ("Gate reason codes", "gate_reason_codes"),
        ("Gate warning codes", "gate_warning_codes"),
        ("Model failures", "model_failures"),
        ("System errors", "system_errors"),
    ):
        lines.extend(["", f"## {title}", ""])
        _append_counter(lines, statistics[key])

    lines.extend(
        [
            "",
            "## Case results",
            "",
            "| Case | Status | Sample evidence | Fixture/response digest | Strategy | Gate | Evidence |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for case in report["cases"]:
        sample = case.get("sample") if isinstance(case.get("sample"), dict) else {}
        strategy_result = (
            case.get("strategy") if isinstance(case.get("strategy"), dict) else {}
        )
        gate = case.get("gate") if isinstance(case.get("gate"), dict) else {}
        strategy_label = (
            "accepted"
            if strategy_result.get("accepted") is True
            else "rejected"
            if strategy_result
            else "unavailable"
        )
        evidence = [
            *strategy_result.get("reason_codes", []),
            *gate.get("reason_codes", []),
            *gate.get("warning_codes", []),
        ]
        if case.get("error_code"):
            evidence.append(case["error_code"])
        sample_evidence = "unavailable"
        if sample:
            sample_evidence = "{sample_id}; `{sha}`; `{path}`".format(
                sample_id=sample.get("sample_id"),
                sha=sample.get("sha256"),
                path=sample.get("path"),
            )
        lines.append(
            "| {case_id} | {status} | {sample} | `{fixture}` | {strategy} | {gate} | {evidence} |".format(
                case_id=case["case_id"],
                status=case["status"],
                sample=sample_evidence,
                fixture=case.get("fixture_response_sha256") or "unavailable",
                strategy=strategy_label,
                gate=gate.get("decision") or "not_run",
                evidence=", ".join(evidence) or "none",
            )
        )

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
    source_paths = [
        args.manifest,
        args.strategy_config,
        args.prompt,
        args.gate_policy,
        *(case.replay_sample_path for case in manifest.cases),
        *(
            case.fixture_response_path
            for case in manifest.cases
            if case.fixture_response_path is not None
        ),
    ]
    _validate_output_paths(
        json_output=args.json_output,
        markdown_output=args.markdown_output,
        input_paths=source_paths,
    )
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
