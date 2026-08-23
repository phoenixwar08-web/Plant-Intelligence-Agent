#!/usr/bin/env python3
"""Build a read-only, deterministic daily summary for soil2."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from analytics.trend_analyzer import DEVICE_CODE, TIMEZONE, normalize_datetime, write_trend

OUTPUT_DIR = BASE_DIR / "outputs"
STATUS_PATH = OUTPUT_DIR / "soil2_status.json"
TREND_PATH = OUTPUT_DIR / "soil2_trend.json"
DECISION_PATH = OUTPUT_DIR / "soil2_decision.json"
SUMMARY_PATH = OUTPUT_DIR / "soil2_daily_summary.json"
HISTORY_PATH = OUTPUT_DIR / "history" / "daily_summary" / "soil2.jsonl"
FRESHNESS = {"status": timedelta(minutes=30), "decision": timedelta(minutes=30), "trend": timedelta(minutes=10), "visual": timedelta(hours=24)}


def _now(now: Optional[datetime] = None) -> datetime:
    return normalize_datetime(now) or datetime.now(TIMEZONE)


def _load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _quality(document: Optional[Dict[str, Any]], timestamp: Any, threshold: timedelta, now: datetime) -> str:
    if not document:
        return "unavailable"
    value = normalize_datetime(timestamp)
    if value is None:
        return "unavailable"
    return "fresh" if now - value <= threshold and value <= now else "stale"


def build_daily_summary(device_code: str = DEVICE_CODE, now: Optional[datetime] = None) -> Dict[str, Any]:
    if device_code != DEVICE_CODE:
        raise ValueError("only soil2 is supported")
    generated_at = _now(now)
    write_trend(device_code, generated_at)
    status, trend, decision = _load(STATUS_PATH), _load(TREND_PATH), _load(DECISION_PATH)
    sensor = (status or {}).get("sensor", {})
    visual = (status or {}).get("visual", {})
    control = (status or {}).get("control", {})
    decision_value = (decision or {}).get("decision", {})
    quality = {
        "status": _quality(status, (status or {}).get("generated_at"), FRESHNESS["status"], generated_at),
        "decision": _quality(decision, (decision or {}).get("generated_at"), FRESHNESS["decision"], generated_at),
        "trend": _quality(trend, (trend or {}).get("generated_at"), FRESHNESS["trend"], generated_at),
        "visual": _quality(status, visual.get("observed_at"), FRESHNESS["visual"], generated_at),
    }
    return {
        "schema_version": 1, "device_code": device_code, "generated_at": generated_at.isoformat(),
        "summary_date": generated_at.date().isoformat(), "timezone": "Asia/Shanghai",
        "current": {"soil_humidity": sensor.get("soil_humidity"), "soil_temperature": sensor.get("soil_temperature"),
                    "air_humidity": sensor.get("air_humidity"), "lux": sensor.get("lux"),
                    "phase3_strategy": control.get("decision"), "decision_action": decision_value.get("action"),
                    "decision_water_sec": decision_value.get("water_sec")},
        "trend": {"soil_humidity": (trend or {}).get("soil_humidity", {}), "irrigation": (trend or {}).get("irrigation", {}),
                  "human_watering": (trend or {}).get("human_watering", {}), "visual": (trend or {}).get("visual", {})},
        "visual": {"observed_at": visual.get("observed_at"), "plant_health": visual.get("plant_health"),
                   "yellow_leaf_ratio": visual.get("yellow_leaf_ratio")},
        "decision": {"reasoning": decision_value.get("reasoning", []) if isinstance(decision_value.get("reasoning", []), list) else [],
                     "safety_blocking_reasons": ((status or {}).get("safety", {}).get("blocking_reasons", []))},
        "data_quality": quality,
    }


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", mode="w", encoding="utf-8", delete=False) as handle:
            temp_name = handle.name; json.dump(payload, handle, ensure_ascii=False, indent=2); handle.flush(); os.fsync(handle.fileno())
        Path(temp_name).replace(path)
    finally:
        if temp_name and Path(temp_name).exists(): Path(temp_name).unlink()


def _append_history(payload: Dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"); handle.flush(); os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_daily_summary(device_code: str = DEVICE_CODE, now: Optional[datetime] = None) -> Path:
    payload = build_daily_summary(device_code, now)
    _atomic_write(SUMMARY_PATH, payload)
    try:
        _append_history(payload)
    except OSError:
        print("warning: daily summary history unavailable", file=sys.stderr)
    return SUMMARY_PATH


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("device_code", nargs="?", default=DEVICE_CODE); parser.add_argument("--json", action="store_true"); args = parser.parse_args()
    output = write_daily_summary(args.device_code)
    print(output.read_text(encoding="utf-8") if not args.json else json.dumps(_load(output) or {}, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__": main()
