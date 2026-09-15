import copy
import json
import os
import threading


DEFAULT_CONFIG = {
    "paths": {
        "request": "/dev/shm/pred_request.json",
        "response": "/dev/shm/pred_response.json",
        "soil_csv": "/root/data/water_test_soil3.csv",
        "air_humidity_csv": "/root/data/air_humidity.csv",
        "model_weights": "/root/water/wyc/brain_weights_v3.pth",
        "state": "/root/water/wyc/system_state_v3.json",
        "memory_bank": "/root/water/wyc/memory_bank_v3.pkl",
        "training_queue": "/root/water/wyc/training_queue_v3.pkl",
        "feedback_log": "/root/water/phase3/soil3/phase2_feedback_log.csv",
        "predictor_log": "/root/water/wyc/predictor_log.csv",
        "training_log": "/root/water/wyc/training_log.csv",
        "service_log": "/root/water/wyc/phase2_service.log"
    },
    "service": {
        "poll_seconds": 0.2,
        "max_horizon_steps": 72,
        "weather_cache_seconds": 1800,
        "weather_timeout_seconds": 0.8,
        "minimum_history_rows": 12
    },
    "model": {
        "device": "cpu",
        "input_dim": 6,
        "sequence_length": 12,
        "hidden_dim": 32,
        "num_threads": 1,
        "num_interop_threads": 1
    },
    "weather": {
        "url": "https://api.open-meteo.com/v1/forecast",
        "latitude": 31.6426,
        "longitude": 120.7435,
        "default_temperature": 25.0,
        "default_humidity": 50.0
    },
    "prediction": {
        "field_capacity": 45.0,
        "dynamic_max_tolerance": 1.0,
        "minimum_humidity": 0.0,
        "default_drying_per_step": 0.2,
        "maximum_drying_per_step": 2.0,
        "vpd_drying_weight": 0.08,
        "model_weight": 0.25,
        "memory_weight_limit": 0.25,
        "conflicting_model_weight_scale": 0.0,
        "watering_decay": 0.90,
        "watering_soft_cap_fraction": 0.98,
        "maximum_step_jump": 3.0,
        "maximum_rise_safety_factor": 1.5
    },
    "learning": {
        "default_efficiency_k": 0.5,
        "minimum_efficiency_k": 0.05,
        "maximum_efficiency_k": 5.0,
        "feedback_alpha": 0.15,
        "memory_capacity": 200,
        "memory_similarity_threshold": 0.25
    },
    "training": {
        "enabled": True,
        "learning_rate": 0.0005,
        "target_delay_seconds": 43200,
        "max_batches_per_cycle": 2,
        "max_queue_size": 200,
        "gradient_clip": 1.0,
        "save_every_batches": 1
    }
}


def _deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigManager:
    def __init__(self, path=None):
        default_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
        self.path = path or os.environ.get("WYC_PHASE2_CONFIG", default_path)
        self._mtime = None
        self._config = copy.deepcopy(DEFAULT_CONFIG)
        self._lock = threading.Lock()
        self.reload_if_changed(force=True)

    def reload_if_changed(self, force=False):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if not force and self._mtime == mtime:
            return False
        with open(self.path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        with self._lock:
            self._config = _deep_merge(DEFAULT_CONFIG, loaded)
            self._mtime = mtime
        return True

    def get(self):
        with self._lock:
            return copy.deepcopy(self._config)
