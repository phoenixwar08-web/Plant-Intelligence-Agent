#!/usr/bin/env python3
"""Build a deterministic 24-hour trend summary for soil2."""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from plant_state_builder import parse_float, parse_int, run_gsql


OUTPUT_DIR = BASE_DIR / "outputs"
VISION_HISTORY_PATH = OUTPUT_DIR / "vision" / "history" / "soil2.jsonl"
DEVICE_CODE = "soil2"
TIMEZONE = ZoneInfo("Asia/Shanghai")
WINDOW_HOURS = 24
HUMIDITY_STABLE_THRESHOLD = 1.0
YELLOW_LEAF_STABLE_THRESHOLD = 0.01
HEALTH_SCORES = {"normal": 0, "warning": 1, "critical": 2}


def normalize_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # openGauss renders UTC offsets as `+00`; make that legacy database
        # form explicit before ISO parsing, without rewriting stored facts.
        if len(text) >= 3 and text[-3] in "+-" and text[-2:].isdigit():
            text += ":00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TIMEZONE)
    return parsed.astimezone(TIMEZONE)


def iso_datetime(value: Any) -> Optional[str]:
    parsed = normalize_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def sql_timestamptz(value: datetime) -> str:
    """Render a fixed, offset-bearing SQL timestamp literal for trend bounds."""
    normalized = normalize_datetime(value)
    if normalized is None:
        raise ValueError("trend window requires a valid timestamp")
    return "'" + normalized.isoformat() + "'::timestamptz"


def valid_device(device_code: str) -> None:
    if device_code != DEVICE_CODE:
        raise ValueError(f"only {DEVICE_CODE} is supported")


def resolve_now(now: Optional[datetime] = None) -> datetime:
    return normalize_datetime(now) or datetime.now(TIMEZONE)


def window_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    end = resolve_now(now)
    return end - timedelta(hours=WINDOW_HOURS), end


