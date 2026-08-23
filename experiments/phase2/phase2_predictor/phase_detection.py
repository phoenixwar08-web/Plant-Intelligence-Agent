import csv
import json
import logging
import os
import time

import numpy as np

from .learning import atomic_json_write


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


def _response_event_id(event):
    return "|".join(str(event.get(name, "") or "") for name in (
        "event_start_time", "watering_end_time", "natural_start_time",
    ))


def _logged_response_event_ids(path):
    if not os.path.exists(path):
        return set()
    result = set()
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                event_id = row.get("event_id")
                if not event_id:
                    event_id = "|".join(row.get(name, "") for name in (
                        "watering_start_time", "watering_end_time", "natural_start_time",
                    ))
                if event_id:
                    result.add(event_id)
    except (OSError, csv.Error):
        return set()
    return result


def _watering(metadata):
    return metadata["watered"] > 0 or metadata["water_seconds"] > 0


def _group_key(metadata):
    return "|".join((
        "day" if metadata["light"] >= 100 else "night",
        f"t{int(metadata['temperature'] // 5) * 5}",
        f"h{int(metadata['humidity'] // 10) * 10}",
    ))


class DynamicPhaseTracker:
    """Deterministically rebuilds phase and segment labels from the history window."""

    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.path = config["paths"]["phase_state"]
        self.previous = self._load()
        self.state = dict(self.previous)

    def update_config(self, config):
        self.config = config
        self.path = config["paths"]["phase_state"]

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    return loaded
            except Exception:
                self.logger.exception("Failed to load phase state")
        return {"last_processed_timestamp": 0.0, "baseline": {}, "group_baselines": {}}

    def learn_baseline(self, rows):
        cfg = self.config["phase_detection"]
        window = int(cfg["window_rows"])
        guard = float(cfg["natural_guard_seconds"])
        global_values = {"delta": [], "range": [], "slope": []}
        groups = {}
        last_watering = None
        for index, row in enumerate(rows):
            if _watering(row[3]):
                last_watering = row[0]
                continue
            if last_watering is not None and row[0] - last_watering < guard:
                continue
            if index < window - 1:
                continue
            recent = rows[index - window + 1:index + 1]
            if any(_watering(item[3]) for item in recent):
                continue
            humidities = [item[2] for item in recent]
            values = (
                max(abs(b - a) for a, b in zip(humidities, humidities[1:])),
                max(humidities) - min(humidities),
                abs(humidities[-1] - humidities[0]) / max(1, window - 1),
            )
            for name, value in zip(("delta", "range", "slope"), values):
                global_values[name].append(value)
            group = groups.setdefault(_group_key(row[3]), {"delta": [], "range": [], "slope": []})
            for name, value in zip(("delta", "range", "slope"), values):
                group[name].append(value)
        percentile = float(cfg["baseline_percentile"])
        minimum = int(cfg["minimum_baseline_windows"])

        def summarize(values):
            if len(values["range"]) < minimum:
                return None
            return {
                "delta_limit": float(np.percentile(values["delta"], percentile)),
                "range_limit": float(np.percentile(values["range"], percentile)),
                "slope_limit": float(np.percentile(values["slope"], percentile)),
                "sample_windows": len(values["range"]),
            }

        baseline = summarize(global_values) or self.previous.get("baseline", {})
        group_baselines = {
            key: summary for key, values in groups.items()
            if (summary := summarize(values)) is not None
        }
        return baseline, group_baselines

    def _baseline_for(self, metadata, baseline, group_baselines):
        return group_baselines.get(_group_key(metadata), baseline)

    def _window_is_natural(self, recent, baseline):
        if not baseline or len(recent) < int(self.config["phase_detection"]["window_rows"]):
            return False
        humidities = [row[2] for row in recent]
        epsilon = 1e-6
        return (
            max(abs(b - a) for a, b in zip(humidities, humidities[1:]))
            <= baseline["delta_limit"] + epsilon
            and max(humidities) - min(humidities) <= baseline["range_limit"] + epsilon
            and abs(humidities[-1] - humidities[0]) / max(1, len(humidities) - 1)
            <= baseline["slope_limit"] + epsilon
        )

    def _window_is_returning_to_natural(self, recent, baseline, event):
        if not self._window_is_natural(recent, baseline):
            return False
        humidities = [row[2] for row in recent]
        tolerance = max(1.0, float(baseline.get("range_limit", 0.0)))
        near_pre_water = humidities[-1] <= float(event["humidity_before"]) + tolerance
        not_rising = humidities[-1] <= humidities[0] + float(baseline.get("slope_limit", 0.0))
        return near_pre_water and not_rising

    def label_rows(self, rows):
        baseline_age = time.time() - float(self.previous.get("baseline_calculated_at", 0.0))
        if baseline_age >= 86400 or not self.previous.get("baseline"):
            baseline, groups = self.learn_baseline(rows)
            baseline_calculated_at = time.time()
        else:
            baseline = self.previous.get("baseline", {})
            groups = self.previous.get("group_baselines", {})
            baseline_calculated_at = self.previous.get("baseline_calculated_at", 0.0)
        cfg = self.config["phase_detection"]
        window = int(cfg["window_rows"])
        required = int(cfg["required_stable_windows"])
        maximum_response = float(cfg["maximum_response_seconds"])
        maximum_merge_gap = float(cfg.get("maximum_watering_merge_gap_seconds", 1800))
        maximum_event_age = float(cfg.get("maximum_watering_event_seconds", 3600))
        minimum_interrupted_response = float(
            cfg.get("minimum_interrupted_response_seconds", 1800)
        )
        previous_last = float(self.previous.get("last_processed_timestamp", 0.0))
        logged_event_ids = set(self.previous.get("logged_response_event_ids", []))
        logged_event_ids.update(_logged_response_event_ids(self.config["paths"]["response_events_log"]))
        phase, stable_windows = "NATURAL", 0
        natural_start = rows[0][0] if rows else 0.0
        event = None
        event_last_response_row = None
        recent = []
        completed_events = []

        for row in rows:
            metadata = row[3]
            if metadata.get("quality_gap_before"):
                phase, stable_windows, event = "NATURAL", 0, None
                event_last_response_row = None
                natural_start = row[0]
                recent = []
            is_watering = _watering(metadata)
            if is_watering:
                start_new_event = phase == "NATURAL" or event is None
                if not start_new_event:
                    last_watering = float(event.get("last_watering_at", event["event_start"]))
                    event_age = row[0] - float(event["event_start"])
                    if row[0] - last_watering > maximum_merge_gap or event_age >= maximum_event_age:
                        response_start = float(event.get("watering_end") or last_watering)
                        observed_response = max(0.0, row[0] - response_start)
                        if (
                            event.get("watering_end") is not None
                            and event_last_response_row is not None
                            and observed_response >= minimum_interrupted_response
                        ):
                            event_last_response_row[3]["segment_peak_complete"] = True
                            completed_events.append({
                                **event,
                                "natural_start": row[0],
                                "natural_start_time": metadata["time"],
                            })
                            self.logger.info(
                                "Closing watering response at next-watering boundary: "
                                "segment=%s observed=%.0fs gap=%.0fs",
                                event.get("segment_id"), observed_response,
                                row[0] - last_watering,
                            )
                        else:
                            self.logger.warning(
                                "Discarding watering response with insufficient observation: "
                                "segment=%s observed=%.0fs age=%.0fs gap=%.0fs",
                                event.get("segment_id"), observed_response,
                                event_age, row[0] - last_watering,
                            )
                        start_new_event = True
                if start_new_event:
                    event = {
                        "segment_id": f"watering-{int(row[0])}",
                        "event_start": row[0], "event_start_time": metadata["time"],
                        "humidity_before": metadata.get(
                            "pre_watering_humidity", metadata["humidity"]
                        ), "water_seconds": 0.0,
                        "peak_humidity": metadata["humidity"], "peak_timestamp": row[0],
                        "watering_end": None, "watering_end_time": "",
                        "last_watering_at": row[0],
                    }
                    event_last_response_row = None
                phase = "WATERING"
                event["water_seconds"] += metadata["water_seconds"]
                event["last_watering_at"] = row[0]
                if metadata["humidity"] >= event["peak_humidity"]:
                    event["peak_humidity"], event["peak_timestamp"] = metadata["humidity"], row[0]
            elif phase == "WATERING":
                phase = "RESPONSE_RISING"
                event["watering_end"], event["watering_end_time"] = row[0], metadata["time"]
                event_last_response_row = row
            elif phase in ("RESPONSE_RISING", "RESPONSE_SETTLING"):
                event_last_response_row = row
                if metadata["humidity"] > event["peak_humidity"]:
                    event["peak_humidity"], event["peak_timestamp"] = metadata["humidity"], row[0]
                    phase, stable_windows = "RESPONSE_RISING", 0
                else:
                    phase = "RESPONSE_SETTLING"
                    current_baseline = self._baseline_for(metadata, baseline, groups)
                    recent_window = (recent + [row])[-window:]
                    stable = self._window_is_returning_to_natural(recent_window, current_baseline, event)
                    stable_windows = stable_windows + 1 if stable else 0
                    response_age = row[0] - float(event["watering_end"] or row[0])
                    if stable_windows >= required or response_age >= maximum_response:
                        completed_events.append({**event, "natural_start": row[0], "natural_start_time": metadata["time"]})
                        phase, stable_windows, event = "NATURAL", 0, None
                        event_last_response_row = None
                        natural_start = row[0]
            segment_id = (
                event["segment_id"] if phase != "NATURAL" and event
                else f"natural-{int(natural_start)}"
            )
            metadata["learned_phase"] = phase
            metadata["segment_id"] = segment_id
            if phase != "NATURAL" and event:
                metadata["segment_water_seconds"] = event["water_seconds"]
                metadata["watering_event_start"] = event["event_start"]
            recent.append(row)
            recent = recent[-window:]

        for completed_event in completed_events:
            if previous_last <= 0.0 or completed_event["natural_start"] <= previous_last:
                continue
            event_id = _response_event_id(completed_event)
            if event_id in logged_event_ids:
                continue
            event_start = float(completed_event["event_start"])
            watering_end = float(completed_event["watering_end"] or event_start)
            row = {
                "watering_start_time": completed_event["event_start_time"],
                "watering_end_time": completed_event["watering_end_time"],
                "natural_start_time": completed_event["natural_start_time"],
                "water_seconds": completed_event["water_seconds"],
                "humidity_before": completed_event["humidity_before"],
                "actual_peak_humidity": completed_event["peak_humidity"],
                "minutes_to_peak": round(max(0.0, completed_event["peak_timestamp"] - event_start) / 60.0, 2),
                "minutes_to_natural": round((completed_event["natural_start"] - watering_end) / 60.0, 2),
            }
            _append_csv(self.config["paths"]["response_events_log"], row)
            logged_event_ids.add(event_id)

        self.state = {
            "phase": rows[-1][3]["learned_phase"] if rows else "NATURAL",
            "segment_id": rows[-1][3]["segment_id"] if rows else "natural-1",
            "last_processed_timestamp": rows[-1][0] if rows else previous_last,
            "baseline": baseline, "group_baselines": groups,
            "baseline_calculated_at": baseline_calculated_at,
            "active_event": event if phase != "NATURAL" else None,
            "logged_response_event_ids": sorted(logged_event_ids)[-256:],
        }
        atomic_json_write(self.path, self.state)
        return self.state
