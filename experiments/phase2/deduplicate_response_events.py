"""Deduplicate response_events.csv while preserving its first occurrence order."""

import csv
import os
import tempfile


def main():
    path = "response_events.csv"
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        rows = list(reader)
    unique = []
    seen = set()
    for row in rows:
        key = (
            row.get("watering_start_time", ""),
            row.get("watering_end_time", ""),
            row.get("natural_start_time", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    fd, temporary = tempfile.mkstemp(prefix="response_events.", suffix=".tmp", dir=".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(unique)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    print(f"response_events.csv: {len(rows)} rows -> {len(unique)} unique rows")


if __name__ == "__main__":
    main()
