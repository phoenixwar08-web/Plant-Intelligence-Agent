from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


ALLOWED_ACTIONS = {"water", "wait", "observe", "stop"}
PROTOCOL_MAX_ACTIONS = 12
PROTOCOL_MAX_PUMP_SECONDS = 120.0
PROTOCOL_MAX_WAIT_SECONDS = 86400.0
PROTOCOL_MAX_TOTAL_PUMP_SECONDS = 240.0
PROTOCOL_MAX_TOTAL_SECONDS = 86400.0
ALLOWED_EXECUTION_FIELDS = {"mode", "actuator_commands_allowed"}
REQUIRED_FIELDS = {
    "schema_version",
    "strategy_id",
    "state_id",
    "plant_id",
    "created_at",
    "actions",
    "reason_summary",
    "expected_outcome",
    "confidence",
    "model",
    "execution",
}


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    reason_codes: List[str]
    strategy: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason_codes": list(self.reason_codes),
            "strategy": self.strategy if self.accepted else None,
        }


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        # float(10**400) raises instead of returning inf, so an unguarded
        # conversion lets a JSON number crash the Validator rather than reject it.
        return False


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _is_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except ValueError:
        return False


class StrategyValidator:
    def __init__(
        self,
        *,
        max_actions: int = 12,
        max_pump_seconds: float = 120.0,
        max_wait_seconds: float = 86400.0,
        max_total_pump_seconds: float = 240.0,
        max_total_seconds: float = 86400.0,
    ) -> None:
        try:
            max_pump_seconds = float(max_pump_seconds)
            max_wait_seconds = float(max_wait_seconds)
            max_total_pump_seconds = float(max_total_pump_seconds)
            max_total_seconds = float(max_total_seconds)
        except (TypeError, OverflowError, ValueError):
            raise ValueError("validator limits must be finite numbers")

        if not 1 <= max_actions <= PROTOCOL_MAX_ACTIONS:
            raise ValueError("max_actions exceeds strategy.v1 protocol bounds")
        if not 0 < max_pump_seconds <= PROTOCOL_MAX_PUMP_SECONDS:
            raise ValueError("max_pump_seconds exceeds strategy.v1 protocol bounds")
        if not 0 < max_wait_seconds <= PROTOCOL_MAX_WAIT_SECONDS:
            raise ValueError("max_wait_seconds exceeds strategy.v1 protocol bounds")
        if not max_pump_seconds <= max_total_pump_seconds <= PROTOCOL_MAX_TOTAL_PUMP_SECONDS:
            raise ValueError("max_total_pump_seconds exceeds strategy.v1 protocol bounds")
        if not max(max_pump_seconds, max_wait_seconds) <= max_total_seconds <= PROTOCOL_MAX_TOTAL_SECONDS:
            raise ValueError("max_total_seconds exceeds strategy.v1 protocol bounds")
        self.max_actions = max_actions
        self.max_pump_seconds = max_pump_seconds
        self.max_wait_seconds = max_wait_seconds
        self.max_total_pump_seconds = max_total_pump_seconds
        self.max_total_seconds = max_total_seconds

    def validate(self, value: Any, state: Dict[str, Any]) -> ValidationResult:
        reasons: List[str] = []
        if not isinstance(value, dict):
            return ValidationResult(False, ["strategy_not_object"])

        reasons.extend(f"missing_field:{name}" for name in sorted(REQUIRED_FIELDS - set(value)))
        unknown_fields = sorted(set(value) - REQUIRED_FIELDS)
        if unknown_fields:
            reasons.append("unknown_top_level_fields:" + ",".join(unknown_fields))

        if value.get("schema_version") != "strategy.v1":
            reasons.append("invalid_schema_version")
        if not _is_uuid(value.get("strategy_id")):
            reasons.append("invalid_strategy_id")
        state_id = value.get("state_id")
        if not isinstance(state_id, str) or not state_id.strip():
            reasons.append("invalid_state_id")
        else:
            if not isinstance(state.get("state_id"), str) or not str(state.get("state_id")).strip():
                reasons.append("state_has_no_usable_state_id")
            elif state_id != state.get("state_id"):
                reasons.append("state_id_mismatch")
        if value.get("plant_id") != state.get("plant_id") or value.get("plant_id") != "soil3":
            reasons.append("plant_id_mismatch")
        if not _is_datetime(value.get("created_at")):
            reasons.append("invalid_created_at")

        actions = value.get("actions")
        if not isinstance(actions, list) or not actions:
            reasons.append("actions_not_nonempty_list")
        elif len(actions) > self.max_actions:
            reasons.append("too_many_actions")
        else:
            reasons.extend(self._validate_actions(actions))

        summary = value.get("reason_summary")
        if (
            not isinstance(summary, list)
            or not summary
            or any(not isinstance(item, str) or not item.strip() for item in summary)
        ):
            reasons.append("invalid_reason_summary")
        elif len(summary) > 8:
            reasons.append("reason_summary_too_long")

        if not isinstance(value.get("expected_outcome"), dict):
            reasons.append("invalid_expected_outcome")

        confidence = value.get("confidence")
        if not _is_number(confidence) or not 0 <= float(confidence) <= 1:
            reasons.append("invalid_confidence")

        model = value.get("model")
        if not isinstance(model, dict):
            reasons.append("invalid_model_metadata")
        else:
            for field in ("provider", "name"):
                if not isinstance(model.get(field), str) or not model[field].strip():
                    reasons.append(f"invalid_model_field:{field}")
            if model.get("prompt_version") != "strategy-prompt.v1":
                reasons.append("invalid_model_field:prompt_version")

        execution = value.get("execution")
        if not isinstance(execution, dict):
            reasons.append("invalid_execution_metadata")
        else:
            if execution.get("mode") != "proposal_only":
                reasons.append("execution_mode_not_proposal_only")
            if execution.get("actuator_commands_allowed") is not False:
                reasons.append("actuator_permission_must_be_false")
            unknown_execution = sorted(set(execution) - ALLOWED_EXECUTION_FIELDS)
            if unknown_execution:
                reasons.append("execution_unknown_fields:" + ",".join(unknown_execution))

        return ValidationResult(not reasons, reasons, value if not reasons else None)

    def _validate_actions(self, actions: List[Any]) -> List[str]:
        reasons: List[str] = []
        action_ids = set()
        stop_seen = False
        pump_total = 0.0
        duration_total = 0.0
        for index, action in enumerate(actions):
            prefix = f"action[{index}]"
            if not isinstance(action, dict):
                reasons.append(f"{prefix}:not_object")
                continue
            action_id = action.get("action_id")
            if not isinstance(action_id, str) or not action_id.strip():
                reasons.append(f"{prefix}:invalid_action_id")
            elif action_id in action_ids:
                reasons.append(f"{prefix}:duplicate_action_id")
            else:
                action_ids.add(action_id)

            action_type = action.get("type")
            if action_type not in ALLOWED_ACTIONS:
                reasons.append(f"{prefix}:unknown_action")
                continue
            if stop_seen:
                reasons.append(f"{prefix}:action_after_stop")
            if action_type == "stop":
                stop_seen = True

            if action_type == "water":
                seconds = action.get("pump_seconds")
                if not _is_number(seconds):
                    reasons.append(f"{prefix}:invalid_pump_seconds_type")
                elif float(seconds) <= 0:
                    reasons.append(f"{prefix}:pump_seconds_not_positive")
                else:
                    pump_total += float(seconds)
                    duration_total += float(seconds)
                    if float(seconds) > self.max_pump_seconds:
                        reasons.append(f"{prefix}:pump_seconds_extreme")
                unexpected = set(action) - {"action_id", "type", "pump_seconds"}
            elif action_type == "wait":
                seconds = action.get("seconds")
                if not _is_number(seconds):
                    reasons.append(f"{prefix}:invalid_wait_seconds_type")
                elif float(seconds) <= 0:
                    reasons.append(f"{prefix}:wait_seconds_not_positive")
                else:
                    duration_total += float(seconds)
                    if float(seconds) > self.max_wait_seconds:
                        reasons.append(f"{prefix}:wait_seconds_extreme")
                unexpected = set(action) - {"action_id", "type", "seconds"}
            else:
                unexpected = set(action) - {"action_id", "type"}
            if unexpected:
                reasons.append(f"{prefix}:unknown_fields:{','.join(sorted(unexpected))}")

        # A per-action ceiling alone lets a proposal spend its whole budget on
        # pump time by repeating the largest allowed action up to max_actions.
        if pump_total > self.max_total_pump_seconds:
            reasons.append("total_pump_seconds_extreme")
        if duration_total > self.max_total_seconds:
            reasons.append("total_seconds_extreme")
        return reasons
