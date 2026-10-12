"""Build a factual Decision Context without making an irrigation decision."""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

from services.soil3.feedback.feedback_v1 import FeedbackError, FeedbackStore
from services.soil3.feedback_collector.action_receipt_v1 import (
    ActionReceiptError,
    ActionReceiptStore,
    episode_id_for_receipt,
)
from services.soil3.cloud_strategy.validator import parse_timestamp
from services.soil3.state.state_v1 import PHASE3_SAFETY_FLAG_KEYS


PREDICTOR_STATES = frozenset({"CLOSED", "HALF_OPEN", "OPEN"})
REAL_ACTION_EVIDENCE = frozenset({
    "command_completed",
    "manual_confirmed",
    "device_confirmed",
})


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _distance(left: Any, right: Any) -> float | None:
    left_number = _finite(left)
    right_number = _finite(right)
    if left_number is None or right_number is None:
        return None
    return round(left_number - right_number, 3)


def _timestamp_seconds(value: Any) -> float | None:
    parsed = parse_timestamp(value)
    return parsed.timestamp() if parsed is not None else None


def _active_state(
    name: str,
    value: Any,
    *,
    observed_at: Any,
    phase3_system_state: Any,
) -> str:
    if isinstance(value, bool):
        return "active" if value else "inactive"
    if isinstance(value, dict) and isinstance(value.get("active"), bool):
        return "active" if value["active"] else "inactive"
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return "active" if value == 1 else "inactive"
    if not isinstance(value, dict):
        return "invalid"

    observed_seconds = _timestamp_seconds(observed_at)
    if name == "pending_soak":
        pump_end = _finite(value.get("pump_end_time_wall"))
        duration = _finite(value.get("soak_duration"))
        if pump_end is None or duration is None or duration < 0 or observed_seconds is None:
            return "invalid"
        return "active" if observed_seconds <= pump_end + duration else "inactive"
    if name == "dynamic_cooldown":
        cooldown = _finite(value.get("cooldown_sec"))
        if not isinstance(phase3_system_state, dict):
            return "invalid"
        phase3 = phase3_system_state
        last_water = _finite(phase3.get("last_normal_irrigation_timestamp"))
        if cooldown is None or cooldown < 0 or observed_seconds is None:
            return "invalid"
        if last_water is None:
            return "inactive" if "last_normal_irrigation_timestamp" not in phase3 else "invalid"
        return "active" if observed_seconds < last_water + cooldown else "inactive"
    if name == "hard_safety_low_guard":
        effective_low = _finite(value.get("effective_low"))
        updated_at = _finite(value.get("updated_at"))
        if effective_low is None or updated_at is None or effective_low < 0:
            return "invalid"
        # This Phase3 record guards the dynamic threshold itself. It is not an
        # active hazard unless a producer explicitly supplies active=true.
        return "inactive"
    return "invalid"


def _latest_receipt(receipt_dir: str | Path | None) -> dict[str, Any] | None:
    if receipt_dir is None:
        return None
    try:
        receipts = list(ActionReceiptStore(receipt_dir).iter_receipts())
    except (ActionReceiptError, OSError, TypeError, ValueError):
        return None
    receipts = [
        receipt
        for receipt in receipts
        if (
            isinstance(receipt.get("execution_evidence"), dict)
            and receipt["execution_evidence"].get("level") in REAL_ACTION_EVIDENCE
            and receipt.get("reference_action_at") is not None
        )
    ]
    if not receipts:
        return None
    return max(
        receipts,
        key=lambda record: str(record.get("reference_action_at") or record.get("created_at") or ""),
    )


