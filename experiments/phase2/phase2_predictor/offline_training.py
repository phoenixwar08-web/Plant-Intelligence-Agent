import collections
import csv
import copy
import logging
import os
import shutil
from bisect import bisect_left
from datetime import datetime

from .data_sources import normalize
from .learning import atomic_json_write
from .models import ModelRuntime


TIMESTAMP_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


def _parse_timestamp(value):
    text = value.strip()
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except (ValueError, OSError, OverflowError):
            continue
    return None


def load_historical_rows(config):
    path = config["paths"]["soil_csv"]
    maximum_rows = int(config.get("shadow_training", {}).get("max_history_rows", 100000))
    raw_rows = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        source = collections.deque(handle, maxlen=maximum_rows + 1)
        for raw in csv.reader(source):
            if len(raw) < 5:
                continue
            timestamp = _parse_timestamp(raw[0])
            if timestamp is None:
                continue
            try:
                temperature = float(raw[2])
                humidity = float(raw[3])
                ec = float(raw[4])
                watered = float(raw[5]) if len(raw) > 5 and raw[5] else 0.0
                light = float(raw[6]) if len(raw) > 6 and raw[6] else 0.0
                water_seconds = float(raw[7]) if len(raw) > 7 and raw[7] else 0.0
            except ValueError:
                continue
            raw_rows.append((timestamp, {
                "time": raw[0].strip(),
                "temperature": temperature,
                "humidity": humidity,
                "light": light,
                "ec": ec,
                "watered": watered,
                "water_seconds": water_seconds,
            }))
    raw_rows.sort(key=lambda item: item[0])
    rows = []
    previous_temperature = None
    previous_humidity = None
    last_watering_timestamp = None
    for timestamp, metadata in raw_rows:
        temperature_delta = (
            0.0 if previous_temperature is None else metadata["temperature"] - previous_temperature
        )
        humidity_delta = 0.0 if previous_humidity is None else metadata["humidity"] - previous_humidity
        if metadata["watered"] > 0 or metadata["water_seconds"] > 0:
            last_watering_timestamp = timestamp
        seconds_since_watering = (
            604800.0 if last_watering_timestamp is None else timestamp - last_watering_timestamp
        )
        metadata["temperature_delta"] = temperature_delta
        metadata["humidity_delta"] = humidity_delta
        metadata["seconds_since_watering"] = min(604800.0, max(0.0, seconds_since_watering))
        features = normalize([
            metadata["temperature"], metadata["ec"], metadata["humidity"], metadata["light"],
            metadata["watered"], metadata["water_seconds"], temperature_delta, humidity_delta,
            metadata["seconds_since_watering"],
        ])
        rows.append((timestamp, features, metadata["humidity"], metadata))
        previous_temperature = metadata["temperature"]
        previous_humidity = metadata["humidity"]
    # The logger marks a watering command on the next sensor report. Preserve
    # the preceding reading as time-aligned metadata for quality matching and a
    # future model-schema migration. Do not change the feature vector here:
    # existing production weights were trained with the legacy representation.
    for index in range(1, len(rows)):
        timestamp, features, observed_humidity, metadata = rows[index]
        if metadata["watered"] <= 0 and metadata["water_seconds"] <= 0:
            continue
        previous = rows[index - 1]
        metadata["pre_watering_humidity"] = previous[2]
        metadata["observed_post_command_humidity"] = observed_humidity
        metadata["training_source_humidity"] = previous[2]
    return rows


def build_timestamped_training_samples(rows, config):
    sequence_length = int(config["model"]["sequence_length"])
    delay = float(config["training"]["target_delay_seconds"])
    tolerance = float(config["offline_training"]["target_tolerance_seconds"])
    timestamps = [row[0] for row in rows]
    samples = []
    for end in range(sequence_length - 1, len(rows)):
        target_time = rows[end][0] + delay
        target_index = bisect_left(timestamps, target_time, lo=end + 1)
        if target_index >= len(rows):
            continue
        if not 0.0 <= timestamps[target_index] - target_time <= tolerance:
            continue
        sequence = [row[1] for row in rows[end - sequence_length + 1:end + 1]]
        intervened = any(
            row[3]["watered"] > 0 or row[3]["water_seconds"] > 0
            for row in rows[end + 1:target_index + 1]
        )
        samples.append((rows[end][0], sequence, rows[target_index][2], intervened))
    return samples


def build_training_samples(rows, config):
    samples = build_timestamped_training_samples(rows, config)
    if config["training"].get("skip_intervened_targets", True):
        samples = [sample for sample in samples if not sample[3]]
    return [(sequence, target) for _, sequence, target, _ in samples]


class OfflineTrainer:
    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    def run(self):
        report = {
            "status": "failed",
            "rows": 0,
            "samples": 0,
            "train_samples": 0,
            "validation_samples": 0,
            "epochs_completed": 0,
            "best_validation_mae": None,
            "reason": "",
        }
        try:
            rows = load_historical_rows(self.config)
            samples = build_training_samples(rows, self.config)
            report["rows"] = len(rows)
            report["samples"] = len(samples)
            minimum = int(self.config["offline_training"]["minimum_samples"])
            if len(samples) < minimum:
                report["reason"] = f"insufficient samples: {len(samples)} < {minimum}"
                return self._finish(report)

            validation_count = max(
                1, int(len(samples) * float(self.config["offline_training"]["validation_fraction"]))
            )
            train_samples = samples[:-validation_count]
            validation_samples = samples[-validation_count:]
            report["train_samples"] = len(train_samples)
            report["validation_samples"] = len(validation_samples)

            production_weights = self.config["paths"]["model_weights"]
            candidate_weights = production_weights + ".offline_candidate"
            if os.path.exists(production_weights):
                shutil.copy2(production_weights, candidate_weights)
            elif os.path.exists(candidate_weights):
                os.remove(candidate_weights)
            training_config = copy.deepcopy(self.config)
            training_config["paths"]["model_weights"] = candidate_weights
            runtime = ModelRuntime(training_config, self.logger, allow_fresh=True)
            if not runtime.supports_training():
                report["reason"] = f"model cannot train: {runtime.status}"
                return self._finish(report)

            epochs = int(self.config["offline_training"]["epochs"])
            patience = int(self.config["offline_training"]["early_stopping_patience"])
            best_mae = float("inf")
            stale_epochs = 0
            for epoch in range(epochs):
                for sequence, target in train_samples:
                    runtime.train_sample(
                        sequence, target, self.config["training"]["gradient_clip"]
                    )
                validation_mae = runtime.evaluate_samples(validation_samples)
                report["epochs_completed"] = epoch + 1
                if validation_mae is not None and validation_mae < best_mae:
                    best_mae = validation_mae
                    stale_epochs = 0
                    runtime.save_weights()
                else:
                    stale_epochs += 1
                if stale_epochs >= patience:
                    break

            report["best_validation_mae"] = round(best_mae, 6)
            maximum_mae = float(self.config["offline_training"]["maximum_validation_mae"])
            if best_mae <= maximum_mae:
                os.replace(candidate_weights, production_weights)
                report["status"] = "passed"
                report["reason"] = "offline training acceptance passed"
            else:
                report["reason"] = f"validation MAE {best_mae:.4f} exceeds {maximum_mae:.4f}"
                if os.path.exists(candidate_weights):
                    os.remove(candidate_weights)
            return self._finish(report)
        except Exception as exc:
            self.logger.exception("Offline training failed")
            report["reason"] = str(exc)
            return self._finish(report)

    def _finish(self, report):
        atomic_json_write(self.config["paths"]["offline_training_report"], report)
        return report
