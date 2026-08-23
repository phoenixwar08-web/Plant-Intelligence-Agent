#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


DEVICE_CODE = "soil3"
PUMP_TOPIC = "esp32/pump3/cmd"
PUMP_META_TOPIC = "esp32/pump3/cmd_meta"
MQTT_HOST = "localhost"
STATE_PATH = Path("/root/water/phase3/soil3/system_state.json")
AUDIT_PATH = Path("/root/.openclaw/workspace/logs/openclaw_irrigation_requests.jsonl")
LOCK_PATH = Path("/tmp/openclaw_soil3_water.lock")

RECOMMENDED_SEC = 3.0
MAX_USER_SEC = 10.0
TARGET_LOW_FALLBACK = 33.0
MAX_SENSOR_AGE_SEC = 30 * 60
MIN_INTERVAL_SEC = 10 * 60
ALLOW_MARGIN = 0.8


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def audit(payload: dict[str, Any]) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def reject(request: dict[str, Any], reason: str, detail: str, context: dict[str, Any]) -> None:
    payload = {
        **request,
        "ok": False,
        "status": "rejected",
        "reason": reason,
        "detail": detail,
        "context": context,
        "checked_at": now_iso(),
    }
    audit(payload)
    emit(payload)
    raise SystemExit(2)


def needs_confirmation(
    request: dict[str, Any],
    reason: str,
    detail: str,
    context: dict[str, Any],
    confirm_hint: str,
) -> None:
    payload = {
        **request,
        "ok": False,
        "status": "needs_confirmation",
        "risk_type": "soft",
        "reason": reason,
        "detail": detail,
        "confirm_hint": confirm_hint,
        "context": context,
        "checked_at": now_iso(),
    }
    audit(payload)
    emit(payload)
    raise SystemExit(3)


