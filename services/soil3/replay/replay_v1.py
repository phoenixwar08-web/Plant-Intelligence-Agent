"""Build reproducible state.v1 samples from facts at or before a cutoff."""
from __future__ import annotations

import copy
from bisect import bisect_right
import hashlib
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.soil3.state.state_v1 import StateBuilder


REQUIRED_SENSOR_FIELDS = ("humidity", "temperature", "ec_raw")
MIN_PLAUSIBLE_TIMESTAMP = datetime(2020, 1, 1, tzinfo=timezone.utc)
MAX_PLAUSIBLE_TIMESTAMP = datetime(2100, 1, 1, tzinfo=timezone.utc)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any, source_zone: ZoneInfo) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            parsed = datetime.fromtimestamp(float(value), timezone.utc)
        elif text.replace(".", "", 1).isdigit():
            parsed = datetime.fromtimestamp(float(text), timezone.utc)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=source_zone)
        parsed = parsed.astimezone(timezone.utc)
        if not MIN_PLAUSIBLE_TIMESTAMP <= parsed < MAX_PLAUSIBLE_TIMESTAMP:
            return None
        return parsed
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_timestamp(record: dict[str, Any]) -> Any:
    return record.get("timestamp", record.get("observed_at"))


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReplayBuilder:
    """Construct one read-only replay sample without mutating source records."""

    def __init__(
        self,
        device_code: str = "soil3",
        *,
        source_timezone: str = "Asia/Shanghai",
        max_gap_seconds: float = 900.0,
        response_window_seconds: float = 14400.0,
    ) -> None:
        if device_code != "soil3":
            raise ValueError("replay.v1 currently supports soil3 only")
        self.device_code = device_code
        self.source_zone = ZoneInfo(source_timezone)
        self.max_gap_seconds = float(max_gap_seconds)
        self.response_window_seconds = float(response_window_seconds)

    def build(
        self,
        *,
        replay_at: Any,
        sensor_readings: list[Any],
        watering_history: list[Any] | None = None,
        state_history: list[Any] | None = None,
        parameter_history: list[Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        cutoff = _parse_time(replay_at, self.source_zone)
        if cutoff is None:
            raise ValueError("replay_at must be a valid timestamp")

        source_inputs = {
            "sensor": copy.deepcopy(sensor_readings or []),
            "watering": copy.deepcopy(watering_history or []),
            "state": copy.deepcopy(state_history or []),
            "parameters": copy.deepcopy(parameter_history or []),
        }
        selected: dict[str, list[dict[str, Any]]] = {}
        timestamp_quality: dict[str, dict[str, Any]] = {}
        for source, records in source_inputs.items():
            selected[source], timestamp_quality[source] = self._select(records, cutoff)

        sensors = selected["sensor"]
        watering = selected["watering"]
        system_state, system_state_at = self._latest_value(selected["state"])
        parameters, parameters_at = self._latest_value(selected["parameters"])
        latest_sensor_at = sensors[-1]["timestamp"] if sensors else None
        latest_air = self._latest_air(sensors)

        facts = {
            "observed_at": latest_sensor_at,
            "generated_at": _iso(cutoff),
            "sensor_readings": sensors,
            "system_state": system_state,
            "watering_history": watering,
            "parameters": parameters,
            "environment": {"air": latest_air},
            "source_timestamps": {
                "phase3_state": system_state_at,
                "air": latest_air.get("observed_at"),
            },
        }
        state = StateBuilder(self.device_code).build(facts)
        continuity = self._continuity(sensors, cutoff)
        missing_fields = self._missing_sensor_fields(sensors)
        watering_relationships = self._watering_relationships(watering, sensors, cutoff)
        limitations = self._limitations(selected)
        status = self._quality_status(
            sensors=sensors,
            timestamp_quality=timestamp_quality,
            continuity=continuity,
            missing_fields=missing_fields,
            watering_relationships=watering_relationships,
            limitations=limitations,
        )

        quality_source_summary = {
            source: {
                "included_at_or_before_cutoff": len(selected[source]),
                "excluded_after_cutoff": timestamp_quality[source]["excluded_after_cutoff"],
                "invalid_or_missing_timestamp": timestamp_quality[source]["invalid_or_missing_timestamp"],
            }
            for source in selected
        }
        sample_source_summary = {
            source: {"included_at_or_before_cutoff": len(selected[source])}
            for source in selected
        }
        sample_basis = {
            "device_code": self.device_code,
            "replay_at": _iso(cutoff),
            "state": state,
            "source_summary": sample_source_summary,
            "fact_digest": _canonical_hash(selected),
        }
        sample = {
            "schema_version": "replay_sample.v1",
            "sample_id": "replay-" + _canonical_hash(sample_basis)[:24],
            **sample_basis,
            "fact_window": {
                "first_sensor_at": sensors[0]["timestamp"] if sensors else None,
                "last_sensor_at": latest_sensor_at,
                "sensor_count": len(sensors),
                "watering_count": len(watering),
            },
            "source_policy": {
                "cutoff_inclusive": True,
                "future_data_excluded": True,
                "source_timezone": str(self.source_zone),
            },
        }
        quality = {
            "schema_version": "replay_quality.v1",
            "sample_id": sample["sample_id"],
            "device_code": self.device_code,
            "replay_at": _iso(cutoff),
            "status": status,
            "source_summary": quality_source_summary,
            "timestamp_quality": timestamp_quality,
            "continuity": continuity,
            "missing_sensor_fields": missing_fields,
            "watering_sensor_relationships": watering_relationships,
            "limitations": limitations,
        }
        return sample, quality

    def _select(
        self,
        records: list[Any],
        cutoff: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        included: list[tuple[datetime, dict[str, Any]]] = []
        invalid = 0
        excluded_future = 0
        regressions = 0
        duplicates = 0
        previous: datetime | None = None
        seen: set[str] = set()
        for raw in records:
            if not isinstance(raw, dict):
                invalid += 1
                continue
            timestamp = _parse_time(_record_timestamp(raw), self.source_zone)
            if timestamp is None:
                invalid += 1
                continue
            if previous is not None and timestamp < previous:
                regressions += 1
            previous = timestamp
            normalized = dict(raw)
            normalized["timestamp"] = _iso(timestamp)
            key = normalized["timestamp"] + "|" + _canonical_hash(normalized)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if timestamp > cutoff:
                excluded_future += 1
                continue
            included.append((timestamp, normalized))
        included.sort(key=lambda item: item[0])
        return [record for _, record in included], {
            "total_input_records": len(records),
            "included_at_or_before_cutoff": len(included),
            "excluded_after_cutoff": excluded_future,
            "invalid_or_missing_timestamp": invalid,
            "duplicate_records": duplicates,
            "source_order_regressions": regressions,
        }

    @staticmethod
    def _latest_value(records: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
        if not records:
            return {}, None
        latest = records[-1]
        value = latest.get("value")
        if not isinstance(value, dict):
            value = {key: item for key, item in latest.items() if key != "timestamp"}
        return value, latest.get("timestamp")

    @staticmethod
    def _latest_air(sensors: list[dict[str, Any]]) -> dict[str, Any]:
        for row in reversed(sensors):
            humidity = _number(row.get("air_humidity", row.get("air_humidity_percent")))
            temperature = _number(row.get("air_temperature", row.get("air_temperature_c")))
            if humidity is not None or temperature is not None:
                return {
                    "observed_at": row.get("timestamp"),
                    "humidity_percent": humidity,
                    "temperature_c": temperature,
                    "source": "historical_sensor_readings",
                }
        return {}

    def _continuity(self, sensors: list[dict[str, Any]], cutoff: datetime) -> dict[str, Any]:
        stamps = [_parse_time(row.get("timestamp"), self.source_zone) for row in sensors]
        valid = [stamp for stamp in stamps if stamp is not None]
        intervals = [
            (current - previous).total_seconds()
            for previous, current in zip(valid, valid[1:])
        ]
        expected = statistics.median(intervals) if intervals else None
        threshold = max(
            self.max_gap_seconds,
            (expected * 2.5) if expected is not None else self.max_gap_seconds,
        )
        gaps = []
        for previous, current in zip(valid, valid[1:]):
            duration = (current - previous).total_seconds()
            if duration > threshold:
                gaps.append({
                    "after": _iso(previous),
                    "before": _iso(current),
                    "duration_seconds": round(duration, 3),
                })
        tail_gap = (cutoff - valid[-1]).total_seconds() if valid else None
        return {
            "expected_interval_seconds": round(expected, 3) if expected is not None else None,
            "gap_threshold_seconds": round(threshold, 3),
            "gaps": gaps,
            "tail_gap_seconds": round(tail_gap, 3) if tail_gap is not None else None,
            "tail_gap_exceeds_threshold": tail_gap is None or tail_gap > threshold,
        }

    @staticmethod
    def _missing_sensor_fields(sensors: list[dict[str, Any]]) -> dict[str, int]:
        return {
            field: sum(1 for row in sensors if _number(row.get(field)) is None)
            for field in REQUIRED_SENSOR_FIELDS
        }

    def _watering_relationships(
        self,
        watering: list[dict[str, Any]],
        sensors: list[dict[str, Any]],
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        sensor_rows = sorted([
            (stamp, row)
            for row in sensors
            if (stamp := _parse_time(row.get("timestamp"), self.source_zone)) is not None
        ], key=lambda item: item[0])
        sensor_times = [item[0] for item in sensor_rows]
        relationships = []
        for event in watering:
            event_at = _parse_time(event.get("timestamp"), self.source_zone)
            if event_at is None:
                continue
            response_end = min(cutoff, event_at + timedelta(seconds=self.response_window_seconds))
            insertion = bisect_right(sensor_times, event_at)
            before_row = sensor_rows[insertion - 1] if insertion > 0 else None
            after_row = (
                sensor_rows[insertion]
                if insertion < len(sensor_rows) and sensor_rows[insertion][0] <= response_end
                else None
            )
            before_humidity = _number(before_row[1].get("humidity")) if before_row else None
            after_humidity = _number(after_row[1].get("humidity")) if after_row else None
            if before_row is None:
                status = "missing_before_sensor"
            elif after_row is None:
                status = "missing_after_sensor"
            elif before_humidity is None or after_humidity is None:
                status = "missing_humidity"
            else:
                status = "matched"
            relationships.append({
                "watering_at": _iso(event_at),
                "water_sec": _number(event.get("water_sec")),
                "status": status,
                "before_sensor_at": _iso(before_row[0]) if before_row else None,
                "after_sensor_at": _iso(after_row[0]) if after_row else None,
                "before_humidity": before_humidity,
                "after_humidity": after_humidity,
                "humidity_delta": (
                    round(after_humidity - before_humidity, 3)
                    if before_humidity is not None and after_humidity is not None
                    else None
                ),
            })
        return relationships

    @staticmethod
    def _limitations(selected: dict[str, list[dict[str, Any]]]) -> list[str]:
        limitations = []
        if not selected["state"]:
            limitations.append("phase3_state_history_unavailable")
        if not selected["parameters"]:
            limitations.append("parameter_history_unavailable")
        if not selected["watering"]:
            limitations.append("watering_history_unavailable")
        return limitations

    @staticmethod
    def _quality_status(
        *,
        sensors: list[dict[str, Any]],
        timestamp_quality: dict[str, dict[str, Any]],
        continuity: dict[str, Any],
        missing_fields: dict[str, int],
        watering_relationships: list[dict[str, Any]],
        limitations: list[str],
    ) -> str:
        if not sensors:
            return "unusable"
        has_timestamp_issue = any(
            details["invalid_or_missing_timestamp"]
            or details["duplicate_records"]
            or details["source_order_regressions"]
            for details in timestamp_quality.values()
        )
        has_relationship_issue = any(
            item["status"] != "matched" for item in watering_relationships
        )
        if (
            has_timestamp_issue
            or continuity["gaps"]
            or continuity["tail_gap_exceeds_threshold"]
            or any(missing_fields.values())
            or has_relationship_issue
            or limitations
        ):
            return "warning"
        return "good"
