import csv
import json
import logging
import math
import os
import random
from datetime import datetime

from .learning import atomic_json_write
from .watering_models import WateringModelRuntime
from .watering_samples import _nearest_index


def _append_csv(path, row):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _window_backprop_log_path(config):
    hourly_path = config["paths"]["natural_backprop_log"]
    return os.path.join(os.path.dirname(hourly_path), "natural_window_backprop_log.csv")


def _safe_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def natural_horizon_bias_adjustments(config):
    settings = config.get("recent_bias_correction", {})
    if not settings.get("natural_enabled", True):
        return {}
    path = config.get("paths", {}).get("prediction_effect_report")
    if not path or not os.path.exists(path):
        return {}
    hours = [int(hour) for hour in settings.get("natural_hours", [9, 10, 11, 12])]
    min_samples = max(1, int(settings.get("min_samples", 6)))
    max_samples = max(min_samples, int(settings.get("max_samples", 48)))
    minimum_sign_consistency = float(settings.get("minimum_sign_consistency", 0.65))
    maximum = abs(float(settings.get("maximum_adjustment", 3.0)))
    residuals = {hour: [] for hour in hours}
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return {}
    for row in rows:
        if row.get("model") != "natural":
            continue
        try:
            hour = int(float(row.get("forecast_hour", "")))
        except (TypeError, ValueError):
            continue
        if hour not in residuals:
            continue
        predicted = _safe_float(row.get("predicted_humidity"))
        actual = _safe_float(row.get("actual_humidity"))
        if predicted is None or actual is None:
            continue
        residuals[hour].append(actual - predicted)
        residuals[hour] = residuals[hour][-max_samples:]

    pooled = []
    for values in residuals.values():
        pooled.extend(values)
    pooled = pooled[-max_samples:]

    def stable_mean(values):
        if len(values) < min_samples:
            return None
        positive = sum(1 for value in values if value > 0)
        negative = sum(1 for value in values if value < 0)
        sign_consistency = max(positive, negative) / float(len(values))
        if sign_consistency < minimum_sign_consistency:
            return None
        value = sum(values) / float(len(values))
        return max(-maximum, min(maximum, value))

    pooled_value = stable_mean(pooled)
    adjustments = {}
    for hour, values in residuals.items():
        value = stable_mean(values)
        if value is None:
            value = pooled_value
        if value is not None:
            adjustments[hour] = round(value, 4)
    return adjustments


def apply_natural_horizon_bias_correction(trajectory, config):
    adjustments = natural_horizon_bias_adjustments(config)
    if not adjustments:
        return list(trajectory), {}
    corrected = list(trajectory)
    applied = {}
    for hour, adjustment in adjustments.items():
        index = int(hour) - 1
        if 0 <= index < len(corrected):
            corrected[index] = round(float(corrected[index]) + float(adjustment), 4)
            applied[f"h{hour}"] = round(float(adjustment), 4)
    return corrected, applied


def build_natural_samples(rows, config):
    timestamps = [row[0] for row in rows]
    sequence_length = int(config["model"]["sequence_length"])
    tolerance = float(config["offline_training"]["target_tolerance_seconds"])
    samples = []
    maximum = int(config["shadow_training"]["maximum_natural_samples_in_memory"])
    source_start = max(sequence_length - 1, len(rows) - maximum * 2)
    for source in range(source_start, len(rows)):
        metadata = rows[source][3]
        if metadata.get("learned_phase") != "NATURAL":
            continue
        if metadata["watered"] > 0 or metadata["water_seconds"] > 0:
            continue
        targets = []
        for hour in range(1, 13):
            index = _nearest_index(timestamps, rows[source][0] + hour * 3600, source + 1, tolerance)
            if index is None:
                targets = []
                break
            targets.append(index)
        if not targets:
            continue
        source_segment = metadata.get("segment_id")
        intervened = any(
            rows[index][3].get("segment_id") != source_segment
            for index in range(source + 1, targets[-1] + 1)
        )
        sequence = [row[1] for row in rows[source - sequence_length + 1:source + 1]]
        trajectory = [rows[index][2] for index in targets]
        samples.append((sequence, trajectory, {
            **metadata,
            "source_timestamp": rows[source][0],
            "target_timestamp": rows[source][0] + 43200,
            "intervened": intervened,
            "segment_id": source_segment,
        }))
        if len(samples) > maximum:
            samples.pop(0)
    return samples