def run_checked(cmd: list[str], *, input_text: str | None = None, timeout: int = 20) -> str:
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def gsql_scalar_rows(sql: str) -> list[list[str]]:
    command = "gsql -d soil_data -p 7654 -t -A -F , -c " + shlex.quote(sql)
    out = run_checked(
        [
            "su",
            "-",
            "opengauss",
            "-c",
            command,
        ],
        timeout=30,
    )
    rows: list[list[str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("("):
            continue
        rows.append(line.split(","))
    return rows


def read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    with STATE_PATH.open(encoding="utf-8", errors="ignore") as fh:
        return json.load(fh)


def truthy_state(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("active") is not None:
            return bool(value.get("active"))
        until = value.get("until") or value.get("pause_until")
        if isinstance(until, (int, float)) and until > time.time():
            return True
        return any(bool(v) for v in value.values())
    return bool(value)


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


def latest_sensor() -> dict[str, Any]:
    rows = gsql_scalar_rows(
        "SELECT recv_time,humidity,temp,lux "
        "FROM soil_sensor_readings "
        "WHERE device_code='soil3' "
        "ORDER BY recv_time DESC LIMIT 1;"
    )
    if not rows:
        return {}
    row = rows[0]
    recv_time = row[0]
    age_sec = max(0.0, (datetime.now() - datetime.strptime(recv_time, "%Y-%m-%d %H:%M:%S")).total_seconds())
    return {
        "recv_time": recv_time,
        "age_sec": age_sec,
        "humidity": float(row[1]),
        "temp": float(row[2]) if row[2] else None,
        "lux": float(row[3]) if row[3] else None,
    }


def latest_irrigation_age_sec() -> float | None:
    rows = gsql_scalar_rows(
        "SELECT command_time,water_sec,status,source "
        "FROM irrigation_events "
        "WHERE device_code='soil3' AND water_sec IS NOT NULL AND water_sec > 0 "
        "ORDER BY command_time DESC LIMIT 1;"
    )
    if not rows:
        return None
    command_time = rows[0][0]
    return max(0.0, (datetime.now() - datetime.strptime(command_time, "%Y-%m-%d %H:%M:%S")).total_seconds())


def publish(payload: str) -> None:
    run_checked(["mosquitto_pub", "-h", MQTT_HOST, "-t", PUMP_TOPIC, "-m", payload], timeout=10)


def publish_meta(request: dict[str, Any], status: str, context: dict[str, Any], *, command: str | None = None) -> None:
    payload = {
        "device_code": DEVICE_CODE,
        "pump_topic": PUMP_TOPIC,
        "request_id": request.get("request_id"),
        "operator_id": request.get("operator_id"),
        "operator_name": request.get("operator_name"),
        "source": request.get("source") or "openclaw_qqbot",
        "reason": request.get("request_reason") or "user_requested_irrigation",
        "status": status,
        "water_sec": request.get("requested_seconds"),
        "command": command,
        "created_at": now_iso(),
        "context": {
            "humidity": context.get("humidity"),
            "target_low": context.get("target_low"),
            "sensor_age_sec": context.get("sensor_age_sec"),
            "soft_risk_overridden": context.get("soft_risk_overridden"),
        },
    }
    run_checked(
        [
            "mosquitto_pub",
            "-h",
            MQTT_HOST,
            "-t",
            PUMP_META_TOPIC,
            "-m",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ],
        timeout=10,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw guarded soil3 watering gateway")
    parser.add_argument("--seconds", type=float, default=RECOMMENDED_SEC)
    parser.add_argument("--operator-id", default="openclaw-mainbot")
    parser.add_argument("--operator-name", default="植境智养")
    parser.add_argument("--source", default="openclaw_qqbot")
    parser.add_argument("--reason", default="user_requested_irrigation")
    parser.add_argument("--risk-confirmed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    request = {
        "request_id": f"openclaw-{uuid.uuid4().hex[:12]}",
        "device_code": DEVICE_CODE,
        "pump_topic": PUMP_TOPIC,
        "requested_seconds": args.seconds,
        "operator_id": args.operator_id,
        "operator_name": args.operator_name,
        "source": args.source,
        "request_reason": args.reason,
        "risk_confirmed": bool(args.risk_confirmed),
        "dry_run": bool(args.dry_run),
        "created_at": now_iso(),
    }

    if args.seconds <= 0 or args.seconds > MAX_USER_SEC:
        reject(
            request,
            "seconds_out_of_range",
            f"用户指令秒数必须在 0 到 {MAX_USER_SEC:g} 秒之间。",
            {"max_user_sec": MAX_USER_SEC},
        )

    if not args.dry_run and os.environ.get("OPENCLAW_IRRIGATION_WATCHER") != "1":
        reject(
            request,
            "executor_not_authorized",
            "真实开泵只能由 openclaw-irrigation-watcher 调用，不能由 OpenClaw 主对话直接执行。",
            {"required_executor": "openclaw-irrigation-watcher"},
        )

    with LOCK_PATH.open("w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            reject(request, "busy", "已有一个 soil3 用户浇水请求正在执行。", {})

        state = read_state()
        sensor = latest_sensor()
        target_low = target_low_from_state(state)
        context = {
            "target_low": round(target_low, 3),
            "sensor": sensor,
            "pending_soak": state.get("pending_soak"),
            "water_delivery_suspect": state.get("water_delivery_suspect"),
            "dynamic_cooldown": state.get("dynamic_cooldown"),
        }

        if not sensor:
            reject(request, "sensor_missing", "没有读到 soil3 最新传感器数据，先不碰水泵。", context)

        sensor_age = float(sensor["age_sec"])
        context["sensor_age_sec"] = round(sensor_age, 1)
        if sensor_age > MAX_SENSOR_AGE_SEC:
            reject(request, "sensor_stale", "soil3 传感器数据过旧，先不碰水泵。", context)

        humidity = float(sensor["humidity"])
        context["humidity"] = humidity

        if truthy_state(state.get("pending_soak")):
            reject(request, "pending_soak", "我刚喝过，水还在土里散开，先别重复浇。", context)

        suspect = state.get("water_delivery_suspect")
        if isinstance(suspect, dict) and suspect.get("active"):
            reject(request, "water_delivery_suspect", "系统怀疑水没送到根部，需要先检查水管/水箱/探头。", context)

        if humidity > target_low + ALLOW_MARGIN:
            if not args.risk_confirmed:
                needs_confirmation(
                    request,
                    "humidity_above_target",
                    "我现在还不渴，继续浇水有把根区弄太湿的风险。",
                    context,
                    f"如果你仍要浇，请再次明确回复：确认浇水 {args.seconds:g} 秒",
                )
            context["soft_risk_overridden"] = "humidity_above_target"

        recent_age = latest_irrigation_age_sec()
        context["latest_irrigation_age_sec"] = None if recent_age is None else round(recent_age, 1)
        if recent_age is not None and recent_age < MIN_INTERVAL_SEC:
            if not args.risk_confirmed:
                needs_confirmation(
                    request,
                    "recent_irrigation",
                    "刚有过一次有效浇水记录，继续浇水需要你二次确认。",
                    context,
                    f"如果你仍要浇，请再次明确回复：确认浇水 {args.seconds:g} 秒",
                )
            context["soft_risk_overridden"] = (
                context.get("soft_risk_overridden", "") + ";recent_irrigation"
            ).strip(";")

        if context.get("soft_risk_overridden") and args.seconds > RECOMMENDED_SEC:
            reject(
                request,
                "risk_override_seconds_too_high",
                f"风险确认模式下最多只允许推荐小口 {RECOMMENDED_SEC:g} 秒，不执行更大剂量。",
                context,
            )

        payload = {
            **request,
            "ok": True,
            "status": "dry_run_ok" if args.dry_run else "executing",
            "approved_seconds": round(args.seconds, 3),
            "context": context,
            "checked_at": now_iso(),
        }
        audit(payload)

        if args.dry_run:
            emit(payload)
            return

        try:
            publish_meta(request, "user_requested_irrigation_started", context, command="on")
            publish("on")
            time.sleep(args.seconds)
        finally:
            publish_meta(request, "user_requested_irrigation_completed", context, command="off")
            publish("off")

        done = {
            **payload,
            "status": "completed",
            "completed_at": now_iso(),
        }
        audit(done)
        emit(done)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "reason": "executor_error",
            "detail": str(exc),
            "checked_at": now_iso(),
        }
        audit(error_payload)
        emit(error_payload)
        raise SystemExit(1)
