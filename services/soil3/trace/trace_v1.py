"""Trace V1: local analysis records with no decision or execution authority."""
from __future__ import annotations

import copy
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.soil3.telemetry.common import atomic_write_json, load_json


SCHEMA_VERSION = "trace.v1"
TRACE_ID_PATTERN = re.compile(r"^tr-[0-9a-f]{24}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DECISION_FIELDS = frozenset({
    "state_ref",
    "vision",
    "experience",
    "model_metrics",
    "strategy_ref",
    "gate_ref",
    "gate_decision",
    "gate_reason_codes",
    "runner_ref",
    "bridge_ref",
    "episode_ref",
})
REFERENCE_FIELDS = frozenset({
    "state_ref",
    "strategy_ref",
    "gate_ref",
    "runner_ref",
    "bridge_ref",
    "episode_ref",
})
AVAILABILITY_FIELDS = frozenset({"vision", "experience"})
AVAILABILITY_VALUES = frozenset({"available", "unavailable", "not_requested"})
GATE_DECISIONS = frozenset({"allow", "allow_with_warning", "deny"})
METRIC_UNITS = {"token_usage": "tokens", "cost": "CNY", "latency": "ms"}
METRIC_SOURCES = {
    "token_usage": frozenset({"provider_response.usage"}),
    "cost": frozenset({"provider_response.billing"}),
    "latency": frozenset({"runtime_monotonic_clock"}),
}


class TraceError(Exception):
    """A Trace operation was refused with a stable code and reason list."""

    def __init__(self, code: str, reasons: list[str] | None = None):
        self.code = code
        self.reasons = list(reasons or [])
        detail = f": {', '.join(self.reasons)}" if self.reasons else ""
        super().__init__(f"{code}{detail}")


