"""Sort and deduplicate the human-facing hourly prediction evaluation logs."""

import csv
import os

from phase2_predictor.segmented_online import _append_evaluation_rows_sorted


def main():
    for path in ("natural_backprop_log.csv", "online_backprop_log.csv"):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        _append_evaluation_rows_sorted(path, rows)
        print(f"sorted {path}: {len(rows)} input rows")


if __name__ == "__main__":
    main()
