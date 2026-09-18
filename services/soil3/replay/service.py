"""CLI for building read-only soil3 replay.v1 artifacts."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from services.soil3.replay.replay_v1 import ReplayBuilder
from services.soil3.telemetry.common import atomic_write_json


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return [dict(row) for row in csv.DictReader(line.replace("\x00", "") for line in handle)]


def load_json_records(path: Path | None) -> list[Any]:
    if path is None:
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("records")
    if not isinstance(value, list):
        raise ValueError(f"{path} must contain a list or an object with records")
    return list(value)


def load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at {path}:{line_number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"JSONL record at {path}:{line_number} must be an object")
        records.append(value)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only soil3 replay sample at time T")
    parser.add_argument("--at", required=True, help="Inclusive historical cutoff in ISO 8601 form")
    parser.add_argument("--sensor-csv", type=Path, required=True)
    parser.add_argument("--watering-json", type=Path)
    parser.add_argument("--state-history-jsonl", type=Path)
    parser.add_argument("--parameter-history-jsonl", type=Path)
    parser.add_argument("--source-timezone", default="Asia/Shanghai")
    parser.add_argument("--max-gap-seconds", type=float, default=900.0)
    parser.add_argument("--response-window-seconds", type=float, default=14400.0)
    parser.add_argument("--sample-output", type=Path, required=True)
    parser.add_argument("--quality-output", type=Path, required=True)
    args = parser.parse_args()

    builder = ReplayBuilder(
        source_timezone=args.source_timezone,
        max_gap_seconds=args.max_gap_seconds,
        response_window_seconds=args.response_window_seconds,
    )
    sample, quality = builder.build(
        replay_at=args.at,
        sensor_readings=load_csv(args.sensor_csv),
        watering_history=load_json_records(args.watering_json),
        state_history=load_jsonl(args.state_history_jsonl),
        parameter_history=load_jsonl(args.parameter_history_jsonl),
    )
    atomic_write_json(args.sample_output, sample)
    atomic_write_json(args.quality_output, quality)
    print(json.dumps({
        "sample_id": sample["sample_id"],
        "replay_at": sample["replay_at"],
        "quality_status": quality["status"],
        "sample_output": str(args.sample_output),
        "quality_output": str(args.quality_output),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
