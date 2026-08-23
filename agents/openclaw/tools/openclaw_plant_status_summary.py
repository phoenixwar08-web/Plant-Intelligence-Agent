#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from openclaw_plant_registry import device_config


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/root/.openclaw/workspace"))
WATCHER_STATE_PATH = ROOT / "state" / "openclaw_irrigation_watcher.json"
TARGET_LOW_FALLBACK = 33.0
MAX_SENSOR_AGE_SEC = 30 * 60


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def run_checked(cmd: list[str], *, timeout: int = 30) -> str:
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def gsql_rows(sql: str) -> list[list[str]]:
    command = "gsql -d soil_data -p 7654 -t -A -F , -c " + shlex.quote(sql)
    out = run_checked(["su", "-", "opengauss", "-c", command], timeout=30)
    rows: list[list[str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("("):
            continue
        rows.append(line.split(","))
    return rows


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def target_low_from_state(state: dict[str, Any]) -> float:
    for key in ("learned_target_low", "target_low"):
        value = state.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    suspect = state.get("water_delivery_suspect")
    if isinstance(suspect, dict) and isinstance(suspect.get("target_low"), (int, float)):
        return float(suspect["target_low"])
    cooldown = state.get("dynamic_cooldown")
    if isinstance(cooldown, dict):
        hum = cooldown.get("humidity")
        margin = cooldown.get("target_low_margin")
        if isinstance(hum, (int, float)) and isinstance(margin, (int, float)):
            return float(hum) - float(margin)
    return TARGET_LOW_FALLBACK


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def latest_readings(device_code: str, limit: int = 12) -> list[dict[str, Any]]:
    rows = gsql_rows(
        "SELECT recv_time,humidity,temp,lux "
        "FROM soil_sensor_readings "
        f"WHERE device_code='{device_code}' "
        "ORDER BY recv_time DESC "
        f"LIMIT {int(limit)};"
    )
    readings: list[dict[str, Any]] = []
    for row in rows:
        readings.append(
            {
                "recv_time": row[0],
                "humidity": float(row[1]),
                "temp": float(row[2]) if len(row) > 2 and row[2] else None,
                "lux": float(row[3]) if len(row) > 3 and row[3] else None,
            }
        )
    readings.reverse()
    return readings


def normalize_irrigation_source(
    source: str | None,
    reason: str | None,
    operator_name: str | None,
    request_id: str | None,
) -> tuple[str, str]:
    source_text = (source or "").lower()
    reason_text = (reason or "").lower()
    if (
        source_text.startswith("openclaw")
        or (request_id or "").startswith("openclaw-")
        or "user_requested" in reason_text
    ):
        return "user_confirmed_command", "你通过植境智养确认的小口浇水"
    if source_text in {"mqtt", "direct_mqtt"} and not operator_name:
        return "automatic_system", "自动养护浇水"
    if source_text == "unknown_direct_mqtt":
        return "unattributed_command", "来源尚未确认的水泵动作"
    if operator_name:
        return "operator_command", f"{operator_name}发起的浇水"
    return "system_record", "系统记录的浇水"


def latest_irrigation(device_code: str) -> dict[str, Any] | None:
    rows = gsql_rows(
        "SELECT command_time,water_sec,status,source,reason,operator_name,request_id "
        "FROM irrigation_events "
        f"WHERE device_code='{device_code}' AND water_sec IS NOT NULL AND water_sec > 0 "
        "ORDER BY command_time DESC LIMIT 1;"
    )
    if not rows:
        return None
    row = rows[0]
    source = row[3] if len(row) > 3 else None
    reason = row[4] if len(row) > 4 else None
    operator_name = row[5] if len(row) > 5 else None
    request_id = row[6] if len(row) > 6 else None
    source_kind, source_label = normalize_irrigation_source(source, reason, operator_name, request_id)
    return {
        "time": row[0],
        "water_sec": float(row[1]),
        "status": row[2] if len(row) > 2 else None,
        "source": source,
        "source_kind": source_kind,
        "source_label": source_label,
        "reason": reason,
        "operator_name": operator_name,
        "request_id": request_id,
        "age_sec": max(0.0, (datetime.now() - parse_time(row[0])).total_seconds()),
    }


def latest_external_watering(device_code: str) -> dict[str, Any] | None:
    try:
        rows = gsql_rows(
            "SELECT event_time,amount_ml,operator_name,raw_text "
            "FROM external_watering_events "
            f"WHERE device_code='{device_code}' "
            "ORDER BY event_time DESC LIMIT 1;"
        )
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0]
    event_time = row[0]
    return {
        "time": event_time,
        "amount_ml": float(row[1]) if len(row) > 1 and row[1] else None,
        "operator_name": row[2] if len(row) > 2 else None,
        "raw_text": row[3] if len(row) > 3 else None,
        "source_kind": "user_reported_manual",
        "source_label": "你报告的人工浇水",
        "age_sec": max(0.0, (datetime.now() - parse_time(event_time)).total_seconds()),
    }


def trend_from_readings(readings: list[dict[str, Any]]) -> dict[str, Any]:
    if len(readings) < 2:
        return {"label": "unknown", "delta": None, "description": "数据还不够看趋势"}
    first = readings[0]["humidity"]
    last = readings[-1]["humidity"]
    delta = round(last - first, 3)
    recent = [r["humidity"] for r in readings[-5:]]
    if abs(delta) < 0.2 and (max(recent) - min(recent) < 0.3):
        label = "stable"
        desc = "最近读数很稳"
    elif delta < -0.5:
        label = "drying"
        desc = "湿度在往下走"
    elif delta > 0.5:
        label = "wetting"
        desc = "湿度在回升"
    else:
        label = "slight_change"
        desc = "有轻微变化"
    return {"label": label, "delta": delta, "description": desc, "recent_avg": round(mean(recent), 3)}


def bool_active(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("active") is not None:
            return bool(value.get("active"))
        until = value.get("until") or value.get("pause_until")
        if isinstance(until, (int, float)) and until > time.time():
            return True
    return bool(value)


def latest_activity(
    irrigation: dict[str, Any] | None,
    external: dict[str, Any] | None,
) -> dict[str, Any] | None:
    candidates = [item for item in (irrigation, external) if item and item.get("time")]
    if not candidates:
        return None
    return max(candidates, key=lambda item: parse_time(str(item["time"])))


def build_summary(device_code: str, config: dict[str, Any]) -> dict[str, Any]:
    state = read_json(Path(config["state_path"]))
    watcher = read_json(WATCHER_STATE_PATH)
    readings = latest_readings(device_code)
    latest = readings[-1] if readings else None
    target_low = target_low_from_state(state)
    trend = trend_from_readings(readings)
    irrigation = latest_irrigation(device_code)
    external = latest_external_watering(device_code)
    activity = latest_activity(irrigation, external)

    sensor_age = None
    if latest:
        sensor_age = max(0.0, (datetime.now() - parse_time(latest["recv_time"])).total_seconds())

    pending_soak = bool_active(state.get("pending_soak"))
    water_delivery_suspect = bool_active(state.get("water_delivery_suspect"))
    watcher_pending = watcher.get("pending") or {}
    pending_user_choice = bool(
        watcher_pending
        and watcher_pending.get("device_code", "soil3") == device_code
    )

    humidity = latest["humidity"] if latest else None
    if latest is None:
        water_status = "unknown"
        risk_level = "high"
        recommended_action = "check_sensor"
    elif sensor_age is not None and sensor_age > MAX_SENSOR_AGE_SEC:
        water_status = "unknown"
        risk_level = "medium"
        recommended_action = "wait_for_fresh_data"
    elif water_delivery_suspect:
        water_status = "needs_attention"
        risk_level = "high"
        recommended_action = "check_water_path"
    elif pending_soak:
        water_status = "just_watered_waiting"
        risk_level = "low"
        recommended_action = "observe"
    elif humidity < target_low - 1.0:
        water_status = "thirsty"
        risk_level = "medium"
        recommended_action = "consider_watering"
    elif humidity < target_low:
        water_status = "near_drink_line"
        risk_level = "low"
        recommended_action = "observe"
    else:
        water_status = "not_thirsty"
        risk_level = "low"
        recommended_action = "observe"

    points: list[str] = []
    if latest:
        points.append(f"根部附近湿度 {humidity:.1f}%")
        points.append(f"参考喝水线 {target_low:.1f}%")
    points.append(trend["description"])
    if irrigation and irrigation["age_sec"] < 3 * 3600:
        points.append("最近刚小口补过水")
    if external and external["age_sec"] < 6 * 3600:
        points.append("记得你刚手动照顾过我")
    if pending_user_choice:
        points.append("我刚发过一个选择题，正在等你安排")

    return {
        "ok": True,
        "display_name": config.get("user_visible_name") or "这盆植物",
        "device_code": device_code,
        "checked_at": now_iso(),
        "user_language": {
            "hide_internal_names": True,
            "preferred_terms": ["这盆植物", "我", "根部附近", "自动养护", "当前养护建议"],
        },
        "water_status": water_status,
        "risk_level": risk_level,
        "recommended_action": recommended_action,
        "sensor": {
            **(latest or {}),
            "age_sec": round(sensor_age, 1) if sensor_age is not None else None,
            "fresh": bool(sensor_age is not None and sensor_age <= MAX_SENSOR_AGE_SEC),
            "recent_readings": readings,
        },
        "thresholds": {"drink_line": round(target_low, 3)},
        "trend": trend,
        "flags": {
            "pending_soak": pending_soak,
            "water_delivery_suspect": water_delivery_suspect,
            "pending_user_choice": pending_user_choice,
        },
        "recent_irrigation": irrigation,
        "recent_external_watering": external,
        "recent_watering_activity": activity,
        "human_message_points": points,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build structured current-plant status for OpenClaw replies.")
    parser.add_argument("--device")
    parser.add_argument("--config")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    code, config, _ = device_config(args.device, args.config)
    payload = build_summary(code, config)
    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        emit(payload)


if __name__ == "__main__":
    main()
