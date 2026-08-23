"""One-time, idempotent migration of existing Phase3-linked trials into the effect queue."""

import argparse
import json
import os
import tempfile


def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_atomic(path, value):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="phase3-response-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", required=True)
    parser.add_argument("--queue", required=True)
    args = parser.parse_args()
    trials = load(args.trials, [])
    queue = load(args.queue, [])
    by_id = {
        item.get("request_id"): item for item in queue
        if isinstance(item, dict) and item.get("request_id")
    }
    before = len(by_id)
    for item in trials:
        request_id = item.get("request_id")
        source_timestamp = item.get("prediction_timestamp")
        if not request_id or not isinstance(source_timestamp, (int, float)):
            continue
        record = {
            "request_id": request_id,
            "device_code": item.get("device_code") or "soil3",
            "source": "phase3_response_historical_migration",
            "source_timestamp": source_timestamp,
            "source_humidity": item.get("humidity_before"),
            "selected_label": item.get("plan_label"),
            "water_sec": item.get("water_sec"),
            "zone": item.get("prediction_zone") or item.get("zone"),
            "formal_trajectory": item.get("predicted_trajectory") or [],
            "formal_peak": item.get("predicted_peak"),
            "formal_h12": item.get("predicted_h12"),
            "shadow_trajectory": item.get("shadow_predicted_trajectory") or [],
            "shadow_peak": item.get("shadow_predicted_peak"),
            "shadow_h12": item.get("shadow_predicted_h12"),
            "recorded_at": item.get("timestamp"),
        }
        if request_id in by_id:
            record = {**record, **by_id[request_id]}
        by_id[request_id] = record
    output = sorted(by_id.values(), key=lambda item: float(item.get("source_timestamp") or 0))[-1000:]
    save_atomic(args.queue, output)
    print(f"queue_before={before} queue_after={len(output)} added={len(output) - before}")


if __name__ == "__main__":
    main()
