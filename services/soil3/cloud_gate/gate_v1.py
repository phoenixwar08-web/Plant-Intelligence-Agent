from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from services.soil3.cloud_strategy.validator import (
    StrategyValidator,
    fingerprint,
    normalize_timestamp,
)

from .budget import BudgetLedgerError, BudgetReservationConflict


POLICY_FIELDS = {
    "warning_age_seconds",
    "deny_age_seconds",
    "window_seconds",
    "max_exploration_water_seconds",
}
ACTIVE_PROTECTION_FLAGS = {
    "pending_soak",
    "water_delivery_suspect",
    "reservoir_empty_suspect",
    "low_wet_recovery_suspect",
    "sensor_fault",
    "dynamic_cooldown",
    "watering_trigger_guard",
    "recent_response_guard",
    "hard_safety_low_guard",
    "cloud_protection",
}
PREDICTOR_CIRCUIT_STATES = {"CLOSED", "HALF_OPEN", "OPEN"}


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{field} must be a finite non-negative number") from error
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return number


@dataclass(frozen=True)
class GatePolicy:
    warning_age_seconds: float
    deny_age_seconds: float
    window_seconds: float
    max_exploration_water_seconds: float

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GatePolicy":
        if not isinstance(value, dict) or set(value) != POLICY_FIELDS:
            raise ValueError("gate policy fields are invalid")
        warning_age_seconds = _finite_nonnegative(
            value["warning_age_seconds"], "warning_age_seconds"
        )
        deny_age_seconds = _finite_nonnegative(value["deny_age_seconds"], "deny_age_seconds")
        if warning_age_seconds > deny_age_seconds:
            raise ValueError("warning_age_seconds must not exceed deny_age_seconds")
        window_seconds = _finite_nonnegative(value["window_seconds"], "window_seconds")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than zero")
        max_exploration_water_seconds = _finite_nonnegative(
            value["max_exploration_water_seconds"], "max_exploration_water_seconds"
        )
        return cls(
            warning_age_seconds=warning_age_seconds,
            deny_age_seconds=deny_age_seconds,
            window_seconds=window_seconds,
            max_exploration_water_seconds=max_exploration_water_seconds,
        )


def _finite_nonnegative_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _flag_active(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and isinstance(value.get("active"), bool):
        return value["active"]
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _requested_water_seconds(strategy: dict[str, Any]) -> float:
    return sum(
        float(action["pump_seconds"])
        for action in strategy.get("actions", [])
        if isinstance(action, dict) and action.get("type") == "water"
    )


def _budget_record(exploration_requested: bool, requested_water_seconds: float) -> dict[str, Any]:
    return {
        "exploration_requested": exploration_requested,
        "requested_water_seconds": requested_water_seconds,
        "reserved_water_seconds": 0.0,
        "remaining_water_seconds": None,
        "reservation_id": None,
    }


def evaluate_gate(
    state: dict[str, Any],
    strategy: dict[str, Any],
    policy: GatePolicy,
    exploration_requested: bool,
    ledger: Any = None,
) -> dict[str, Any]:
    """Return a non-executing gate.v1 decision for one state-bound strategy."""
    reasons: list[str] = []
    warnings: list[str] = []
    state = state if isinstance(state, dict) else {}
    strategy = strategy if isinstance(strategy, dict) else {}
    if not isinstance(exploration_requested, bool):
        reasons.append("invalid_exploration_requested")
        exploration_requested = False

    if state.get("schema_version") != "state.v1":
        reasons.append("invalid_state_schema_version")
    if state.get("device_code") != "soil3":
        reasons.append("state_is_not_soil3")
    if normalize_timestamp(state.get("observed_at")) is None:
        reasons.append("invalid_state_observed_at")

    validation = StrategyValidator().validate(strategy, state)
    reasons.extend(f"strategy_invalid:{code}" for code in validation.reason_codes)

    soil = state.get("soil")
    if not isinstance(soil, dict) or _finite_nonnegative_or_none(soil.get("humidity_percent")) is None:
        reasons.append("soil_humidity_unavailable")

    quality = state.get("data_quality")
    if not isinstance(quality, dict):
        quality = {}
    for field, prefix in (("soil_age_sec", "soil_data_age"), ("phase3_state_age_sec", "phase3_state_age")):
        age = _finite_nonnegative_or_none(quality.get(field))
        if age is None:
            reasons.append(f"{prefix}_unavailable")
        elif age > policy.deny_age_seconds:
            reasons.append(f"{prefix}_denied")
        elif age >= policy.warning_age_seconds:
            warnings.append(f"{prefix}_warning")

    irrigation = state.get("irrigation")
    if not isinstance(irrigation, dict) or not isinstance(irrigation.get("pump_active"), bool):
        reasons.append("pump_state_unavailable")
    elif irrigation["pump_active"]:
        reasons.append("pump_active")

    safety = state.get("safety")
    flags = safety.get("flags") if isinstance(safety, dict) else None
    if not isinstance(flags, dict):
        reasons.append("safety_flags_unavailable")
        flags = {}
    for name in sorted(ACTIVE_PROTECTION_FLAGS):
        if name not in flags:
            continue
        active = _flag_active(flags[name])
        if active is None:
            reasons.append(f"safety_flag_invalid:{name}")
        elif active:
            reasons.append(f"safety_flag_active:{name}")

    if "predictor_circuit" in flags:
        predictor = flags["predictor_circuit"]
        circuit_state = predictor.get("state") if isinstance(predictor, dict) else None
        if circuit_state not in PREDICTOR_CIRCUIT_STATES:
            reasons.append("predictor_circuit_invalid")
        elif circuit_state == "OPEN":
            reasons.append("predictor_circuit_open")
        elif circuit_state == "HALF_OPEN":
            warnings.append("predictor_circuit_half_open")

    decided_at = _utc_now()
    state_sha256 = fingerprint(state)
    requested_water_seconds = (
        _requested_water_seconds(strategy)
        if exploration_requested and validation.accepted
        else 0.0
    )
    budget = _budget_record(exploration_requested, requested_water_seconds)
    if exploration_requested and not reasons:
        if ledger is None:
            reasons.append("budget_ledger_required")
        else:
            reservation_id = f"{strategy.get('strategy_id')}:{state_sha256}"
            try:
                reservation = ledger.reserve(
                    "soil3",
                    reservation_id,
                    requested_water_seconds,
                    policy,
                    decided_at,
                )
            except BudgetReservationConflict:
                reasons.append("budget_reservation_conflict")
            except BudgetLedgerError:
                reasons.append("budget_ledger_unavailable")
            else:
                budget.update(
                    {
                        "reserved_water_seconds": reservation.reserved_water_seconds,
                        "remaining_water_seconds": reservation.remaining_water_seconds,
                        "reservation_id": reservation.reservation_id,
                    }
                )
                if not reservation.available:
                    reasons.append("exploration_budget_exhausted")

    decision = "deny" if reasons else ("allow_with_warning" if warnings else "allow")
    return {
        "schema_version": "gate.v1",
        "gate_id": str(uuid.uuid4()),
        "decided_at": decided_at,
        "device_code": state.get("device_code"),
        "strategy_id": strategy.get("strategy_id"),
        "state_observed_at": normalize_timestamp(state.get("observed_at")),
        "state_sha256": state_sha256,
        "decision": decision,
        "reason_codes": reasons,
        "warning_codes": warnings if not reasons else [],
        "budget": budget,
        "execution": {"mode": "admission_only", "actuator_commands_allowed": False},
    }
