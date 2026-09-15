import csv
import json
import logging
import os
import signal
import time

from .config import ConfigManager
from .data_sources import WeatherProvider, read_sensor_snapshot
from .learning import AdaptiveWateringLearner, PatternMemoryBank, atomic_json_write
from .models import ModelRuntime
from .trajectory import generate_trajectories
from .training import OnlineTrainer


def configure_logging(path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    logger = logging.getLogger("phase2")
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


def validate_request(payload, max_horizon):
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    timestamp = float(payload["timestamp"])
    horizon = int(payload.get("horizon_steps", 12))
    if not 1 <= horizon <= max_horizon:
        raise ValueError("horizon_steps is outside allowed range")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("candidates must be a non-empty list")
    candidates = []
    labels = set()
    for raw in raw_candidates:
        label = raw.get("label")
        water_sec = float(raw.get("water_sec"))
        if not isinstance(label, str) or not label.strip() or label in labels:
            raise ValueError("candidate labels must be unique non-empty strings")
        if not 0.0 <= water_sec <= 3600.0:
            raise ValueError("water_sec is outside allowed range")
        labels.add(label)
        candidates.append({"label": label, "water_sec": water_sec})
    return timestamp, candidates, horizon


def append_prediction_log(path, row):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


class PredictorService:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.config = config_manager.get()
        self.logger = configure_logging(self.config["paths"]["service_log"])
        self.weather = WeatherProvider(self.logger)
        self.model = ModelRuntime(self.config, self.logger)
        learning = self.config["learning"]
        self.memory = PatternMemoryBank(
            self.config["paths"]["memory_bank"],
            learning["memory_capacity"],
            learning["memory_similarity_threshold"],
            self.logger
        )
        self.learner = AdaptiveWateringLearner(self.config, self.logger)
        self.trainer = OnlineTrainer(self.config, self.logger)
        self.last_timestamp = None
        self.running = True

    def stop(self, *_args):
        self.running = False

    def close(self):
        for handler in self.logger.handlers:
            handler.close()
        self.logger.handlers.clear()

    def process_once(self):
        self.config_manager.reload_if_changed()
        self.config = self.config_manager.get()
        request_path = self.config["paths"]["request"]
        if not os.path.exists(request_path):
            return False
        with open(request_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        timestamp, candidates, horizon = validate_request(
            payload, self.config["service"]["max_horizon_steps"]
        )
        if timestamp == self.last_timestamp:
            return False

        started = time.perf_counter()
        self.learner.config = self.config
        feedback_count = self.learner.process_feedback(self.memory)
        snapshot = read_sensor_snapshot(self.config, self.weather)
        trajectories, details = generate_trajectories(
            snapshot, candidates, horizon, self.config, self.learner, self.memory, self.model
        )
        minimum_rows = self.config["service"]["minimum_history_rows"]
        fallback = len(snapshot.normalized_history) < minimum_rows or not details["model_used"]
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        response = {
            "timestamp": timestamp,
            "model": "wyc_v3_edge_transformer" if not fallback else "wyc_physical_fallback",
            "status": "ok",
            "trajectories": trajectories
        }
        atomic_json_write(self.config["paths"]["response"], response)
        self.trainer.config = self.config
        training_result = self.trainer.train_matured(self.model, snapshot.humidity)
        self.trainer.enqueue(snapshot, self.model)
        append_prediction_log(self.config["paths"]["predictor_log"], {
            "request_timestamp": timestamp,
            "sensor_timestamp": snapshot.timestamp,
            "current_humidity": round(snapshot.humidity, 2),
            "trajectory_ends": json.dumps(
                {label: values[-1] for label, values in trajectories.items()},
                ensure_ascii=False
            ),
            "elapsed_ms": elapsed_ms,
            "model_status": self.model.status,
            "fallback": fallback,
            "feedback_rows": feedback_count,
            "trained_batches": training_result["trained_batches"],
            "training_loss": training_result["mean_loss"],
            "training_queue_size": training_result["queue_size"]
        })
        self.last_timestamp = timestamp
        return True

    def run(self):
        self.logger.info("WYC Phase 2 predictor service started")
        try:
            while self.running:
                try:
                    self.process_once()
                except Exception:
                    self.logger.exception("Prediction request failed")
                time.sleep(float(self.config["service"]["poll_seconds"]))
            self.logger.info("WYC Phase 2 predictor service stopped")
        finally:
            self.close()


def main():
    manager = ConfigManager()
    service = PredictorService(manager)
    signal.signal(signal.SIGINT, service.stop)
    signal.signal(signal.SIGTERM, service.stop)
    service.run()
