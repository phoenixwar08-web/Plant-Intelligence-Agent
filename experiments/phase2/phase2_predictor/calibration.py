import json
import logging
import math
import os
import time
from collections import Counter
from statistics import median


TRUSTED_TERMINAL_REASONS = {
    "accepted_into_pattern_memory": "normal_response",
    "post_h_above_fc_tolerance": "overshoot",
    "non_positive_delta_m": "ineffective_water",
}


def _read_json(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _phase3_context(config):
    paths = config["paths"]
    state = _read_json(paths.get("phase3_state"), {})
    trials = _read_json(paths.get("phase3_trials"), [])
    params = _read_json(paths.get("phase3_evolving_params"), {})
    profile = _read_json(paths.get("phase3_irrigation_profile"), {})
    return state, trials if isinstance(trials, list) else [], params, profile


def annotate_delivery_quality(rows, config):
    """Attach time-aligned Phase3 delivery labels without dropping sensor rows."""
    quality = config.get("training_quality", {})
    if not quality.get("enabled", True):
        return {"labels": {"normal_response": len(rows)}, "excluded_rows": 0}
    state, trials, _params, _profile = _phase3_context(config)
    suspect = state.get("water_delivery_suspect") or {}
    fault_start = float(suspect.get("first_seen_at") or 0.0)
    fault_end = float(suspect.get("cleared_at") or (time.time() if suspect.get("active") else 0.0))
    retest_start = float(suspect.get("repair_retest_started_at") or 0.0)
    retest_end = float(suspect.get("repair_retest_completed_at") or 0.0)
    recovered = suspect.get("repair_retest_result") == "recovered"
    terminal = [
        item for item in trials
        if item.get("reason") in TRUSTED_TERMINAL_REASONS
        and isinstance(item.get("timestamp"), (int, float))
    ]
    match_window = float(quality.get("trial_match_window_seconds", 2400))
    humidity_tolerance = float(quality.get("trial_humidity_tolerance", 1.0))
    counts = Counter()
    excluded = 0
    for timestamp, _features, _humidity, metadata in rows:
        label = "normal_response"
        eligible = True
        if fault_start and fault_start <= timestamp <= fault_end:
            if recovered and retest_start <= timestamp <= retest_end:
                label = "recovered_retest"
            else:
                label = "hardware_suspect"
                eligible = False
        if eligible and (metadata.get("watered", 0) > 0 or metadata.get("water_seconds", 0) > 0):
            candidates = [
                item for item in terminal
                if timestamp <= float(item["timestamp"]) <= timestamp + match_window
                and abs(float(item.get("water_sec") or 0.0) - float(metadata.get("water_seconds") or 0.0)) <= 0.11
                and abs(
                    float(item.get("humidity_before") or metadata["humidity"])
                    - float(metadata.get("pre_watering_humidity", metadata["humidity"]))
                ) <= humidity_tolerance
            ]
            if candidates:
                matched = min(candidates, key=lambda item: float(item["timestamp"]))
                label = TRUSTED_TERMINAL_REASONS[matched["reason"]]
                metadata["phase3_zone"] = matched.get("zone")
                metadata["measured_kp"] = (
                    float(matched["delta_m"]) / float(matched["water_sec"])
                    if float(matched.get("water_sec") or 0.0) > 0 else None
                )
        metadata["training_label"] = label
        metadata["training_eligible"] = eligible
        counts[label] += 1
        excluded += int(not eligible)
    return {"labels": dict(counts), "excluded_rows": excluded}


def _zone_bounds(params, config):
    calibration = config.get("calibration", {})
    low = float(params.get("TARGET_LOW", calibration.get("target_low", 33.0)))
    high = float(params.get("M_SAFE_SLEEP", calibration.get("safe_sleep", 42.5)))
    width = max(high - low, 0.1)
    return {
        "low_max": low + width / 3.0,
        "high_min": high - width / 3.0,
    }


def zone_for_humidity(humidity, bounds):
    if float(humidity) <= float(bounds["low_max"]):
        return "low"
    if float(humidity) >= float(bounds["high_min"]):
        return "high"
    return "mid"


def _ema(values, alpha):
    result = float(values[0])
    for value in values[1:]:
        result = (1.0 - alpha) * result + alpha * float(value)
    return result


def _robust_bias_stats(values):
    if not values:
        return {"median": 0.0, "mad": 0.0, "sign_consistency": 0.0}
    center = float(median(values))
    mad = float(median([abs(float(value) - center) for value in values]))
    positive = sum(float(value) > 0 for value in values)
    negative = sum(float(value) < 0 for value in values)
    return {
        "median": center,
        "mad": mad,
        "sign_consistency": max(positive, negative) / len(values),
    }


def build_peak_calibration(samples, runtime, config):
    calibration = config.get("calibration", {})
    _state, _trials, params, profile = _phase3_context(config)
    bounds = _zone_bounds(params, config)
    maximum = float(calibration.get("maximum_absolute_bias", 3.0))
    sample_clip = float(calibration.get("maximum_sample_residual", 10.0))
    maximum_mad = float(calibration.get("maximum_bias_mad", 2.5))
    minimum_sign = float(calibration.get("minimum_sign_consistency", 0.9))
    minimum_global = int(calibration.get("minimum_global_samples", 5))
    minimum_zone = int(calibration.get("minimum_zone_samples", 3))
    shrinkage = float(calibration.get("zone_shrinkage_samples", 5.0))
    maximum_events = int(calibration.get("maximum_events", 100))
    grouped = {"global": [], "low": [], "mid": [], "high": []}
    labels = Counter()
    for sequence, actual_peak, metadata in samples[-maximum_events:]:
        if not metadata.get("training_eligible", True):
            continue
        predicted_peak = float(runtime.predict(sequence)[1])
        residual = max(-sample_clip, min(sample_clip, float(actual_peak) - predicted_peak))
        zone = metadata.get("phase3_zone") or zone_for_humidity(metadata.get("humidity", 0.0), bounds)
        grouped["global"].append(residual)
        grouped.setdefault(zone, []).append(residual)
        labels[metadata.get("training_label", "normal_response")] += 1
    global_values = grouped["global"]
    global_robust = _robust_bias_stats(global_values)
    global_raw = global_robust["median"]
    global_active = (
        len(global_values) >= minimum_global
        and global_robust["mad"] <= maximum_mad
        and global_robust["sign_consistency"] >= minimum_sign
    )
    global_bias = max(-maximum, min(maximum, global_raw)) if global_active else 0.0
    zones = {}
    for zone in ("low", "mid", "high"):
        values = grouped[zone]
        robust = _robust_bias_stats(values)
        raw = robust["median"]
        active = (
            len(values) >= minimum_zone
            and robust["mad"] <= maximum_mad
            and robust["sign_consistency"] >= minimum_sign
        )
        blend = len(values) / (len(values) + shrinkage) if values else 0.0
        bias = blend * raw + (1.0 - blend) * global_bias if active else global_bias
        zones[zone] = {
            "count": len(values),
            "raw_ema": round(raw, 6),
            "robust_median": round(raw, 6),
            "mad": round(robust["mad"], 6),
            "sign_consistency": round(robust["sign_consistency"], 6),
            "bias": round(max(-maximum, min(maximum, bias)) if active else global_bias, 6),
            "active": active or global_active,
        }
    zone_profiles = profile.get("zones", {}) if isinstance(profile, dict) else {}
    kp_by_zone = {}
    for zone, key in (("low", "K_P_LOW"), ("mid", "K_P_MID"), ("high", "K_P_HIGH")):
        value = params.get(key)
        if not isinstance(value, (int, float)):
            value = (zone_profiles.get(zone) or {}).get("kp_ema")
        kp_by_zone[zone] = {
            "value": float(value) if isinstance(value, (int, float)) and value > 0 else None,
            "samples": int((zone_profiles.get(zone) or {}).get("stable_success", 0)),
        }
    return {
        "version": 1,
        "generated_at": time.time(),
        "source": "trusted_chronological_validation_residuals",
        "mode": calibration.get("mode", "shadow"),
        "bounds": bounds,
        "global": {
            "count": len(global_values),
            "raw_ema": round(global_raw, 6),
            "robust_median": round(global_raw, 6),
            "mad": round(global_robust["mad"], 6),
            "sign_consistency": round(global_robust["sign_consistency"], 6),
            "bias": round(global_bias, 6),
            "active": global_active,
            "stability_guard_passed": global_active,
        },
        "zones": zones,
        "kp_by_zone": kp_by_zone,
        "training_labels": dict(labels),
    }


class PeakCalibrationStore:
    def __init__(self, config, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.config = config
        self.path = config["paths"]["peak_calibration"]
        self._mtime = None
        self._value = {}

    def update_config(self, config):
        self.config = config
        self.path = config["paths"]["peak_calibration"]
        self._mtime = None

    def _get(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return {}
        if self._mtime != mtime:
            self._value = _read_json(self.path, {})
            self._mtime = mtime
        return self._value

    def apply(self, trajectory, raw_peak, current, water_seconds):
        cfg = self.config.get("calibration", {})
        if not cfg.get("enabled", True) or float(water_seconds) <= 0:
            return trajectory, raw_peak, {"status": "disabled", "total_adjustment": 0.0}
        if cfg.get("mode", "shadow") != "apply":
            return trajectory, raw_peak, {"status": "shadow_only", "total_adjustment": 0.0}
        value = self._get()
        if not value:
            return trajectory, raw_peak, {"status": "not_ready", "total_adjustment": 0.0}
        zone = zone_for_humidity(current, value["bounds"])
        zone_value = value.get("zones", {}).get(zone, {})
        global_value = value.get("global", {})
        bias = float(zone_value.get("bias", global_value.get("bias", 0.0)))
        if not zone_value.get("active", global_value.get("active", False)):
            bias = 0.0
        kp_info = value.get("kp_by_zone", {}).get(zone, {})
        kp = kp_info.get("value")
        kp_adjustment = 0.0
        if kp and int(kp_info.get("samples", 0)) >= int(cfg.get("minimum_kp_samples", 5)):
            maximum_peak = float(self.config["prediction"]["field_capacity"]) + float(
                self.config["prediction"]["dynamic_max_tolerance"]
            )
            prior_peak = min(maximum_peak, float(current) + float(kp) * float(water_seconds))
            kp_adjustment = max(0.0, prior_peak - (float(raw_peak) + bias)) * float(
                cfg.get("kp_prior_blend", 0.25)
            )
        maximum_adjustment = float(cfg.get("maximum_total_adjustment", 3.0))
        total = max(-maximum_adjustment, min(maximum_adjustment, bias + kp_adjustment))
        decay = float(cfg.get("trajectory_correction_decay", 0.85))
        lower = float(self.config["prediction"]["minimum_humidity"])
        upper = float(self.config["prediction"]["field_capacity"]) + float(
            self.config["prediction"]["dynamic_max_tolerance"]
        )
        corrected = [
            round(max(lower, min(upper, float(point) + total * (decay ** index))), 4)
            for index, point in enumerate(trajectory)
        ]
        corrected_peak = round(max(float(current), min(upper, float(raw_peak) + total), max(corrected)), 4)
        return corrected, corrected_peak, {
            "status": "applied" if total else "no_adjustment",
            "zone": zone,
            "residual_bias": round(bias, 4),
            "kp_prior_adjustment": round(kp_adjustment, 4),
            "total_adjustment": round(total, 4),
        }
