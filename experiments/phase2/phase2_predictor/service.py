import argparse
import csv
import json
import logging
import math
import os
import re
import signal
import time
from datetime import datetime

from .config import ConfigManager
from .calibration import PeakCalibrationStore
from .data_sources import normalize
from .learning import AdaptiveWateringLearner, atomic_json_write
from .natural_training import apply_natural_horizon_bias_correction
from .offline_training import load_historical_rows
from .watering_models import WateringModelRuntime


def configure_logging(path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    logger = logging.getLogger("phase2.predictor")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def _append_csv(path, row):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    new_file = not os.path.exists(path)
    fieldnames = list(row.keys())
    if not new_file:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            existing = reader.fieldnames or []
            missing = [key for key in fieldnames if key not in existing]
            if missing:
                rows = list(reader)
                fieldnames = existing + missing
                with open(path, "w", encoding="utf-8", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=fieldnames)
                    writer.writeheader()
                    for item in rows:
                        writer.writerow({key: item.get(key, "") for key in fieldnames})
                    writer.writerow({key: row.get(key, "") for key in fieldnames})
                return
            fieldnames = existing
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if new_file:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fieldnames})


def _format_time(timestamp):
    return datetime.fromtimestamp(float(timestamp)).strftime("%Y/%m/%d %H:%M")


def _label_for_seconds(seconds):
    value = int(seconds) if float(seconds).is_integer() else seconds
    return f"water_{value}s"


def _default_candidates(config):
    return [
        {"label": _label_for_seconds(float(seconds)), "water_sec": float(seconds)}
        for seconds in config.get("decision", {}).get("candidate_water_seconds", [3.0, 6.0, 9.0])
    ]


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _normalize_identifier(value, field, config):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    limit = int(config.get("profile_correction", {}).get("maximum_identifier_length", 128))
    if not value or len(value) > limit or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} contains invalid characters or is too long")
    return value


