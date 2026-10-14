"""Read existing Phase3 parameter facts without creating defaults."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def load_state_parameters(
    parameters_path: str | Path,
    system_state: Any,
) -> dict[str, float | None]:
    """Map persisted Phase3 facts to the names already accepted by StateBuilder."""

    try:
        with Path(parameters_path).open("r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}

    state = system_state if isinstance(system_state, dict) else {}
    guard = state.get("hard_safety_low_guard")
    if not isinstance(guard, dict):
        guard = {}

    return {
        "FC": _finite_number(stored.get("FC")),
        "TARGET_LOW": _finite_number(stored.get("TARGET_LOW")),
        "HARD_SAFETY_LOW": _finite_number(guard.get("effective_low")),
        "K_P": _finite_number(stored.get("K_P")),
    }