def utc_now() -> str:
    """Return the project's canonical UTC timestamp representation."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_trace_id() -> str:
    return "tr-" + uuid.uuid4().hex[:24]


def _new_record() -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "trace_id": new_trace_id(),
        "created_at": now,
        "updated_at": now,
        "decision": {
            "state_ref": None,
            "vision": {"availability": "not_requested", "ref": None},
            "experience": {"availability": "not_requested", "ref": None},
            "model_metrics": {
                "provider": None,
                "model": None,
                "token_usage": None,
                "cost": None,
                "latency": None,
            },
            "strategy_ref": None,
            "gate_ref": None,
            "gate_decision": None,
            "gate_reason_codes": None,
            "runner_ref": None,
            "bridge_ref": None,
            "episode_ref": None,
        },
        "execution": {
            "phase3_called": False,
            "physical_actions_performed": False,
        },
        "feedback_refs": [],
        "outcome_ref": None,
    }


def _initial_decision() -> dict[str, Any]:
    return _new_record()["decision"]


def _reference_reasons(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label}_not_object"]
    if set(value) != {"schema_version", "path", "sha256", "record_id"}:
        return [f"{label}_fields_invalid"]
    reasons = []
    if not isinstance(value["schema_version"], str) or not value["schema_version"].strip():
        reasons.append(f"{label}_schema_version_invalid")
    if not isinstance(value["path"], str) or not value["path"].strip():
        reasons.append(f"{label}_path_invalid")
    if not isinstance(value["sha256"], str) or not SHA256_PATTERN.fullmatch(value["sha256"]):
        reasons.append(f"{label}_sha256_invalid")
    if value["record_id"] is not None and (
        not isinstance(value["record_id"], str) or not value["record_id"].strip()
    ):
        reasons.append(f"{label}_record_id_invalid")
    return reasons


def _association_reasons(field: str, value: Any) -> list[str]:
    if not isinstance(value, dict) or set(value) != {"availability", "ref"}:
        return [f"{field}_association_invalid"]
    availability = value["availability"]
    reference = value["ref"]
    if not isinstance(availability, str) or availability not in AVAILABILITY_VALUES:
        return [f"{field}_availability_invalid"]
    if availability == "available":
        if reference is None:
            return [f"{field}_available_requires_ref"]
        return _reference_reasons(reference, label=f"{field}_ref")
    if reference is not None:
        return [f"{field}_{availability}_requires_null_ref"]
    return []


def _metric_reasons(name: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict) or set(value) != {"source", "value", "unit"}:
        return [f"{name}_metric_invalid"]
    if (
        not isinstance(value["source"], str)
        or not value["source"].strip()
        or value["source"] not in METRIC_SOURCES[name]
        or isinstance(value["value"], bool)
        or not isinstance(value["value"], (int, float))
        or not math.isfinite(float(value["value"]))
        or float(value["value"]) < 0
        or value["unit"] != METRIC_UNITS[name]
    ):
        return [f"{name}_metric_invalid"]
    return []


def _model_metrics_reasons(value: Any) -> list[str]:
    if not isinstance(value, dict) or set(value) != {
        "provider", "model", "token_usage", "cost", "latency"
    }:
        return ["model_metrics_fields_invalid"]
    reasons = []
    for key in ("provider", "model"):
        current = value[key]
        if current is not None and (not isinstance(current, str) or not current.strip()):
            reasons.append(f"model_metrics_{key}_invalid")
    for name in METRIC_UNITS:
        reasons.extend(_metric_reasons(name, value[name]))
    return reasons


def _decision_field_reasons(field: str, value: Any) -> list[str]:
    if field in REFERENCE_FIELDS:
        if value is None:
            return [f"{field}_required"]
        return _reference_reasons(value, label=field)
    if field in AVAILABILITY_FIELDS:
        return _association_reasons(field, value)
    if field == "model_metrics":
        return _model_metrics_reasons(value)
    if field == "gate_decision":
        if not isinstance(value, str) or value not in GATE_DECISIONS:
            return ["invalid_gate_decision"]
        return []
    if field == "gate_reason_codes":
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            return ["gate_reason_codes_invalid"]
        return []
    return [f"unknown_decision_field:{field}"]


def _apply_set_once(container: dict[str, Any], field: str, value: Any) -> bool:
    current = container[field]
    initial = _initial_decision()[field]
    if current == value:
        return False
    if current != initial:
        raise TraceError("validation_failed", [f"{field}_already_set"])
    container[field] = copy.deepcopy(value)
    return True


def _validated_new_feedback_refs(value: Any, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise TraceError("validation_failed", ["feedback_refs_required"])
    reasons = []
    seen = list(existing)
    for index, reference in enumerate(value):
        reasons.extend(_reference_reasons(reference, label=f"feedback_ref[{index}]"))
        if reference in seen:
            reasons.append("feedback_ref_already_appended")
        else:
            seen.append(reference)
    if reasons:
        raise TraceError("validation_failed", reasons)
    return copy.deepcopy(value)


class TraceStore:
    """Persist one trace record per generated identifier under a caller-owned root."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def create(self) -> dict[str, Any]:
        record = _new_record()
        self._write(record)
        return copy.deepcopy(record)

    def read(self, trace_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._load(trace_id))

    def set_decision(self, trace_id: str, **updates: Any) -> dict[str, Any]:
        reasons = []
        if not updates:
            reasons.append("nothing_to_update")
        for field, value in updates.items():
            if field not in DECISION_FIELDS:
                reasons.append(f"unknown_decision_field:{field}")
            else:
                reasons.extend(_decision_field_reasons(field, value))
        if reasons:
            raise TraceError("validation_failed", reasons)

        record = self._load(trace_id)
        candidate = copy.deepcopy(record)
        changed = False
        for field, value in updates.items():
            changed = _apply_set_once(candidate["decision"], field, value) or changed
        if not changed:
            return copy.deepcopy(record)
        candidate["updated_at"] = utc_now()
        self._write(candidate)
        return copy.deepcopy(candidate)

    def append_feedback_refs(self, trace_id: str, refs: list[dict[str, Any]]) -> dict[str, Any]:
        record = self._load(trace_id)
        values = _validated_new_feedback_refs(refs, record["feedback_refs"])
        candidate = copy.deepcopy(record)
        candidate["feedback_refs"].extend(values)
        candidate["updated_at"] = utc_now()
        self._write(candidate)
        return copy.deepcopy(candidate)

    def set_outcome_ref(self, trace_id: str, ref: dict[str, Any]) -> dict[str, Any]:
        reasons = _reference_reasons(ref, label="outcome_ref")
        if reasons:
            raise TraceError("validation_failed", reasons)
        record = self._load(trace_id)
        current = record["outcome_ref"]
        if current == ref:
            return copy.deepcopy(record)
        if current is not None:
            raise TraceError("validation_failed", ["outcome_ref_already_set"])
        candidate = copy.deepcopy(record)
        candidate["outcome_ref"] = copy.deepcopy(ref)
        candidate["updated_at"] = utc_now()
        self._write(candidate)
        return copy.deepcopy(candidate)

    def trace_path(self, trace_id: str) -> Path:
        if not isinstance(trace_id, str) or not TRACE_ID_PATTERN.fullmatch(trace_id):
            raise TraceError("invalid_trace_id")
        return self.root / f"{trace_id}.json"

    def _write(self, record: dict[str, Any]) -> None:
        atomic_write_json(self.trace_path(record["trace_id"]), record)

    def _load(self, trace_id: str) -> dict[str, Any]:
        path = self.trace_path(trace_id)
        if not path.exists():
            raise TraceError("trace_not_found")
        value = load_json(path)
        if not isinstance(value, dict):
            raise TraceError("trace_invalid")
        return value