class NaturalTrainer:
    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger("phase2.natural_training")
        self.runtime = WateringModelRuntime(config, self.logger, "natural_weights")
        self.state = self._load_state()

    def update_config(self, config):
        self.config = config
        self.runtime.update_config(config)

    def _load_state(self):
        state = {
            "offline_bootstrap_complete": False,
            "trained_natural_windows": 0,
            "last_predicted_source_timestamp": 0.0,
            "pending_predictions": [],
        }
        path = self.config["paths"]["natural_state"]
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    state.update(loaded)
            except Exception:
                self.logger.exception("Failed to load natural training state")
        return state

    def _train(self, samples, epochs):
        if not samples:
            return []
        shuffled = list(samples)
        rng = random.Random(20260614)
        size = int(self.config["shadow_training"]["batch_size"])
        losses = []
        for _epoch in range(int(epochs)):
            rng.shuffle(shuffled)
            for start in range(0, len(shuffled), size):
                loss = self.runtime.train_trajectory_batch(
                    shuffled[start:start + size], self.config["training"]["gradient_clip"]
                )
                if loss is not None:
                    losses.append(loss)
        if losses:
            self.runtime.save()
        return losses

    def _bootstrap(self, samples):
        if self.state["offline_bootstrap_complete"]:
            return 0, []
        clean = [sample for sample in samples if not sample[2]["intervened"]]
        validation_count = max(1, int(len(clean) * float(self.config["shadow_training"]["validation_fraction"])))
        gap = int(self.config["shadow_training"]["validation_gap_samples"])
        train = clean[:max(0, len(clean) - validation_count - gap)]
        train = train[-int(self.config["shadow_training"]["natural_max_bootstrap_samples"]):]
        losses = self._train(
            [(sample[0], sample[1]) for sample in train],
            self.config["shadow_training"]["natural_bootstrap_epochs"],
        )
        self.state["offline_bootstrap_complete"] = True
        self.state["trained_natural_windows"] += len(train) if losses else 0
        self.state["last_predicted_source_timestamp"] = max(
            (sample[2]["source_timestamp"] for sample in samples), default=0.0
        )
        return len(train), losses

    def _record_predictions(self, rows):
        sequence_length = int(self.config["model"]["sequence_length"])
        last_timestamp = float(self.state["last_predicted_source_timestamp"])
        created = 0
        for index in range(sequence_length - 1, len(rows)):
            metadata = rows[index][3]
            if rows[index][0] <= last_timestamp:
                continue
            if metadata.get("learned_phase") != "NATURAL":
                self.state["last_predicted_source_timestamp"] = rows[index][0]
                continue
            if metadata["watered"] > 0 or metadata["water_seconds"] > 0:
                self.state["last_predicted_source_timestamp"] = rows[index][0]
                continue
            sequence = [row[1] for row in rows[index - sequence_length + 1:index + 1]]
            trajectory, _peak = self.runtime.predict(sequence)
            trajectory, bias_adjustments = apply_natural_horizon_bias_correction(
                trajectory, self.config
            )
            pending = {
                "source_timestamp": rows[index][0],
                "target_timestamp": rows[index][0] + 43200,
                "sequence": sequence,
                "metadata": metadata,
                "predicted_trajectory": trajectory,
                "bias_adjustments": bias_adjustments,
            }
            self.state["pending_predictions"].append(pending)
            output = {
                "source_time": metadata["time"], "temperature": metadata["temperature"],
                "humidity": metadata["humidity"], "light": metadata["light"], "ec": metadata["ec"],
                "watered": metadata["watered"], "water_seconds": metadata["water_seconds"],
            }
            output.update({f"predicted_humidity_h{hour}": round(value, 4) for hour, value in enumerate(trajectory, 1)})
            _append_csv(self.config["paths"]["natural_prediction_log"], output)
            self.state["last_predicted_source_timestamp"] = rows[index][0]
            created += 1
        self.state["pending_predictions"] = self.state["pending_predictions"][-500:]
        return created

    def _train_matured(self, samples):
        by_timestamp = {sample[2]["source_timestamp"]: sample for sample in samples}
        matured, remaining = [], []
        for pending in self.state["pending_predictions"]:
            sample = by_timestamp.get(pending["source_timestamp"])
            if sample is None:
                remaining.append(pending)
            else:
                matured.append((pending, sample))
        clean = [sample for _pending, sample in matured if not sample[2]["intervened"]]
        losses = self._train(
            [(sample[0], sample[1]) for sample in clean],
            self.config["shadow_training"]["epochs_per_cycle"],
        )
        self.state["trained_natural_windows"] += len(clean) if losses else 0
        mean_loss = sum(losses) / len(losses) if losses else ""
        for pending, sample in matured:
            metadata = sample[2]
            predicted = pending["predicted_trajectory"]
            actual = sample[1]
            output = {
                "source_time": metadata["time"], "temperature": metadata["temperature"],
                "humidity": metadata["humidity"], "light": metadata["light"], "ec": metadata["ec"],
                "intervening_watering": metadata["intervened"],
                "training_status": "skipped_watering" if metadata["intervened"] else "backpropagated",
                "trajectory_mae": round(sum(abs(a - b) for a, b in zip(predicted, actual)) / 12.0, 4),
                "backprop_loss": round(mean_loss, 8) if not metadata["intervened"] and mean_loss != "" else "",
                "trained_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
            }
            output.update({f"actual_humidity_h{hour}": value for hour, value in enumerate(actual, 1)})
            _append_csv(_window_backprop_log_path(self.config), output)
        self.state["pending_predictions"] = remaining
        return len(clean), losses

    def process(self, rows, online_enabled=True, run_historical=True):
        samples = build_natural_samples(rows, self.config) if run_historical else []
        offline_count, offline_losses = self._bootstrap(samples)
        if offline_count > 0 and rows:
            self.state["last_predicted_source_timestamp"] = rows[-1][0]
            self.state["pending_predictions"] = []
        online_count, online_losses, predictions = 0, [], 0
        if online_enabled:
            online_count, online_losses = self._train_matured(samples)
            predictions = self._record_predictions(rows)
        clean = [sample for sample in samples if not sample[2]["intervened"]]
        validation_count = max(
            1, int(len(clean) * float(self.config["shadow_training"]["validation_fraction"]))
        ) if clean else 0
        validation = clean[-validation_count:] if validation_count else []
        trajectory_mae = None
        if validation:
            trajectory_mae, _unused = self.runtime.evaluate(
                [(sample[0], sample[1], max(sample[1]), sample[2]) for sample in validation[-288:]]
            )
            self.state["last_validation_mae"] = trajectory_mae
        atomic_json_write(self.config["paths"]["natural_state"], self.state)
        return {
            "natural_offline_windows_trained": offline_count,
            "natural_online_windows_trained": online_count,
            "natural_predictions_created": predictions,
            "natural_offline_mean_loss": sum(offline_losses) / len(offline_losses) if offline_losses else None,
            "natural_online_mean_loss": sum(online_losses) / len(online_losses) if online_losses else None,
            "natural_trajectory_mae": (
                trajectory_mae if trajectory_mae is not None
                else self.state.get("last_validation_mae")
            ),
            "natural_pending_predictions": len(self.state["pending_predictions"]),
            "trained_natural_windows": self.state["trained_natural_windows"],
        }
