"""Controlled, auditable consumption of a verified Phase3 Bridge handoff.

This module never constructs Phase3, imports ActuatorLayer, or publishes MQTT.
It reloads the persisted Trace-bound chain, reruns the public Phase3 Bridge
verification, and accepts only an already-created formal ``DecisionBrain``
instance after Owner approval. An approval is consumed before the class-owned
``run_cycle`` method is invoked, giving the adapter at-most-once semantics
even if the process is interrupted.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.soil3.cloud_strategy.validator import fingerprint, normalize_timestamp, parse_timestamp
from services.soil3.phase3_bridge import Phase3Bridge
from services.soil3.phase3_bridge.bridge_v1 import FORMAL_PHASE3_ENTRYPOINT
from services.soil3.telemetry.common import atomic_write_json, load_json
from services.soil3.trace import TraceStore


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
OWNER_APPROVER = "owner"
TRACE_FIELDS = {
    "schema_version", "trace_id", "created_at", "updated_at", "decision",
    "execution", "feedback_refs", "outcome_ref",
}
ARTIFACT_REF_FIELDS = {"schema_version", "path", "sha256", "record_id"}
TRACE_DECISION_FIELDS = {
    "state_ref", "vision", "experience", "model_metrics", "strategy_ref",
    "gate_ref", "gate_decision", "gate_reason_codes", "runner_ref",
    "bridge_ref", "episode_ref",
}
TRACE_METRICS = {
    "token_usage": ("provider_response.usage", "tokens"),
    "cost": ("provider_response.billing", "CNY"),
    "latency": ("runtime_monotonic_clock", "ms"),
}


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
    if not _nonempty(value.get("scenario_id")):
        reasons.append("scenario_id_invalid")
    if value.get("approved_by") != OWNER_APPROVER:
        reasons.append("approved_by_not_owner")
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


def _load_artifact(
    reference: Any,
    *,
    label: str,
    schema_version: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(reference, dict) or set(reference) != ARTIFACT_REF_FIELDS:
        return None, [f"{label}_ref_invalid"]
    if reference.get("schema_version") != schema_version:
        return None, [f"{label}_ref_schema_invalid"]
    path_value = reference.get("path")
    expected_hash = reference.get("sha256")
    if (
        not isinstance(path_value, str)
        or not path_value
        or not isinstance(expected_hash, str)
        or not SHA256_PATTERN.fullmatch(expected_hash)
    ):
        return None, [f"{label}_ref_invalid"]
    path = Path(path_value)
    try:
        payload = path.read_bytes()
    except OSError:
        return None, [f"{label}_artifact_unavailable"]
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        return None, [f"{label}_artifact_hash_mismatch"]
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, [f"{label}_artifact_invalid"]
    if not isinstance(value, dict) or value.get("schema_version") != schema_version:
        return None, [f"{label}_artifact_schema_invalid"]
    return value, []


def _code_objects_match(actual: Any, expected: Any) -> bool:
    """Compare code identity using the line table exposed by this Python."""

    fields = [
        "co_argcount", "co_posonlyargcount", "co_kwonlyargcount",
        "co_nlocals", "co_stacksize", "co_flags", "co_code", "co_consts",
        "co_names", "co_varnames", "co_freevars", "co_cellvars",
        "co_firstlineno",
    ]
    if hasattr(actual, "co_linetable") and hasattr(expected, "co_linetable"):
        fields.append("co_linetable")
    else:
        fields.append("co_lnotab")
    if hasattr(actual, "co_exceptiontable") and hasattr(expected, "co_exceptiontable"):
        fields.append("co_exceptiontable")
    try:
        return all(getattr(actual, field) == getattr(expected, field) for field in fields)
    except AttributeError:
        return False


def _formal_phase3_instance(value: Any) -> bool:
    """Recognize the already-created formal Phase3 facade without importing it.

    Phase3 uses deployment-local imports and cannot safely be imported by this
    adapter on every development platform. The adapter nevertheless accepts an
    instance only when its concrete class is the formal DecisionBrain facade;
    it invokes the class method directly, so instance-level callable
    substitution cannot redirect execution.
    """

    cls = type(value)
    module = sys.modules.get(cls.__module__)
    module_path = getattr(module, "__file__", None)
    expected_path = (
        Path(__file__).resolve().parents[1] / "phase3" / "decision_brain.py"
    ).resolve()
    try:
        resolved_module_path = Path(module_path).resolve()
    except (TypeError, OSError):
        return False
    method = cls.__dict__.get("run_cycle")
    if not isinstance(method, types.FunctionType):
        return False
    try:
        source_code = compile(
            expected_path.read_text(encoding="utf-8"),
            str(expected_path),
            "exec",
            dont_inherit=True,
        )
        class_code = next(
            item
            for item in source_code.co_consts
            if isinstance(item, types.CodeType) and item.co_name == "DecisionBrain"
        )
        expected_method_code = next(
            item
            for item in class_code.co_consts
            if isinstance(item, types.CodeType) and item.co_name == "run_cycle"
        )
    except (OSError, SyntaxError, StopIteration):
        return False
    actual_code = method.__code__
    code_matches = _code_objects_match(actual_code, expected_method_code)
    return (
        cls.__name__ == "DecisionBrain"
        and cls.__module__ in {
            "decision_brain",
            "services.soil3.phase3.decision_brain",
        }
        and module is not None
        and getattr(module, "DecisionBrain", None) is cls
        and resolved_module_path == expected_path
        and method.__globals__.get("DecisionBrain") is cls
        and Path(method.__globals__.get("__file__", "")).resolve()
        == expected_path
        and code_matches
    )


def _invoke_formal_phase3_cycle(decision_brain: Any) -> Any:
    """Invoke the class-owned method after identity and source verification."""

    return type(decision_brain).run_cycle(decision_brain)


def _trace_reference_shape_valid(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == ARTIFACT_REF_FIELDS
        and isinstance(value.get("schema_version"), str)
        and bool(value["schema_version"].strip())
        and isinstance(value.get("path"), str)
        and bool(value["path"].strip())
        and isinstance(value.get("sha256"), str)
        and SHA256_PATTERN.fullmatch(value["sha256"]) is not None
        and (
            value.get("record_id") is None
            or (
                isinstance(value["record_id"], str)
                and bool(value["record_id"].strip())
            )
        )
    )


def _trace_structure_reasons(trace: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    created_at = parse_timestamp(trace.get("created_at"))
    updated_at = parse_timestamp(trace.get("updated_at"))
    if created_at is None or updated_at is None:
        reasons.append("trace_timestamps_invalid")
    elif updated_at < created_at:
        reasons.append("trace_timestamps_out_of_order")
    decision = trace.get("decision")
    if not isinstance(decision, dict) or set(decision) != TRACE_DECISION_FIELDS:
        return reasons + ["trace_decision_fields_invalid"]
    for association_name in ("vision", "experience"):
        association = decision.get(association_name)
        if (
            not isinstance(association, dict)
            or set(association) != {"availability", "ref"}
            or association.get("availability")
            not in {"available", "unavailable", "not_requested"}
            or (
                association.get("availability") == "available"
                and not _trace_reference_shape_valid(association.get("ref"))
            )
            or (
                association.get("availability") != "available"
                and association.get("ref") is not None
            )
        ):
            reasons.append(f"trace_{association_name}_invalid")
    metrics = decision.get("model_metrics")
    if not isinstance(metrics, dict) or set(metrics) != {
        "provider", "model", "token_usage", "cost", "latency"
    }:
        reasons.append("trace_model_metrics_invalid")
    else:
        if any(
            metrics.get(field) is not None
            and (
                not isinstance(metrics[field], str)
                or not metrics[field].strip()
            )
            for field in ("provider", "model")
        ):
            reasons.append("trace_model_metrics_invalid")
        for name, (source, unit) in TRACE_METRICS.items():
            metric = metrics.get(name)
            if metric is None:
                continue
            if (
                not isinstance(metric, dict)
                or set(metric) != {"source", "value", "unit"}
                or metric.get("source") != source
                or metric.get("unit") != unit
                or _finite_number(metric.get("value")) is None
                or float(metric["value"]) < 0
            ):
                reasons.append("trace_model_metrics_invalid")
                break
    if decision.get("gate_decision") not in {
        "allow", "allow_with_warning", "deny"
    }:
        reasons.append("trace_gate_decision_invalid")
    gate_reasons = decision.get("gate_reason_codes")
    if not isinstance(gate_reasons, list) or any(
        not isinstance(item, str) or not item for item in gate_reasons
    ):
        reasons.append("trace_gate_reason_codes_invalid")
    feedback_refs = trace.get("feedback_refs")
    if not isinstance(feedback_refs, list) or any(
        not _trace_reference_shape_valid(item) for item in feedback_refs
    ):
        reasons.append("trace_feedback_refs_invalid")
    outcome_ref = trace.get("outcome_ref")
    if outcome_ref is not None and not _trace_reference_shape_valid(outcome_ref):
        reasons.append("trace_outcome_ref_invalid")
    return reasons


def _load_verified_chain(
    trace_dir: str | Path,
    approval: Any,
    now: datetime,
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    trace_id = approval.get("trace_id") if isinstance(approval, dict) else None
    if not isinstance(trace_id, str) or not TRACE_ID_PATTERN.fullmatch(trace_id):
        return None, ["approval_trace_id_invalid"]
    try:
        trace = TraceStore(trace_dir).read(trace_id)
    except Exception:
        return None, ["trace_unavailable"]
    if set(trace) != TRACE_FIELDS or trace.get("schema_version") != "trace.v1":
        return None, ["trace_invalid"]
    if trace.get("trace_id") != trace_id:
        reasons.append("trace_id_mismatch")
    reasons.extend(_trace_structure_reasons(trace))
    if trace.get("execution") != {
        "phase3_called": False,
        "physical_actions_performed": False,
    }:
        reasons.append("trace_execution_boundary_invalid")
    decision = trace.get("decision")
    if not isinstance(decision, dict):
        return None, reasons + ["trace_decision_invalid"]

    artifacts: dict[str, dict[str, Any]] = {}
    specs = (
        ("state", "state_ref", "state.v1"),
        ("strategy", "strategy_ref", "strategy.v1"),
        ("gate", "gate_ref", "gate.v2"),
        ("runner", "runner_ref", "runner_state.v1"),
        ("bridge", "bridge_ref", "phase3_bridge_response.v1"),
        ("episode", "episode_ref", "episode.v1"),
    )
    for label, field, schema in specs:
        artifact, artifact_reasons = _load_artifact(
            decision.get(field), label=label, schema_version=schema
        )
        reasons.extend(artifact_reasons)
        if artifact is not None:
            artifacts[label] = artifact
    if reasons:
        return None, list(dict.fromkeys(reasons))

    state = artifacts["state"]
    strategy = artifacts["strategy"]
    gate = artifacts["gate"]
    runner = artifacts["runner"]
    bridge = artifacts["bridge"]
    episode = artifacts["episode"]
    refs = {label: decision[field] for label, field, _ in specs}

    if refs["state"]["record_id"] is not None:
        reasons.append("state_ref_record_id_invalid")
    if refs["strategy"]["record_id"] != strategy.get("strategy_id"):
        reasons.append("strategy_ref_record_id_mismatch")
    if refs["gate"]["record_id"] != gate.get("gate_id"):
        reasons.append("gate_ref_record_id_mismatch")
    if refs["runner"]["record_id"] != strategy.get("strategy_id"):
        reasons.append("runner_ref_record_id_mismatch")
    if refs["episode"]["record_id"] != episode.get("episode_id"):
        reasons.append("episode_ref_record_id_mismatch")
    if decision.get("gate_decision") != gate.get("decision"):
        reasons.append("trace_gate_decision_mismatch")
    if decision.get("gate_reason_codes") != gate.get("reason_codes"):
        reasons.append("trace_gate_reasons_mismatch")

    reasons.extend(_bridge_reasons(bridge))
    handoff = bridge.get("handoff") if isinstance(bridge, dict) else None
    if isinstance(handoff, dict):
        if refs["bridge"]["record_id"] != handoff.get("request_id"):
            reasons.append("bridge_ref_record_id_mismatch")
        reasons.extend(_approval_reasons(approval, handoff, now))

    if episode.get("episode_id") != approval.get("episode_id"):
        reasons.append("approval_episode_mismatch")
    if episode.get("device_code") != "soil3" or episode.get("status") != "open":
        reasons.append("episode_not_open")
    if episode.get("initial_state") != state:
        reasons.append("episode_state_mismatch")
    if episode.get("strategy") != strategy:
        reasons.append("episode_strategy_mismatch")
    if episode.get("gate_result") != gate:
        reasons.append("episode_gate_mismatch")
    state_binding = episode.get("state_binding")
    if not isinstance(state_binding, dict):
        reasons.append("episode_state_binding_invalid")
    else:
        if state_binding.get("state_sha256") != fingerprint(state):
            reasons.append("episode_state_hash_mismatch")
        if state_binding.get("state_observed_at") != normalize_timestamp(state.get("observed_at")):
            reasons.append("episode_state_time_mismatch")

    rebuilt = Phase3Bridge().verify(state, strategy, gate, runner)
    if rebuilt.get("accepted") is not True:
        reasons.extend(
            f"bridge_reverification:{code}"
            for code in rebuilt.get("reason_codes", ["rejected"])
        )
    rebuilt_handoff = rebuilt.get("handoff")
    if isinstance(handoff, dict) and isinstance(rebuilt_handoff, dict):
        for field in (
            "device_code", "operation", "bindings", "phase3_interface", "execution"
        ):
            if handoff.get(field) != rebuilt_handoff.get(field):
                reasons.append(f"bridge_reverification_{field}_mismatch")
    elif isinstance(handoff, dict) != isinstance(rebuilt_handoff, dict):
        reasons.append("bridge_reverification_handoff_mismatch")

    if reasons:
        return None, list(dict.fromkeys(reasons))
    return {"trace": trace, "bridge": bridge, "handoff": handoff}, []


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
    """Reverify one persisted Shadow chain before one formal Phase3 cycle."""

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
        trace_dir: str | Path,
        approval: dict[str, Any],
        decision_brain: Any,
    ) -> dict[str, Any]:
        now = self.clock()
        if now.tzinfo is None:
            raise ControlledExecutionError("clock_invalid")
        if not _formal_phase3_instance(decision_brain):
            raise ControlledExecutionError("formal_phase3_instance_required")
        verified, reasons = _load_verified_chain(trace_dir, approval, now)
        if reasons:
            raise ControlledExecutionError("preflight_rejected", list(dict.fromkeys(reasons)))
        if verified is None:
            raise ControlledExecutionError("preflight_rejected", ["verified_chain_missing"])
        handoff = verified["handoff"]

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
            result = _invoke_formal_phase3_cycle(decision_brain)
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
