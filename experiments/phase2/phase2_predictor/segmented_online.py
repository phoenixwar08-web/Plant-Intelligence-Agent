import csv
import json
import logging
import os
import shutil
import tempfile
import time
from bisect import bisect_left
from datetime import datetime, timedelta

from .learning import atomic_json_write
from .watering_models import WateringModelRuntime


def _append_csv(path, row):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    fieldnames = list(row.keys())
    existing_rows = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                existing_fieldnames = reader.fieldnames or []
                existing_rows = list(reader)
            for name in existing_fieldnames:
                if name not in fieldnames:
                    fieldnames.append(name)
            for name in row:
                if name not in existing_fieldnames:
                    existing_fieldnames.append(name)
            if existing_fieldnames != fieldnames:
                fieldnames = existing_fieldnames
                fd, temporary = tempfile.mkstemp(
                    prefix=os.path.basename(path), suffix=".tmp", dir=folder or "."
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
                        writer.writeheader()
                        writer.writerows(existing_rows)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.remove(temporary)
        except (OSError, csv.Error):
            existing_rows = []
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _append_evaluation_rows_sorted(path, new_rows):
    """Keep the human-facing hourly evaluation table sorted and deduplicated."""
    if not new_rows:
        return
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fieldnames = list(new_rows[0].keys())
    rows = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", newline="") as handle:
                rows.extend(
                    row for row in csv.DictReader(handle)
                    if row.get("source_time") and row.get("trained_hour") and row.get("actual_time")
                )
        except (OSError, csv.Error):
            rows = []
    rows.extend(new_rows)
    deduplicated = {}
    for row in rows:
        try:
            source = datetime.strptime(row["source_time"], "%Y/%m/%d %H:%M:%S")
            actual = datetime.strptime(row["actual_time"], "%Y/%m/%d %H:%M:%S")
            hour = int(float(row["trained_hour"]))
            if actual < source + timedelta(hours=hour):
                continue
        except (ValueError, TypeError):
            continue
        key = (row["source_time"], row.get("model", ""), row["trained_hour"], row["actual_time"])
        deduplicated[key] = {name: row.get(name, "") for name in fieldnames}

    def sort_key(row):
        try:
            source = datetime.strptime(row["source_time"], "%Y/%m/%d %H:%M:%S")
        except ValueError:
            source = datetime.min
        try:
            hour = int(float(row["trained_hour"]))
        except ValueError:
            hour = 0
        return source, hour, row["actual_time"]

    ordered = sorted(deduplicated.values(), key=sort_key)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path), suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(ordered)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _atomic_copy(source, destination):
    folder = os.path.dirname(destination) or "."
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(destination), suffix=".tmp", dir=folder)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


