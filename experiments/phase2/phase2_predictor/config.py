import copy
import json
import os
import threading


DEFAULT_CONFIG = {
    "paths": {
        "request": "runtime/requests/pred_request.json",
        "response": "runtime/responses/pred_response.json",
        "soil_csv": "dataset/soil.csv",
        "air_humidity_csv": "dataset/air_humidity.csv",
        "model_weights": "artifacts/models/phase2-model.pth",
        "state": "runtime/state/predictor_state.json",
        "feedback_log": "runtime/logs/phase2_feedback_log.csv",
        "predictor_log": "runtime/logs/predictor_log.csv",
        "training_log": "runtime/logs/training_log.csv",
        "offline_training_report": "runtime/reports/offline_training_report.json",
        "shadow_weights": "artifacts/models/phase2-shadow-model.pth",
        "shadow_state": "runtime/state/shadow_training_state.json",
        "model_readiness": "runtime/state/model_readiness.json",
        "shadow_training_log": "runtime/logs/shadow_training_log.csv",
        "online_prediction_log": "runtime/logs/online_predictions.csv",
        "online_backprop_log": "runtime/logs/online_backprop_log.csv",
        "natural_weights": "artifacts/models/natural-model.pth",
        "natural_state": "runtime/state/natural_training_state.json",
        "natural_prediction_log": "runtime/logs/natural_predictions.csv",
        "natural_backprop_log": "runtime/logs/natural_backprop_log.csv",
        "prediction_effect_report": "runtime/reports/prediction_effect.csv",
        "prediction_effect_12h_report": "runtime/reports/prediction_effect_12h.csv",
        "watering_prediction_effect_report": "runtime/reports/watering_prediction_effect.csv",
        "peak_calibration": "runtime/state/peak_calibration.json",
        "phase3_state": "runtime/control/phase3_state.json",
        "phase3_trials": "runtime/control/phase3_trials.json",
        "phase3_evolving_params": "runtime/control/phase3_evolving_params.json",
        "phase3_irrigation_profile": "runtime/control/phase3_irrigation_profile.json",
        "phase3_response_predictions": "runtime/reports/phase3_response_predictions.json",
        "phase3_response_effect_12h": "runtime/reports/phase3_response_prediction_effect_12h.csv",
        "shadow_interface_status": "runtime/state/shadow_pred_status.json",
        "expired_tasks_log": "runtime/logs/expired_tasks.csv",
        "segmented_queue": "runtime/state/segmented_training_queue.json",
        "phase_state": "runtime/state/phase_state.json",
        "response_events_log": "runtime/logs/response_events.csv",
        "data_quality_log": "runtime/logs/data_quality_issues.csv",
        "resource_log": "runtime/logs/resource_usage.csv",
        "service_log": "runtime/logs/training_service.log"
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
        "input_dim": 9,
        "sequence_length": 12,
        "hidden_dim": 32,
        "num_threads": 1,
        "num_interop_threads": 1
    },
    "weather": {
        "url": "https://api.open-meteo.com/v1/forecast",
        "latitude": 0.0,
        "longitude": 0.0,
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
        "save_every_batches": 1,
        "skip_intervened_targets": True
    },
    "loss_weights": {
        "trajectory_h12_point": 4.0,
        "trajectory": 0.35,
        "peak": 2.0,
        "h12": 2.0,
        "consistency": 0.2,
        "time": 0.2,
        "smoothness": 0.05,
        "natural_rise": 0.10
    },
    "training_quality": {
        "enabled": True,
        "trial_match_window_seconds": 2400,
        "trial_humidity_tolerance": 1.0
    },
    "calibration": {
        "enabled": True,
        "mode": "shadow",
        "ema_alpha": 0.2,
        "minimum_global_samples": 5,
        "minimum_zone_samples": 3,
        "zone_shrinkage_samples": 5.0,
        "maximum_events": 100,
        "maximum_sample_residual": 10.0,
        "maximum_activation_residual": 6.0,
        "maximum_absolute_bias": 15.0,
        "maximum_bias_mad": 2.5,
        "minimum_sign_consistency": 0.9,
        "minimum_kp_samples": 5,
        "kp_prior_blend": 0.25,
        "maximum_total_adjustment": 15.0,
        "trajectory_correction_decay": 0.85,
        "target_low": 33.0,
        "safe_sleep": 42.5
    },
    "profile_correction": {
        "enabled": True,
        "mode": "shadow",
        "max_peak_bias": 15.0,
        "kp_blend_weight": 0.35,
        "min_samples_for_bias": 3,
        "maximum_profile_bytes": 8192,
        "maximum_identifier_length": 128
    },
    "natural_observe_guard": {
        "enabled": True,
        "zones": ["high"],
        "minimum_source_humidity_by_zone": {
            "high": 37.0
        },
        "max_drop_12h_by_zone": {
            "high": 2.8
        },
        "min_drop_12h_by_zone": {
            "high": 0.8
        },
        "slope_steps_per_hour": 12.0,
        "horizon_hours": 12.0,
        "slope_multiplier": 1.0,
        "slope_margin_12h": 0.8,
        "drop_curve_exponent": 1.0
    },
    "recent_bias_correction": {
        "natural_enabled": True,
        "natural_hours": [9, 10, 11, 12],
        "min_samples": 6,
        "max_samples": 48,
        "minimum_sign_consistency": 0.65,
        "maximum_adjustment": 3.0
    },
    "phase3_response_retention": {
        "queue_max_records": 500,
        "queue_retention_days": 7,
        "effect_max_rows": 2000,
        "observe_labels": [
            "style_observe",
            "style_drydown_observe",
            "style_wet_hold_observe",
            "trigger_guard_observe"
        ]
    },
    "offline_training": {
        "epochs": 20,
        "validation_fraction": 0.2,
        "minimum_samples": 24,
        "maximum_validation_mae": 15.0,
        "target_tolerance_seconds": 1800,
        "early_stopping_patience": 5
    },
    "shadow_training": {
        "poll_seconds": 300,
        "max_history_rows": 100000,
        "max_samples_per_cycle": 50000,
        "batch_size": 64,
        "epochs_per_cycle": 3,
        "bootstrap_epochs": 300,
        "natural_bootstrap_epochs": 8,
        "natural_max_bootstrap_samples": 12000,
        "maximum_natural_samples_in_memory": 15000,
        "validation_fraction": 0.2,
        "validation_gap_samples": 144,
        "validation_stride": 10,
        "validation_window": 288,
        "minimum_trained_samples": 1000,
        "maximum_validation_mae": 8.0,
        "required_consecutive_passes": 3,
        "peak_recalibration_version": 2,
        "peak_recalibration_epochs": 120,
        "peak_recalibration_patience": 15,
        "peak_recalibration_learning_rate": 0.002,
        "peak_recalibration_validation_fraction": 0.2,
        "promote_when_ready": True
    },
    "online_cycle": {
        "prediction_interval_seconds": 3600,
        "label_interval_seconds": 3600,
        "task_expiry_seconds": 259200,
        "maximum_pending_tasks": 96,
        "online_batch_size": 16,
        "maximum_backpropagations_per_cycle": 8,
        "validation_interval_seconds": 86400,
        "maximum_validation_mae": 8.0,
        "maximum_time_mae_minutes": 240,
        "online_peak_epochs": 5,
        "online_peak_patience": 3,
        "online_peak_learning_rate": 0.0001,
        "online_peak_batch_size": 8
    },
    "readiness": {
        "minimum_independent_watering_events": 30,
        "maximum_trajectory_mae": 3.0,
        "maximum_peak_mae": 2.0,
        "maximum_h12_mae": 2.0,
        "maximum_dose_peak_mae": 2.0,
        "maximum_natural_mae": 2.0,
        "maximum_peak_bias": 0.5,
        "maximum_peak_p90_error": 3.0
    },
    "phase_detection": {
        "baseline_history_rows": 100000,
        "natural_guard_seconds": 43200,
        "window_rows": 6,
        "baseline_percentile": 95,
        "minimum_baseline_windows": 100,
        "required_stable_windows": 3,
        "maximum_response_seconds": 43200,
        "maximum_watering_merge_gap_seconds": 1800,
        "maximum_watering_event_seconds": 3600,
        "minimum_interrupted_response_seconds": 1800
    },
    "data_quality": {
        "maximum_gap_seconds": 1800,
        "maximum_humidity_jump": 15.0,
        "minimum_humidity": 0.0,
        "maximum_humidity": 100.0,
        "minimum_temperature": -20.0,
        "maximum_temperature": 70.0,
        "minimum_light": 0.0,
        "maximum_light": 200000.0,
        "minimum_ec": 0.0,
        "maximum_ec": 10000.0,
        "maximum_water_seconds": 120.0
    },
    "resource_limits": {
        "maximum_rss_mb": 450,
        "pause_training_rss_mb": 420
    },
    "decision": {
        "candidate_water_seconds": [3.0, 6.0, 9.0],
        "minimum_humidity_12h": 32.0,
        "target_humidity_12h": None,
        "maximum_peak_humidity": 45.0,
        "under_target_penalty_weight": 20.0,
        "over_peak_penalty_weight": 20.0,
        "target_error_weight": 1.0,
        "water_seconds_penalty_weight": 0.05,
        "control_horizon": {
            "enabled": True,
            "short_steps": 3,
            "medium_steps": 6,
            "short_weight": 1.0,
            "medium_weight": 0.4,
            "long_weight": 0.1,
            "h12_weight": 0.25,
            "peak_weight": 0.15,
            "hard_peak_margin": 2.0
        }
    },
    "npu_inference": {
        "enabled": False,
        "required": False,
        "device_id": 0,
        "soc_version": "Ascend310B4",
        "natural_om": "artifacts/npu/natural_model.om",
        "watering_om": "artifacts/npu/watering_model.om",
        "log": "runtime/logs/npu_inference_log.csv"
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
