"""Experience Retrieval V1.

The retriever scans caller-selected ``episode.v1`` files and compares their
embedded state snapshot with one current ``state.v1`` snapshot.  It is a
read-only consumer: it does not update episodes, propose an action, call
Phase3, or touch an actuator.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from services.soil3.telemetry.common import load_json


SCHEMA_VERSION = "experience_retrieval.v1"
EXPECTED_DEVICE_CODE = "soil3"
MIN_EVIDENCE_COVERAGE = 0.50


class RetrievalError(ValueError):
    """The retrieval request itself is invalid."""


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _path(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _numeric_feature(
    current: Any,
    historical: Any,
    scale: float,
) -> Optional[Tuple[float, float]]:
    left = _number(current)
    right = _number(historical)
    if left is None or right is None:
        return None
    distance = abs(left - right)
    return max(0.0, 1.0 - distance / scale), distance


def _hours_since_water(state: Dict[str, Any]) -> Optional[float]:
    observed = _time(state.get("observed_at"))
    last_water = _time(_path(state, "irrigation", "last_water_at"))
    if observed is None or last_water is None or last_water > observed:
        return None
    return (observed - last_water).total_seconds() / 3600.0


def _active_safety_flags(state: Dict[str, Any]) -> Optional[frozenset[str]]:
    flags = _path(state, "safety", "flags")
    if not isinstance(flags, dict):
        return None
    return frozenset(sorted(key for key, value in flags.items() if bool(value)))


def _flag_similarity(
    current: Dict[str, Any], historical: Dict[str, Any]
) -> Optional[Tuple[float, float]]:
    left = _active_safety_flags(current)
    right = _active_safety_flags(historical)
    if left is None or right is None:
        return None
    union = left | right
    similarity = 1.0 if not union else len(left & right) / len(union)
    return similarity, 1.0 - similarity


# name, weight, normalized comparison range, getter.  Weights total 1.0 and
# are intentionally visible in every response so ranking stays explainable.
NUMERIC_FEATURES: Tuple[
    Tuple[str, float, float, Callable[[Dict[str, Any]], Any]], ...
] = (
    ("soil_humidity_percent", 0.25, 40.0, lambda s: _path(s, "soil", "humidity_percent")),
    ("humidity_trend_1h", 0.06, 15.0, lambda s: _path(s, "trends", "humidity_1h")),
    ("humidity_trend_3h", 0.07, 15.0, lambda s: _path(s, "trends", "humidity_3h")),
    ("humidity_trend_6h", 0.07, 15.0, lambda s: _path(s, "trends", "humidity_6h")),
    ("air_humidity_percent", 0.05, 50.0, lambda s: _path(s, "air", "humidity_percent")),
    ("air_temperature_c", 0.05, 20.0, lambda s: _path(s, "air", "temperature_c")),
    ("last_water_sec", 0.08, 60.0, lambda s: _path(s, "irrigation", "last_water_sec")),
    ("hours_since_last_water", 0.12, 72.0, _hours_since_water),
    ("target_low", 0.04, 20.0, lambda s: _path(s, "safety", "target_low")),
    ("hard_safety_low", 0.04, 20.0, lambda s: _path(s, "safety", "hard_safety_low")),
)
SAFETY_FLAGS_WEIGHT = 0.17

OUTCOME_FIELDS = ("experience_result", "result", "status", "classification", "recovery")
SUCCESS_VALUES = frozenset({"success", "succeeded", "effective", "good", "recovered"})
FAILURE_VALUES = frozenset({"failure", "failed", "ineffective", "poor", "adverse"})


def classify_outcome(outcome: Any) -> Optional[str]:
    """Conservatively map explicit outcome labels to success or failure.

    ``episode.v1`` deliberately did not invent one unified reward.  Retrieval
    therefore accepts a small set of explicit scalar labels and refuses to
    infer a class from measurements, free text, Gate decisions, or actions.
    Conflicting labels are unusable rather than silently resolved.
    """
    if not isinstance(outcome, dict):
        return None
    classes = set()
    for key in OUTCOME_FIELDS:
        value = outcome.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower()
        if normalized in SUCCESS_VALUES:
            classes.add("success")
        elif normalized in FAILURE_VALUES:
            classes.add("failure")
    return next(iter(classes)) if len(classes) == 1 else None


def compare_states(current: Dict[str, Any], historical: Dict[str, Any]) -> Dict[str, Any]:
    matched_on: List[Dict[str, Any]] = []
    missing: List[str] = []
    weighted_score = 0.0
    covered_weight = 0.0

    for name, weight, scale, getter in NUMERIC_FEATURES:
        current_value = getter(current)
        historical_value = getter(historical)
        comparison = _numeric_feature(current_value, historical_value, scale)
        if comparison is None:
            missing.append(name)
            continue
        similarity, distance = comparison
        covered_weight += weight
        weighted_score += weight * similarity
        matched_on.append({
            "feature": name,
            "current": round(float(current_value), 4) if name != "hours_since_last_water" else round(float(_hours_since_water(current)), 4),
            "historical": round(float(historical_value), 4) if name != "hours_since_last_water" else round(float(_hours_since_water(historical)), 4),
            "absolute_distance": round(distance, 4),
            "similarity": round(similarity, 4),
            "weight": weight,
        })

    flags = _flag_similarity(current, historical)
    if flags is None:
        missing.append("safety_flags")
    else:
        similarity, distance = flags
        covered_weight += SAFETY_FLAGS_WEIGHT
        weighted_score += SAFETY_FLAGS_WEIGHT * similarity
        matched_on.append({
            "feature": "safety_flags",
            "current": sorted(_active_safety_flags(current) or ()),
            "historical": sorted(_active_safety_flags(historical) or ()),
            "absolute_distance": round(distance, 4),
            "similarity": round(similarity, 4),
            "weight": SAFETY_FLAGS_WEIGHT,
        })

    score = weighted_score / covered_weight if covered_weight else 0.0
    return {
        "similarity_score": round(score, 4),
        "evidence_coverage": round(covered_weight, 4),
        "usable": covered_weight >= MIN_EVIDENCE_COVERAGE,
        "matched_on": matched_on,
        "missing_components": missing,
    }


def _action_summary(episode: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for entry in episode.get("executed_actions") or []:
        if not isinstance(entry, dict):
            continue
        result.append({
            key: copy.deepcopy(entry[key])
            for key in ("kind", "type", "pump_seconds", "physical_action_performed", "executed_at")
            if key in entry
        })
    return result


def _summary(episode: Dict[str, Any]) -> Dict[str, Any]:
    state = episode["initial_state"]
    return {
        "state_observed_at": state.get("observed_at"),
        "soil_humidity_percent": _path(state, "soil", "humidity_percent"),
        "humidity_trends": copy.deepcopy(state.get("trends")),
        "last_water_at": _path(state, "irrigation", "last_water_at"),
        "last_water_sec": _path(state, "irrigation", "last_water_sec"),
        "active_safety_flags": sorted(_active_safety_flags(state) or ()),
        "executed_actions": _action_summary(episode),
        "outcome": copy.deepcopy(episode.get("outcome")),
    }


class ExperienceRetriever:
    """Rank successful and failed closed episodes without changing history."""

    def __init__(self, episode_dir: str | Path, *, min_evidence_coverage: float = MIN_EVIDENCE_COVERAGE):
        self.episode_dir = Path(episode_dir)
        if not 0.0 <= min_evidence_coverage <= 1.0:
            raise RetrievalError("min_evidence_coverage must be between 0 and 1")
        self.min_evidence_coverage = float(min_evidence_coverage)

    def retrieve(self, current_state: Dict[str, Any], *, limit_per_class: int = 3) -> Dict[str, Any]:
        self._validate_request(current_state, limit_per_class)
        ranked: Dict[str, List[Dict[str, Any]]] = {"success": [], "failure": []}
        skipped = {
            "malformed_or_unreadable": 0,
            "not_closed": 0,
            "outcome_unclassified": 0,
            "insufficient_state_evidence": 0,
        }
        scanned = 0

        paths: Iterable[Path] = sorted(self.episode_dir.glob("ep-*.json")) if self.episode_dir.is_dir() else ()
        for path in paths:
            scanned += 1
            try:
                episode = load_json(path)
            except (OSError, ValueError):
                skipped["malformed_or_unreadable"] += 1
                continue
            if not self._valid_episode_envelope(episode):
                skipped["malformed_or_unreadable"] += 1
                continue
            if episode.get("status") != "closed":
                skipped["not_closed"] += 1
                continue
            outcome_class = classify_outcome(episode.get("outcome"))
            if outcome_class is None:
                skipped["outcome_unclassified"] += 1
                continue
            comparison = compare_states(current_state, episode["initial_state"])
            comparison["usable"] = comparison["evidence_coverage"] >= self.min_evidence_coverage
            if not comparison["usable"]:
                skipped["insufficient_state_evidence"] += 1
                continue
            ranked[outcome_class].append({
                "episode_id": episode["episode_id"],
                "outcome_class": outcome_class,
                **comparison,
                "summary": _summary(episode),
            })

        for values in ranked.values():
            values.sort(key=lambda item: (-item["similarity_score"], -item["evidence_coverage"], item["episode_id"]))

        successes = ranked["success"][:limit_per_class]
        failures = ranked["failure"][:limit_per_class]
        empty_reasons = []
        if not successes:
            empty_reasons.append("no_usable_success_history")
        if not failures:
            empty_reasons.append("no_usable_failure_history")
        return {
            "schema_version": SCHEMA_VERSION,
            "device_code": EXPECTED_DEVICE_CODE,
            "current_state_observed_at": current_state.get("observed_at"),
            "history_scanned": scanned,
            "usable_history": {"success": len(ranked["success"]), "failure": len(ranked["failure"])},
            "availability": {
                "success": bool(successes),
                "failure": bool(failures),
                "both_classes": bool(successes and failures),
            },
            "empty_reasons": empty_reasons,
            "successful_cases": successes,
            "failed_cases": failures,
            "skipped_history": skipped,
        }

    @staticmethod
    def _validate_request(current_state: Any, limit_per_class: Any) -> None:
        if not isinstance(current_state, dict) or current_state.get("schema_version") != "state.v1":
            raise RetrievalError("current_state must be state.v1")
        if current_state.get("device_code") != EXPECTED_DEVICE_CODE:
            raise RetrievalError("current_state must belong to soil3")
        if isinstance(limit_per_class, bool) or not isinstance(limit_per_class, int) or limit_per_class < 1:
            raise RetrievalError("limit_per_class must be a positive integer")

    @staticmethod
    def _valid_episode_envelope(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and value.get("schema_version") == "episode.v1"
            and value.get("device_code") == EXPECTED_DEVICE_CODE
            and isinstance(value.get("episode_id"), str)
            and isinstance(value.get("initial_state"), dict)
            and value["initial_state"].get("schema_version") == "state.v1"
            and value["initial_state"].get("device_code") == EXPECTED_DEVICE_CODE
        )
