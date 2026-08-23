"""Build the dedicated 12-hour effect CSV for forecasts selected by Phase3."""

import csv
import json
import math
import os
import tempfile
from bisect import bisect_left
from collections import defaultdict, deque
from datetime import datetime


FIELDS = (
    "request_id", "device_code", "source", "source_time", "source_humidity",
    "selected_label", "water_sec", "zone", "model", "forecast_hour",
    "target_time", "actual_time", "actual_time_offset_minutes",
    "predicted_humidity", "actual_humidity", "absolute_error",
    "raw_predicted_humidity", "slope_corrected_humidity", "recent_slope",
    "observe_guard_status", "observe_guard_max_drop_12h",
    "effect_grade", "model_sample_count", "model_mae", "model_rmse",
    "recent_20_mae", "status", "training_status", "trained_at",
)


def _format_time(timestamp):
    return datetime.fromtimestamp(float(timestamp)).strftime("%Y/%m/%d %H:%M:%S")


def _grade(error):
    if error <= 1.0:
        return "excellent"
    if error <= 3.0:
        return "usable"
    if error <= 5.0:
        return "needs_training"
    return "poor"


def _observe_labels(config):
    retention = config.get("phase3_response_retention", {})
    return set(retention.get("observe_labels", [
        "style_observe",
        "style_drydown_observe",
        "style_wet_hold_observe",
        "trigger_guard_observe",
    ]))


def _source_for_item(item, config):
    label = str(item.get("selected_label") or "")
    water_sec = item.get("water_sec")
    try:
        water_value = float(water_sec)
    except (TypeError, ValueError):
        water_value = None
    if label in _observe_labels(config) or water_value == 0.0:
        return "phase3_observe"
    return "phase3_response"


def _load_queue(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_queue(path, rows):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path), suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _prune_queue(queue, config, latest, tolerance):
    retention = config.get("phase3_response_retention", {})
    max_records = int(retention.get("queue_max_records", 500))
    retention_days = float(retention.get("queue_retention_days", 7))
    cutoff = float(latest or 0.0) - retention_days * 86400.0
    valid = []
    for item in queue:
        source_timestamp = item.get("source_timestamp")
        if not isinstance(source_timestamp, (int, float)):
            continue
        pending_until = float(source_timestamp) + 12 * 3600 + float(tolerance)
        if float(source_timestamp) >= cutoff or pending_until >= float(latest or 0.0):
            valid.append(item)
    valid.sort(key=lambda item: float(item.get("source_timestamp") or 0.0))
    if max_records > 0 and len(valid) > max_records:
        pending = [
            item for item in valid
            if float(item.get("source_timestamp") or 0.0) + 12 * 3600 + float(tolerance)
            >= float(latest or 0.0)
        ]
        completed = [item for item in valid if item not in pending]
        remaining = max(max_records - len(pending), 0)
        valid = completed[-remaining:] + pending if remaining else pending[-max_records:]
        valid.sort(key=lambda item: float(item.get("source_timestamp") or 0.0))
    return valid


