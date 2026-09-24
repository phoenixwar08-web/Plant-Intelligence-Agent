"""Controlled, auditable consumption of a verified Phase3 Bridge handoff.

This module never constructs Phase3, imports ActuatorLayer, or publishes MQTT.
The caller must explicitly supply the existing zero-argument
``DecisionBrain.run_cycle`` bound method after Owner approval.  An approval is
consumed before that callable is invoked, giving the adapter at-most-once
semantics even if the process is interrupted.
"""
from __future__ import annotations

import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.soil3.cloud_strategy.validator import parse_timestamp
from services.soil3.phase3_bridge.bridge_v1 import FORMAL_PHASE3_ENTRYPOINT
from services.soil3.telemetry.common import atomic_write_json, load_json


APPROVAL_SCHEMA = "controlled_scenario_approval.v1"
RECEIPT_SCHEMA = "controlled_phase3_receipt.v1"
APPROVAL_SCOPE = "phase3_run_cycle_once"
NO_EXECUTION = {
    "mode": "verification_only",
    "phase3_called": False,
    "physical_actions_performed": False,
}
APPROVAL_FIELDS = {
    "schema_version",
    "approval_id",
    "scenario_id",
    "device_code",
    "approved_by",
    "approved_at",
    "expires_at",
    "scope",
    "max_runs",
    "bridge_request_id",
    "bridge_bindings",
    "trace_id",
    "episode_id",
}
BRIDGE_RESPONSE_FIELDS = {
    "schema_version",
    "accepted",
    "checked_at",
    "reason_codes",
    "handoff",
    "execution",
}
HANDOFF_FIELDS = {
    "schema_version",
    "request_id",
    "created_at",
    "device_code",
    "operation",
    "bindings",
    "phase3_interface",
    "execution",
}
BINDING_FIELDS = {"state_sha256", "strategy_id", "strategy_sha256", "gate_id"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TRACE_ID_PATTERN = re.compile(r"^tr-[0-9a-f]{24}$")
EPISODE_ID_PATTERN = re.compile(r"^ep-[0-9a-f]{24}$")
MAX_APPROVAL_WINDOW_SECONDS = 900


class ControlledExecutionError(RuntimeError):
    def __init__(self, code: str, reasons: list[str] | None = None):
        self.code = code
        self.reasons = list(reasons or [])
        detail = f": {', '.join(self.reasons)}" if self.reasons else ""
        super().__init__(f"{code}{detail}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ControlledExecutionError("clock_invalid")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return isinstance(value, str)
    except (TypeError, ValueError, AttributeError):
        return False


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _bridge_reasons(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["bridge_response_not_object"]
    reasons = []
    if set(value) != BRIDGE_RESPONSE_FIELDS:
        reasons.append("bridge_response_fields_invalid")
    if value.get("schema_version") != "phase3_bridge_response.v1":
        reasons.append("bridge_response_schema_invalid")
    if value.get("accepted") is not True:
        reasons.append("bridge_not_accepted")
    if value.get("reason_codes") != []:
        reasons.append("bridge_has_reasons")
    if value.get("execution") != NO_EXECUTION:
        reasons.append("bridge_execution_boundary_invalid")
    if parse_timestamp(value.get("checked_at")) is None:
        reasons.append("bridge_checked_at_invalid")
    handoff = value.get("handoff")
    if not isinstance(handoff, dict):
        reasons.append("bridge_handoff_missing")
        return reasons
    if set(handoff) != HANDOFF_FIELDS:
        reasons.append("bridge_handoff_fields_invalid")
    if handoff.get("schema_version") != "phase3_bridge_request.v1":
        reasons.append("bridge_handoff_schema_invalid")
    if parse_timestamp(handoff.get("created_at")) is None:
        reasons.append("bridge_created_at_invalid")
    if not _is_uuid(handoff.get("request_id")):
        reasons.append("bridge_request_id_invalid")
    if handoff.get("device_code") != "soil3":
        reasons.append("bridge_device_invalid")
    if handoff.get("operation") != "run_cycle":
        reasons.append("bridge_operation_invalid")
    if handoff.get("phase3_interface") != {
        "entrypoint": FORMAL_PHASE3_ENTRYPOINT,
        "arguments": [],
    }:
        reasons.append("bridge_phase3_interface_invalid")
    if handoff.get("execution") != NO_EXECUTION:
        reasons.append("bridge_handoff_execution_invalid")
    bindings = handoff.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != BINDING_FIELDS:
        reasons.append("bridge_bindings_invalid")
    else:
        if not isinstance(bindings.get("state_sha256"), str) or not SHA256_PATTERN.fullmatch(bindings["state_sha256"]):
            reasons.append("bridge_state_hash_invalid")
        if not isinstance(bindings.get("strategy_sha256"), str) or not SHA256_PATTERN.fullmatch(bindings["strategy_sha256"]):
            reasons.append("bridge_strategy_hash_invalid")
        if not _is_uuid(bindings.get("strategy_id")):
            reasons.append("bridge_strategy_id_invalid")
        if not _is_uuid(bindings.get("gate_id")):
            reasons.append("bridge_gate_id_invalid")
    return reasons


def _approval_reasons(
    value: Any,
    handoff: dict[str, Any],
    now: datetime,
) -> list[str]:
    if not isinstance(value, dict):
        return ["approval_not_object"]
    reasons = []
    if set(value) != APPROVAL_FIELDS:
        reasons.append("approval_fields_invalid")
    if value.get("schema_version") != APPROVAL_SCHEMA:
        reasons.append("approval_schema_invalid")
    if not _is_uuid(value.get("approval_id")):
        reasons.append("approval_id_invalid")
    for field in ("scenario_id", "approved_by"):
        if not _nonempty(value.get(field)):
            reasons.append(f"{field}_invalid")
    if value.get("device_code") != "soil3":
        reasons.append("approval_device_invalid")
    if value.get("scope") != APPROVAL_SCOPE or value.get("max_runs") != 1:
        reasons.append("approval_scope_invalid")
    if value.get("bridge_request_id") != handoff.get("request_id"):
        reasons.append("approval_bridge_request_mismatch")
    if value.get("bridge_bindings") != handoff.get("bindings"):
        reasons.append("approval_bridge_bindings_mismatch")
    if not isinstance(value.get("trace_id"), str) or not TRACE_ID_PATTERN.fullmatch(value["trace_id"]):
        reasons.append("approval_trace_id_invalid")
    if not isinstance(value.get("episode_id"), str) or not EPISODE_ID_PATTERN.fullmatch(value["episode_id"]):
        reasons.append("approval_episode_id_invalid")
    approved_at = parse_timestamp(value.get("approved_at"))
    expires_at = parse_timestamp(value.get("expires_at"))
    if approved_at is None:
        reasons.append("approval_time_invalid")
    elif approved_at > now:
        reasons.append("approval_not_yet_valid")
    if expires_at is None:
        reasons.append("approval_expiry_invalid")
    elif expires_at <= now:
        reasons.append("approval_expired")
    if approved_at is not None and expires_at is not None and expires_at <= approved_at:
        reasons.append("approval_window_invalid")
    elif (
        approved_at is not None
        and expires_at is not None
        and (expires_at - approved_at).total_seconds() > MAX_APPROVAL_WINDOW_SECONDS
    ):
        reasons.append("approval_window_too_long")
    return reasons


def _value_name(value: Any) -> str | None:
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(value) if value is not None else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _phase3_projection(result: Any) -> tuple[dict[str, Any], bool | None]:
    action_sec = _finite_number(getattr(result, "action_sec", None))
    chosen_plan = getattr(result, "chosen_plan", None)
    reading = getattr(result, "reading", None)
    projection = {
        "result_type": type(result).__name__,
        "zone": _value_name(getattr(result, "zone", None)),
        "action_sec": action_sec,
        "plan_label": (
            str(getattr(chosen_plan, "label"))
            if chosen_plan is not None and getattr(chosen_plan, "label", None) is not None
            else None
        ),
        "notes": (
            str(getattr(result, "notes"))[:1000]
            if getattr(result, "notes", None) is not None
            else None
        ),
        "reading": {
            "humidity": _finite_number(getattr(reading, "humidity", None)),
            "temperature": _finite_number(getattr(reading, "temperature", None)),
            "ec_raw": _finite_number(getattr(reading, "ec_raw", None)),
        },
    }
    physical = action_sec > 0 if action_sec is not None and action_sec >= 0 else None
    return projection, physical


def _claim_once(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise ControlledExecutionError("approval_already_consumed") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        if path.exists():
            path.unlink()
        raise


class ControlledPhase3Executor:
    """Consume one Owner approval and call only a supplied zero-argument cycle."""

    def __init__(self, receipt_dir: str | Path, *, clock: Callable[[], datetime] = utc_now):
        self.receipt_dir = Path(receipt_dir)
        self.clock = clock

    def receipt_path(self, approval_id: str) -> Path:
        if not _is_uuid(approval_id):
            raise ControlledExecutionError("approval_id_invalid")
        return self.receipt_dir / f"{approval_id}.json"

    def read(self, approval_id: str) -> dict[str, Any]:
        path = self.receipt_path(approval_id)
        if not path.exists():
            raise ControlledExecutionError("receipt_not_found")
        value = load_json(path)
        if not isinstance(value, dict) or value.get("schema_version") != RECEIPT_SCHEMA:
            raise ControlledExecutionError("receipt_invalid")
        return value

    def execute(
        self,
        bridge_response: dict[str, Any],
        approval: dict[str, Any],
        phase3_cycle: Callable[[], Any],
    ) -> dict[str, Any]:
        now = self.clock()
        if now.tzinfo is None:
            raise ControlledExecutionError("clock_invalid")
        reasons = _bridge_reasons(bridge_response)
        handoff = bridge_response.get("handoff") if isinstance(bridge_response, dict) else None
        if not reasons and isinstance(handoff, dict):
            reasons.extend(_approval_reasons(approval, handoff, now))
        if not callable(phase3_cycle):
            reasons.append("phase3_cycle_not_callable")
        if reasons:
            raise ControlledExecutionError("preflight_rejected", list(dict.fromkeys(reasons)))

        approval_id = approval["approval_id"]
        receipt_path = self.receipt_path(approval_id)
        started = {
            "schema_version": RECEIPT_SCHEMA,
            "approval_id": approval_id,
            "scenario_id": approval["scenario_id"],
            "approved_by": approval["approved_by"],
            "trace_id": approval["trace_id"],
            "episode_id": approval["episode_id"],
            "bridge_request_id": handoff["request_id"],
            "bindings": handoff["bindings"],
            "started_at": iso_utc(now),
            "finished_at": None,
            "status": "started",
            "phase3_called": False,
            "physical_actions_performed": None,
            "phase3_decision": None,
            "error_type": None,
            "feedback_entry": {
                "trace_id": approval["trace_id"],
                "episode_id": approval["episode_id"],
                "status": "pending",
            },
        }
        _claim_once(receipt_path, started)

        attempted = dict(started)
        attempted["phase3_called"] = True
        atomic_write_json(receipt_path, attempted)
        try:
            result = phase3_cycle()
        except Exception as error:
            attempted.update({
                "finished_at": iso_utc(self.clock()),
                "status": "phase3_error",
                "physical_actions_performed": None,
                "error_type": type(error).__name__,
            })
            atomic_write_json(receipt_path, attempted)
            raise ControlledExecutionError("phase3_cycle_failed", [type(error).__name__]) from error

        decision, physical = _phase3_projection(result)
        attempted.update({
            "finished_at": iso_utc(self.clock()),
            "status": "completed" if physical is not None else "phase3_result_invalid",
            "physical_actions_performed": physical,
            "phase3_decision": decision,
        })
        atomic_write_json(receipt_path, attempted)
        if physical is None:
            raise ControlledExecutionError("phase3_result_invalid")
        return attempted
