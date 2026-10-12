"""Deterministic Trigger Shadow decisions; never an irrigation authority."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .context_v1 import semantic_projection


CALL_QWEN = "CALL_QWEN"
SKIP_CLOUD = "SKIP_CLOUD"
LOCAL_SAFETY_ONLY = "LOCAL_SAFETY_ONLY"

DANGEROUS_FLAGS = frozenset({
    "water_delivery_suspect",
    "reservoir_empty_suspect",
    "low_wet_recovery_suspect",
    "sensor_fault",
    "hard_safety_low_guard",
    "cloud_protection",
})
PROTECTION_FLAGS = (
    "pump_active",
    "pending_soak",
    "dynamic_cooldown",
    "recent_response_guard",
    "watering_trigger_guard",
)


@dataclass(frozen=True)
class TriggerDecision:
    decision: str
    reason_codes: tuple[str, ...]


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def evaluate_trigger(
    context: Any,
    baseline_context: Any,
    *,
    deny_age_seconds: float,
) -> TriggerDecision:
    """Classify cloud invocation value while preserving local fail-closed facts."""

    context = context if isinstance(context, dict) else {}
    boundaries = context.get("boundaries") if isinstance(context.get("boundaries"), dict) else {}
    quality = context.get("data_quality") if isinstance(context.get("data_quality"), dict) else {}
    flags = context.get("safety_flags") if isinstance(context.get("safety_flags"), dict) else {}
    reasons: list[str] = []

    humidity = _finite_nonnegative(context.get("humidity"))
    if humidity is None:
        reasons.append("core_fact_unavailable:humidity")
    for name in ("target_low", "field_capacity", "hard_safety_low"):
        if _finite_nonnegative(boundaries.get(name)) is None:
            reasons.append(f"core_fact_unavailable:{name}")
    try:
        deny_age = float(deny_age_seconds)
    except (TypeError, ValueError, OverflowError):
        deny_age = -1.0
    if not math.isfinite(deny_age) or deny_age < 0:
        reasons.append("deny_age_invalid")
    for name in ("soil_age_sec", "phase3_state_age_sec"):
        age = _finite_nonnegative(quality.get(name))
        if age is None:
            reasons.append(f"core_fact_unavailable:{name}")
        elif deny_age >= 0 and age > deny_age:
            reasons.append(f"stale:{name}")
    for name, status in sorted(flags.items()):
        if status in {"missing", "invalid"}:
            reasons.append(f"safety_fact_{status}:{name}")
    predictor = context.get("predictor_circuit")
    if predictor in {"missing", "invalid", None}:
        reasons.append(f"predictor_circuit_{predictor or 'missing'}")
    if reasons:
        return TriggerDecision(LOCAL_SAFETY_ONLY, tuple(reasons))

    hard_low = _finite_nonnegative(boundaries.get("hard_safety_low"))
    if humidity is not None and hard_low is not None and humidity <= hard_low:
        reasons.append("hard_safety_low_reached")
    for name in sorted(DANGEROUS_FLAGS):
        if flags.get(name) == "active":
            reasons.append(f"dangerous_safety_active:{name}")
    if predictor == "OPEN":
        reasons.append("predictor_circuit_open")
    if reasons:
        return TriggerDecision(LOCAL_SAFETY_ONLY, tuple(reasons))

    protections = [name for name in PROTECTION_FLAGS if flags.get(name) == "active"]
    if protections:
        return TriggerDecision(
            SKIP_CLOUD,
            tuple(f"protection_active:{name}" for name in protections),
        )

    if baseline_context is None:
        return TriggerDecision(CALL_QWEN, ("no_valid_call_baseline",))
    current_projection = semantic_projection(context)
    if isinstance(baseline_context, dict) and baseline_context.get("schema_version") == "decision_context.v1":
        baseline_projection = semantic_projection(baseline_context)
    else:
        baseline_projection = baseline_context
    if current_projection != baseline_projection:
        return TriggerDecision(CALL_QWEN, ("semantic_context_changed",))
    return TriggerDecision(SKIP_CLOUD, ("semantic_context_unchanged",))
