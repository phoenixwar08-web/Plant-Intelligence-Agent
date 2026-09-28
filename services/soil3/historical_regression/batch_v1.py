"""Batch historical regression over existing replay_sample.v1 artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.soil3.cloud_gate.gate_v1 import GatePolicy
from services.soil3.cloud_gate.gate_v2 import evaluate_gate_v2
from services.soil3.cloud_strategy.service import parse_model_json, run_chain


MANIFEST_FIELDS = {"schema_version", "cases"}
CASE_FIELDS = {"case_id", "replay_sample", "fixture_response", "labels"}
SUPPORTED_MODES = {"fixture", "live-provider"}
REPLAY_SAMPLE_FIELDS = {
    "schema_version",
    "sample_id",
    "device_code",
    "replay_at",
    "state",
    "source_summary",
    "fact_digest",
    "fact_window",
    "source_policy",
}


@dataclass(frozen=True)
class BatchCase:
    case_id: str
    replay_sample_path: Path
    fixture_response_path: Path | None
    labels: tuple[str, ...]


@dataclass(frozen=True)
class BatchManifest:
    schema_version: str
    path: Path
    sha256: str
    mode: str
    cases: tuple[BatchCase, ...]


@dataclass(frozen=True)
class LoadedCaseInput:
    case: BatchCase
    sample_id: str
    sample_sha256: str
    state: dict[str, Any]


def _object_from_bytes(payload: bytes, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{name} must contain a JSON object") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return value


def _resolve(base: Path, value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def load_batch_manifest(path: Path, *, mode: str = "fixture") -> BatchManifest:
    """Load a historical regression manifest."""
    if mode not in SUPPORTED_MODES:
        raise ValueError("mode must be fixture or live-provider")
    path = Path(path).resolve()
    payload = path.read_bytes()
    value = _object_from_bytes(payload, name="manifest")
    if set(value) != MANIFEST_FIELDS:
        raise ValueError("manifest fields are invalid")
    if value.get("schema_version") != "historical_regression_manifest.v1":
        raise ValueError("invalid historical regression manifest schema_version")
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("manifest cases must be a non-empty list")

    cases: list[BatchCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict) or not set(raw) <= CASE_FIELDS:
            raise ValueError("case fields are invalid")
        required = {"case_id", "replay_sample"}
        if mode == "fixture":
            required.add("fixture_response")
        if not required <= set(raw):
            missing = sorted(required - set(raw))
            raise ValueError("missing case field: " + ",".join(missing))

        case_id = raw.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        if case_id in seen:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen.add(case_id)

        labels = raw.get("labels", [])
        if (
            not isinstance(labels, list)
            or any(not isinstance(label, str) or not label.strip() for label in labels)
        ):
            raise ValueError("labels must be non-empty strings")
        fixture = raw.get("fixture_response")
        cases.append(
            BatchCase(
                case_id=case_id,
                replay_sample_path=_resolve(
                    path.parent, raw.get("replay_sample"), field="replay_sample"
                ),
                fixture_response_path=(
                    _resolve(path.parent, fixture, field="fixture_response")
                    if fixture is not None
                    else None
                ),
                labels=tuple(labels),
            )
        )
    return BatchManifest(
        value["schema_version"],
        path,
        hashlib.sha256(payload).hexdigest(),
        mode,
        tuple(cases),
    )


def load_case_input(case: BatchCase) -> LoadedCaseInput:
    """Load and validate one case so malformed samples remain case-local."""
    payload = case.replay_sample_path.read_bytes()
    sample = _object_from_bytes(payload, name="replay sample")
    _validate_replay_sample(sample)
    sample_id = sample.get("sample_id")
    state = sample.get("state")
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != "state.v1"
        or state.get("device_code") != "soil3"
    ):
        raise ValueError("case input must contain a soil3 state.v1")
    return LoadedCaseInput(
        case=case,
        sample_id=sample_id,
        sample_sha256=hashlib.sha256(payload).hexdigest(),
        state=state,
    )


def _valid_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_replay_sample(sample: dict[str, Any]) -> None:
    valid = set(sample) == REPLAY_SAMPLE_FIELDS
    valid = valid and sample.get("schema_version") == "replay_sample.v1"
    valid = valid and sample.get("device_code") == "soil3"
    valid = valid and isinstance(sample.get("sample_id"), str)
    valid = valid and re.fullmatch(
        r"replay-[0-9a-f]{24}", sample.get("sample_id", "")
    ) is not None
    valid = valid and _valid_datetime(sample.get("replay_at"))
    valid = valid and isinstance(sample.get("source_summary"), dict)
    valid = valid and isinstance(sample.get("fact_digest"), str)
    valid = valid and re.fullmatch(
        r"[0-9a-f]{64}", sample.get("fact_digest", "")
    ) is not None

    state = sample.get("state")
    valid = valid and isinstance(state, dict)
    valid = valid and {"schema_version", "device_code", "generated_at"} <= set(state or {})
    valid = valid and (state or {}).get("schema_version") == "state.v1"
    valid = valid and (state or {}).get("device_code") == "soil3"

    fact_window = sample.get("fact_window")
    valid = valid and isinstance(fact_window, dict)
    valid = valid and {
        "first_sensor_at",
        "last_sensor_at",
        "sensor_count",
        "watering_count",
    } <= set(fact_window or {})

    policy = sample.get("source_policy")
    valid = valid and isinstance(policy, dict)
    valid = valid and {
        "cutoff_inclusive",
        "future_data_excluded",
        "source_timezone",
    } <= set(policy or {})
    valid = valid and (policy or {}).get("cutoff_inclusive") is True
    valid = valid and (policy or {}).get("future_data_excluded") is True
    valid = valid and isinstance((policy or {}).get("source_timezone"), str)
    valid = valid and bool((policy or {}).get("source_timezone", "").strip())
    if not valid:
        raise ValueError("case input must satisfy replay_sample.v1")


def _case_error(
    case: BatchCase,
    code: str,
    *,
    loaded: LoadedCaseInput | None = None,
    strategy: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    fixture_response_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "labels": list(case.labels),
        "status": "system_error",
        "error_code": code,
        "sample": (
            {
                "sample_id": loaded.sample_id,
                "sha256": loaded.sample_sha256,
                "path": str(case.replay_sample_path),
            }
            if loaded is not None
            else None
        ),
        "strategy": strategy,
        "gate": gate,
        "fixture_response_sha256": fixture_response_sha256,
    }


def _strategy_summary(chain: dict[str, Any]) -> dict[str, Any]:
    validation = chain.get("validation")
    if not isinstance(validation, dict):
        validation = {}
    strategy = validation.get("strategy")
    actions = strategy.get("actions") if isinstance(strategy, dict) else None
    proposed_actions: list[Any] = []
    raw_response = chain.get("raw_model_response")
    if (
        isinstance(raw_response, str)
        and chain.get("raw_model_response_truncated") is False
    ):
        try:
            proposed = parse_model_json(raw_response)
        except (ValueError, json.JSONDecodeError):
            pass
        else:
            candidate = proposed.get("actions")
            if isinstance(candidate, list):
                proposed_actions = candidate
    failure = chain.get("failure")
    parse_error = chain.get("parse_error")
    return {
        "accepted": validation.get("accepted") is True,
        "reason_codes": list(validation.get("reason_codes") or []),
        "failure_code": (
            failure.get("code")
            if isinstance(failure, dict)
            else parse_error.get("code")
            if isinstance(parse_error, dict)
            else None
        ),
        "raw_response_sha256": chain.get("raw_model_response_sha256"),
        "actions": actions if isinstance(actions, list) else [],
        "proposed_actions": proposed_actions,
        "strategy": strategy if isinstance(strategy, dict) else None,
    }


def _gate_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record.get("schema_version"),
        "decision": record.get("decision"),
        "reason_codes": list(record.get("reason_codes") or []),
        "warning_codes": list(record.get("warning_codes") or []),
        "strategy_sha256": record.get("strategy_sha256"),
        "state_sha256": record.get("state_sha256"),
        "budget": record.get("budget"),
        "execution": record.get("execution"),
    }


def _counter(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _water_seconds(actions: list[Any]) -> float:
    total = 0.0
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "water":
            continue
        seconds = action.get("pump_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            continue
        if math.isfinite(float(seconds)):
            total += float(seconds)
    return total


def _has_water(actions: list[Any]) -> bool:
    return any(
        isinstance(action, dict) and action.get("type") == "water"
        for action in actions
    )


def _deny_risk_codes(reason_codes: list[str]) -> list[str]:
    codes: list[str] = []
    if any(
        reason.startswith(("safety_", "predictor_"))
        or reason in {"pump_active", "pump_state_unavailable"}
        for reason in reason_codes
    ):
        codes.append("gate_safety_denied")
    if any(
        reason.endswith("_unavailable")
        or reason in {"soil_humidity_unavailable", "safety_flags_unavailable"}
        for reason in reason_codes
    ):
        codes.append("gate_data_denied")
    if any(
        reason.startswith(("soil_data_age_", "phase3_state_age_"))
        and reason.endswith("_denied")
        for reason in reason_codes
    ):
        codes.append("gate_freshness_denied")
    if any(
        reason.startswith("strategy_invalid:")
        or reason in {"invalid_state_schema_version", "state_is_not_soil3", "invalid_state_observed_at"}
        for reason in reason_codes
    ):
        codes.append("gate_binding_denied")
    return codes


def _risk_entry(result: dict[str, Any]) -> dict[str, Any] | None:
    risk_codes: list[str] = []
    strategy = result.get("strategy")
    gate = result.get("gate")
    if result.get("status") == "system_error":
        risk_codes.append("system_error")
    elif isinstance(strategy, dict):
        if strategy.get("failure_code") is not None:
            risk_codes.append("model_error")
        proposed_actions = strategy.get("proposed_actions") or []
        actions = strategy.get("actions") or []
        if not strategy.get("accepted") and _has_water(proposed_actions):
            risk_codes.append("water_strategy_rejected")
        if isinstance(gate, dict):
            decision = gate.get("decision")
            if decision == "deny":
                risk_codes.extend(_deny_risk_codes(gate.get("reason_codes") or []))
                if _has_water(actions):
                    risk_codes.append("water_strategy_gate_denied")
            elif decision == "allow_with_warning" and _has_water(actions):
                risk_codes.append("water_strategy_gate_warning")
    if not risk_codes:
        return None
    sample = result.get("sample") if isinstance(result.get("sample"), dict) else {}
    strategy = strategy if isinstance(strategy, dict) else {}
    gate = gate if isinstance(gate, dict) else {}
    return {
        "case_id": result.get("case_id"),
        "sample_id": sample.get("sample_id"),
        "sample_sha256": sample.get("sha256"),
        "source": sample.get("path"),
        "risk_codes": sorted(set(risk_codes)),
        "validator_reason_codes": list(strategy.get("reason_codes") or []),
        "gate_reason_codes": list(gate.get("reason_codes") or []),
        "gate_warning_codes": list(gate.get("warning_codes") or []),
        "error_code": result.get("error_code"),
    }


def _statistics(results: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [result for result in results if result.get("status") == "completed"]
    errors = [result for result in results if result.get("status") == "system_error"]
    strategies = [result["strategy"] for result in results if isinstance(result.get("strategy"), dict)]
    accepted = [strategy for strategy in strategies if strategy.get("accepted") is True]
    gates = [result["gate"] for result in results if isinstance(result.get("gate"), dict)]
    actions = [action for strategy in accepted for action in strategy.get("actions", [])]
    gate_decisions = {"allow": 0, "allow_with_warning": 0, "deny": 0}
    gate_decisions.update(
        _counter(
            [
                gate.get("decision")
                for gate in gates
                if isinstance(gate.get("decision"), str)
            ]
        )
    )
    return {
        "cases": {
            "total": len(results),
            "completed": len(completed),
            "system_error": len(errors),
        },
        "strategy": {
            "accepted": len(accepted),
            "rejected": len(strategies) - len(accepted),
        },
        "validator_reason_codes": _counter(
            [code for strategy in strategies for code in strategy.get("reason_codes", [])]
        ),
        "actions": {
            "type_counts": _counter(
                [action.get("type") for action in actions if isinstance(action, dict) and isinstance(action.get("type"), str)]
            ),
            "count_distribution": _counter([str(len(strategy.get("actions", []))) for strategy in accepted]),
            "proposed_water_seconds": _water_seconds(actions),
        },
        "gate_decisions": gate_decisions,
        "gate_reason_codes": _counter(
            [code for gate in gates for code in gate.get("reason_codes", [])]
        ),
        "gate_warning_codes": _counter(
            [code for gate in gates for code in gate.get("warning_codes", [])]
        ),
        "model_failures": _counter(
            [strategy["failure_code"] for strategy in strategies if strategy.get("failure_code")]
        ),
        "system_errors": _counter(
            [result["error_code"] for result in errors if result.get("error_code")]
        ),
    }


def _stable_projection(
    mode: str,
    input_digests: dict[str, str],
    results: list[dict[str, Any]],
    statistics: dict[str, Any],
    risky_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    stable_cases = []
    for result in results:
        sample = result.get("sample") if isinstance(result.get("sample"), dict) else {}
        strategy = result.get("strategy") if isinstance(result.get("strategy"), dict) else {}
        gate = result.get("gate") if isinstance(result.get("gate"), dict) else {}
        stable_cases.append(
            {
                "case_id": result.get("case_id"),
                "status": result.get("status"),
                "error_code": result.get("error_code"),
                "sample_id": sample.get("sample_id"),
                "sample_sha256": sample.get("sha256"),
                "fixture_response_sha256": result.get("fixture_response_sha256"),
                "strategy_accepted": strategy.get("accepted"),
                "validator_reason_codes": strategy.get("reason_codes", []),
                "failure_code": strategy.get("failure_code"),
                "raw_response_sha256": strategy.get("raw_response_sha256"),
                "actions": strategy.get("actions", []),
                "gate_decision": gate.get("decision"),
                "gate_reason_codes": gate.get("reason_codes", []),
                "gate_warning_codes": gate.get("warning_codes", []),
            }
        )
    stable_risks = [{key: value for key, value in risk.items() if key != "source"} for risk in risky_cases]
    return {
        "mode": mode,
        "input_digests": input_digests,
        "cases": stable_cases,
        "statistics": statistics,
        "risky_cases": stable_risks,
    }


def run_batch(
    manifest: BatchManifest,
    *,
    strategy_config: dict[str, Any],
    prompt: str,
    gate_policy: GatePolicy,
    session: Any = None,
) -> dict[str, Any]:
    """Run ordered cases through public Strategy and Gate v2 boundaries."""
    canonical_config = json.dumps(
        strategy_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    canonical_policy = json.dumps(
        {
            "warning_age_seconds": gate_policy.warning_age_seconds,
            "deny_age_seconds": gate_policy.deny_age_seconds,
            "window_seconds": gate_policy.window_seconds,
            "max_exploration_water_seconds": gate_policy.max_exploration_water_seconds,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    input_digests = {
        "manifest": manifest.sha256,
        "strategy_config": hashlib.sha256(canonical_config).hexdigest(),
        "prompt": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "gate_policy": hashlib.sha256(canonical_policy).hexdigest(),
    }
    results: list[dict[str, Any]] = []
    for case in manifest.cases:
        try:
            loaded = load_case_input(case)
        except (OSError, ValueError):
            results.append(_case_error(case, "case_input_unavailable"))
            continue

        fixture_content = None
        fixture_response_sha256 = None
        if manifest.mode == "fixture":
            try:
                if case.fixture_response_path is None:
                    raise ValueError("fixture response is required")
                fixture_payload = case.fixture_response_path.read_bytes()
                fixture_response_sha256 = hashlib.sha256(fixture_payload).hexdigest()
                fixture_content = fixture_payload.decode("utf-8")
            except (OSError, UnicodeError, ValueError):
                results.append(
                    _case_error(case, "fixture_response_unavailable", loaded=loaded)
                )
                continue

        try:
            chain = run_chain(
                state=loaded.state,
                config=strategy_config,
                prompt=prompt,
                fixture_content=fixture_content,
                session=session,
            )
            strategy_summary = _strategy_summary(chain)
        except Exception:
            results.append(
                _case_error(
                    case,
                    "strategy_chain_error",
                    loaded=loaded,
                    fixture_response_sha256=fixture_response_sha256,
                )
            )
            continue

        validated_strategy = strategy_summary.pop("strategy")
        gate_summary = None
        if strategy_summary["accepted"]:
            try:
                gate = evaluate_gate_v2(
                    loaded.state,
                    validated_strategy,
                    gate_policy,
                    False,
                    None,
                )
                gate_summary = _gate_summary(gate)
            except Exception:
                results.append(
                    _case_error(
                        case,
                        "gate_evaluation_error",
                        loaded=loaded,
                        strategy=strategy_summary,
                        fixture_response_sha256=fixture_response_sha256,
                    )
                )
                continue
        results.append(
            {
                "case_id": case.case_id,
                "labels": list(case.labels),
                "status": "completed",
                "error_code": None,
                "sample": {
                    "sample_id": loaded.sample_id,
                    "sha256": loaded.sample_sha256,
                    "path": str(case.replay_sample_path),
                },
                "strategy": strategy_summary,
                "gate": gate_summary,
                "fixture_response_sha256": fixture_response_sha256,
            }
        )
    statistics = _statistics(results)
    risky_cases = [risk for result in results if (risk := _risk_entry(result)) is not None]
    stable = _stable_projection(
        manifest.mode, input_digests, results, statistics, risky_cases
    )
    summary_sha256 = hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "report_version": "historical_regression_report.v1",
        "mode": manifest.mode,
        "deterministic": manifest.mode == "fixture",
        "input_digests": input_digests,
        "cases": results,
        "statistics": statistics,
        "risky_cases": risky_cases,
        "summary_sha256": summary_sha256,
    }
