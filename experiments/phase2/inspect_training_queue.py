"""Print a compact diagnostic summary of segmented online training tasks."""

import json
from collections import Counter
from datetime import datetime


def fmt(timestamp):
    return datetime.fromtimestamp(float(timestamp)).strftime("%Y/%m/%d %H:%M:%S")


with open("segmented_training_queue.json", encoding="utf-8") as handle:
    state = json.load(handle)

tasks = state.get("tasks", [])
print("pending_tasks:", len(tasks))
print("last_seen:", fmt(state.get("last_seen_timestamp", 0)))
print("last_prediction:", fmt(state.get("last_prediction_timestamp", 0)))
print("status_counts:", dict(Counter(task["status"] for task in tasks)))
print("mask_totals:", {
    "waiting": sum(task["trained_mask"].count(0) for task in tasks),
    "trained": sum(task["trained_mask"].count(1) for task in tasks),
    "invalidated": sum(task["trained_mask"].count(-1) for task in tasks),
})
for task in tasks:
    print(
        fmt(task["source_timestamp"]),
        task["status"],
        "waiting=", task["trained_mask"].count(0),
        "trained=", task["trained_mask"].count(1),
        "invalidated=", task["trained_mask"].count(-1),
    )
