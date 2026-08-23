import csv
import os


def _append(path, row):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    new = not os.path.exists(path)
    if not new:
        try:
            key = tuple(str(row.get(name, "")) for name in ("time", "reason", "humidity", "water_seconds"))
            with open(path, "r", encoding="utf-8", newline="") as handle:
                for existing in csv.DictReader(handle):
                    existing_key = tuple(
                        str(existing.get(name, ""))
                        for name in ("time", "reason", "humidity", "water_seconds")
                    )
                    if existing_key == key:
                        return
        except (OSError, csv.Error):
            pass
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if new:
            writer.writeheader()
        writer.writerow(row)


def filter_quality_rows(rows, config, emit_after=0.0):
    limits = config["data_quality"]
    clean = []
    seen = set()
    issues = 0
    previous = None
    for row in rows:
        reason = None
        if row[0] in seen:
            reason = "duplicate_timestamp"
        elif previous and row[0] <= previous[0]:
            reason = "non_increasing_timestamp"
        elif not float(limits["minimum_humidity"]) <= row[2] <= float(limits["maximum_humidity"]):
            reason = "invalid_humidity"
        elif not float(limits["minimum_temperature"]) <= row[3]["temperature"] <= float(limits["maximum_temperature"]):
            reason = "invalid_temperature"
        elif not float(limits["minimum_light"]) <= row[3]["light"] <= float(limits["maximum_light"]):
            reason = "invalid_light"
        elif not float(limits["minimum_ec"]) <= row[3]["ec"] <= float(limits["maximum_ec"]):
            reason = "invalid_ec"
        elif row[3]["water_seconds"] < 0 or row[3]["water_seconds"] > float(limits["maximum_water_seconds"]):
            reason = "invalid_water_seconds"
        elif previous and row[0] - previous[0] > float(limits["maximum_gap_seconds"]):
            reason = "large_time_gap"
        elif previous and abs(row[2] - previous[2]) > float(limits["maximum_humidity_jump"]):
            reason = "humidity_jump"
        if reason:
            if reason == "large_time_gap":
                row[3]["quality_gap_before"] = True
            if row[0] > float(emit_after):
                _append(config["paths"]["data_quality_log"], {
                    "time": row[3]["time"], "reason": reason,
                    "humidity": row[2], "water_seconds": row[3]["water_seconds"],
                })
            issues += 1
            if reason != "large_time_gap":
                continue
        clean.append(row)
        seen.add(row[0])
        previous = row
    return clean, issues
