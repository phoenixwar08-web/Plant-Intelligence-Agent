"""State V1: a read-only, fact-normalized input for future plant agents."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any


PHASE3_SAFETY_FLAG_KEYS = (
    "pump_active",
    "pending_soak",
    "water_delivery_suspect",
    "reservoir_empty_suspect",
    "low_wet_recovery_suspect",
    "sensor_fault",
    "dynamic_cooldown",
    "predictor_circuit",
    "watering_trigger_guard",
    "recent_response_guard",
    "hard_safety_low_guard",
    "cloud_protection",
)

# A trend is only meaningful when the historical reading is close to its named
# horizon; an older reading must not silently stand in for (for example) 3h ago.
TREND_SAMPLE_TOLERANCE = timedelta(minutes=30)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) and math.isfinite(value):
            return datetime.fromtimestamp(value, timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _age_seconds(reference_at: Any, source_at: Any) -> float | None:
    reference = _time(reference_at)
    source = _time(source_at)
    if reference is None or source is None:
        return None
    delta = (reference - source).total_seconds()
    return round(delta, 1) if delta >= 0 else None


def _latest_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Use the newest timestamped row; preserve source order only as a fallback."""
    dated = [(stamp, row) for row in rows if (stamp := _time(row.get("timestamp"))) is not None]
    return max(dated, key=lambda item: item[0])[1] if dated else (rows[-1] if rows else {})


def _source_age(value: Any) -> float | None:
    age = _number(value)
    return round(age, 1) if age is not None and age >= 0 else None


