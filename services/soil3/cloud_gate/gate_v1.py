from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


POLICY_FIELDS = {
    "warning_age_seconds",
    "deny_age_seconds",
    "window_seconds",
    "max_exploration_water_seconds",
}


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
