import csv
import gc
import json
import logging
import os
import random
import shutil
import signal
import tempfile
import time
from datetime import datetime

from .learning import atomic_json_write
from .natural_training import NaturalTrainer
from .segmented_online import SegmentedOnlineTrainer
from .phase_detection import DynamicPhaseTracker
from .quality import filter_quality_rows
from .resources import append_resource_log, rss_mb
from .effect_report import build_prediction_effect_report
from .offline_training import load_historical_rows
from .watering_models import WateringModelRuntime
from .watering_samples import build_dose_response_samples, build_watering_samples


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


class ShadowTrainer:
    """Trains watering trajectory and watering dose-response tasks."""

    def __init__(self, config_manager, logger=None):
        self.config_manager = config_manager
        self.config = config_manager.get()
        self.logger = logger or logging.getLogger("phase2.watering_training")
        self.state = self._load_state()
        self.runtime = WateringModelRuntime(self.config, self.logger)
        self.natural_trainer = NaturalTrainer(self.config, self.logger)
        self.segmented = SegmentedOnlineTrainer(
            self.config, self.natural_trainer.runtime, self.runtime, self.logger
        )
        self.phase_tracker = DynamicPhaseTracker(self.config, self.logger)
        self.running = True

    def _load_state(self):
        state = {
            "offline_bootstrap_complete": False,
            "trained_watering_events": 0,
            "last_predicted_event_timestamp": 0.0,
            "pending_events": [],
        }
        path = self.config["paths"]["shadow_state"]
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    state.update(loaded)
            except Exception:
                self.logger.exception("Failed to load watering training state")
        return state

    def stop(self, *_args):
        self.running = False

    def _train(self, samples):
        if not samples:
            return []
        batches = []
        shuffled = list(samples)
        rng = random.Random(20260614)
        size = int(self.config["shadow_training"]["batch_size"])
        for _epoch in range(int(self.config["shadow_training"]["epochs_per_cycle"])):
            rng.shuffle(shuffled)
            for start in range(0, len(shuffled), size):
                loss = self.runtime.train_batch(
                    shuffled[start:start + size], self.config["training"]["gradient_clip"]
                )
                if loss is not None:
                    batches.append(loss)
        if batches:
            self.runtime.save()
        return batches

    def _train_task(self, samples, task, epochs=None):
        if not samples:
            return []
        losses = []
        shuffled = list(samples)
        rng = random.Random(20260614)
        size = int(self.config["shadow_training"]["batch_size"])
        trainer = (
            self.runtime.train_masked_trajectory_batch if task == "masked_trajectory"
            else self.runtime.train_trajectory_batch if task == "trajectory"
            else self.runtime.train_time_batch if task == "time"
            else self.runtime.train_peak_batch
        )
        epoch_count = int(epochs or self.config["shadow_training"]["epochs_per_cycle"])
        for _epoch in range(epoch_count):
            rng.shuffle(shuffled)
            for start in range(0, len(shuffled), size):
                loss = trainer(
                    shuffled[start:start + size], self.config["training"]["gradient_clip"]
                )
                if loss is not None:
                    losses.append(loss)
        if losses:
            self.runtime.save()
        return losses

    def _bootstrap(self, trajectory_samples, dose_samples):
        if self.state["offline_bootstrap_complete"]:
            return 0, 0, []
        clean = [sample for sample in trajectory_samples if not sample[3]["intervened"]]
        trajectory_validation = max(1, int(len(clean) * 0.2)) if clean else 0
        dose_validation = max(1, int(len(dose_samples) * 0.2)) if dose_samples else 0
        trajectory_train = clean[:-trajectory_validation] if trajectory_validation else clean
        dose_train = dose_samples[:-dose_validation] if dose_validation else dose_samples
        losses = self._train_task(
            [(sample[0], sample[1], sample[3]["mask"]) for sample in trajectory_train],
            "masked_trajectory",
            self.config["shadow_training"]["bootstrap_epochs"],
        )
        losses += self._train_task(
            [(sample[0], sample[1]) for sample in dose_train], "peak",
            self.config["shadow_training"]["bootstrap_epochs"],
        )
        losses += self._train_task(
            [(sample[0], sample[3]) for sample in trajectory_train], "time",
            self.config["shadow_training"]["bootstrap_epochs"],
        )
        self.state["offline_bootstrap_complete"] = True
        if trajectory_samples:
            self.state["last_predicted_event_timestamp"] = max(
                sample[3]["event_timestamp"] for sample in trajectory_samples
            )
        self.state["trained_watering_events"] += (
            len(trajectory_train) + len(dose_train) if losses else 0
        )
        return len(trajectory_train), len(dose_train), losses

    def _record_new_watering_predictions(self, rows):
        sequence_length = int(self.config["model"]["sequence_length"])
        last_event = float(self.state["last_predicted_event_timestamp"])
        created = 0
        for index in range(sequence_length - 1, len(rows)):
            metadata = rows[index][3]
            if rows[index][0] <= last_event:
                continue
            if metadata["watered"] <= 0 and metadata["water_seconds"] <= 0:
                continue
            sequence = [row[1] for row in rows[index - sequence_length + 1:index + 1]]
            trajectory, peak = self.runtime.predict(sequence)
            pending = {
                "event_timestamp": rows[index][0],
                "target_timestamp": rows[index][0] + 43200,
                "sequence": sequence,
                "metadata": metadata,
                "predicted_trajectory": trajectory,
                "predicted_peak": peak,
            }
            self.state["pending_events"].append(pending)
            row = {
                "watering_time": metadata["time"], "temperature": metadata["temperature"],
                "humidity_before": metadata["humidity"], "light": metadata["light"],
                "ec": metadata["ec"], "watered": metadata["watered"],
                "water_seconds": metadata["water_seconds"], "predicted_peak_humidity": round(peak, 4),
            }
            row.update({f"predicted_humidity_h{hour}": round(value, 4) for hour, value in enumerate(trajectory, 1)})
            _append_csv(self.config["paths"]["online_prediction_log"], row)
            self.state["last_predicted_event_timestamp"] = rows[index][0]
            created += 1
        self.state["pending_events"] = self.state["pending_events"][-500:]
        return created

    def _train_matured(self, complete_samples):
        by_timestamp = {sample[3]["event_timestamp"]: sample for sample in complete_samples}
        matured, remaining = [], []
        for pending in self.state["pending_events"]:
            sample = by_timestamp.get(pending["event_timestamp"])
            if sample is None:
                remaining.append(pending)
            else:
                matured.append((pending, sample))
        clean = [sample for _pending, sample in matured if not sample[3]["intervened"]]
        losses = self._train(clean)
        self.state["trained_watering_events"] += len(clean) if losses else 0
        mean_loss = sum(losses) / len(losses) if losses else ""
        for pending, sample in matured:
            trajectory, peak, metadata = sample[1], sample[2], sample[3]
            row = {
                "watering_time": metadata["time"], "temperature": metadata["temperature"],
                "humidity_before": metadata["humidity"], "light": metadata["light"],
                "ec": metadata["ec"], "watered": metadata["watered"],
                "water_seconds": metadata["water_seconds"],
                "predicted_peak_humidity": round(pending["predicted_peak"], 4),
                "actual_peak_humidity": round(peak, 4),
                "peak_absolute_error": round(abs(pending["predicted_peak"] - peak), 4),
                "intervening_watering": metadata["intervened"],
                "training_status": "skipped_second_watering" if metadata["intervened"] else "backpropagated",
                "backprop_loss": round(mean_loss, 8) if not metadata["intervened"] and mean_loss != "" else "",
                "trained_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
            }
            row.update({f"actual_humidity_h{hour}": value for hour, value in enumerate(trajectory, 1)})
            _append_csv(self.config["paths"]["online_backprop_log"], row)
        self.state["pending_events"] = remaining
        return len(clean), losses

    def process_once(self):
        started = time.perf_counter()
        self.config_manager.reload_if_changed()
        self.config = self.config_manager.get()
        rows = load_historical_rows(self.config)
        rows, quality_issues = filter_quality_rows(
            rows, self.config, self.phase_tracker.previous.get("last_processed_timestamp", 0.0)
        )
        phase_state = self.phase_tracker.label_rows(rows)
        if phase_state.get("baseline"):
            self.segmented.config["phase_detection"]["fallback_slope_limit"] = phase_state["baseline"].get(
                "slope_limit", 0.5
            )
        validation_due = self.segmented.should_validate()
        historical_due = (
            not self.state["offline_bootstrap_complete"]
            or not self.natural_trainer.state["offline_bootstrap_complete"]
            or validation_due
        )
        natural_report = self.natural_trainer.process(
            rows, online_enabled=False, run_historical=historical_due
        )
        samples = build_watering_samples(rows, self.config) if historical_due else []
        dose_samples = build_dose_response_samples(rows, self.config) if historical_due else []
        offline_trajectory_count, offline_dose_count, offline_losses = self._bootstrap(
            samples, dose_samples
        )
        if (offline_trajectory_count > 0 or offline_dose_count > 0) and rows:
            self.state["last_predicted_event_timestamp"] = rows[-1][0]
            self.state["pending_events"] = []
        online_count, online_losses, predictions_created = 0, [], 0
        memory_paused = rss_mb() >= float(self.config["resource_limits"]["pause_training_rss_mb"])
        segmented_report = self.segmented.process(rows, allow_training=not memory_paused)
        clean = [sample for sample in samples if not sample[3]["intervened"]]
        validation_count = max(1, int(len(clean) * 0.2)) if clean else 0
        validation = clean[-validation_count:] if validation_count else []
        trajectory_mae = self.runtime.evaluate_masked_trajectories(validation)
        peak_mae = (
            sum(abs(self.runtime.predict(sample[0])[1] - sample[2]) for sample in validation) / len(validation)
            if validation else None
        )
        peak_time_mae, natural_time_mae = self.runtime.evaluate_times(validation)
        dose_validation_count = max(1, int(len(dose_samples) * 0.2)) if dose_samples else 0
        dose_validation = dose_samples[-dose_validation_count:] if dose_validation_count else []
        dose_peak_mae = self.runtime.evaluate_peaks(dose_validation)
        if historical_due:
            self.state["last_validation_metrics"] = {
                "trajectory_mae": trajectory_mae, "trajectory_peak_mae": peak_mae,
                "dose_peak_mae": dose_peak_mae, "peak_time_mae_minutes": peak_time_mae,
                "natural_time_mae_minutes": natural_time_mae,
            }
        else:
            metrics = self.state.get("last_validation_metrics", {})
            trajectory_mae = metrics.get("trajectory_mae")
            peak_mae = metrics.get("trajectory_peak_mae")
            dose_peak_mae = metrics.get("dose_peak_mae")
            peak_time_mae = metrics.get("peak_time_mae_minutes")
            natural_time_mae = metrics.get("natural_time_mae_minutes")
        watering_counts = {}
        watering_errors = {}
        for sample in dose_validation:
            seconds = str(int(round(sample[2]["water_seconds"])))
            watering_counts[seconds] = watering_counts.get(seconds, 0) + 1
            watering_errors.setdefault(seconds, []).append(
                abs(self.runtime.predict(sample[0])[1] - sample[1])
            )
        watering_mae_by_seconds = {
            seconds: sum(errors) / len(errors) for seconds, errors in watering_errors.items()
        }
        if historical_due:
            self.state["confidence_profile"] = {
                "natural_samples": len(validation),
                "natural_mae": natural_report["natural_trajectory_mae"] or 20.0,
                "watering_counts": watering_counts,
                "watering_mae_by_seconds": watering_mae_by_seconds,
            }
        else:
            profile = self.state.get("confidence_profile", {})
            watering_counts = profile.get("watering_counts", {})
            watering_mae_by_seconds = profile.get("watering_mae_by_seconds", {})
        self.segmented.config["_confidence_profile"] = self.state.get("confidence_profile", {})
        ready = (
            trajectory_mae is not None and dose_peak_mae is not None
            and natural_report["natural_trajectory_mae"] is not None
            and trajectory_mae <= 8.0 and dose_peak_mae <= 8.0
            and natural_report["natural_trajectory_mae"] <= 8.0
            and peak_time_mae is not None and natural_time_mae is not None
            and peak_time_mae <= float(self.config["online_cycle"]["maximum_time_mae_minutes"])
            and natural_time_mae <= float(self.config["online_cycle"]["maximum_time_mae_minutes"])
        )
        if ready and validation_due:
            _atomic_copy(self.config["paths"]["shadow_weights"], self.config["paths"]["model_weights"])
            _atomic_copy(
                self.config["paths"]["natural_weights"],
                self.config["paths"]["natural_weights"] + ".accepted",
            )
        elif validation_due:
            watering_accepted = self.config["paths"]["model_weights"]
            natural_accepted = self.config["paths"]["natural_weights"] + ".accepted"
            if os.path.exists(watering_accepted):
                _atomic_copy(watering_accepted, self.config["paths"]["shadow_weights"])
                self.runtime.reload_weights(watering_accepted)
            if os.path.exists(natural_accepted):
                _atomic_copy(natural_accepted, self.config["paths"]["natural_weights"])
                self.natural_trainer.runtime.reload_weights(natural_accepted)
        if validation_due:
            self.segmented.mark_validated()
        report = {
            "ready": ready, "training_device": self.runtime.device,
            "formal_inference_backend": getattr(
                self.runtime, "last_inference_backend", self.runtime.device
            ),
            "watering_rows": sum(
                row[3]["watered"] > 0 or row[3]["water_seconds"] > 0 for row in rows
            ),
            "response_trajectory_events": len(clean), "trained_watering_events": self.state["trained_watering_events"],
            "dose_response_events": len(dose_samples), "trajectory_mae": trajectory_mae,
            "trajectory_peak_mae": peak_mae, "dose_peak_mae": dose_peak_mae,
            "peak_time_mae_minutes": peak_time_mae,
            "natural_time_mae_minutes": natural_time_mae,
            "pending_watering_events": len(self.state["pending_events"]),
            **natural_report,
            **segmented_report,
            "daily_validation_ran": validation_due,
            "current_phase": phase_state["phase"],
            "natural_baseline": phase_state.get("baseline", {}),
            "quality_issues": quality_issues,
            "watering_validation_by_seconds": {
                seconds: {
                    "samples": watering_counts[seconds],
                    "mae": watering_mae_by_seconds[seconds],
                    "confidence": (
                        "low" if watering_counts[seconds] < 5
                        else "medium" if watering_counts[seconds] < 20 else "high"
                    ),
                }
                for seconds in watering_counts
            },
            "training_paused_for_memory": memory_paused,
        }
        atomic_json_write(self.config["paths"]["shadow_state"], self.state)
        atomic_json_write(self.config["paths"]["model_readiness"], report)
        _append_csv(self.config["paths"]["shadow_training_log"], {
            "time": datetime.now().strftime("%Y/%m/%d %H:%M"),
            "offline_trajectory_events_trained": offline_trajectory_count,
            "offline_dose_events_trained": offline_dose_count, "new_predictions": predictions_created,
            "online_events_backpropagated": online_count,
            "offline_mean_loss": sum(offline_losses) / len(offline_losses) if offline_losses else "",
            "online_mean_loss": sum(online_losses) / len(online_losses) if online_losses else "",
            "trajectory_mae": trajectory_mae if trajectory_mae is not None else "",
            "trajectory_peak_mae": peak_mae if peak_mae is not None else "",
            "dose_peak_mae": dose_peak_mae if dose_peak_mae is not None else "",
            "natural_offline_windows_trained": natural_report["natural_offline_windows_trained"],
            "natural_online_windows_trained": natural_report["natural_online_windows_trained"],
            "natural_predictions_created": natural_report["natural_predictions_created"],
            "natural_trajectory_mae": (
                natural_report["natural_trajectory_mae"]
                if natural_report["natural_trajectory_mae"] is not None else ""
            ),
            "segmented_predictions_created": segmented_report["segmented_predictions_created"],
            "segmented_hours_backpropagated": segmented_report["segmented_hours_backpropagated"],
            "segmented_pending_tasks": segmented_report["segmented_pending_tasks"],
            "segmented_expired_tasks": segmented_report["segmented_expired_tasks"],
        })
        build_prediction_effect_report(self.config)
        del rows, samples, dose_samples, clean, validation, dose_validation
        gc.collect()
        append_resource_log(
            self.config["paths"]["resource_log"], started, report["watering_rows"],
            segmented_report["segmented_pending_tasks"],
            segmented_report["segmented_hours_backpropagated"], memory_paused,
            self.runtime.device,
        )
        return report

    def run(self):
        while self.running:
            try:
                self.process_once()
            except Exception:
                self.logger.exception("Watering training cycle failed")
            time.sleep(float(self.config["shadow_training"]["poll_seconds"]))


def main(config_manager):
    trainer = ShadowTrainer(config_manager)
    signal.signal(signal.SIGINT, trainer.stop)
    signal.signal(signal.SIGTERM, trainer.stop)
    trainer.run()