def fetch_sensor_readings(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    start_sql = sql_timestamptz(start)
    end_sql = sql_timestamptz(end)
    sql = f"""
SELECT recv_time, humidity
FROM soil_sensor_readings
WHERE device_code = '{DEVICE_CODE}'
  AND (recv_time AT TIME ZONE 'Asia/Shanghai') >= {start_sql}
  AND (recv_time AT TIME ZONE 'Asia/Shanghai') <= {end_sql}
ORDER BY recv_time ASC;
"""
    rows = run_gsql(sql)
    return [
        {"recv_time": row[0], "humidity": row[1]}
        for row in rows
        if len(row) >= 2
    ]


def fetch_irrigation_events(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    start_sql = sql_timestamptz(start)
    end_sql = sql_timestamptz(end)
    sql = f"""
SELECT id, device_code, command_time, water_sec, reason, source, status
FROM irrigation_events
WHERE device_code = '{DEVICE_CODE}'
  AND (command_time AT TIME ZONE 'Asia/Shanghai') >= {start_sql}
  AND (command_time AT TIME ZONE 'Asia/Shanghai') <= {end_sql}
  AND source = 'mqtt'
  AND reason = 'mqtt_watering_event'
  AND status = 'issued'
ORDER BY command_time ASC;
"""
    rows = run_gsql(sql)
    events = []
    for row in rows:
        if len(row) < 7:
            continue
        events.append(
            {
                "id": parse_int(row[0]),
                "device_code": row[1],
                "command_time": row[2],
                "water_sec": parse_float(row[3]),
                "reason": row[4] or None,
                "source": row[5] or None,
                "status": row[6] or None,
            }
        )
    return events


def fetch_human_watering_events(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Read manual-watering facts separately from MQTT irrigation events."""
    start_sql = sql_timestamptz(start)
    end_sql = sql_timestamptz(end)
    sql = f"""
SELECT id, device_code, occurred_at, duration_sec, volume_ml, note, event_type, source, confirmation_status, trust_status
FROM human_events
WHERE device_code = '{DEVICE_CODE}'
  AND event_type = 'manual_watering'
  AND confirmation_status = 'confirmed'
  AND trust_status IN ('attested', 'legacy_verified')
  AND occurred_at >= {start_sql}
  AND occurred_at <= {end_sql}
ORDER BY occurred_at ASC;
"""
    rows = run_gsql(sql)
    events = []
    for row in rows:
        if len(row) < 10:
            continue
        events.append({
            "id": parse_int(row[0]),
            "device_code": row[1],
            "occurred_at": row[2],
            "duration_sec": parse_float(row[3]),
            "volume_ml": parse_float(row[4]),
            "note": row[5] or None,
            "event_type": row[6] or None,
            "source": row[7] or None,
            "confirmation_status": row[8] or None,
            "trust_status": row[9] or None,
        })
    return events


def load_visual_observations() -> List[Dict[str, Any]]:
    if not VISION_HISTORY_PATH.exists():
        return []

    observations = []
    with VISION_HISTORY_PATH.open("r", encoding="utf-8") as history_file:
        for line in history_file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                observations.append(record)
    return observations


def empty_sensor_result() -> Dict[str, Any]:
    return {
        "sample_count": 0,
        "invalid_sample_count": 0,
        "start_value": None,
        "end_value": None,
        "change": None,
        "min": None,
        "max": None,
        "trend": "insufficient_data",
    }


def analyze_sensor_readings(readings: Iterable[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    start, end = window_bounds(now)
    valid = []
    invalid_count = 0
    for reading in readings:
        observed_at = normalize_datetime(reading.get("recv_time"))
        humidity = parse_float(str(reading.get("humidity", "")))
        if (
            observed_at is None
            or observed_at < start
            or observed_at > end
            or humidity is None
            or not 0 <= humidity <= 100
        ):
            invalid_count += 1
            continue
        valid.append((observed_at, humidity))

    valid.sort(key=lambda item: item[0])
    result = empty_sensor_result()
    result["sample_count"] = len(valid)
    result["invalid_sample_count"] = invalid_count
    if len(valid) < 2:
        return result

    first_hour_end = start + timedelta(hours=1)
    last_hour_start = end - timedelta(hours=1)
    start_values = [value for timestamp, value in valid if timestamp <= first_hour_end]
    end_values = [value for timestamp, value in valid if timestamp >= last_hour_start]
    if not start_values or not end_values:
        return result

    start_value = float(median(start_values))
    end_value = float(median(end_values))
    change = end_value - start_value
    result.update(
        {
            "start_value": round(start_value, 3),
            "end_value": round(end_value, 3),
            "change": round(change, 3),
            "min": round(min(value for _, value in valid), 3),
            "max": round(max(value for _, value in valid), 3),
            "trend": (
                "stable"
                if abs(change) < HUMIDITY_STABLE_THRESHOLD
                else "increasing"
                if change > 0
                else "decreasing"
            ),
        }
    )
    return result


def analyze_irrigation_events(
    events: Iterable[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    start, end = window_bounds(now)
    deduplicated = {}
    for event in events:
        event_id = event.get("id")
        observed_at = normalize_datetime(event.get("command_time"))
        water_sec = event.get("water_sec")
        if not isinstance(water_sec, (int, float)):
            water_sec = parse_float(str(water_sec or ""))
        if (
            event_id is None
            or event.get("device_code") != DEVICE_CODE
            or event.get("source") != "mqtt"
            or event.get("reason") != "mqtt_watering_event"
            or event.get("status") != "issued"
            or observed_at is None
            or observed_at < start
            or observed_at > end
            or water_sec is None
            or water_sec <= 0
        ):
            continue
        deduplicated[event_id] = (observed_at, float(water_sec))

    values = sorted(deduplicated.values(), key=lambda item: item[0])
    return {
        "count": len(values),
        "total_water_sec": round(sum(value for _, value in values), 3),
        "last_irrigation_at": values[-1][0].isoformat() if values else None,
    }


def analyze_human_watering_events(
    events: Iterable[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    start, end = window_bounds(now)
    deduplicated = {}
    for event in events:
        event_id = event.get("id")
        occurred_at = normalize_datetime(event.get("occurred_at"))
        if (
            event_id is None
            or event.get("device_code") != DEVICE_CODE
            or event.get("event_type") != "manual_watering"
            or event.get("confirmation_status", "confirmed") != "confirmed"
            or event.get("trust_status") not in {"attested", "legacy_verified"}
            or occurred_at is None
            or occurred_at < start
            or occurred_at > end
        ):
            continue
        duration = event.get("duration_sec")
        volume = event.get("volume_ml")
        duration = duration if isinstance(duration, (int, float)) else parse_float(str(duration or ""))
        volume = volume if isinstance(volume, (int, float)) else parse_float(str(volume or ""))
        deduplicated[event_id] = (occurred_at, duration, volume, event.get("note"))

    values = sorted(deduplicated.values(), key=lambda item: item[0])
    known_durations = [float(duration) for _, duration, _, _ in values if isinstance(duration, (int, float)) and duration > 0]
    known_volumes = [float(volume) for _, _, volume, _ in values if isinstance(volume, (int, float)) and volume > 0]
    latest_note = values[-1][3] if values else None
    return {
        "count": len(values),
        "known_duration_sample_count": len(known_durations),
        "total_duration_sec": round(sum(known_durations), 3) if known_durations else None,
        "known_volume_sample_count": len(known_volumes),
        "total_volume_ml": round(sum(known_volumes), 3) if known_volumes else None,
        "last_human_watering_at": values[-1][0].isoformat() if values else None,
        "last_note": latest_note,
    }


def empty_visual_result() -> Dict[str, Any]:
    return {
        "observation_count": 0,
        "invalid_record_count": 0,
        "yellow_leaf_ratio_sample_count": 0,
        "yellow_leaf_ratio_start": None,
        "yellow_leaf_ratio_end": None,
        "yellow_leaf_ratio_change": None,
        "yellow_leaf_ratio_trend": "insufficient_data",
        "health_sample_count": 0,
        "health_start": None,
        "health_end": None,
        "health_trend": "insufficient_data",
    }


def analyze_visual_observations(
    observations: Iterable[Dict[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    start, end = window_bounds(now)
    valid = []
    invalid_count = 0
    for observation in observations:
        observed_at = normalize_datetime(observation.get("observed_at"))
        visual = observation.get("visual")
        if (
            observation.get("device_code") != DEVICE_CODE
            or observed_at is None
            or observed_at < start
            or observed_at > end
            or not isinstance(visual, dict)
        ):
            invalid_count += 1
            continue
        valid.append((observed_at, visual))

    valid.sort(key=lambda item: item[0])
    result = empty_visual_result()
    result["observation_count"] = len(valid)
    result["invalid_record_count"] = invalid_count

    yellow = []
    health = []
    for observed_at, visual in valid:
        ratio = visual.get("yellow_leaf_ratio")
        if not isinstance(ratio, (int, float)):
            ratio = parse_float(str(ratio or ""))
        if ratio is not None and 0 <= ratio <= 1:
            yellow.append((observed_at, float(ratio)))

        health_value = visual.get("plant_health")
        normalized_health = (
            health_value.strip().lower()
            if isinstance(health_value, str)
            else None
        )
        if normalized_health in HEALTH_SCORES:
            health.append((observed_at, normalized_health))

    result["yellow_leaf_ratio_sample_count"] = len(yellow)
    if len(yellow) >= 2:
        start_value = yellow[0][1]
        end_value = yellow[-1][1]
        change = end_value - start_value
        result.update(
            {
                "yellow_leaf_ratio_start": round(start_value, 4),
                "yellow_leaf_ratio_end": round(end_value, 4),
                "yellow_leaf_ratio_change": round(change, 4),
                "yellow_leaf_ratio_trend": (
                    "stable"
                    if abs(change) < YELLOW_LEAF_STABLE_THRESHOLD
                    else "increasing"
                    if change > 0
                    else "decreasing"
                ),
            }
        )

    result["health_sample_count"] = len(health)
    if len(health) >= 2:
        start_health = health[0][1]
        end_health = health[-1][1]
        score_change = HEALTH_SCORES[end_health] - HEALTH_SCORES[start_health]
        result.update(
            {
                "health_start": start_health,
                "health_end": end_health,
                "health_trend": (
                    "stable"
                    if score_change == 0
                    else "worsening"
                    if score_change > 0
                    else "improving"
                ),
            }
        )
    return result


def build_trend(device_code: str = DEVICE_CODE, now: Optional[datetime] = None) -> Dict[str, Any]:
    valid_device(device_code)
    start, end = window_bounds(now)
    sensor_result = empty_sensor_result()
    irrigation_result = {"count": None, "total_water_sec": None, "last_irrigation_at": None}
    human_watering_result = {
        "count": None,
        "known_duration_sample_count": None,
        "total_duration_sec": None,
        "known_volume_sample_count": None,
        "total_volume_ml": None,
        "last_human_watering_at": None,
        "last_note": None,
    }
    visual_result = empty_visual_result()
    quality = {"sensor": "unavailable", "irrigation": "unavailable", "human_watering": "unavailable", "visual": "unavailable"}

    try:
        sensor_result = analyze_sensor_readings(fetch_sensor_readings(start, end), end)
        quality["sensor"] = "ok" if sensor_result["trend"] != "insufficient_data" else "insufficient_data"
    except Exception:
        pass

    try:
        irrigation_result = analyze_irrigation_events(fetch_irrigation_events(start, end), end)
        quality["irrigation"] = "ok"
    except Exception:
        pass

    try:
        human_watering_result = analyze_human_watering_events(fetch_human_watering_events(start, end), end)
        quality["human_watering"] = "ok"
    except Exception:
        pass

    try:
        visual_result = analyze_visual_observations(load_visual_observations(), end)
        quality["visual"] = "ok" if visual_result["observation_count"] >= 2 else "insufficient_data"
    except Exception:
        pass

    return {
        "device_code": device_code,
        "generated_at": end.isoformat(),
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hours": WINDOW_HOURS,
            "timezone": "Asia/Shanghai",
        },
        "soil_humidity": sensor_result,
        "irrigation": irrigation_result,
        "human_watering": human_watering_result,
        "visual": visual_result,
        "data_quality": quality,
    }


def write_trend(device_code: str = DEVICE_CODE, now: Optional[datetime] = None) -> Path:
    trend = build_trend(device_code, now)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"{device_code}_trend.json"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(OUTPUT_DIR),
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(trend, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(output_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 soil2 最近24小时趋势")
    parser.add_argument("device_code", nargs="?", default=DEVICE_CODE)
    args = parser.parse_args()
    try:
        output_path = write_trend(args.device_code)
    except ValueError as error:
        parser.error(str(error))
    print(output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
