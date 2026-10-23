"""Durable observation records for counterfactual Trigger Shadow decisions."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from services.soil3.cloud_strategy.validator import normalize_timestamp, parse_timestamp
from services.soil3.telemetry.common import atomic_write_json, load_json
from services.soil3.trace.trace_v1 import utc_now

from .context_v1 import semantic_projection
from .evaluator_v1 import CALL_QWEN, LOCAL_SAFETY_ONLY, SKIP_CLOUD, TriggerDecision


SCHEMA_VERSION = "trigger_shadow.v1"
TRACE_ID_PATTERN = re.compile(r"^tr-[0-9a-f]{24}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DECISIONS = frozenset({CALL_QWEN, SKIP_CLOUD, LOCAL_SAFETY_ONLY})
RECORD_FIELDS = frozenset({
    "schema_version",
    "trace_id",
    "created_at",
    "updated_at",
    "state_ref",
    "trigger_decision",
    "trigger_reason_codes",
    "decision_context_snapshot",
    "semantic_projection",
    "baseline_ref",
    "actual_qwen_result",
    "execution",
})
ACTUAL_FIELDS = frozenset({
    "provider",
    "model",
    "validator_accepted",
    "validator_reason_codes",
    "strategy_ref",
    "water_proposed",
    "proposed_water_seconds",
    "gate_ref",
    "gate_decision",
    "gate_reason_codes",
    "token_usage",
    "cost",
    "latency",
})


class TriggerShadowError(ValueError):
    def __init__(self, code: str, reasons: list[str] | None = None):
        self.code = code
        self.reasons = list(reasons or [])
        detail = f": {', '.join(self.reasons)}" if self.reasons else ""
        super().__init__(f"{code}{detail}")


def _reference_valid(value: Any, *, schema_version: str | None = None) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"schema_version", "path", "sha256", "record_id"}
        and isinstance(value.get("schema_version"), str)
        and (schema_version is None or value["schema_version"] == schema_version)
        and isinstance(value.get("path"), str)
        and bool(value["path"])
        and isinstance(value.get("sha256"), str)
        and SHA256_PATTERN.fullmatch(value["sha256"]) is not None
        and (
            value.get("record_id") is None
            or (isinstance(value["record_id"], str) and bool(value["record_id"]))
        )
    )


def _metric_valid(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"source", "value", "unit"}:
        return False
    number = value.get("value")
    return (
        isinstance(value.get("source"), str)
        and bool(value["source"])
        and not isinstance(number, bool)
        and isinstance(number, (int, float))
        and math.isfinite(float(number))
        and float(number) >= 0
        and isinstance(value.get("unit"), str)
        and bool(value["unit"])
    )


def _actual_valid(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != ACTUAL_FIELDS:
        return False
    if value["provider"] is not None and not isinstance(value["provider"], str):
        return False
    if value["model"] is not None and not isinstance(value["model"], str):
        return False
    accepted = value["validator_accepted"]
    if not isinstance(accepted, bool):
        return False
    reasons = value["validator_reason_codes"]
    if not isinstance(reasons, list) or not all(isinstance(item, str) and item for item in reasons):
        return False
    strategy_ref = value["strategy_ref"]
    if accepted != (strategy_ref is not None) or (
        strategy_ref is not None and not _reference_valid(strategy_ref, schema_version="strategy.v1")
    ):
        return False
    if not isinstance(value["water_proposed"], bool):
        return False
    seconds = value["proposed_water_seconds"]
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(float(seconds))
        or float(seconds) < 0
    ):
        return False
    gate_ref = value["gate_ref"]
    gate_decision = value["gate_decision"]
    gate_reasons = value["gate_reason_codes"]
    if gate_ref is None:
        if gate_decision is not None or gate_reasons is not None:
            return False
    elif (
        not _reference_valid(gate_ref, schema_version="gate.v2")
        or gate_decision not in {"allow", "allow_with_warning", "deny"}
        or not isinstance(gate_reasons, list)
        or not all(isinstance(item, str) and item for item in gate_reasons)
    ):
        return False
    return all(_metric_valid(value[name]) for name in ("token_usage", "cost", "latency"))


def _pre_deadband_semantic_projection(context: dict[str, Any]) -> dict[str, Any]:
    """Reproduce the former zero-deadband projection for read compatibility."""

    projection = semantic_projection(context)
    trends = context.get("trends") if isinstance(context.get("trends"), dict) else {}
    directions = projection["trend_directions"]
    for key, direction in directions.items():
        if direction != "stable":
            continue
        value = trends.get(key)
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(number):
            continue
        if number < 0:
            directions[key] = "falling"
        elif number > 0:
            directions[key] = "rising"
    return projection


def _json_values_equal(left: Any, right: Any) -> bool:
    """Compare JSON values recursively without Python bool/number coercion."""

    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and (not isinstance(left, float) or math.isfinite(left))
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
            and (not isinstance(right, float) or math.isfinite(right))
            and left == right
        )
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_json_values_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and set(left) == set(right)
            and all(_json_values_equal(left[key], right[key]) for key in left)
        )
    return False


class TriggerShadowStore:
    """Persist one set-once Trigger observation beside each runtime Trace."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path_for(self, trace_id: str) -> Path:
        if not isinstance(trace_id, str) or TRACE_ID_PATTERN.fullmatch(trace_id) is None:
            raise TriggerShadowError("invalid_trace_id")
        return self.root / f"{trace_id}.json"

    def create(
        self,
        trace_id: str,
        state_ref: dict[str, Any],
        decision: TriggerDecision,
        context: dict[str, Any],
        baseline_ref: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self.path_for(trace_id)
        if not _reference_valid(state_ref, schema_version="state.v1"):
            raise TriggerShadowError("validation_failed", ["state_ref_invalid"])
        if not isinstance(decision, TriggerDecision) or decision.decision not in DECISIONS:
            raise TriggerShadowError("validation_failed", ["trigger_decision_invalid"])
        if (
            not isinstance(decision.reason_codes, tuple)
            or not decision.reason_codes
            or not all(isinstance(item, str) and item for item in decision.reason_codes)
        ):
            raise TriggerShadowError("validation_failed", ["trigger_reason_codes_invalid"])
        if not isinstance(context, dict) or context.get("schema_version") != "decision_context.v1":
            raise TriggerShadowError("validation_failed", ["decision_context_invalid"])
        if baseline_ref is not None and not _reference_valid(baseline_ref, schema_version=SCHEMA_VERSION):
            raise TriggerShadowError("validation_failed", ["baseline_ref_invalid"])
        now = utc_now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": trace_id,
            "created_at": now,
            "updated_at": now,
            "state_ref": copy.deepcopy(state_ref),
            "trigger_decision": decision.decision,
            "trigger_reason_codes": list(decision.reason_codes),
            "decision_context_snapshot": copy.deepcopy(context),
            "semantic_projection": semantic_projection(context),
            "baseline_ref": copy.deepcopy(baseline_ref),
            "actual_qwen_result": None,
            "execution": {"phase3_called": False, "physical_actions_performed": False},
        }
        path = self.path_for(trace_id)
        if path.exists():
            existing = self.read(trace_id)
            comparable = dict(record)
            comparable["created_at"] = existing["created_at"]
            comparable["updated_at"] = existing["updated_at"]
            if existing != comparable:
                raise TriggerShadowError("trace_id_conflict")
            return existing
        atomic_write_json(path, record)
        return copy.deepcopy(record)

    def read(self, trace_id: str) -> dict[str, Any]:
        try:
            record = load_json(self.path_for(trace_id))
        except (OSError, ValueError, TypeError) as error:
            raise TriggerShadowError("record_invalid", [str(error)]) from error
        self._validate_stored(record)
        return copy.deepcopy(record)

    def finalize(self, trace_id: str, actual_qwen_result: dict[str, Any]) -> dict[str, Any]:
        if not _actual_valid(actual_qwen_result):
            raise TriggerShadowError("validation_failed", ["actual_qwen_result_invalid"])
        record = self.read(trace_id)
        if record["actual_qwen_result"] is not None:
            if record["actual_qwen_result"] != actual_qwen_result:
                raise TriggerShadowError("actual_qwen_result_already_set")
            return record
        record["actual_qwen_result"] = copy.deepcopy(actual_qwen_result)
        record["updated_at"] = utc_now()
        self._validate_stored(record)
        atomic_write_json(self.path_for(trace_id), record)
        return copy.deepcopy(record)

    def artifact_ref(self, trace_id: str) -> dict[str, Any]:
        path = self.path_for(trace_id)
        self.read(trace_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "record_id": trace_id,
        }

    def latest_valid_call_baseline(self) -> dict[str, Any] | None:
        if not self.root.is_dir():
            return None
        candidates = []
        for path in sorted(self.root.glob("tr-*.json")):
            record = self.read(path.stem)
            actual = record["actual_qwen_result"]
            if (
                record["trigger_decision"] == CALL_QWEN
                and isinstance(actual, dict)
                and actual.get("validator_accepted") is True
            ):
                candidates.append(record)
        if not candidates:
            return None
        latest = max(candidates, key=lambda record: (record["updated_at"], record["trace_id"]))
        return {
            "ref": self.artifact_ref(latest["trace_id"]),
            "decision_context_snapshot": copy.deepcopy(latest["decision_context_snapshot"]),
            "semantic_projection": copy.deepcopy(latest["semantic_projection"]),
            "actual_qwen_result": copy.deepcopy(latest["actual_qwen_result"]),
        }

    def latest_call_admission_at(self) -> str | None:
        """Return the newest durable local CALL admission, finalized or not."""

        if not self.root.is_dir():
            return None
        candidates: list[tuple[Any, str, str]] = []
        for path in sorted(self.root.glob("tr-*.json")):
            record = self.read(path.stem)
            if record["trigger_decision"] != CALL_QWEN:
                continue
            admitted_at = parse_timestamp(record["created_at"])
            normalized = normalize_timestamp(record["created_at"])
            if admitted_at is None or normalized is None:
                raise TriggerShadowError("record_invalid")
            candidates.append((admitted_at, record["trace_id"], normalized))
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    @staticmethod
    def _validate_stored(record: Any) -> None:
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            raise TriggerShadowError("record_invalid")
        if record.get("schema_version") != SCHEMA_VERSION:
            raise TriggerShadowError("record_invalid")
        trace_id = record.get("trace_id")
        if not isinstance(trace_id, str) or TRACE_ID_PATTERN.fullmatch(trace_id) is None:
            raise TriggerShadowError("record_invalid")
        if normalize_timestamp(record.get("created_at")) is None or normalize_timestamp(record.get("updated_at")) is None:
            raise TriggerShadowError("record_invalid")
        if not _reference_valid(record.get("state_ref"), schema_version="state.v1"):
            raise TriggerShadowError("record_invalid")
        if record.get("trigger_decision") not in DECISIONS:
            raise TriggerShadowError("record_invalid")
        reasons = record.get("trigger_reason_codes")
        if not isinstance(reasons, list) or not all(isinstance(item, str) and item for item in reasons):
            raise TriggerShadowError("record_invalid")
        context = record.get("decision_context_snapshot")
        if not isinstance(context, dict) or context.get("schema_version") != "decision_context.v1":
            raise TriggerShadowError("record_invalid")
        stored_projection = record.get("semantic_projection")
        if (
            not _json_values_equal(stored_projection, semantic_projection(context))
            and not _json_values_equal(
                stored_projection,
                _pre_deadband_semantic_projection(context),
            )
        ):
            raise TriggerShadowError("record_invalid")
        baseline = record.get("baseline_ref")
        if baseline is not None and not _reference_valid(baseline, schema_version=SCHEMA_VERSION):
            raise TriggerShadowError("record_invalid")
        actual = record.get("actual_qwen_result")
        if actual is not None and not _actual_valid(actual):
            raise TriggerShadowError("record_invalid")
        if record.get("execution") != {"phase3_called": False, "physical_actions_performed": False}:
            raise TriggerShadowError("record_invalid")
