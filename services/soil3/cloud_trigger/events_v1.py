"""Business-event trigger v1: admission based on plant-state events, not trend jitter.

Pure, dependency-free functions (stdlib only, Python 3.9 compatible).

The caller keeps one small JSON ``memory`` dict between runs and passes it back
in; this module never performs IO.  Admission decision:

    events = detect_events(...) -> one event or None

An event is the only legitimate reason for CALL_QWEN.  Trend direction
classification is deliberately NOT used here: classification jitter around a
deadband must not admit a paid call.

Event set (first batch, no dependency on fact-chain repairs):

    E1  falling_start / falling_accel : onset of sustained moisture decline
        (predictive: uses only data known at decision time)
    E2  valley_confirm               : decline followed by confirmed rebound
        (confirmatory: review after the turn, never pretends to know the
        valley in real time)
    E3  rise_threshold_cross         : humidity crosses a configured absolute
        threshold on the way up, with hysteresis
    E4  plateau_long                 : unusually long flat period (diagnostic only)

Event object contract: every string field non-empty, evidence serializable.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


EVENT_PRIORITY = {
    "falling_start": 0,
    "falling_accel": 1,
    "valley_confirm": 2,
    "rise_threshold_cross": 3,
    "plateau_long": 4,
}

TASK_TYPES = {
    "falling_start": "falling_trend_review",
    "falling_accel": "falling_trend_review",
    "valley_confirm": "valley_review",
    "rise_threshold_cross": "rise_review",
    "plateau_long": "plateau_diagnosis",
}

MEMORY_VERSION = 1
DEDUPE_CAP = 200


class EventPolicy(object):
    """Thresholds.  All values are calibration targets, not health truths."""

    FIELDS = (
        "fall_slope_pp_per_hour",
        "accel_delta_pp_per_hour",
        "min_drop_pp_2h",
        "confirm_periods",
        "valley_confirm_rise_pp",
        "valley_confirm_periods",
        "rise_threshold_high_pp",
        "rise_hysteresis_pp",
        "plateau_hours",
        "plateau_max_range_pp",
        "plateau_stable_slope",
        "cooldown_seconds",
        "forecast_horizon_hours",
    )

    def __init__(
        self,
        fall_slope_pp_per_hour: float = -0.8,
        accel_delta_pp_per_hour: float = -0.4,
        min_drop_pp_2h: float = 1.5,
        confirm_periods: int = 2,
        valley_confirm_rise_pp: float = 1.0,
        valley_confirm_periods: int = 2,
        rise_threshold_high_pp: Optional[float] = None,
        rise_hysteresis_pp: float = 0.2,
        plateau_hours: float = 6.0,
        plateau_max_range_pp: float = 0.3,
        plateau_stable_slope: float = 0.05,
        cooldown_seconds: float = 6 * 3600.0,
        forecast_horizon_hours: float = 6.0,
    ) -> None:
        self.fall_slope_pp_per_hour = float(fall_slope_pp_per_hour)
        self.accel_delta_pp_per_hour = float(accel_delta_pp_per_hour)
        self.min_drop_pp_2h = float(min_drop_pp_2h)
        self.confirm_periods = max(1, int(confirm_periods))
        self.valley_confirm_rise_pp = float(valley_confirm_rise_pp)
        self.valley_confirm_periods = max(1, int(valley_confirm_periods))
        self.rise_threshold_high_pp = (
            float(rise_threshold_high_pp) if rise_threshold_high_pp is not None else None
        )
        self.rise_hysteresis_pp = float(rise_hysteresis_pp)
        self.plateau_hours = float(plateau_hours)
        self.plateau_max_range_pp = float(plateau_max_range_pp)
        self.plateau_stable_slope = float(plateau_stable_slope)
        self.cooldown_seconds = float(cooldown_seconds)
        self.forecast_horizon_hours = float(forecast_horizon_hours)

    @classmethod
    def from_dict(cls, value: Any) -> "EventPolicy":
        if not isinstance(value, dict):
            raise ValueError("event policy must be an object")
        unknown = set(value) - set(cls.FIELDS)
        if unknown:
            raise ValueError("unknown event policy fields: %s" % sorted(unknown))
        return cls(**value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fall_slope_pp_per_hour": self.fall_slope_pp_per_hour,
            "accel_delta_pp_per_hour": self.accel_delta_pp_per_hour,
            "min_drop_pp_2h": self.min_drop_pp_2h,
            "confirm_periods": self.confirm_periods,
            "valley_confirm_rise_pp": self.valley_confirm_rise_pp,
            "valley_confirm_periods": self.valley_confirm_periods,
            "rise_threshold_high_pp": self.rise_threshold_high_pp,
            "rise_hysteresis_pp": self.rise_hysteresis_pp,
            "plateau_hours": self.plateau_hours,
            "plateau_max_range_pp": self.plateau_max_range_pp,
            "plateau_stable_slope": self.plateau_stable_slope,
            "cooldown_seconds": self.cooldown_seconds,
            "forecast_horizon_hours": self.forecast_horizon_hours,
        }


# ---------------------------------------------------------------------------
# Time and samples
# ---------------------------------------------------------------------------

_SHORT_OFFSET = re.compile(r"([+-]\d{2})$")


def parse_timestamp(value: Any) -> Optional[float]:
    """Parse ISO-8601 into epoch seconds; tolerate 'Z' and short '+08' offsets.

    Board Python 3.9 cannot parse 'Z' or '+00' via fromisoformat, so normalize
    first.  Returns None instead of raising on anything unparseable.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = _SHORT_OFFSET.search(text)
    if match:
        text = text + ":00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None or not math.isfinite(float(epoch)):
        return None
    return (
        datetime.fromtimestamp(float(epoch), timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def normalize_readings(readings: Iterable[Any]) -> List[Tuple[float, float]]:
    """Return sorted (epoch, humidity) pairs, dropping invalid or duplicate rows."""
    points: Dict[float, float] = {}
    for item in readings or []:
        if isinstance(item, dict):
            stamp = item.get("observed_at")
            raw_value = item.get("humidity_percent", item.get("humidity"))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            stamp, raw_value = item[0], item[1]
        else:
            continue
        epoch = parse_timestamp(stamp)
        if isinstance(raw_value, bool):
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if epoch is None or not math.isfinite(value):
            continue
        points[epoch] = value
    return sorted(points.items())


def _window(points: Sequence[Tuple[float, float]], end_ts: float, seconds: float) -> List[Tuple[float, float]]:
    start = end_ts - seconds
    return [(t, v) for t, v in points if start <= t <= end_ts]


def _slope_per_hour(window: Sequence[Tuple[float, float]]) -> Optional[float]:
    if len(window) < 2:
        return None
    origin = window[0][0]
    xs = [(t - origin) / 3600.0 for t, _ in window]
    ys = [v for _, v in window]
    n = float(len(xs))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0.0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return sxy / sxx


def _range_pp(window: Sequence[Tuple[float, float]]) -> Optional[float]:
    if not window:
        return None
    values = [v for _, v in window]
    return max(values) - min(values)


def compute_signals(
    readings: Iterable[Any],
    now_ts: float,
    policy: EventPolicy,
) -> Dict[str, Any]:
    """Compute the signal windows an admission decision needs.

    All keys are always present; unavailable values are None so that callers
    can fail closed deliberately rather than guessed.
    """
    points = normalize_readings(readings)
    latest = points[-1] if points else None
    signals: Dict[str, Any] = {
        "sample_count_6h": len(_window(points, now_ts, 6 * 3600.0)),
        "latest_value": latest[1] if latest else None,
        "latest_at": _iso(latest[0]) if latest else None,
        "slope_30m": None,
        "slope_2h": None,
        "slope_prev_30m": None,
        "slope_delta": None,
        "drop_2h": None,
        "range_60m": None,
        "value_2h_ago": None,
        "forecast_6h": None,
        "forecast_low_crossed": None,
    }
    w30 = _window(points, now_ts, 30 * 60.0)
    w2h = _window(points, now_ts, 2 * 3600.0)
    wprev30 = _window(points, now_ts - 30 * 60.0, 30 * 60.0)
    w60 = _window(points, now_ts, 60 * 60.0)

    signals["slope_30m"] = _slope_per_hour(w30)
    signals["slope_2h"] = _slope_per_hour(w2h)
    signals["slope_prev_30m"] = _slope_per_hour(wprev30)
    if signals["slope_30m"] is not None and signals["slope_prev_30m"] is not None:
        signals["slope_delta"] = signals["slope_30m"] - signals["slope_prev_30m"]
    signals["range_60m"] = _range_pp(w60)

    if w2h:
        first = w2h[0]
        value2h = first[1]
        signals["value_2h_ago"] = value2h
        if latest is not None:
            signals["drop_2h"] = value2h - latest[1]

    if signals["slope_2h"] is not None and latest is not None:
        signals["forecast_6h"] = latest[1] + signals["slope_2h"] * policy.forecast_horizon_hours

    return signals


# ---------------------------------------------------------------------------
# Event detection
# ---------------------------------------------------------------------------

def empty_memory() -> Dict[str, Any]:
    return {
        "version": MEMORY_VERSION,
        "decline": None,
        "plateau_since": None,
        "events": {},
    }


def _memory_normalized(memory: Any) -> Dict[str, Any]:
    if not isinstance(memory, dict) or memory.get("version") != MEMORY_VERSION:
        return empty_memory()
    normalized = empty_memory()
    normalized["decline"] = memory.get("decline") if isinstance(memory.get("decline"), dict) else None
    normalized["plateau_since"] = memory.get("plateau_since") if isinstance(memory.get("plateau_since"), (int, float)) else None
    events = memory.get("events")
    normalized["events"] = dict(events) if isinstance(events, dict) else {}
    return normalized


def _remember(memory: Dict[str, Any], event_id: str, fired_ts: float) -> None:
    memory["events"][event_id] = {"fired_at": _iso(fired_ts)}
    if len(memory["events"]) > DEDUPE_CAP:
        ordered = sorted(
            memory["events"].items(),
            key=lambda item: parse_timestamp(item[1].get("fired_at")) or 0.0,
        )
        for stale_id, _ in ordered[: len(ordered) - DEDUPE_CAP]:
            del memory["events"][stale_id]


def _dedupe(memory: Dict[str, Any], event_id: str, now_ts: float, policy: EventPolicy) -> bool:
    record = memory["events"].get(event_id)
    if not record:
        return False
    fired_ts = parse_timestamp(record.get("fired_at"))
    if fired_ts is None:
        return False
    return now_ts - fired_ts < policy.cooldown_seconds


def _event(
    event_type: str,
    event_id: str,
    anchor_ts: Optional[float],
    severity: str,
    evidence: Dict[str, Any],
    allowed_scope: str = "diagnose_only",
) -> Dict[str, Any]:
    return {
        "event_type": event_type,
        "event_id": event_id,
        "anchor_at": _iso(anchor_ts),
        "severity": severity,
        "task_type": TASK_TYPES[event_type],
        "allowed_scope": allowed_scope,
        "evidence": evidence,
    }


def detect_events(
    readings: Iterable[Any],
    now_ts: float,
    memory: Any,
    policy: Optional[EventPolicy] = None,
    state_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return {'events': [...], 'signals': {...}, 'memory': {...}}.

    ``events`` is ordered by priority; the caller should admit at most one of
    them per decision round (see ``rank_events``).  Empty list means
    SKIP_CLOUD (or LOCAL_SAFETY_ONLY, decided upstream).
    """
    policy = policy or EventPolicy()
    memory = _memory_normalized(memory)
    params = state_params if isinstance(state_params, dict) else {}
    signals = compute_signals(readings, now_ts, policy)
    events: List[Dict[str, Any]] = []

    latest = signals["latest_value"]
    slope_2h = signals["slope_2h"]
    slope_30m = signals["slope_30m"]
    drop_2h = signals["drop_2h"]
    range_60m = signals["range_60m"]
    low_water = _as_float(params.get("hard_safety_low")) or _as_float(params.get("target_low"))

    # ---- decline state machine (E1 / E1-accel / E2 anchors) -------------
    decline = memory["decline"]
    is_falling = (
        slope_2h is not None
        and slope_2h <= policy.fall_slope_pp_per_hour
        and (drop_2h is not None and drop_2h >= policy.min_drop_pp_2h)
    )
    forecast_near_low = (
        signals["forecast_6h"] is not None
        and low_water is not None
        and signals["forecast_6h"] <= low_water
    )
    signals["forecast_low_crossed"] = forecast_near_low

    if is_falling:
        if decline is None:
            decline = {
                "segment_id": _segment_id(now_ts),
                "started_at": _iso(now_ts),
                "streak": 0,
                "e1_fired": False,
                "accel_fired": False,
                "min_value": latest,
                "min_at": _iso(now_ts),
            }
        decline["streak"] = int(decline.get("streak", 0)) + 1
        if latest is not None:
            min_value = decline.get("min_value")
            if min_value is None or latest < float(min_value):
                decline["min_value"] = latest
                decline["min_at"] = _iso(now_ts)
        evidence = {
            "slope_2h": _round(slope_2h),
            "drop_2h": _round(drop_2h),
            "range_60m": _round(range_60m),
            "forecast_6h": _round(signals["forecast_6h"]),
            "hard_safety_low": _round(low_water),
            "confirm_streak": decline["streak"],
        }
        if not decline.get("e1_fired") and decline["streak"] >= policy.confirm_periods:
            event_id = "E1:%s" % decline["segment_id"]
            if not _dedupe(memory, event_id, now_ts, policy):
                severity = "high" if forecast_near_low else "medium"
                events.append(_event("falling_start", event_id, parse_timestamp(decline["started_at"]), severity, evidence))
                _remember(memory, event_id, now_ts)
            decline["e1_fired"] = True
        elif decline.get("e1_fired") and not decline.get("accel_fired"):
            delta = signals["slope_delta"]
            if delta is not None and delta <= policy.accel_delta_pp_per_hour:
                event_id = "E1a:%s:%d" % (decline["segment_id"], decline["streak"])
                if not _dedupe(memory, event_id, now_ts, policy):
                    events.append(_event("falling_accel", event_id, now_ts, "medium", dict(evidence, slope_delta=_round(delta))))
                    _remember(memory, event_id, now_ts)
                decline["accel_fired"] = True
    else:
        # Rebound confirmation consumes the decline segment => E2.
        if decline is not None:
            min_value = decline.get("min_value")
            rise = (latest - float(min_value)) if (latest is not None and min_value is not None) else None
            rising = slope_30m is not None and slope_30m > 0.0
            if rise is not None and rise >= policy.valley_confirm_rise_pp and rising:
                decline["rebound_streak"] = int(decline.get("rebound_streak", 0)) + 1
            else:
                decline["rebound_streak"] = 0
            if int(decline.get("rebound_streak", 0)) >= policy.valley_confirm_periods:
                event_id = "E2:%s" % decline["min_at"]
                if not _dedupe(memory, event_id, now_ts, policy):
                    evidence = {
                        "low_value": _round(min_value),
                        "low_at": decline["min_at"],
                        "rebound_pp": _round(rise),
                        "slope_30m": _round(slope_30m),
                    }
                    events.append(_event("valley_confirm", event_id, parse_timestamp(decline["min_at"]), "medium", evidence))
                    _remember(memory, event_id, now_ts)
                decline = None
            elif not rising and slope_2h is not None and slope_2h > policy.plateau_stable_slope:
                # decline simply flattened; drop the segment to avoid stale anchors
                decline = None
    memory["decline"] = decline

    # ---- E3 rise threshold crossing (absolute band, with hysteresis) ----
    target = policy.rise_threshold_high_pp
    if target is not None and latest is not None and slope_30m is not None and slope_30m > 0.0:
        points = normalize_readings(readings)
        recent = _window(points, now_ts, 30 * 60.0)
        band = target - policy.rise_hysteresis_pp
        was_below = any(v < band for _, v in recent)
        if was_below and latest >= target:
            event_id = "E3:%0.1f" % target
            if not _dedupe(memory, event_id, now_ts, policy):
                evidence = {
                    "threshold_pp": target,
                    "hysteresis_pp": policy.rise_hysteresis_pp,
                    "latest_value": _round(latest),
                    "slope_30m": _round(slope_30m),
                }
                events.append(_event("rise_threshold_cross", event_id, now_ts, "low", evidence))
                _remember(memory, event_id, now_ts)

    # ---- E4 unusually long plateau (diagnostic only) ---------------------
    stable = (
        slope_2h is not None
        and abs(slope_2h) <= policy.plateau_stable_slope
        and range_60m is not None
        and range_60m <= policy.plateau_max_range_pp
    )
    if stable:
        if memory["plateau_since"] is None:
            memory["plateau_since"] = now_ts
        duration_hours = (now_ts - float(memory["plateau_since"])) / 3600.0
        if duration_hours >= policy.plateau_hours:
            event_id = "E4:%s" % _iso(memory["plateau_since"]).split("T")[0]
            if not _dedupe(memory, event_id, now_ts, policy):
                evidence = {
                    "plateau_hours": _round(duration_hours, 1),
                    "range_60m": _round(range_60m),
                    "slope_2h": _round(slope_2h),
                }
                events.append(_event("plateau_long", event_id, memory["plateau_since"], "low", evidence))
                _remember(memory, event_id, now_ts)
    else:
        memory["plateau_since"] = None

    return {
        "events": rank_events(events),
        "signals": signals,
        "memory": memory,
    }


def rank_events(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(event: Dict[str, Any]) -> Tuple[int, float]:
        anchor = parse_timestamp(event.get("anchor_at")) or 0.0
        return (EVENT_PRIORITY.get(str(event.get("event_type")), 99), anchor)

    return sorted(events, key=key)


def choose_event(events: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ranked = rank_events(events)
    return ranked[0] if ranked else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _segment_id(now_ts: float) -> str:
    return _iso(now_ts)


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Optional[float], digits: int = 3) -> Optional[float]:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)