class StateBuilder:
    """Build an on-demand, read-only State V1 from the latest available facts.

    This class does not schedule or persist snapshots. Episode code may later save
    its returned state at decision, watering, observation, or anomaly boundaries.
    """

    def __init__(self, device_code: str):
        self.device_code = device_code

    def build(self, facts: dict[str, Any]) -> dict[str, Any]:
        facts = facts if isinstance(facts, dict) else {}
        readings = [row for row in facts.get("sensor_readings", []) if isinstance(row, dict)]
        latest = _latest_row(readings)
        parameters = facts.get("parameters") if isinstance(facts.get("parameters"), dict) else {}
        system = facts.get("system_state") if isinstance(facts.get("system_state"), dict) else {}
        environment = facts.get("environment") if isinstance(facts.get("environment"), dict) else {}
        air = environment.get("air") if isinstance(environment.get("air"), dict) else {}
        watering = [row for row in facts.get("watering_history", []) if isinstance(row, dict)]
        last_water = _latest_row(watering)
        observed_at = facts.get("observed_at") or latest.get("timestamp")
        generated_at = facts.get("generated_at") or _iso_now()
        supplied_timestamps = facts.get("source_timestamps") if isinstance(facts.get("source_timestamps"), dict) else {}
        source_timestamps = {
            "soil": latest.get("timestamp"),
            "air": air.get("observed_at") or supplied_timestamps.get("air"),
            "phase3_state": supplied_timestamps.get("phase3_state"),
            "watering_history": last_water.get("timestamp"),
        }
        supplied_ages = facts.get("source_ages") if isinstance(facts.get("source_ages"), dict) else {}
        data_quality = {
            "soil_age_sec": _age_seconds(generated_at, source_timestamps["soil"]),
            "air_age_sec": _age_seconds(generated_at, source_timestamps["air"]),
            "phase3_state_age_sec": _age_seconds(generated_at, source_timestamps["phase3_state"]),
            "watering_history_age_sec": _age_seconds(generated_at, source_timestamps["watering_history"]),
        }
        for key in ("soil_age_sec", "air_age_sec", "phase3_state_age_sec", "watering_history_age_sec"):
            if data_quality[key] is None:
                data_quality[key] = _source_age(supplied_ages.get(key))
        return {
            "schema_version": "state.v1",
            "device_code": self.device_code,
            "observed_at": observed_at,
            "generated_at": generated_at,
            "source_timestamps": source_timestamps,
            "data_quality": data_quality,
            "soil": {
                "humidity_percent": _number(latest.get("humidity")),
                "temperature_c": _number(latest.get("temperature")),
                "ec_raw": _number(latest.get("ec_raw")),
                "light_lux": _number(latest.get("lux")),
            },
            "air": {"humidity_percent": _number(air.get("humidity_percent")), "temperature_c": _number(air.get("temperature_c"))},
            "trends": self._trends(readings, observed_at),
            "irrigation": {
                "pump_active": bool(system.get("pump_active")),
                "last_water_at": last_water.get("timestamp"),
                "last_water_sec": _number(last_water.get("water_sec")),
                "total_cycles": system.get("pump_total_cycles"),
                "total_water_sec": _number(system.get("total_water_sec_dispensed")),
                # Trial records are not post-watering feedback results. Keep the
                # interface stable but do not mislabel historical requests as evidence.
                "recent_results": [],
            },
            "safety": {
                "field_capacity": _number(parameters.get("FC")),
                "target_low": _number(parameters.get("TARGET_LOW")),
                "hard_safety_low": _number(parameters.get("HARD_SAFETY_LOW")),
                "kp": _number(parameters.get("K_P")),
                "flags": {
                    key: system[key]
                    for key in PHASE3_SAFETY_FLAG_KEYS
                    if key in system
                },
            },
            "vision": {"image_quality": None, "leaf_droop": None, "leaf_spread": None, "wilting": None, "yellowing": None, "change_vs_previous": None, "confidence": None},
            "extensions": {"pot_weight_g": None, "bottom_water_level": None, "runoff_detected": None},
            "fact_sources": {
                "soil": "sensor_readings",
                "air": air.get("source") or "environment.air",
                "safety": "parameters+system_state",
                "irrigation": "watering_history+system_state",
                "data_quality": "source_timestamps+source_ages",
                "vision": "not_collected",
            },
        }

    def build_from_health_snapshot(self, snapshot: dict[str, Any], parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Adapt the existing soil telemetry health snapshot without side effects."""
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        environment = snapshot.get("environment") if isinstance(snapshot.get("environment"), dict) else {}
        air = environment.get("air") if isinstance(environment.get("air"), dict) else {}
        state_file = snapshot.get("state_file") if isinstance(snapshot.get("state_file"), dict) else {}
        return self.build({
            "observed_at": snapshot.get("observed_at"),
            "sensor_readings": snapshot.get("sensor_readings") or [],
            "system_state": snapshot.get("system_state") or {},
            "watering_history": snapshot.get("watering_history") or [],
            "environment": environment,
            "parameters": parameters or {},
            "source_timestamps": snapshot.get("source_timestamps") or {},
            "source_ages": {
                "air_age_sec": air.get("age_seconds"),
                "phase3_state_age_sec": state_file.get("age_seconds"),
            },
        })

    def _trends(self, readings: list[dict[str, Any]], observed_at: Any) -> dict[str, float | None]:
        """Return current humidity minus the reading at each named horizon.

        Values are percentage-point deltas. A reading must be within 30 minutes
        of the N-hours-ago target; otherwise that horizon is unknown (``None``).
        """
        end = _time(observed_at)
        dated = sorted(
            [(stamp, row) for row in readings if (stamp := _time(row.get("timestamp"))) is not None and (end is None or stamp <= end)],
            key=lambda item: item[0],
        )
        latest = _number(dated[-1][1].get("humidity")) if dated else None
        result = {}
        for hours in (1, 3, 6):
            baseline = None
            if end is not None:
                target = end - timedelta(hours=hours)
                candidates = [
                    (stamp, row)
                    for stamp, row in dated
                    if abs(stamp - target) <= TREND_SAMPLE_TOLERANCE
                ]
                if candidates:
                    baseline = _number(min(candidates, key=lambda item: abs(item[0] - target))[1].get("humidity"))
            result[f"humidity_{hours}h"] = round(latest - baseline, 3) if latest is not None and baseline is not None else None
        return result
