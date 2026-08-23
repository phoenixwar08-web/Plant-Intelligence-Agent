#!/usr/bin/env python3
"""Build a non-realtime soil2 reference from the last valid sensor window."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from plant_state_builder import run_gsql
from trend_analyzer import DEVICE_CODE, TIMEZONE, build_trend, normalize_datetime, valid_device


OUTPUT_DIR = BASE_DIR / "outputs"


def latest_sensor_time(device_code: str) -> datetime:
    rows = run_gsql(
        "SELECT recv_time FROM soil_sensor_readings "
        f"WHERE device_code = '{device_code}' AND humidity BETWEEN 0 AND 100 "
        "ORDER BY recv_time DESC LIMIT 1;"
    )
    if not rows or not rows[0]:
        raise ValueError("no valid sensor history")
    observed_at = normalize_datetime(rows[0][0])
    if observed_at is None:
        raise ValueError("invalid latest sensor timestamp")
    return observed_at


def build_historical_reference(device_code: str = DEVICE_CODE, now: datetime | None = None) -> dict[str, Any]:
    valid_device(device_code)
    reference_end = latest_sensor_time(device_code)
    generated_at = now or datetime.now(TIMEZONE)
    age_hours = max(0.0, (generated_at - reference_end).total_seconds() / 3600)
    return {
        "schema_version": 1,
        "device_code": device_code,
        "generated_at": generated_at.isoformat(),
        "reference_end": reference_end.isoformat(),
        "age_hours": round(age_hours, 2),
        "trend": build_trend(device_code, now=reference_end),
    }


def write_historical_reference(device_code: str = DEVICE_CODE) -> Path:
    payload = build_historical_reference(device_code)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT_DIR / f"{device_code}_historical_reference.json"
    with tempfile.NamedTemporaryFile(
        dir=str(OUTPUT_DIR), prefix=f".{destination.name}.", suffix=".tmp",
        mode="w", encoding="utf-8", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 soil2 历史参考摘要")
    parser.add_argument("device_code", nargs="?", default=DEVICE_CODE)
    args = parser.parse_args()
    try:
        print(write_historical_reference(args.device_code).read_text(encoding="utf-8"))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
