"""Reconcile queue trained masks with valid hourly backpropagation CSV records."""

import csv
import json
import os
import tempfile


def valid_keys(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8", newline="") as handle:
        return {
            (row.get("source_time", ""), row.get("model", ""), int(float(row["trained_hour"])))
            for row in csv.DictReader(handle)
            if row.get("source_time") and row.get("model") and row.get("trained_hour")
        }


keys = valid_keys("natural_backprop_log.csv") | valid_keys("online_backprop_log.csv")
path = "segmented_training_queue.json"
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)

reset = kept = 0
for task in state.get("tasks", []):
    source_time = task.get("metadata", {}).get("time", "")
    model = task.get("status", "")
    for index, value in enumerate(task.get("trained_mask", [])):
        if value != 1:
            continue
        if (source_time, model, index + 1) in keys:
            kept += 1
        else:
            task["trained_mask"][index] = 0
            reset += 1

fd, temporary = tempfile.mkstemp(prefix="segmented_training_queue.", suffix=".tmp", dir=".")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.remove(temporary)
print(f"queue reconciled: kept={kept}, reset_for_correct_retraining={reset}")
