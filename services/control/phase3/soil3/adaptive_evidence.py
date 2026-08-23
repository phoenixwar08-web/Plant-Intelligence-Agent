"""Evidence tiers and conservative, bidirectional threshold adaptation.

This module deliberately contains no I/O, MQTT, or actuator code.  It makes
two policy decisions only:

* classify a completed watering outcome as hard-invalid, weak, or strong;
* decide whether a set of same-direction observations has earned a small
  movement of a long-term threshold.

Keeping this policy pure makes it testable and prevents learning code from
becoming a second irrigation-control path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


HARD_INVALID = "hard_invalid"
WEAK = "weak"
STRONG = "strong"

_HARD_EXCLUSIONS = {
    "water_delivery_suspect",
    "reservoir_empty_suspect",
    "low_wet_recovery_suspect",
    "sensor_fault",
    "manual_or_unknown_watering",
    "sensor_context_unreliable",
    "unsafe_or_unphysical_outcome",
    "forced_exploration_quarantined",
    "emergency_safety_sample",
    "reservoir_retest_sample",
}


@dataclass(frozen=True)
class Evidence:
    tier: str
    weight: float
    reason: str


@dataclass(frozen=True)
class ThresholdMove:
    value: float
    moved: bool
    stabilized: bool
    reason: str
    strong_count: int
    weak_weight: float


def classify_trial(record: dict[str, Any]) -> Evidence:
    """Classify one settled watering record without discarding weak feedback.

    Hard-invalid records are excluded because the action or observation is not
    trustworthy.  A weak result still remains useful for trend detection, but
    cannot update the physical pump gain on its own.
    """
    exclusions = set(record.get("learning_exclusion_reasons") or [])
    if exclusions & _HARD_EXCLUSIONS or record.get("learning_valid") is False:
        return Evidence(HARD_INVALID, 0.0, "hard_safety_or_measurement_exclusion")

    try:
        delta = float(record.get("delta_m") or 0.0)
    except (TypeError, ValueError):
        delta = 0.0
    try:
        quality = float(record.get("quality_score"))
    except (TypeError, ValueError):
        quality = 0.0
    try:
        penalty = float(record.get("penalty") or 0.0)
    except (TypeError, ValueError):
        penalty = 0.0

    window = str(record.get("watering_window_sample_context") or "normal_window")
    if delta <= 0:
        return Evidence(WEAK, 0.2, "no_settled_rise_keep_for_trend_only")
    if window == "poor_window":
        return Evidence(WEAK, 0.5, "poor_watering_window")
    if quality < 0.65 or penalty > 0.25:
        return Evidence(WEAK, 0.4, "partial_or_model_mismatched_response")
    return Evidence(STRONG, 1.0, "complete_normal_response")


def progressive_move(
    *,
    current: float,
    candidate: float,
    strong_count: int,
    weak_weight: float,
    vpd_bucket_count: int,
    lower_bound: float,
    upper_bound: float,
    step: float = 0.2,
    max_step: float = 0.5,
) -> ThresholdMove:
    """Move toward a supported candidate by at most 0.2--0.5 percentage point.

    One strong sample only records a direction.  Two strong samples, or one
    strong sample reinforced by two weak-weight units, unlock a 0.2-point
    exploratory move.  Five strong samples across at least two VPD buckets
    mark the direction as stable; they still never bypass the hard bounds.
    """
    current = float(current)
    candidate = min(max(float(candidate), float(lower_bound)), float(upper_bound))
    strong_count = max(int(strong_count), 0)
    weak_weight = max(float(weak_weight), 0.0)
    vpd_bucket_count = max(int(vpd_bucket_count), 0)

    if abs(candidate - current) < 1e-9:
        return ThresholdMove(current, False, False, "candidate_matches_current", strong_count, weak_weight)

    enough = strong_count >= 2 or (strong_count >= 1 and weak_weight >= 2.0)
    stabilized = strong_count >= 5 and vpd_bucket_count >= 2
    if not enough:
        return ThresholdMove(current, False, stabilized, "direction_recorded_wait_for_more_evidence", strong_count, weak_weight)

    distance = abs(candidate - current)
    amount = min(distance, max(0.0, min(step, max_step)))
    value = current + amount if candidate > current else current - amount
    value = min(max(value, float(lower_bound)), float(upper_bound))
    return ThresholdMove(round(value, 3), value != current, stabilized, "progressive_evidence_move", strong_count, weak_weight)


def evidence_totals(records: Iterable[dict[str, Any]]) -> tuple[int, float, int]:
    """Return strong count, weak evidence weight, and distinct VPD buckets."""
    strong = 0
    weak = 0.0
    buckets: set[str] = set()
    for record in records:
        evidence = classify_trial(record)
        if evidence.tier == STRONG:
            strong += 1
        elif evidence.tier == WEAK:
            weak += evidence.weight
        if evidence.tier != HARD_INVALID:
            try:
                vpd = float(record.get("vpd"))
            except (TypeError, ValueError):
                continue
            buckets.add("low" if vpd < 0.8 else "mid" if vpd < 1.5 else "high")
    return strong, round(weak, 3), len(buckets)