def _normalize_profile_value(value, depth=0):
    if depth > 3:
        raise ValueError("profile nesting is too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("profile numbers must be finite")
        return value
    if isinstance(value, str):
        if len(value) > 256:
            raise ValueError("profile strings are too long")
        return value
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError("profile contains too many fields")
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64 or not _IDENTIFIER_RE.fullmatch(key):
                raise ValueError("profile field names are invalid")
            normalized[key] = _normalize_profile_value(item, depth + 1)
        return normalized
    raise ValueError("profile values must be scalar values or objects")


def _normalize_profile(value, config):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("profile must be a JSON object")
    limit = int(config.get("profile_correction", {}).get("maximum_profile_bytes", 8192))
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > limit:
        raise ValueError("profile exceeds maximum_profile_bytes")
    normalized = _normalize_profile_value(value)
    numeric_paths = [
        ("fc",), ("target_low",), ("kp",), ("kp_low",), ("kp_mid",), ("kp_high",),
        ("recent_slope",), ("last_delta_m",),
        ("zone_stats", "stable_success"), ("zone_stats", "failure_count"),
        ("zone_stats", "max_allowed_sec"),
        ("history", "accepted_samples"), ("history", "rejected_samples"),
        ("history", "peak_error_samples"), ("history", "peak_error_median"),
        ("air_humidity_fallback", "age_sec"),
    ]
    for path in numeric_paths:
        current = normalized
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current is not None and (
            isinstance(current, bool) or not isinstance(current, (int, float))
        ):
            raise ValueError(f"profile {'.'.join(path)} must be numeric or null")
    if normalized.get("zone") not in {None, "low", "mid", "high"}:
        raise ValueError("profile zone must be low, mid, or high")
    return normalized


def validate_request(payload, config):
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    timestamp = float(payload.get("timestamp", time.time()))
    horizon = int(payload.get("horizon_steps", 12))
    if not 1 <= horizon <= int(config["service"]["max_horizon_steps"]):
        raise ValueError("horizon_steps is outside allowed range")
    raw_candidates = payload.get("candidates") or _default_candidates(config)
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("candidates must be a non-empty list")
    candidates = []
    labels = set()
    maximum_water_seconds = float(config["data_quality"].get("maximum_water_seconds", 120.0))
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            raise ValueError("each candidate must be a JSON object")
        water_sec = float(raw.get("water_sec"))
        label = raw.get("label") or _label_for_seconds(water_sec)
        if not isinstance(label, str) or not label.strip() or label in labels:
            raise ValueError("candidate labels must be unique non-empty strings")
        if not 0.0 <= water_sec <= maximum_water_seconds:
            raise ValueError("water_sec is outside allowed range")
        labels.add(label)
        candidates.append({"label": label.strip(), "water_sec": water_sec})
    metadata = {
        "device_code": _normalize_identifier(payload.get("device_code"), "device_code", config),
        "request_id": _normalize_identifier(payload.get("request_id"), "request_id", config),
        "profile": _normalize_profile(payload.get("profile"), config),
    }
    return timestamp, candidates, horizon, metadata


def _profile_number(profile, *path):
    value = profile
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _shadow_profile_peak(metric, water_seconds, current_humidity, profile, config):
    settings = config.get("profile_correction", {})
    live_peak = float(metric["predicted_peak"])
    result = {
        "shadow_corrected_peak": round(live_peak, 4),
        "profile_bias_used": 0.0,
        "profile_correction_status": "disabled",
    }
    if not settings.get("enabled", True) or settings.get("mode", "shadow") != "shadow":
        return result
    if float(water_seconds) <= 0 or not isinstance(profile, dict):
        result["profile_correction_status"] = "not_applicable"
        return result

    zone = profile.get("zone")
    if zone not in {"low", "mid", "high"}:
        result["profile_correction_status"] = "invalid_zone"
        return result
    minimum = int(settings.get("min_samples_for_bias", 3))
    stable_samples = _profile_number(profile, "zone_stats", "stable_success") or 0.0
    history_samples = _profile_number(profile, "history", "peak_error_samples") or 0.0
    historical_bias = _profile_number(profile, "history", "peak_error_median")
    total = 0.0
    used = []
    if historical_bias is not None and history_samples >= minimum:
        total += historical_bias
        used.append("history")

    kp = _profile_number(profile, f"kp_{zone}")
    if kp is None:
        kp = _profile_number(profile, "kp")
    fc = _profile_number(profile, "fc")
    if kp is not None and kp > 0 and stable_samples >= minimum:
        prior_peak = float(current_humidity) + kp * float(water_seconds)
        if fc is not None:
            prior_peak = min(prior_peak, max(fc, float(current_humidity)))
        total += (prior_peak - (live_peak + total)) * float(settings.get("kp_blend_weight", 0.35))
        used.append("kp")

    if not used:
        result["profile_correction_status"] = "insufficient_samples"
        return result
    maximum = abs(float(settings.get("max_peak_bias", 6.0)))
    total = max(-maximum, min(maximum, total))
    shadow_peak = live_peak + total
    if fc is not None:
        shadow_peak = min(shadow_peak, max(fc, float(current_humidity)))
    shadow_peak = max(float(current_humidity), shadow_peak)
    total = max(-maximum, min(maximum, shadow_peak - live_peak))
    shadow_peak = live_peak + total
    result.update({
        "shadow_corrected_peak": round(shadow_peak, 4),
        "profile_bias_used": round(total, 4),
        "profile_correction_status": "shadow_" + "_and_".join(used),
    })
    return result


def _apply_natural_observe_guard(metric, water_seconds, current_humidity, profile, config):
    """Constrain no-water high-zone H12 drydown using Phase3's live drydown profile."""
    settings = config.get("natural_observe_guard", {})
    raw_h12 = float(metric.get("predicted_humidity_12h", current_humidity))
    result = {
        "raw_predicted_humidity_12h": round(raw_h12, 4),
        "slope_corrected_humidity_12h": round(raw_h12, 4),
        "observe_guard_status": "disabled",
        "observe_guard_recent_slope": None,
        "observe_guard_drydown_rate_ema": None,
        "observe_guard_max_drop_12h": None,
    }
    if not settings.get("enabled", True):
        metric.update(result)
        return metric
    if float(water_seconds) > 0 or not isinstance(profile, dict):
        result["observe_guard_status"] = "not_applicable"
        metric.update(result)
        return metric

    zone = profile.get("zone")
    zones = set(settings.get("zones", ["high"]))
    if zone not in zones:
        result["observe_guard_status"] = "zone_not_guarded"
        metric.update(result)
        return metric
    minimum_by_zone = settings.get("minimum_source_humidity_by_zone", {})
    minimum_humidity = float(minimum_by_zone.get(zone, settings.get("minimum_source_humidity", 37.0)))
    current = float(current_humidity)
    if current < minimum_humidity:
        result["observe_guard_status"] = "below_minimum_humidity"
        metric.update(result)
        return metric

    recent_slope = _profile_number(profile, "recent_slope")
    drydown_rate_ema = _profile_number(profile, "zone_stats", "drydown_rate_ema")
    result["observe_guard_recent_slope"] = (
        round(recent_slope, 6) if recent_slope is not None else None
    )
    result["observe_guard_drydown_rate_ema"] = (
        round(drydown_rate_ema, 6) if drydown_rate_ema is not None else None
    )

    steps_per_hour = float(settings.get("slope_steps_per_hour", 12.0))
    horizon_hours = float(settings.get("horizon_hours", 12.0))
    slope_multiplier = float(settings.get("slope_multiplier", 1.0))
    slope_margin = float(settings.get("slope_margin_12h", 0.8))
    max_drop_by_zone = settings.get("max_drop_12h_by_zone", {})
    min_drop_by_zone = settings.get("min_drop_12h_by_zone", {})
    hard_max_drop = float(max_drop_by_zone.get(zone, settings.get("max_drop_12h", 2.8)))
    hard_min_drop = float(min_drop_by_zone.get(zone, settings.get("min_drop_12h", 0.8)))

    rate_candidates = []
    if recent_slope is not None and recent_slope < 0:
        rate_candidates.append(abs(recent_slope))
    if drydown_rate_ema is not None and drydown_rate_ema > 0:
        rate_candidates.append(abs(drydown_rate_ema))
    if rate_candidates:
        rate_drop = max(rate_candidates) * steps_per_hour * horizon_hours * slope_multiplier
        max_drop = min(hard_max_drop, max(hard_min_drop, rate_drop + slope_margin))
    else:
        max_drop = hard_max_drop
    max_drop = max(0.0, max_drop)
    result["observe_guard_max_drop_12h"] = round(max_drop, 4)

    trajectory = [
        float(value) for value in metric.get("trajectory", [])
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    if not trajectory:
        result["observe_guard_status"] = "no_trajectory"
        metric.update(result)
        return metric

    exponent = max(float(settings.get("drop_curve_exponent", 1.0)), 0.1)
    corrected = []
    changed = False
    total = max(len(trajectory), 1)
    for index, value in enumerate(trajectory):
        fraction = ((index + 1) / total) ** exponent
        floor = current - max_drop * fraction
        corrected_value = max(value, floor)
        if corrected_value > value + 1e-6:
            changed = True
        corrected.append(round(corrected_value, 4))
    if not changed:
        result["observe_guard_status"] = "within_live_drydown"
        metric.update(result)
        return metric

    metric["trajectory"] = corrected
    metric["predicted_humidity_12h"] = round(corrected[min(11, len(corrected) - 1)], 4)
    metric["predicted_peak"] = round(max(float(metric.get("predicted_peak", current)), max(corrected), current), 4)
    result["slope_corrected_humidity_12h"] = metric["predicted_humidity_12h"]
    result["observe_guard_status"] = "applied"
    metric.update(result)
    return metric


def _compact_profile_summary(profile):
    if not isinstance(profile, dict):
        return {}
    return {
        key: profile.get(key)
        for key in ("zone", "fc", "target_low", "recent_slope", "last_delta_m")
        if profile.get(key) is not None
    }


def _current_sequence(rows, config, water_seconds):
    length = int(config["model"]["sequence_length"])
    latest = rows[-1][3]
    previous = rows[-2][3] if len(rows) >= 2 else latest
    watered = 1.0 if float(water_seconds) > 0.0 else 0.0
    seconds_since = 0.0 if watered else float(latest.get("seconds_since_watering", 604800.0))
    features = normalize([
        latest["temperature"], latest["ec"], latest["humidity"], latest["light"],
        watered, float(water_seconds),
        latest["temperature"] - previous["temperature"],
        latest["humidity"] - previous["humidity"],
        seconds_since,
    ])
    sequence = [row[1] for row in rows[-length:]]
    sequence[-1] = features
    return sequence


def _recent_drying_rate(rows, config):
    default = float(config["prediction"].get("default_drying_per_step", 0.2))
    maximum = float(config["prediction"].get("maximum_drying_per_step", 2.0))
    rates = []
    for earlier, later in zip(rows[-24:-1], rows[-23:]):
        dt_hours = max((later[0] - earlier[0]) / 3600.0, 1e-6)
        drop = earlier[2] - later[2]
        if drop > 0:
            rates.append(drop / dt_hours)
    if not rates:
        return default
    rates.sort()
    return max(0.0, min(maximum, rates[len(rates) // 2]))


def _physical_prediction(rows, config, learner, candidate, horizon):
    latest = rows[-1]
    current = float(latest[2])
    prediction = config["prediction"]
    upper = min(
        float(prediction["field_capacity"]),
        float(learner.state.get("dyn_max", prediction["field_capacity"]))
        + float(prediction["dynamic_max_tolerance"]),
    )
    lower = float(prediction["minimum_humidity"])
    water_seconds = float(candidate["water_sec"])
    raw_rise = learner.efficiency_k * water_seconds
    rise = min(raw_rise, raw_rise * float(prediction["maximum_rise_safety_factor"]))
    rise = min(rise, max(0.0, upper - current))
    drying_rate = _recent_drying_rate(rows, config)
    decay = float(prediction.get("watering_decay", 0.9))
    maximum_jump = float(prediction.get("maximum_step_jump", 3.0))
    values = []
    previous = current
    for hour in range(1, horizon + 1):
        water_effect = rise * (decay ** max(0, hour - 1))
        proposed = current + water_effect - drying_rate * hour
        if water_seconds <= 0:
            proposed = min(proposed, previous)
        proposed = max(previous - maximum_jump, min(previous + maximum_jump, proposed))
        proposed = max(lower, min(upper, proposed))
        values.append(round(proposed, 4))
        previous = proposed
    peak = max([current] + values)
    peak_index = values.index(peak) + 1 if peak in values else 0
    minutes_to_peak = max(0.0, peak_index * 60.0)
    return {
        "trajectory": values,
        "predicted_peak": round(peak, 4),
        "predicted_minutes_to_peak": round(minutes_to_peak, 2),
        "predicted_peak_time": _format_time(latest[0] + minutes_to_peak * 60.0),
        "predicted_humidity_12h": round(values[min(11, len(values) - 1)], 4),
        "backend": "physical_fallback",
    }


def _extend_trajectory(values, rows, config, horizon):
    if len(values) >= horizon:
        return values[:horizon]
    prediction = config["prediction"]
    lower = float(prediction["minimum_humidity"])
    upper = float(prediction["field_capacity"]) + float(prediction["dynamic_max_tolerance"])
    drying_rate = _recent_drying_rate(rows, config)
    previous = values[-1] if values else rows[-1][2]
    extended = list(values)
    while len(extended) < horizon:
        previous = max(lower, min(upper, previous - drying_rate))
        extended.append(round(previous, 4))
    return extended


def _model_prediction(rows, config, runtime, candidate, horizon, calibration=None):
    sequence = _current_sequence(rows, config, candidate["water_sec"])
    details = runtime.predict_details(sequence)
    trajectory = _extend_trajectory(
        [round(float(value), 4) for value in details["trajectory"]],
        rows, config, horizon,
    )
    if float(candidate["water_sec"]) > 0:
        current = float(rows[-1][2])
        drying_rate = _recent_drying_rate(rows, config)
        floor_margin = float(config["prediction"].get("watering_floor_margin", 0.5))
        recent_window = int(config["prediction"].get("watering_recent_floor_window", 24))
        recent_values = [float(row[2]) for row in rows[-max(1, recent_window):]]
        recent_floor = min(recent_values) - floor_margin if recent_values else current - floor_margin
        trajectory = [
            round(max(value, recent_floor, current - drying_rate * (index + 1) - floor_margin), 4)
            for index, value in enumerate(trajectory)
        ]
    else:
        trajectory, bias_adjustments = apply_natural_horizon_bias_correction(
            trajectory, config
        )
        if bias_adjustments:
            details["natural_bias_adjustments"] = bias_adjustments
    minutes_to_peak = max(0.0, min(720.0, float(details["minutes_to_peak"])))
    raw_peak = round(max(float(details["peak"]), max(trajectory), rows[-1][2]), 4)
    calibration_info = {"status": "not_configured", "total_adjustment": 0.0}
    if calibration is not None:
        trajectory, predicted_peak, calibration_info = calibration.apply(
            trajectory, raw_peak, rows[-1][2], candidate["water_sec"]
        )
    else:
        predicted_peak = raw_peak
    return {
        "trajectory": trajectory,
        "predicted_peak": predicted_peak,
        "raw_predicted_peak": raw_peak,
        "calibration": calibration_info,
        "predicted_minutes_to_peak": round(minutes_to_peak, 2),
        "predicted_peak_time": _format_time(rows[-1][0] + minutes_to_peak * 60.0),
        "predicted_humidity_12h": round(trajectory[min(11, len(trajectory) - 1)], 4),
        "natural_bias_adjustments": details.get("natural_bias_adjustments", {}),
        "backend": details.get("inference_backend", "cpu_pytorch"),
    }


def _score_candidate(metric, water_seconds, request, config):
    decision = config.get("decision", {})
    minimum_h12 = float(request.get(
        "minimum_humidity_12h",
        decision.get("minimum_humidity_12h", config["prediction"]["minimum_humidity"]),
    ))
    target_h12 = request.get("target_humidity_12h", decision.get("target_humidity_12h"))
    maximum_peak = float(request.get(
        "maximum_peak_humidity",
        decision.get("maximum_peak_humidity", config["prediction"]["field_capacity"]),
    ))
    h12 = float(metric["predicted_humidity_12h"])
    peak = float(metric["predicted_peak"])
    trajectory = [
        float(value) for value in metric.get("trajectory", [])
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    control = decision.get("control_horizon", {})
    if control.get("enabled", True) and trajectory:
        short_steps = max(0, int(control.get("short_steps", 3)))
        medium_steps = max(short_steps, int(control.get("medium_steps", 6)))
        short_weight = float(control.get("short_weight", 1.0))
        medium_weight = float(control.get("medium_weight", 0.4))
        long_weight = float(control.get("long_weight", 0.1))
        h12_weight = float(control.get("h12_weight", 0.25))
        peak_weight = float(control.get("peak_weight", 0.15))
        hard_peak_margin = float(control.get("hard_peak_margin", 2.0))

        score = 0.0
        weighted_peak_excess = 0.0
        weighted_floor_deficit = 0.0
        total_weight = 0.0
        for index, value in enumerate(trajectory):
            if index < short_steps:
                weight = short_weight
            elif index < medium_steps:
                weight = medium_weight
            else:
                weight = long_weight
            weighted_peak_excess += max(0.0, value - maximum_peak) * weight
            weighted_floor_deficit += max(0.0, minimum_h12 - value) * weight
            total_weight += weight
        if total_weight > 0:
            score += (
                weighted_floor_deficit / total_weight
                * float(decision.get("under_target_penalty_weight", 20.0))
            )
            score += (
                weighted_peak_excess / total_weight
                * float(decision.get("over_peak_penalty_weight", 20.0))
            )
        if target_h12 is not None:
            score += abs(h12 - float(target_h12)) * float(decision.get("target_error_weight", 1.0)) * h12_weight
        score += max(0.0, minimum_h12 - h12) * float(decision.get("under_target_penalty_weight", 20.0)) * h12_weight
        score += max(0.0, peak - maximum_peak) * float(decision.get("over_peak_penalty_weight", 20.0)) * peak_weight
        score += max(0.0, peak - maximum_peak - hard_peak_margin) * float(
            decision.get("over_peak_penalty_weight", 20.0)
        )
        score += float(water_seconds) * float(decision.get("water_seconds_penalty_weight", 0.05))
        return round(score, 6), {
            "minimum_humidity_12h": minimum_h12,
            "target_humidity_12h": target_h12,
            "maximum_peak_humidity": maximum_peak,
            "control_horizon": {
                "enabled": True,
                "short_steps": short_steps,
                "medium_steps": medium_steps,
                "short_weight": short_weight,
                "medium_weight": medium_weight,
                "long_weight": long_weight,
                "h12_weight": h12_weight,
                "peak_weight": peak_weight,
                "hard_peak_margin": hard_peak_margin,
            },
        }
    score = 0.0
    if target_h12 is not None:
        score += abs(h12 - float(target_h12)) * float(decision.get("target_error_weight", 1.0))
    score += max(0.0, minimum_h12 - h12) * float(decision.get("under_target_penalty_weight", 20.0))
    score += max(0.0, peak - maximum_peak) * float(decision.get("over_peak_penalty_weight", 20.0))
    score += float(water_seconds) * float(decision.get("water_seconds_penalty_weight", 0.05))
    return round(score, 6), {
        "minimum_humidity_12h": minimum_h12,
        "target_humidity_12h": target_h12,
        "maximum_peak_humidity": maximum_peak,
    }


class PredictorService:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.config = config_manager.get()
        self.logger = configure_logging(
            self.config["paths"].get("predictor_service_log", self.config["paths"]["service_log"])
        )
        self.watering_runtime = WateringModelRuntime(self.config, self.logger, "model_weights")
        self.shadow_runtime = WateringModelRuntime(self.config, self.logger, "shadow_weights")
        self.natural_runtime = WateringModelRuntime(self.config, self.logger, "natural_weights")
        self.learner = AdaptiveWateringLearner(self.config, self.logger)
        self.calibration = PeakCalibrationStore(self.config, self.logger)
        self.last_timestamp = None
        self._rows_cache = None
        self._rows_cache_mtime = None
        self._warmup_models()
        self.running = True

    def stop(self, *_args):
        self.running = False

    def close(self):
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)

    def _reload_config(self):
        self.config_manager.reload_if_changed()
        self.config = self.config_manager.get()
        self.watering_runtime.update_config(self.config)
        self.shadow_runtime.update_config(self.config)
        self.natural_runtime.update_config(self.config)
        self.watering_runtime.reload_if_changed()
        self.shadow_runtime.reload_if_changed()
        self.natural_runtime.reload_if_changed()
        self.learner.update_config(self.config)
        self.calibration.update_config(self.config)

    def _warmup_models(self):
        try:
            rows = self._load_rows()
            if len(rows) < int(self.config["service"]["minimum_history_rows"]):
                return
            for runtime, seconds in (
                (self.watering_runtime, 3.0),
                (self.shadow_runtime, 3.0),
                (self.natural_runtime, 0.0),
            ):
                if runtime.status == "loaded":
                    runtime.predict_details(_current_sequence(rows, self.config, seconds))
        except Exception as exc:
            self.logger.warning("Predictor warmup skipped: %s", exc)

    def _load_rows(self):
        path = self.config["paths"]["soil_csv"]
        mtime = os.path.getmtime(path)
        if self._rows_cache is None or self._rows_cache_mtime != mtime:
            self._rows_cache = load_historical_rows(self.config)
            self._rows_cache_mtime = mtime
        return self._rows_cache

    def _predict_candidate(self, rows, candidate, horizon, use_legacy_calibration=True):
        runtime = self.watering_runtime if candidate["water_sec"] > 0 else self.natural_runtime
        try:
            if runtime.status != "loaded":
                raise RuntimeError(f"model weights are not loaded: {runtime.status}")
            return _model_prediction(
                rows, self.config, runtime, candidate, horizon,
                self.calibration if use_legacy_calibration else None,
            ), False
        except Exception as exc:
            self.logger.warning("Candidate %s fell back to physical prediction: %s", candidate["label"], exc)
            return _physical_prediction(rows, self.config, self.learner, candidate, horizon), True

    def _predict_shadow_candidate(self, rows, candidate, horizon):
        runtime = self.shadow_runtime if candidate["water_sec"] > 0 else self.natural_runtime
        try:
            if runtime.status != "loaded":
                raise RuntimeError(f"shadow model weights are not loaded: {runtime.status}")
            return _model_prediction(
                rows, self.config, runtime, candidate, horizon, calibration=None,
            ), False
        except Exception as exc:
            self.logger.warning(
                "Shadow candidate %s unavailable: %s", candidate["label"], exc
            )
            return None, True

    def _build_response(self, payload):
        timestamp, candidates, horizon, request_meta = validate_request(payload, self.config)
        rows = self._load_rows()
        if len(rows) < int(self.config["service"]["minimum_history_rows"]):
            raise ValueError("soil CSV does not have enough valid history rows")
        trajectories = {}
        metrics = {}
        fallback_labels = []
        policy = None
        profile = request_meta["profile"]
        use_legacy_calibration = not any(request_meta.values())
        for candidate in candidates:
            metric, fallback = self._predict_candidate(
                rows, candidate, horizon, use_legacy_calibration=use_legacy_calibration
            )
            shadow_metric, shadow_fallback = self._predict_shadow_candidate(
                rows, candidate, horizon
            )
            metric.setdefault("raw_predicted_peak", metric["predicted_peak"])
            _apply_natural_observe_guard(
                metric, candidate["water_sec"], rows[-1][2], profile, self.config
            )
            metric.update(_shadow_profile_peak(
                metric, candidate["water_sec"], rows[-1][2], profile, self.config
            ))
            if shadow_metric is not None:
                shadow_metric.setdefault("raw_predicted_peak", shadow_metric["predicted_peak"])
                _apply_natural_observe_guard(
                    shadow_metric, candidate["water_sec"], rows[-1][2], profile, self.config
                )
                metric.update({
                    "shadow_model_trajectory": shadow_metric["trajectory"],
                    "shadow_model_peak": shadow_metric["predicted_peak"],
                    "shadow_model_raw_peak": shadow_metric.get("raw_predicted_peak"),
                    "shadow_model_raw_h12": shadow_metric.get("raw_predicted_humidity_12h"),
                    "shadow_model_h12": shadow_metric["predicted_humidity_12h"],
                    "shadow_model_slope_corrected_h12": shadow_metric.get("slope_corrected_humidity_12h"),
                    "shadow_model_observe_guard_status": shadow_metric.get("observe_guard_status"),
                    "shadow_model_observe_guard_recent_slope": shadow_metric.get("observe_guard_recent_slope"),
                    "shadow_model_observe_guard_max_drop_12h": shadow_metric.get("observe_guard_max_drop_12h"),
                    "shadow_model_minutes_to_peak": shadow_metric["predicted_minutes_to_peak"],
                    "shadow_model_backend": shadow_metric.get("backend"),
                    "shadow_model_status": "model",
                })
            else:
                metric.update({
                    "shadow_model_trajectory": None,
                    "shadow_model_peak": None,
                    "shadow_model_raw_peak": None,
                    "shadow_model_raw_h12": None,
                    "shadow_model_h12": None,
                    "shadow_model_slope_corrected_h12": None,
                    "shadow_model_observe_guard_status": None,
                    "shadow_model_observe_guard_recent_slope": None,
                    "shadow_model_observe_guard_max_drop_12h": None,
                    "shadow_model_minutes_to_peak": None,
                    "shadow_model_backend": None,
                    "shadow_model_status": "unavailable" if shadow_fallback else "unknown",
                })
            score, policy = _score_candidate(metric, candidate["water_sec"], payload or {}, self.config)
            metric = {
                **metric,
                "water_sec": candidate["water_sec"],
                "score": score,
                "status": "fallback" if fallback else "model",
            }
            trajectories[candidate["label"]] = metric["trajectory"]
            metrics[candidate["label"]] = {key: value for key, value in metric.items() if key != "trajectory"}
            if fallback:
                fallback_labels.append(candidate["label"])
        recommended_label = min(
            candidates,
            key=lambda item: (metrics[item["label"]]["score"], item["water_sec"]),
        )["label"]
        recommendation = {
            "label": recommended_label,
            "water_sec": metrics[recommended_label]["water_sec"],
            "score": metrics[recommended_label]["score"],
            "reason": "lowest_score_from_peak_h12_and_water_cost",
            "advisory_only": True,
        }
        minimum_h12 = float((policy or {}).get("minimum_humidity_12h", 0.0))
        best_h12 = max(float(metric["predicted_humidity_12h"]) for metric in metrics.values())
        target_unreachable = best_h12 < minimum_h12
        if target_unreachable:
            recommendation["reason"] = "target_humidity_12h_unreachable_choose_lowest_penalty"
        else:
            recommendation["reason"] = "lowest_score_from_peak_h12_and_water_cost"
        recommendation["target_unreachable"] = target_unreachable
        recommendation["best_candidate_humidity_12h"] = round(best_h12, 4)
        recommendation["target_shortfall"] = round(max(0.0, minimum_h12 - best_h12), 4)
        return {
            "timestamp": timestamp,
            "request_id": request_meta["request_id"],
            "device_code": request_meta["device_code"],
            "model": "wyc_phase2_candidate_predictor",
            "status": "ok",
            "trajectories": trajectories,
            "candidate_metrics": metrics,
            "recommended": recommendation,
            "selected_water_sec": recommendation["water_sec"],
            "selection_scope": "phase2_diagnostic_only_phase3_reselects_from_trajectories",
            "decision_policy": policy or {},
            "target_unreachable": target_unreachable,
            "recommendation_reason": recommendation["reason"],
            "fallback_labels": fallback_labels,
            "sensor_time": rows[-1][3]["time"],
            "current_humidity": round(float(rows[-1][2]), 4),
        }

    def process_once(self):
        self._reload_config()
        request_path = self.config["paths"]["request"]
        if not os.path.exists(request_path):
            return False
        with open(request_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        request_key = payload.get("request_id")
        if request_key is None:
            request_key = float(payload["timestamp"]) if "timestamp" in payload else os.path.getmtime(request_path)
        if request_key == self.last_timestamp:
            return False
        started = time.perf_counter()
        response = self._build_response(payload)
        atomic_json_write(self.config["paths"]["response"], response)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        _append_csv(self.config["paths"]["predictor_log"], {
            "request_timestamp": response["timestamp"],
            "request_id": response.get("request_id"),
            "device_code": response.get("device_code"),
            "profile_summary": json.dumps(
                _compact_profile_summary(payload.get("profile")), ensure_ascii=False
            ),
            "sensor_time": response["sensor_time"],
            "current_humidity": response["current_humidity"],
            "candidates": json.dumps(
                {label: metric["water_sec"] for label, metric in response["candidate_metrics"].items()},
                ensure_ascii=False,
            ),
            "recommended_label": response["recommended"]["label"],
            "selected_water_sec": response["selected_water_sec"],
            "target_unreachable": response["target_unreachable"],
            "recommendation_reason": response["recommendation_reason"],
            "trajectory_ends": json.dumps(
                {label: values[-1] for label, values in response["trajectories"].items()},
                ensure_ascii=False,
            ),
            "candidate_metrics": json.dumps(response["candidate_metrics"], ensure_ascii=False),
            "raw_model_peaks": json.dumps({
                label: metric.get("raw_predicted_peak")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "live_predicted_peaks": json.dumps({
                label: metric.get("predicted_peak")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "shadow_corrected_peaks": json.dumps({
                label: metric.get("shadow_corrected_peak")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "profile_bias_used": json.dumps({
                label: metric.get("profile_bias_used")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "shadow_model_peaks": json.dumps({
                label: metric.get("shadow_model_peak")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "shadow_model_h12": json.dumps({
                label: metric.get("shadow_model_h12")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "raw_model_h12": json.dumps({
                label: metric.get("raw_predicted_humidity_12h")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "slope_corrected_h12": json.dumps({
                label: metric.get("slope_corrected_humidity_12h")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "observe_guard_status": json.dumps({
                label: metric.get("observe_guard_status")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "shadow_model_raw_h12": json.dumps({
                label: metric.get("shadow_model_raw_h12")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "shadow_model_observe_guard_status": json.dumps({
                label: metric.get("shadow_model_observe_guard_status")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "shadow_model_trajectories": json.dumps({
                label: metric.get("shadow_model_trajectory")
                for label, metric in response["candidate_metrics"].items()
            }, ensure_ascii=False),
            "fallback_labels": json.dumps(response["fallback_labels"], ensure_ascii=False),
            "elapsed_ms": elapsed_ms,
        })
        self.last_timestamp = request_key
        return True

    def run(self):
        self.logger.info("WYC Phase 2 candidate predictor service started")
        try:
            while self.running:
                try:
                    self.process_once()
                except Exception:
                    self.logger.exception("Prediction request failed")
                time.sleep(float(self.config["service"]["poll_seconds"]))
        finally:
            self.logger.info("WYC Phase 2 candidate predictor service stopped")
            self.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="process one request and exit")
    args = parser.parse_args(argv)
    service = PredictorService(ConfigManager())
    signal.signal(signal.SIGINT, service.stop)
    signal.signal(signal.SIGTERM, service.stop)
    if args.once:
        try:
            processed = service.process_once()
            print(json.dumps({"processed": processed}, ensure_ascii=False))
        finally:
            service.close()
        return
    service.run()
