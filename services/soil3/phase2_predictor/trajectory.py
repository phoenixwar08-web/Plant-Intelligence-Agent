import math

import numpy as np

from .data_sources import estimate_vpd


def _historical_drying_rate(history, default_rate, maximum_rate):
    if len(history) < 2:
        return default_rate
    differences = [history[index] - history[index + 1] for index in range(len(history) - 1)]
    drying = [value for value in differences[-12:] if value > 0]
    if not drying:
        return default_rate
    return float(np.clip(np.median(drying), 0.0, maximum_rate))


def _bounded_append(values, proposed, lower, upper, max_jump):
    previous = values[-1] if values else proposed
    bounded = float(np.clip(proposed, previous - max_jump, previous + max_jump))
    values.append(round(float(np.clip(bounded, lower, upper)), 2))


def _soft_limited_rise(raw_rise, headroom, soft_cap_fraction):
    if raw_rise <= 0.0 or headroom <= 0.0:
        return 0.0
    return headroom * soft_cap_fraction * (1.0 - math.exp(-raw_rise / headroom))


def generate_trajectories(snapshot, candidates, horizon_steps, config, learner, memory_bank, model_runtime):
    prediction = config["prediction"]
    current = snapshot.humidity
    upper = min(
        prediction["field_capacity"],
        float(learner.state.get("dyn_max", prediction["field_capacity"]))
        + prediction["dynamic_max_tolerance"]
    )
    lower = prediction["minimum_humidity"]
    history_rate = _historical_drying_rate(
        snapshot.humidity_history,
        prediction["default_drying_per_step"],
        prediction["maximum_drying_per_step"]
    )
    vpd = estimate_vpd(snapshot.forecast_temperature, snapshot.forecast_humidity)
    drying_rate = min(
        prediction["maximum_drying_per_step"],
        history_rate + vpd * prediction["vpd_drying_weight"]
    )

    model_prediction = model_runtime.predict_percent(snapshot.normalized_history)
    raw_model_delta = 0.0 if model_prediction is None else model_prediction - (current - drying_rate)
    model_delta = raw_model_delta
    model_direction_gated = False
    if model_delta > 0.0:
        model_delta *= prediction["conflicting_model_weight_scale"]
        model_direction_gated = True
    memory_prediction, confidence = memory_bank.recall(snapshot.normalized_history[-1])
    memory_delta = 0.0 if memory_prediction is None else memory_prediction - current
    memory_weight = min(prediction["memory_weight_limit"], confidence)

    trajectories = {}
    for candidate in candidates:
        water_sec = candidate["water_sec"]
        raw_rise = learner.efficiency_k * water_sec
        safe_rise = min(
            raw_rise,
            learner.efficiency_k * water_sec * prediction["maximum_rise_safety_factor"]
        )
        rise = _soft_limited_rise(
            safe_rise,
            max(0.0, upper - current),
            prediction["watering_soft_cap_fraction"]
        )
        values = [round(float(np.clip(current, lower, upper)), 2)]
        for step in range(horizon_steps):
            water_effect = rise * (prediction["watering_decay"] ** step)
            physical = current + water_effect - drying_rate * (step + 1)
            model_effect = model_delta * prediction["model_weight"] * ((step + 1) / horizon_steps)
            memory_effect = memory_delta * memory_weight * ((step + 1) / horizon_steps)
            if water_sec <= 0.0:
                model_effect = min(0.0, model_effect)
                memory_effect = min(0.0, memory_effect)
            proposed = physical + model_effect + memory_effect
            if water_sec <= 0.0:
                proposed = min(proposed, values[-1])
            _bounded_append(
                values, proposed,
                lower, upper, prediction["maximum_step_jump"]
            )
        trajectories[candidate["label"]] = values[1:]
    return trajectories, {
        "model_used": model_prediction is not None,
        "memory_used": memory_prediction is not None,
        "model_direction_gated": model_direction_gated,
        "drying_rate": round(drying_rate, 4),
        "vpd": round(vpd, 4)
    }
