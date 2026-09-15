import csv
import logging
import os
import pickle
import tempfile
import time


def _atomic_pickle_write(path, value):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            pickle.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _append_training_log(path, row):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


class OnlineTrainer:
    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.queue = []
        self.trained_since_save = 0
        self._load()

    def _load(self):
        path = self.config["paths"]["training_queue"]
        if not os.path.exists(path):
            return
        try:
            with open(path, "rb") as handle:
                loaded = pickle.load(handle)
            if isinstance(loaded, list):
                self.queue = loaded
        except Exception:
            self.logger.exception("Failed to load online training queue")

    def save_queue(self):
        _atomic_pickle_write(self.config["paths"]["training_queue"], self.queue)

    @staticmethod
    def _supports_training(model_runtime):
        checker = getattr(model_runtime, "supports_training", None)
        return bool(checker and checker())

    def enqueue(self, snapshot, model_runtime, now=None):
        training = self.config["training"]
        if not training["enabled"] or not self._supports_training(model_runtime):
            return False
        required_length = self.config["model"]["sequence_length"]
        if len(snapshot.normalized_history) != required_length:
            return False
        if any(item.get("sensor_timestamp") == snapshot.timestamp for item in self.queue):
            return False
        current_time = time.time() if now is None else float(now)
        self.queue.append({
            "sensor_timestamp": snapshot.timestamp,
            "created_at": current_time,
            "target_at": current_time + float(training["target_delay_seconds"]),
            "sequence": snapshot.normalized_history
        })
        self.queue = self.queue[-int(training["max_queue_size"]):]
        self.save_queue()
        return True

    def train_matured(self, model_runtime, actual_humidity, now=None):
        training = self.config["training"]
        result = {"trained_batches": 0, "mean_loss": None, "queue_size": len(self.queue)}
        if not training["enabled"] or not self._supports_training(model_runtime):
            return result
        current_time = time.time() if now is None else float(now)
        limit = int(training["max_batches_per_cycle"])
        matured = [item for item in self.queue if float(item["target_at"]) <= current_time][:limit]
        if not matured:
            return result

        losses = []
        completed_ids = set()
        for item in matured:
            try:
                loss = model_runtime.train_sample(
                    item["sequence"], actual_humidity, training["gradient_clip"]
                )
                if loss is not None:
                    losses.append(loss)
                    completed_ids.add(id(item))
                    _append_training_log(self.config["paths"]["training_log"], {
                        "trained_at": current_time,
                        "source_sensor_timestamp": item.get("sensor_timestamp", ""),
                        "target_humidity": round(float(actual_humidity), 4),
                        "loss": round(loss, 8)
                    })
            except Exception:
                self.logger.exception("Online Transformer backpropagation failed")

        self.queue = [item for item in self.queue if id(item) not in completed_ids]
        self.trained_since_save += len(losses)
        if losses and self.trained_since_save >= int(training["save_every_batches"]):
            model_runtime.save_weights()
            self.trained_since_save = 0
        self.save_queue()
        result["trained_batches"] = len(losses)
        result["mean_loss"] = sum(losses) / len(losses) if losses else None
        result["queue_size"] = len(self.queue)
        return result
