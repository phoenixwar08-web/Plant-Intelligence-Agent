import csv
import json
import logging
import os
import pickle
import tempfile
import time

import numpy as np


def atomic_json_write(path, value):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


class OnlineGMMBoundaryTracker:
    def __init__(self):
        self.humidity_samples = []

    def update(self, humidity):
        self.humidity_samples.append(float(humidity))
        self.humidity_samples = self.humidity_samples[-200:]

    def boundaries(self, default_min, default_max):
        if len(self.humidity_samples) < 5:
            return default_min, default_max
        return (
            max(default_min, float(np.percentile(self.humidity_samples, 5))),
            min(default_max, float(np.percentile(self.humidity_samples, 95)))
        )


class PatternMemoryBank:
    def __init__(self, path, capacity, threshold, logger=None):
        self.path = path
        self.capacity = capacity
        self.threshold = threshold
        self.logger = logger or logging.getLogger(__name__)
        self.memory = []
        self.extra_state = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "rb") as handle:
                state = pickle.load(handle)
            if isinstance(state, dict):
                self.memory = state.get("memory", [])
                self.extra_state = {key: value for key, value in state.items() if key != "memory"}
        except Exception:
            self.logger.exception("Failed to load memory bank")

    def save(self):
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(self.path, "wb") as handle:
                pickle.dump({**self.extra_state, "memory": self.memory}, handle)
        except Exception:
            self.logger.exception("Failed to save memory bank")

    def memorize(self, features, target):
        self.memory.append({"x": np.asarray(features), "y": float(target), "timestamp": time.time()})
        self.memory = self.memory[-self.capacity:]

    def recall(self, features):
        if not self.memory:
            return None, 0.0
        current = np.asarray(features)
        candidates = []
        for item in self.memory:
            try:
                candidates.append((float(np.linalg.norm(current - np.asarray(item["x"]))), item))
            except Exception:
                continue
        if not candidates:
            return None, 0.0
        distance, best = min(candidates, key=lambda item: item[0])
        if distance >= self.threshold:
            return None, 0.0
        confidence = min(1.0, max(0.05, 1.0 - distance / self.threshold))
        target = float(best["y"])
        return target * 100.0 if target <= 1.0 else target, confidence


class AdaptiveWateringLearner:
    def __init__(self, config, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.config = config
        self.state_path = config["paths"]["state"]
        learning = config["learning"]
        self.state = {
            "efficiency_k": learning["default_efficiency_k"],
            "dyn_min": config["prediction"]["minimum_humidity"],
            "dyn_max": config["prediction"]["field_capacity"],
            "feedback_rows_processed": 0
        }
        self.gmm = OnlineGMMBoundaryTracker()
        self._load()

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self.state.update(loaded)
        except Exception:
            self.logger.exception("Failed to load learner state; using defaults")

    @property
    def efficiency_k(self):
        return float(self.state["efficiency_k"])

    def process_feedback(self, memory_bank):
        path = self.config["paths"]["feedback_log"]
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            start = min(int(self.state.get("feedback_rows_processed", 0)), len(rows))
            processed = 0
            for row in rows[start:]:
                if str(row.get("valid_sample", "")).lower() not in ("1", "true", "yes"):
                    continue
                water_sec = float(row["water_sec"])
                delta = float(row["delta_m"])
                if water_sec <= 0:
                    continue
                real_k = delta / water_sec
                learning = self.config["learning"]
                if not learning["minimum_efficiency_k"] <= real_k <= learning["maximum_efficiency_k"]:
                    continue
                alpha = learning["feedback_alpha"]
                self.state["efficiency_k"] = (1.0 - alpha) * self.efficiency_k + alpha * real_k
                post_humidity = float(row["post_humidity"])
                self.gmm.update(post_humidity)
                features = [
                    float(row.get("temp", 0.0)) / 50.0,
                    float(row.get("ec_norm", 0.0)),
                    float(row.get("pre_humidity", 0.0)) / 100.0,
                    0.5,
                    0.5,
                    float(row.get("vpd", 0.0)) / 5.0
                ]
                memory_bank.memorize(features, post_humidity)
                processed += 1
            self.state["feedback_rows_processed"] = len(rows)
            self.state["dyn_min"], self.state["dyn_max"] = self.gmm.boundaries(
                self.config["prediction"]["minimum_humidity"],
                self.config["prediction"]["field_capacity"]
            )
            if rows[start:]:
                atomic_json_write(self.state_path, self.state)
                memory_bank.save()
            return processed
        except Exception:
            self.logger.exception("Failed to process Phase 3 feedback")
            return 0