def _write(path, rows):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path), suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows({name: row.get(name, "") for name in FIELDS} for row in rows)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def build_phase3_response_effect_report(config, historical_rows):
    """Backfill real H12 labels for formal and shadow forecasts without double-training."""
    paths = config["paths"]
    queue = _load_queue(paths["phase3_response_predictions"])
    timestamps = [float(row[0]) for row in historical_rows]
    tolerance = float(config.get("offline_training", {}).get(
        "target_tolerance_seconds", 1800
    ))
    latest = timestamps[-1] if timestamps else 0.0
    pruned_queue = _prune_queue(queue, config, latest, tolerance)
    if pruned_queue != queue:
        _write_queue(paths["phase3_response_predictions"], pruned_queue)
        queue = pruned_queue
    rows = []
    for item in queue:
        source_timestamp = item.get("source_timestamp")
        if not isinstance(source_timestamp, (int, float)):
            continue
        row_source = _source_for_item(item, config)
        target = float(source_timestamp) + 12 * 3600
        index = bisect_left(timestamps, target) if timestamps else 0
        actual = None
        actual_timestamp = None
        status = "pending"
        if index < len(historical_rows):
            offset = float(historical_rows[index][0]) - target
            if 0.0 <= offset <= tolerance:
                actual_timestamp = float(historical_rows[index][0])
                actual = float(historical_rows[index][2])
                status = "complete"
            elif latest >= target + tolerance:
                status = "expired_no_timely_observation"
        elif latest >= target + tolerance:
            status = "expired_no_timely_observation"

        predictions = (
            (
                "phase3_response_formal", item.get("formal_h12"),
                item.get("formal_raw_h12"), item.get("formal_slope_corrected_h12"),
                item.get("formal_observe_guard_recent_slope"),
                item.get("formal_observe_guard_status"),
                item.get("formal_observe_guard_max_drop_12h"),
            ),
            (
                "phase3_response_shadow", item.get("shadow_h12"),
                item.get("shadow_raw_h12"), item.get("shadow_slope_corrected_h12"),
                item.get("shadow_observe_guard_recent_slope"),
                item.get("shadow_observe_guard_status"),
                item.get("shadow_observe_guard_max_drop_12h"),
            ),
        )
        for (
            model, predicted, raw_predicted, corrected_predicted,
            recent_slope, guard_status, guard_max_drop,
        ) in predictions:
            if not isinstance(predicted, (int, float)) or not math.isfinite(float(predicted)):
                continue
            error = abs(float(predicted) - actual) if actual is not None else None
            rows.append({
                "request_id": item.get("request_id", ""),
                "device_code": item.get("device_code", ""),
                "source": row_source,
                "source_time": _format_time(source_timestamp),
                "source_humidity": item.get("source_humidity", ""),
                "selected_label": item.get("selected_label", ""),
                "water_sec": item.get("water_sec", ""),
                "zone": item.get("zone", ""),
                "model": model,
                "forecast_hour": 12,
                "target_time": _format_time(target),
                "actual_time": _format_time(actual_timestamp) if actual_timestamp else "",
                "actual_time_offset_minutes": (
                    round((actual_timestamp - target) / 60.0, 2)
                    if actual_timestamp else ""
                ),
                "predicted_humidity": round(float(predicted), 4),
                "actual_humidity": round(actual, 4) if actual is not None else "",
                "absolute_error": round(error, 4) if error is not None else "",
                "raw_predicted_humidity": (
                    round(float(raw_predicted), 4)
                    if isinstance(raw_predicted, (int, float)) and math.isfinite(float(raw_predicted))
                    else ""
                ),
                "slope_corrected_humidity": (
                    round(float(corrected_predicted), 4)
                    if isinstance(corrected_predicted, (int, float)) and math.isfinite(float(corrected_predicted))
                    else ""
                ),
                "recent_slope": (
                    round(float(recent_slope), 6)
                    if isinstance(recent_slope, (int, float)) and math.isfinite(float(recent_slope))
                    else ""
                ),
                "observe_guard_status": guard_status or "",
                "observe_guard_max_drop_12h": (
                    round(float(guard_max_drop), 4)
                    if isinstance(guard_max_drop, (int, float)) and math.isfinite(float(guard_max_drop))
                    else ""
                ),
                "effect_grade": _grade(error) if error is not None else status,
                "status": status,
                "training_status": (
                    "covered_by_continuous_shadow_training" if status == "complete"
                    else "awaiting_real_h12"
                ),
                "trained_at": (
                    datetime.now().strftime("%Y/%m/%d %H:%M")
                    if status == "complete" else ""
                ),
            })

    rows.sort(key=lambda row: (row["source_time"], row["model"]))
    max_rows = int(config.get("phase3_response_retention", {}).get("effect_max_rows", 2000))
    if max_rows > 0 and len(rows) > max_rows:
        rows = rows[-max_rows:]
    totals = defaultdict(list)
    recent = defaultdict(lambda: deque(maxlen=20))
    for row in rows:
        if row["status"] != "complete":
            continue
        model = (row["source"], row["model"])
        error = float(row["absolute_error"])
        totals[model].append(error)
        recent[model].append(error)
        values = totals[model]
        row.update({
            "model_sample_count": len(values),
            "model_mae": round(sum(values) / len(values), 4),
            "model_rmse": round(math.sqrt(sum(value * value for value in values) / len(values)), 4),
            "recent_20_mae": round(sum(recent[model]) / len(recent[model]), 4),
        })
    _write(paths["phase3_response_effect_12h"], rows)
    return {
        "phase3_response_forecasts": len(rows),
        "phase3_response_completed": sum(
            row["status"] == "complete" and row["source"] == "phase3_response"
            for row in rows
        ),
        "phase3_response_pending": sum(
            row["status"] == "pending" and row["source"] == "phase3_response"
            for row in rows
        ),
        "phase3_observe_completed": sum(
            row["status"] == "complete" and row["source"] == "phase3_observe"
            for row in rows
        ),
        "phase3_observe_pending": sum(
            row["status"] == "pending" and row["source"] == "phase3_observe"
            for row in rows
        ),
    }