class SegmentedOnlineTrainer:
    """Creates hourly/state-change predictions and trains each newly observed hour once."""

    def __init__(self, config, natural_runtime, watering_runtime, logger=None):
        self.config = config
        self.natural = natural_runtime
        self.watering = watering_runtime
        self.logger = logger or logging.getLogger(__name__)
        self.path = config["paths"]["segmented_queue"]
        self.state = self._load()

    def update_config(self, config):
        self.config = config
        self.path = config["paths"]["segmented_queue"]

    def _load(self):
        default = {
            "initialized": False,
            "last_seen_timestamp": 0.0,
            "last_prediction_timestamp": 0.0,
            "last_status": None,
            "last_validation_at": 0.0,
            "tasks": [],
            "peak_candidates": [],
            "peak_trained_segments": [],
        }
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    default.update(loaded)
            except Exception:
                self.logger.exception("Failed to load segmented online queue")
        return default

    @staticmethod
    def _status(metadata):
        phase = metadata.get("learned_phase")
        if phase:
            return "natural" if phase == "NATURAL" else "watering"
        return "watering" if metadata["watered"] > 0 or metadata["water_seconds"] > 0 else "natural"

    def _new_task(self, rows, index, status):
        if status == "watering" and not rows[index][3].get("training_eligible", True):
            return False
        length = int(self.config["model"]["sequence_length"])
        sequence = [row[1] for row in rows[index - length + 1:index + 1]]
        runtime = self.watering if status == "watering" else self.natural
        details = runtime.predict_details(sequence)
        trajectory, peak = details["trajectory"], details["peak"]
        peak_hour = max(1, min(12, round(details["minutes_to_peak"] / 60.0)))
        settling_hour = max(1, min(12, round(details["minutes_to_natural"] / 60.0)))
        metadata = rows[index][3]
        water_seconds = metadata.get("segment_water_seconds", metadata["water_seconds"])
        confidence = self._confidence(metadata, status)
        task = {
            "id": f"{int(rows[index][0])}-{status}",
            "status": status,
            "source_timestamp": rows[index][0],
            "segment_id": metadata.get("segment_id"),
            "sequence": sequence,
            "predicted_trajectory": trajectory,
            "predicted_peak": peak,
            "predicted_peak_hour": peak_hour,
            "predicted_settling_hour": settling_hour,
            "confidence": confidence,
            "trained_mask": [0] * 12,
            "metadata": metadata,
        }
        self.state["tasks"].append(task)
        if status == "watering" and task.get("segment_id"):
            known = {
                item.get("segment_id") for item in self.state.get("peak_candidates", [])
            } | set(self.state.get("peak_trained_segments", []))
            if task["segment_id"] not in known:
                self.state.setdefault("peak_candidates", []).append({
                    "segment_id": task["segment_id"],
                    "source_timestamp": task["source_timestamp"],
                    "sequence": task["sequence"],
                    "metadata": task["metadata"],
                })
        output = {
            "source_time": metadata["time"], "model": status,
            "temperature": metadata["temperature"], "humidity": metadata["humidity"],
            "light": metadata["light"], "ec": metadata["ec"], "watered": metadata["watered"],
            "water_seconds": water_seconds, "predicted_peak": round(peak, 4),
            "predicted_humidity_12h": round(trajectory[11], 4),
            "predicted_peak_hour": peak_hour, "predicted_settling_hour": settling_hour,
            "confidence": confidence, "inference_backend": details.get("inference_backend", "cpu_pytorch"),
        }
        output.update({f"predicted_humidity_h{hour}": round(value, 4) for hour, value in enumerate(trajectory, 1)})
        path = (
            self.config["paths"]["online_prediction_log"]
            if status == "watering" else self.config["paths"]["natural_prediction_log"]
        )
        _append_csv(path, output)
        return True

    def _confidence(self, metadata, status):
        profile = self.config.get("_confidence_profile", {})
        if status == "natural":
            count = int(profile.get("natural_samples", 0))
            mae = float(profile.get("natural_mae", 20.0))
            if count <= 0:
                state_path = self.config.get("paths", {}).get("natural_state")
                try:
                    with open(state_path, encoding="utf-8") as handle:
                        natural_state = json.load(handle)
                    count = int(natural_state.get("trained_natural_windows", 0))
                    mae = float(natural_state.get("last_validation_mae", mae))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    pass
            sample_score = min(1.0, count / 120.0)
            error_scale = float(self.config.get("confidence", {}).get("natural_mae_full_loss", 8.0))
            max_conf = float(self.config.get("confidence", {}).get("natural_max_confidence", 0.65))
        else:
            seconds = str(int(round(metadata.get("segment_water_seconds", metadata["water_seconds"]))))
            count = int(profile.get("watering_counts", {}).get(seconds, 0))
            mae = float(profile.get("watering_mae_by_seconds", {}).get(seconds, 20.0))
            sample_score = min(1.0, count / 30.0)
            error_scale = float(self.config.get("confidence", {}).get("watering_mae_full_loss", 20.0))
            max_conf = 1.0
        error_score = max(0.0, 1.0 - mae / max(error_scale, 0.001))
        return round(min(max_conf, sample_score * error_score), 4)

    def _create_predictions(self, rows):
        if not rows:
            return 0
        if not self.state["initialized"]:
            self.state["initialized"] = True
            self.state["last_seen_timestamp"] = rows[-1][0]
            self.state["last_prediction_timestamp"] = rows[-1][0]
            self.state["last_status"] = self._status(rows[-1][3])
            return 0
        created = 0
        interval = float(self.config["online_cycle"]["prediction_interval_seconds"])
        length = int(self.config["model"]["sequence_length"])
        for index in range(length - 1, len(rows)):
            timestamp = rows[index][0]
            if timestamp <= float(self.state["last_seen_timestamp"]):
                continue
            status = self._status(rows[index][3])
            status_changed = status != self.state["last_status"]
            interval_reached = timestamp - float(self.state["last_prediction_timestamp"]) >= interval
            if status_changed or interval_reached:
                created_task = self._new_task(rows, index, status)
                self.state["last_prediction_timestamp"] = timestamp
                created += int(created_task)
            self.state["last_status"] = status
            self.state["last_seen_timestamp"] = timestamp
        return created

    @staticmethod
    def _nearest_row(rows, timestamps, target, tolerance):
        """Select a completed observation after the label time, never before it."""
        index = bisect_left(timestamps, target)
        if index >= len(rows):
            return None
        return rows[index] if 0.0 <= timestamps[index] - target <= tolerance else None

    def _train_available_hours(self, rows):
        limit = int(self.config["online_cycle"]["maximum_backpropagations_per_cycle"])
        tolerance = float(self.config["offline_training"]["target_tolerance_seconds"])
        jobs = {"natural": [], "watering": []}
        job_records = []
        timestamps = [row[0] for row in rows]
        metadata_by_timestamp = {row[0]: row[3] for row in rows}
        for task in self.state["tasks"]:
            source_metadata = metadata_by_timestamp.get(
                task.get("source_timestamp"), task.get("metadata", {})
            )
            if task["status"] == "watering" and not source_metadata.get("training_eligible", True):
                task["trained_mask"] = [-1] * 12
                continue
            for hour in range(1, 13):
                if task["trained_mask"][hour - 1]:
                    continue
                target_row = self._nearest_row(
                    rows, timestamps, task["source_timestamp"] + hour * 3600, tolerance
                )
                if target_row is None:
                    continue
                actual_status = self._status(target_row[3])
                if (
                    actual_status != task["status"]
                    or target_row[3].get("segment_id") != task.get("segment_id")
                ):
                    task["trained_mask"][hour - 1] = -1
                    continue
                targets = [0.0] * 12
                mask = [0.0] * 12
                targets[hour - 1] = target_row[2]
                mask[hour - 1] = 1.0
                jobs[task["status"]].append((task["sequence"], targets, mask))
                job_records.append((task, hour, target_row))
                if len(job_records) >= limit:
                    break
            if len(job_records) >= limit:
                break
        losses = {}
        for status, samples in jobs.items():
            if not samples:
                continue
            runtime = self.watering if status == "watering" else self.natural
            losses[status] = runtime.train_masked_trajectory_batch(
                samples[:int(self.config["online_cycle"]["online_batch_size"])],
                self.config["training"]["gradient_clip"],
            )
            runtime.save()
        evaluation_rows = {}
        for task, hour, target_row in job_records:
            task["trained_mask"][hour - 1] = 1
            prediction = task["predicted_trajectory"][hour - 1]
            path = (
                self.config["paths"]["online_backprop_log"]
                if task["status"] == "watering" else self.config["paths"]["natural_backprop_log"]
            )
            evaluation_rows.setdefault(path, []).append({
                "source_time": task["metadata"]["time"], "model": task["status"],
                "trained_hour": hour, "actual_time": target_row[3]["time"],
                "predicted_humidity": round(prediction, 4), "actual_humidity": target_row[2],
                "absolute_error": round(abs(prediction - target_row[2]), 4),
                "backprop_loss": round(losses.get(task["status"], 0.0), 8),
                "trained_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        for path, rows_to_write in evaluation_rows.items():
            _append_evaluation_rows_sorted(path, rows_to_write)
        return len(job_records), losses

    def _expire_tasks(self, latest_timestamp):
        expiry = float(self.config["online_cycle"]["task_expiry_seconds"])
        remaining = []
        expired = 0
        for task in self.state["tasks"]:
            complete = all(value != 0 for value in task["trained_mask"])
            too_old = latest_timestamp - float(task["source_timestamp"]) > expiry
            if complete or too_old:
                if too_old and not complete:
                    _append_csv(self.config["paths"]["expired_tasks_log"], {
                        "task_id": task["id"], "source_time": task["metadata"]["time"],
                        "model": task["status"], "trained_mask": json.dumps(task["trained_mask"]),
                        "expired_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
                    })
                    expired += 1
            else:
                remaining.append(task)
        maximum = int(self.config["online_cycle"]["maximum_pending_tasks"])
        overflow = max(0, len(remaining) - maximum)
        for task in remaining[:overflow]:
            _append_csv(self.config["paths"]["expired_tasks_log"], {
                "task_id": task["id"], "source_time": task["metadata"]["time"],
                "model": task["status"], "trained_mask": json.dumps(task["trained_mask"]),
                "expired_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        self.state["tasks"] = remaining[overflow:]
        return expired + overflow

    def _train_completed_peaks(self, rows, allow_training):
        candidates = self.state.get("peak_candidates", [])
        if not candidates or not rows:
            return 0, None
        bounds = {}
        for index, row in enumerate(rows):
            segment_id = row[3].get("segment_id")
            if not segment_id or row[3].get("learned_phase") == "NATURAL":
                continue
            value = bounds.setdefault(segment_id, {
                "first": index, "last": index, "peak": row[2], "eligible": True,
            })
            value["last"] = index
            value["peak"] = max(float(value["peak"]), float(row[2]))
            value["eligible"] = value["eligible"] and row[3].get(
                "training_eligible", True
            )

        train_samples = []
        trained_ids = []
        rejected_ids = []
        remaining = []
        expiry = float(self.config["online_cycle"]["task_expiry_seconds"])
        latest = float(rows[-1][0])
        already_trained = set(self.state.get("peak_trained_segments", []))
        for candidate in candidates:
            segment_id = candidate.get("segment_id")
            if not segment_id or segment_id in already_trained:
                continue
            bound = bounds.get(segment_id)
            if bound is None:
                if latest - float(candidate.get("source_timestamp", latest)) <= expiry:
                    remaining.append(candidate)
                continue
            next_index = int(bound["last"]) + 1
            if next_index >= len(rows):
                remaining.append(candidate)
                continue
            next_metadata = rows[next_index][3]
            completed_naturally = next_metadata.get("learned_phase") == "NATURAL"
            if not completed_naturally or not bound["eligible"]:
                rejected_ids.append(segment_id)
                continue
            train_samples.append((candidate["sequence"], float(bound["peak"])))
            trained_ids.append(segment_id)

        result = None
        if train_samples and allow_training:
            settings = self.config["online_cycle"]
            result = self.watering.fit_peak_head(
                train_samples, train_samples,
                epochs=int(settings.get("online_peak_epochs", 5)),
                patience=int(settings.get("online_peak_patience", 3)),
                learning_rate=float(settings.get("online_peak_learning_rate", 0.0001)),
                gradient_clip=float(self.config["training"]["gradient_clip"]),
                batch_size=int(settings.get("online_peak_batch_size", 8)),
            )
            if result is not None:
                history = self.state.setdefault("peak_trained_segments", [])
                history.extend(trained_ids)
                self.state["peak_trained_segments"] = list(dict.fromkeys(history))[-512:]
            else:
                remaining.extend(
                    candidate for candidate in candidates
                    if candidate.get("segment_id") in trained_ids
                )
        elif train_samples:
            remaining.extend(
                candidate for candidate in candidates
                if candidate.get("segment_id") in trained_ids
            )
        self.state["peak_candidates"] = remaining
        return len(trained_ids) if result is not None else 0, result

    def process(self, rows, allow_training=True):
        created = self._create_predictions(rows)
        trained, losses = self._train_available_hours(rows) if allow_training else (0, {})
        peaks_trained, peak_result = self._train_completed_peaks(rows, allow_training)
        expired = self._expire_tasks(rows[-1][0] if rows else time.time())
        atomic_json_write(self.path, self.state)
        return {
            "segmented_predictions_created": created,
            "segmented_hours_backpropagated": trained,
            "segmented_pending_tasks": len(self.state["tasks"]),
            "segmented_expired_tasks": expired,
            "natural_segment_loss": losses.get("natural"),
            "watering_segment_loss": losses.get("watering"),
            "watering_peak_events_trained": peaks_trained,
            "watering_peak_validation_mae": (
                peak_result.get("best_validation_mae") if peak_result else None
            ),
            "pending_peak_events": len(self.state.get("peak_candidates", [])),
        }

    def should_validate(self, now=None):
        current = time.time() if now is None else float(now)
        return current - float(self.state.get("last_validation_at", 0.0)) >= float(
            self.config["online_cycle"]["validation_interval_seconds"]
        )

    def mark_validated(self, now=None):
        self.state["last_validation_at"] = time.time() if now is None else float(now)
        atomic_json_write(self.path, self.state)
