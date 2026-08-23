"""Build one human-facing CSV that summarizes valid hourly prediction results."""

import csv
import math
import os
import tempfile
from bisect import bisect_right
from collections import defaultdict, deque
from datetime import datetime, timedelta


H12_FORECAST_HOUR = 12
TIME_FORMAT = "%Y/%m/%d %H:%M:%S"
SOURCE_HUMIDITY_LOOKBACK_SECONDS = 1800


FIELDS = (
    "source_time", "source_humidity", "model", "forecast_hour", "target_time",
    "actual_time", "actual_time_offset_minutes",
    "predicted_humidity", "actual_humidity", "absolute_error",
    "effect_grade", "model_sample_count", "model_mae",
    "model_rmse", "recent_20_mae", "trained_at",
)


def _grade(error):
    if error <= 1.0:
        return "excellent"
    if error <= 3.0:
        return "usable"
    if error <= 5.0:
        return "needs_training"
    return "poor"


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_source_humidity_index(path):
    if not os.path.exists(path):
        return [], []
    points = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 4:
                continue
            try:
                timestamp = datetime.strptime(row[0].strip(), TIME_FORMAT).timestamp()
                humidity = float(row[3])
            except (TypeError, ValueError):
                continue
            points.append((timestamp, humidity))
    points.sort(key=lambda item: item[0])
    return [item[0] for item in points], [item[1] for item in points]


def _source_humidity(source_time, timestamps, humidities):
    if not timestamps:
        return ""
    source_timestamp = datetime.strptime(source_time, TIME_FORMAT).timestamp()
    index = bisect_right(timestamps, source_timestamp) - 1
    if index < 0:
        return ""
    if source_timestamp - timestamps[index] > SOURCE_HUMIDITY_LOOKBACK_SECONDS:
        return ""
    return round(humidities[index], 4)


def _time_alignment(source_time, actual_time, hour):
    source = datetime.strptime(source_time, TIME_FORMAT)
    actual = datetime.strptime(actual_time, TIME_FORMAT)
    target = source + timedelta(hours=hour)
    offset_minutes = (actual - target).total_seconds() / 60.0
    return target.strftime(TIME_FORMAT), round(offset_minutes, 2)


def _annotate(valid):
    totals = defaultdict(list)
    recent = defaultdict(lambda: deque(maxlen=20))
    output = []
    for row in valid:
        model = row["model"]
        totals[model].append(row["absolute_error"])
        recent[model].append(row["absolute_error"])
        errors = totals[model]
        output.append({
            **row,
            "effect_grade": _grade(row["absolute_error"]),
            "model_sample_count": len(errors),
            "model_mae": round(sum(errors) / len(errors), 4),
            "model_rmse": round(math.sqrt(sum(value * value for value in errors) / len(errors)), 4),
            "recent_20_mae": round(sum(recent[model]) / len(recent[model]), 4),
        })
    return output


def _write(path, rows):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path), suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def build_prediction_effect_report(config):
    paths = config["paths"]
    max_offset_seconds = float(config.get("offline_training", {}).get(
        "target_tolerance_seconds", 1800
    ))
    source_timestamps, source_humidities = _read_source_humidity_index(paths["soil_csv"])
    source_rows = _read(paths["natural_backprop_log"]) + _read(paths["online_backprop_log"])
    valid = []
    for row in source_rows:
        try:
            error = float(row["absolute_error"])
            predicted = float(row["predicted_humidity"])
            actual = float(row["actual_humidity"])
            hour = int(float(row["trained_hour"]))
            target_time, offset_minutes = _time_alignment(
                row["source_time"], row["actual_time"], hour
            )
            source_humidity = _source_humidity(
                row["source_time"], source_timestamps, source_humidities
            )
        except (KeyError, TypeError, ValueError):
            continue
        if abs(offset_minutes) * 60.0 > max_offset_seconds:
            continue
        valid.append({
            "source_time": row["source_time"], "source_humidity": source_humidity,
            "model": row.get("model", ""),
            "forecast_hour": hour, "target_time": target_time,
            "actual_time": row["actual_time"],
            "actual_time_offset_minutes": offset_minutes,
            "predicted_humidity": predicted, "actual_humidity": actual,
            "absolute_error": error, "trained_at": row.get("trained_at", ""),
        })
    valid.sort(key=lambda row: (row["source_time"], row["model"], row["forecast_hour"]))

    output = _annotate(valid)
    output_12h = _annotate([row for row in valid if row["forecast_hour"] == H12_FORECAST_HOUR])

    path = paths.get(
        "prediction_effect_report", "/root/water/wyc_training/prediction_effect.csv"
    )
    path_12h = paths.get(
        "prediction_effect_12h_report", "/root/water/wyc_training/prediction_effect_12h.csv"
    )
    _write(path, output)
    _write(path_12h, output_12h)
    return len(output)
