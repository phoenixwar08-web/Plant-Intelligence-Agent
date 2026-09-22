"""Phase3 Bridge V1: validate the complete upstream chain before handoff.

V1 is deliberately verification-only.  It creates a zero-argument handoff to
Phase3's existing ``DecisionBrain.run_cycle`` entrypoint after independently
checking Strategy, Gate and Runner bindings.  It never imports or invokes
Phase3 and accepts no actuator command or requested duration.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from services.soil3.cloud_strategy.validator import (
    StrategyValidator,
    fingerprint,
    normalize_timestamp,
)


FORMAL_PHASE3_ENTRYPOINT = "services.soil3.phase3.decision_brain.DecisionBrain.run_cycle"
GATE_KEYS = {
    "schema_version", "gate_id", "decided_at", "device_code", "strategy_id",
    "state_observed_at", "state_sha256", "strategy_sha256", "decision", "reason_codes",
    "warning_codes", "budget", "execution",
}
RUNNER_KEYS = {
    "schema_version", "strategy_id", "strategy_sha256", "mode", "status",
    "current_step_index", "created_at", "updated_at", "steps", "execution",
}
STEP_KEYS = {
    "index", "action", "status", "started_at", "completed_at", "wait_until", "result",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return isinstance(value, str)
    except (ValueError, TypeError, AttributeError):
        return False


def _finite_nonnegative(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and float(value) >= 0.0
        and float(value) < float("inf")
    )


def _validate_gate(
    gate: Any,
    state: Dict[str, Any],
    strategy: Dict[str, Any],
) -> List[str]:
    reasons: List[str] = []
    if not isinstance(gate, dict):
        return ["gate_not_object"]
    if set(gate) != GATE_KEYS:
        reasons.append("gate_shape_invalid")
    if gate.get("schema_version") != "gate.v2":
        reasons.append("gate_schema_invalid")
    if not _is_uuid(gate.get("gate_id")):
        reasons.append("gate_id_invalid")
    if normalize_timestamp(gate.get("decided_at")) is None:
        reasons.append("gate_decided_at_invalid")
    if gate.get("device_code") != "soil3":
        reasons.append("gate_device_mismatch")
    if gate.get("strategy_id") != strategy.get("strategy_id"):
        reasons.append("gate_strategy_mismatch")
    if gate.get("strategy_sha256") != fingerprint(strategy):
        reasons.append("gate_strategy_hash_mismatch")
    if gate.get("state_sha256") != fingerprint(state):
        reasons.append("gate_state_hash_mismatch")
    if normalize_timestamp(gate.get("state_observed_at")) != normalize_timestamp(state.get("observed_at")):
        reasons.append("gate_state_time_mismatch")
    if gate.get("decision") not in {"allow", "allow_with_warning"}:
        reasons.append("gate_not_admitted")
    reason_codes = gate.get("reason_codes")
    if not isinstance(reason_codes, list) or reason_codes:
        reasons.append("gate_has_denial_reasons")
    warning_codes = gate.get("warning_codes")
    if not isinstance(warning_codes, list) or any(not isinstance(item, str) for item in warning_codes):
        reasons.append("gate_warnings_invalid")
    if gate.get("decision") == "allow" and warning_codes:
        reasons.append("gate_allow_has_warnings")
    if gate.get("decision") == "allow_with_warning" and not warning_codes:
        reasons.append("gate_warning_decision_without_warning")
    if gate.get("execution") != {"mode": "admission_only", "actuator_commands_allowed": False}:
        reasons.append("gate_execution_boundary_invalid")

    budget = gate.get("budget")
    budget_keys = {
        "exploration_requested", "requested_water_seconds", "reserved_water_seconds",
        "remaining_water_seconds", "reservation_id",
    }
    if not isinstance(budget, dict) or set(budget) != budget_keys:
        reasons.append("gate_budget_invalid")
    else:
        exploration = budget.get("exploration_requested")
        requested = budget.get("requested_water_seconds")
        reserved = budget.get("reserved_water_seconds")
        remaining = budget.get("remaining_water_seconds")
        if not isinstance(exploration, bool):
            reasons.append("gate_budget_invalid")
        if not _finite_nonnegative(requested) or not _finite_nonnegative(reserved):
            reasons.append("gate_budget_invalid")
        if remaining is not None and not _finite_nonnegative(remaining):
            reasons.append("gate_budget_invalid")
        if exploration:
            if not isinstance(budget.get("reservation_id"), str) or not budget["reservation_id"]:
                reasons.append("gate_exploration_not_reserved")
            if _finite_nonnegative(requested) and _finite_nonnegative(reserved) and float(reserved) < float(requested):
                reasons.append("gate_exploration_not_reserved")
        elif (
            budget.get("reservation_id") is not None
            or not _finite_nonnegative(reserved)
            or float(reserved) != 0.0
        ):
            reasons.append("gate_unrequested_reservation")
    return reasons


def _expected_result(action: Dict[str, Any], result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    action_type = action.get("type")
    if action_type == "water":
        return (
            result.get("kind") == "dry_run_water"
            and result.get("physical_action_performed") is False
            and _finite_nonnegative(result.get("pump_seconds"))
            and _finite_nonnegative(action.get("pump_seconds"))
            and float(result["pump_seconds"]) == float(action["pump_seconds"])
        )
    if action_type == "wait":
        return (
            result.get("kind") == "dry_run_wait"
            and _finite_nonnegative(result.get("requested_seconds"))
            and _finite_nonnegative(action.get("seconds"))
            and float(result["requested_seconds"]) == float(action["seconds"])
        )
    if action_type == "observe":
        return result == {"kind": "dry_run_observe", "observation_collected": False}
    if action_type == "stop":
        return result == {"kind": "dry_run_stop"}
    return False


def _validate_runner(runner: Any, strategy: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if not isinstance(runner, dict):
        return ["runner_not_object"]
    if set(runner) != RUNNER_KEYS:
        reasons.append("runner_shape_invalid")
    if runner.get("schema_version") != "runner_state.v1" or runner.get("mode") != "dry_run":
        reasons.append("runner_mode_invalid")
    if runner.get("strategy_id") != strategy.get("strategy_id"):
        reasons.append("runner_strategy_mismatch")
    if runner.get("strategy_sha256") != fingerprint(strategy):
        reasons.append("runner_strategy_hash_mismatch")
    if runner.get("execution") != {"physical_actions_performed": False, "phase3_called": False}:
        reasons.append("runner_execution_boundary_invalid")
    if runner.get("status") not in {"completed", "stopped"}:
        reasons.append("runner_not_terminal")

    actions = strategy.get("actions") if isinstance(strategy.get("actions"), list) else []
    steps = runner.get("steps")
    if not isinstance(steps, list) or len(steps) != len(actions):
        reasons.append("runner_steps_mismatch")
        return reasons
    terminal_index = runner.get("current_step_index")
    if terminal_index != len(steps):
        reasons.append("runner_progress_incomplete")
    stop_seen = False
    for index, (step, action) in enumerate(zip(steps, actions)):
        if not isinstance(step, dict) or set(step) != STEP_KEYS:
            reasons.append(f"runner_step_invalid:{index}")
            continue
        if step.get("index") != index or step.get("action") != action:
            reasons.append(f"runner_step_binding_mismatch:{index}")
        if step.get("status") != "completed" or not step.get("started_at") or not step.get("completed_at"):
            reasons.append(f"runner_step_not_completed:{index}")
        if not _expected_result(action, step.get("result")):
            reasons.append(f"runner_step_result_invalid:{index}")
        if action.get("type") == "stop":
            stop_seen = True
            if index != len(actions) - 1 or runner.get("status") != "stopped":
                reasons.append("runner_stop_semantics_invalid")
    if not stop_seen and runner.get("status") != "completed":
        reasons.append("runner_completion_semantics_invalid")
    return reasons


class Phase3Bridge:
    """Build a non-executing handoff only after the full chain validates."""

    def verify(
        self,
        state: Dict[str, Any],
        strategy: Dict[str, Any],
        gate: Dict[str, Any],
        runner: Dict[str, Any],
    ) -> Dict[str, Any]:
        reasons: List[str] = []
        if not isinstance(state, dict) or state.get("schema_version") != "state.v1":
            reasons.append("state_schema_invalid")
        if not isinstance(state, dict) or state.get("device_code") != "soil3":
            reasons.append("state_device_invalid")

        if not isinstance(strategy, dict):
            reasons.append("strategy_not_object")
        else:
            validation = StrategyValidator().validate(strategy, state if isinstance(state, dict) else {})
            reasons.extend(f"strategy_invalid:{code}" for code in validation.reason_codes)

        if isinstance(state, dict) and isinstance(strategy, dict):
            reasons.extend(_validate_gate(gate, state, strategy))
            reasons.extend(_validate_runner(runner, strategy))
        else:
            reasons.extend(["gate_not_verifiable", "runner_not_verifiable"])

        reasons = list(dict.fromkeys(reasons))
        checked_at = _utc_now()
        if reasons:
            return {
                "schema_version": "phase3_bridge_response.v1",
                "accepted": False,
                "checked_at": checked_at,
                "reason_codes": reasons,
                "handoff": None,
                "execution": {"mode": "verification_only", "phase3_called": False, "physical_actions_performed": False},
            }

        handoff = {
            "schema_version": "phase3_bridge_request.v1",
            "request_id": str(uuid.uuid4()),
            "created_at": checked_at,
            "device_code": "soil3",
            "operation": "run_cycle",
            "bindings": {
                "state_sha256": fingerprint(state),
                "strategy_id": strategy["strategy_id"],
                "strategy_sha256": fingerprint(strategy),
                "gate_id": gate["gate_id"],
            },
            "phase3_interface": {"entrypoint": FORMAL_PHASE3_ENTRYPOINT, "arguments": []},
            "execution": {"mode": "verification_only", "phase3_called": False, "physical_actions_performed": False},
        }
        return {
            "schema_version": "phase3_bridge_response.v1",
            "accepted": True,
            "checked_at": checked_at,
            "reason_codes": [],
            "handoff": handoff,
            "execution": {"mode": "verification_only", "phase3_called": False, "physical_actions_performed": False},
        }
