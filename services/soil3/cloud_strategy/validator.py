from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


ALLOWED_ACTIONS = {"water", "wait", "observe", "stop"}
EXPECTED_DEVICE_CODE = "soil3"
PROMPT_VERSION = "strategy-prompt.v2"
PROTOCOL_MAX_ACTIONS = 12
PROTOCOL_MAX_PUMP_SECONDS = 120.0
PROTOCOL_MAX_WAIT_SECONDS = 86400.0
PROTOCOL_MAX_TOTAL_PUMP_SECONDS = 240.0
PROTOCOL_MAX_TOTAL_SECONDS = 86400.0
PROTOCOL_MAX_REASON_SUMMARY_ITEMS = 8
ALLOWED_EXECUTION_FIELDS = {"mode", "actuator_commands_allowed"}
# Only the vocabulary the prompt actually asks for. A free-form object here is a
# channel for execution-shaped keys (pump_seconds, gpio, cmd) to enter the record.
REQUIRED_EXPECTED_OUTCOME_FIELDS = {"soil_moisture", "risk_notes"}
ALLOWED_EXPECTED_OUTCOME_FIELDS = REQUIRED_EXPECTED_OUTCOME_FIELDS
REQUIRED_MODEL_FIELDS = {"provider", "name", "prompt_version"}
ALLOWED_MODEL_FIELDS = REQUIRED_MODEL_FIELDS
REQUIRED_FIELDS = {
    "schema_version",
    "strategy_id",
    "device_code",
    "state_observed_at",
    "state_generated_at",
    "created_at",
    "actions",
    "reason_summary",
    "expected_outcome",
    "confidence",
    "model",
    "execution",
}
# State bindings, as (strategy field, state.v1 field). state.v1 emits no identifier
# column of its own, so a proposal points at a snapshot by the pair the producer
# actually writes: which device, and which observation moment it was built from.
STATE_BINDINGS = (
    ("state_observed_at", "observed_at"),
    ("state_generated_at", "generated_at"),
)


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


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Normalize whatever state.v1 passes through into an aware datetime.

    The producer copies `observed_at` from its input untouched, so a real record can
    carry an ISO string with a `Z` suffix or a numeric epoch from a telemetry row.
    Comparing those as text would reject a correct binding, so both sides are parsed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _is_datetime(value: Any) -> bool:
    return isinstance(value, str) and parse_timestamp(value) is not None


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
        device_code = value.get("device_code")
        if not isinstance(device_code, str) or device_code != EXPECTED_DEVICE_CODE:
            reasons.append("invalid_device_code")
        elif state.get("device_code") != EXPECTED_DEVICE_CODE:
            # A strategy can only ever name soil3, so a diverging state is a wrong
            # input file rather than a mismatch between two free-form values.
            reasons.append("state_is_not_soil3")
        for field, state_key in STATE_BINDINGS:
            claimed = parse_timestamp(value.get(field))
            if claimed is None:
                reasons.append(f"invalid_{field}")
            actual = parse_timestamp(state.get(state_key))
            if actual is None:
                reasons.append(f"state_has_no_usable_{state_key}")
            elif claimed is not None and claimed != actual:
                reasons.append(f"{field}_mismatch")
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
        elif len(summary) > PROTOCOL_MAX_REASON_SUMMARY_ITEMS:
            reasons.append("reason_summary_too_long")

        outcome = value.get("expected_outcome")
        if not isinstance(outcome, dict):
            reasons.append("invalid_expected_outcome")
        else:
            unknown_outcome = sorted(set(outcome) - ALLOWED_EXPECTED_OUTCOME_FIELDS)
            if unknown_outcome:
                reasons.append("expected_outcome_unknown_fields:" + ",".join(unknown_outcome))
            missing_outcome = sorted(REQUIRED_EXPECTED_OUTCOME_FIELDS - set(outcome))
            if missing_outcome:
                reasons.append("expected_outcome_missing_fields:" + ",".join(missing_outcome))
            if "soil_moisture" in outcome and (
                not isinstance(outcome["soil_moisture"], str) or not outcome["soil_moisture"].strip()
            ):
                reasons.append("expected_outcome_invalid_field:soil_moisture")
            risk_notes = outcome.get("risk_notes")
            if risk_notes is not None and not isinstance(risk_notes, list):
                reasons.append("expected_outcome_invalid_field:risk_notes")
            elif isinstance(risk_notes, list):
                if any(not isinstance(item, str) or not item.strip() for item in risk_notes):
                    reasons.append("expected_outcome_invalid_field:risk_notes")
                elif len(risk_notes) > PROTOCOL_MAX_REASON_SUMMARY_ITEMS:
                    reasons.append("expected_outcome_risk_notes_too_long")

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
            if model.get("prompt_version") != PROMPT_VERSION:
                reasons.append("invalid_model_field:prompt_version")
            unknown_model = sorted(set(model) - ALLOWED_MODEL_FIELDS)
            if unknown_model:
                reasons.append("model_unknown_fields:" + ",".join(unknown_model))

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