def _action_summary(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    evidence = receipt.get("execution_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    level = evidence.get("level") if isinstance(evidence.get("level"), str) else None
    physical = evidence.get("physical_action_confirmed") is True
    try:
        episode_id = episode_id_for_receipt(receipt)
    except ActionReceiptError:
        episode_id = None
    return {
        "action_id": receipt.get("action_id"),
        "action_source": receipt.get("action_source"),
        "reference_action_at": receipt.get("reference_action_at"),
        "command_status": receipt.get("command_status"),
        "evidence_level": level,
        "physical_action_confirmed": physical,
        "causal_confirmed": physical and level in {"manual_confirmed", "device_confirmed"},
        "episode_id": episode_id,
    }


def _feedback_summary(
    action: dict[str, Any] | None,
    feedback_dir: str | Path | None,
) -> dict[str, Any]:
    if action is None or feedback_dir is None or not action.get("episode_id"):
        return {"status": "unavailable", "record": None}
    try:
        records = FeedbackStore(feedback_dir).list_for_episode(action["episode_id"])
    except (FeedbackError, OSError, TypeError, ValueError):
        return {"status": "unavailable", "record": None}
    if not records:
        return {"status": "missing", "record": None}
    latest = records[-1]
    return {
        "status": "available",
        "record": {
            key: copy.deepcopy(latest.get(key))
            for key in (
                "feedback_id",
                "episode_id",
                "window",
                "observed_at",
                "reference_action_at",
                "observations",
                "assessments",
                "contradictions",
                "missing_observations",
            )
        },
    }


def build_decision_context(
    state: Any,
    *,
    last_model_decision: Any = None,
    receipt_dir: str | Path | None = None,
    feedback_dir: str | Path | None = None,
    phase3_system_state: Any = None,
) -> dict[str, Any]:
    """Project State and durable local records; unknown never becomes safe."""

    state = state if isinstance(state, dict) else {}
    soil = state.get("soil") if isinstance(state.get("soil"), dict) else {}
    safety = state.get("safety") if isinstance(state.get("safety"), dict) else {}
    trends = state.get("trends") if isinstance(state.get("trends"), dict) else {}
    quality = state.get("data_quality") if isinstance(state.get("data_quality"), dict) else {}
    flags = safety.get("flags") if isinstance(safety.get("flags"), dict) else {}

    humidity = _finite(soil.get("humidity_percent"))
    boundaries = {
        "target_low": _finite(safety.get("target_low")),
        "field_capacity": _finite(safety.get("field_capacity")),
        "hard_safety_low": _finite(safety.get("hard_safety_low")),
    }
    safety_flags = {}
    for name in PHASE3_SAFETY_FLAG_KEYS:
        if name == "predictor_circuit":
            continue
        safety_flags[name] = (
            _active_state(
                name,
                flags[name],
                observed_at=state.get("observed_at"),
                phase3_system_state=phase3_system_state,
            )
            if name in flags
            else "missing"
        )
    predictor = flags.get("predictor_circuit")
    predictor_state = predictor.get("state") if isinstance(predictor, dict) else None
    if "predictor_circuit" not in flags:
        predictor_state = "missing"
    elif predictor_state not in PREDICTOR_STATES:
        predictor_state = "invalid"

    action = _action_summary(_latest_receipt(receipt_dir))
    return {
        "schema_version": "decision_context.v1",
        "device_code": state.get("device_code"),
        "state_observed_at": state.get("observed_at"),
        "humidity": humidity,
        "boundaries": boundaries,
        "distances": {
            "humidity_minus_target_low": _distance(humidity, boundaries["target_low"]),
            "humidity_minus_hard_safety_low": _distance(humidity, boundaries["hard_safety_low"]),
            "field_capacity_minus_humidity": _distance(boundaries["field_capacity"], humidity),
        },
        "trends": {
            key: _finite(trends.get(key))
            for key in ("humidity_1h", "humidity_3h", "humidity_6h")
        },
        "data_quality": {
            "soil_age_sec": _finite(quality.get("soil_age_sec")),
            "phase3_state_age_sec": _finite(quality.get("phase3_state_age_sec")),
        },
        "safety_flags": safety_flags,
        "predictor_circuit": predictor_state,
        "last_model_decision": copy.deepcopy(last_model_decision),
        "last_real_action": action,
        "feedback_status": _feedback_summary(action, feedback_dir),
    }


def _relation(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "unknown"
    if number < 0:
        return "below"
    if number > 0:
        return "above"
    return "equal"


def _direction(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "unknown"
    if number < 0:
        return "falling"
    if number > 0:
        return "rising"
    return "stable"


def semantic_projection(context: Any) -> dict[str, Any]:
    """Return the deterministic, explainable fields compared between calls."""

    context = context if isinstance(context, dict) else {}
    boundaries = context.get("boundaries") if isinstance(context.get("boundaries"), dict) else {}
    distances = context.get("distances") if isinstance(context.get("distances"), dict) else {}
    trends = context.get("trends") if isinstance(context.get("trends"), dict) else {}
    flags = context.get("safety_flags") if isinstance(context.get("safety_flags"), dict) else {}
    action = context.get("last_real_action") if isinstance(context.get("last_real_action"), dict) else {}
    feedback = context.get("feedback_status") if isinstance(context.get("feedback_status"), dict) else {}
    feedback_record = feedback.get("record") if isinstance(feedback.get("record"), dict) else {}
    return {
        "boundaries": copy.deepcopy(boundaries),
        "boundary_relations": {
            key: _relation(distances.get(key))
            for key in (
                "humidity_minus_target_low",
                "humidity_minus_hard_safety_low",
                "field_capacity_minus_humidity",
            )
        },
        "trend_directions": {key: _direction(trends.get(key)) for key in sorted(trends)},
        "safety_flags": copy.deepcopy(flags),
        "predictor_circuit": context.get("predictor_circuit"),
        "confirmed_action_id": action.get("action_id") if action.get("causal_confirmed") is True else None,
        "feedback_id": feedback_record.get("feedback_id"),
        "feedback_window": feedback_record.get("window"),
    }
