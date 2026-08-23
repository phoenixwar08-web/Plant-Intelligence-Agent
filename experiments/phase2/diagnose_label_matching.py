"""Diagnose why pending segmented tasks cannot obtain hourly labels."""

import json
from bisect import bisect_left
from datetime import datetime

from phase2_predictor.config import ConfigManager
from phase2_predictor.offline_training import load_historical_rows
from phase2_predictor.phase_detection import DynamicPhaseTracker
from phase2_predictor.quality import filter_quality_rows
from phase2_predictor.segmented_online import SegmentedOnlineTrainer


def fmt(value):
    return datetime.fromtimestamp(float(value)).strftime("%Y/%m/%d %H:%M:%S")


config = ConfigManager().get()
rows = load_historical_rows(config)
rows, _issues = filter_quality_rows(rows, config)
DynamicPhaseTracker(config).label_rows(rows)
timestamps = [row[0] for row in rows]
print("loaded_rows:", len(rows))
print("first_row:", fmt(rows[0][0]) if rows else "none")
print("last_row:", fmt(rows[-1][0]) if rows else "none")
with open(config["paths"]["segmented_queue"], encoding="utf-8") as handle:
    tasks = json.load(handle)["tasks"]
tolerance = float(config["offline_training"]["target_tolerance_seconds"])

for task in tasks:
    for hour, mask in enumerate(task["trained_mask"], 1):
        if mask != 0:
            continue
        target = task["source_timestamp"] + hour * 3600
        index = bisect_left(timestamps, target)
        if index >= len(rows):
            result = "future_or_no_data"
        else:
            row = rows[index]
            delta = row[0] - target
            actual_status = SegmentedOnlineTrainer._status(row[3])
            result = (
                f"delta={delta:.0f}s status={actual_status}/{task['status']} "
                f"segment_match={row[3].get('segment_id') == task.get('segment_id')}"
                if delta <= tolerance else f"gap_too_large={delta:.0f}s"
            )
        print(fmt(task["source_timestamp"]), task["status"], f"h{hour}", result)
        break
