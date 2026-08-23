#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from openclaw_plant_event_memory import DEFAULT_MEMORY, read_events, upsert_event
from openclaw_plant_registry import device_config


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/root/.openclaw/workspace"))
CONFIG_PATH = Path("/root/.openclaw/openclaw.json")
STATUS_TOOL = ROOT / "tools" / "openclaw_plant_status_summary.py"
STATE_PATH = ROOT / "state" / "openclaw_plant_event_monitor.json"
LOG_PATH = ROOT / "logs" / "openclaw_plant_event_monitor.jsonl"
LOCK_PATH = Path("/tmp/openclaw_plant_event_monitor.lock")
DEFAULT_GROUP_ID = "664EDE9402BCA3FE56EA6249185F4BB3"
DEFAULT_ACCOUNT_ID = "main"
FOLLOW_UP_TYPES = {"user_confirmed_watering", "external_watering_confirmed"}


def now() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except Exception:
        return None


def configure_runtime_paths(device_code: str) -> None:
    """Keep the legacy soil3 paths and isolate every other plant monitor."""
    global STATE_PATH, LOG_PATH, LOCK_PATH
    if device_code == "soil3":
        return
    suffix = f"_{device_code}"
    STATE_PATH = ROOT / "state" / f"openclaw_plant_event_monitor{suffix}.json"
    LOG_PATH = ROOT / "logs" / f"openclaw_plant_event_monitor{suffix}.jsonl"
    LOCK_PATH = Path(f"/tmp/openclaw_plant_event_monitor{suffix}.lock")


def log_event(payload: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload.setdefault("at", now_iso())
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"active_alerts": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"active_alerts": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def status_for(device_code: str) -> dict[str, Any]:
    result = subprocess.run(
        [str(STATUS_TOOL), "--device", device_code],
        text=True,
        capture_output=True,
        timeout=45,
    )
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout)[:1000]}
    return json.loads(result.stdout)


def get_qqbot_config(account_id: str) -> dict[str, str]:
    qqbot = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["channels"]["qqbot"]
    source = qqbot if account_id == "main" else (qqbot.get("accounts") or {}).get(account_id) or {}
    app_id = source.get("appId")
    secret = source.get("clientSecret")
    if not app_id or not secret:
        raise RuntimeError(f"QQBot account is not configured: {account_id}")
    return {"appId": str(app_id), "clientSecret": str(secret)}


def get_access_token(account_id: str) -> str:
    cfg = get_qqbot_config(account_id)
    request = urllib.request.Request(
        "https://bots.qq.com/app/getAppAccessToken",
        data=json.dumps(cfg).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    if not payload.get("access_token"):
        raise RuntimeError(f"access token missing: {payload}")
    return str(payload["access_token"])


def send_group(group_id: str, content: str, dry_run: bool, account_id: str) -> dict[str, Any]:
    if dry_run:
        payload = {"dry_run": True, "content": content}
        log_event({"type": "qq_dry_run", **payload})
        return payload
    request = urllib.request.Request(
        f"https://api.sgroup.qq.com/v2/groups/{group_id}/messages",
        data=json.dumps({"content": content, "msg_type": 0}, ensure_ascii=False).encode(),
        headers={
            "Authorization": "QQBot " + get_access_token(account_id),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    log_event({"type": "qq_sent", "content": content, "result": payload})
    return payload


def current_humidity(status: dict[str, Any]) -> float | None:
    value = (status.get("sensor") or {}).get("humidity")
    return float(value) if isinstance(value, (int, float)) else None


def follow_up_message(
    event: dict[str, Any],
    status: dict[str, Any],
    visible_name: str,
) -> tuple[str, str, str]:
    evidence = dict(event.get("evidence") or {})
    before = evidence.get("humidity_before")
    after = current_humidity(status)
    if before is None:
        return (
            f"来汇报一下，{visible_name}在你浇水后的观察时间到了。当前根部附近水分约 "
            + (f"{after:.1f}%" if after is not None else "还没拿稳")
            + "，我会把这次照顾记进日记，暂时别连续补水。",
            "observed_without_baseline",
            "已完成浇水后的观察，但缺少浇水前基线",
        )
    delta = None if after is None else after - float(before)
    if delta is not None and delta >= 0.5:
        return (
            f"来报个平安：你刚才给{visible_name}的水已经有反应了，根部附近水分从 "
            f"{float(before):.1f}% 到 {after:.1f}%。我先慢慢吸收，不用续杯。",
            "moisture_response_observed",
            f"浇水后根部附近水分上升约 {delta:.1f} 个点",
        )
    return (
        f"来跟进一下：给{visible_name}浇水后，根部附近读数暂时没有明显回升。"
        "先别重复浇；方便时看一眼水箱、水管、出水口和探针位置。",
        "moisture_response_not_obvious",
        "浇水后读数暂未明显回升，建议检查水路和探针",
    )


def process_follow_ups(
    events: list[dict[str, Any]],
    statuses: dict[str, dict[str, Any]],
    names: dict[str, str],
    group_id: str,
    dry_run: bool,
    account_id: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for event in events:
        if event.get("status") != "open" or event.get("event_type") not in FOLLOW_UP_TYPES:
            continue
        due = parse_iso(event.get("follow_up_due_at"))
        if not due or due > now():
            continue
        device = str(event.get("device_code"))
        status = statuses.get(device) or {}
        evidence = dict(event.get("evidence") or {})
        humidity = current_humidity(status)
        if evidence.get("humidity_before") is None and humidity is not None:
            occurred = parse_iso(event.get("occurred_at"))
            if occurred and now() - occurred < timedelta(minutes=10):
                event["evidence"] = {**evidence, "humidity_before": humidity}
                event["follow_up_due_at"] = (now() + timedelta(minutes=30)).isoformat(timespec="seconds")
                if not dry_run:
                    upsert_event(DEFAULT_MEMORY, event)
                continue
        message, resolution, summary = follow_up_message(
            event,
            status,
            names.get(device, "这盆植物"),
        )
        send_group(group_id, message, dry_run, account_id)
        event.update(
            {
                "status": "resolved",
                "resolved_at": now_iso(),
                "resolution": resolution,
                "summary": summary,
                "evidence": {
                    **evidence,
                    "humidity_after": humidity,
                },
            }
        )
        if not dry_run:
            upsert_event(DEFAULT_MEMORY, event)
        results.append({"event_id": event.get("event_id"), "resolution": resolution})
    return results


def anomaly_message(status: dict[str, Any], visible_name: str) -> tuple[str | None, str | None]:
    flags = status.get("flags") or {}
    sensor = status.get("sensor") or {}
    if flags.get("water_delivery_suspect"):
        return (
            "water_delivery_suspect",
            f"{visible_name}这边像是水没有顺利到达根部。先别连续加水，麻烦检查水箱、水管、出水口和探针位置。",
        )
    if not sensor.get("fresh", False):
        return (
            "sensor_stale",
            f"{visible_name}有一阵子没传回新数据了。我暂时不会凭旧读数乱下结论，请检查设备供电和连接。",
        )
    return None, None


def process_anomalies(
    state: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
    names: dict[str, str],
    group_id: str,
    dry_run: bool,
    account_id: str,
) -> list[dict[str, Any]]:
    alerts = state.setdefault("active_alerts", {})
    results: list[dict[str, Any]] = []
    for device, status in statuses.items():
        alert_type, message = anomaly_message(status, names.get(device, "这盆植物"))
        previous = alerts.get(device)
        previous_type = previous.get("type") if isinstance(previous, dict) else previous
        if alert_type and previous_type != alert_type:
            send_group(group_id, str(message), dry_run, account_id)
            if not dry_run:
                created = upsert_event(
                    DEFAULT_MEMORY,
                    {
                    "device_code": device,
                    "event_type": alert_type,
                    "status": "open",
                    "importance": "high",
                    "summary": str(message),
                    "source": "openclaw_plant_event_monitor",
                    "evidence": {
                        "sensor_age_sec": (status.get("sensor") or {}).get("age_sec"),
                        "flags": status.get("flags") or {},
                    },
                    },
                )
                alerts[device] = {"type": alert_type, "event_id": created.get("event_id")}
            results.append({"device_code": device, "alert": alert_type})
        elif not alert_type and previous:
            if not dry_run:
                alerts.pop(device, None)
                event_id = previous.get("event_id") if isinstance(previous, dict) else None
                if event_id:
                    for old in read_events(DEFAULT_MEMORY):
                        if old.get("event_id") == event_id:
                            upsert_event(
                                DEFAULT_MEMORY,
                                {
                                    **old,
                                    "status": "resolved",
                                    "resolved_at": now_iso(),
                                    "resolution": "system_state_recovered",
                                    "summary": f"{names.get(device, '这盆植物')}此前的异常已经恢复",
                                },
                            )
                            break
                upsert_event(
                    DEFAULT_MEMORY,
                    {
                    "device_code": device,
                    "event_type": "system_state_recovered",
                    "status": "recorded",
                    "importance": "normal",
                    "summary": f"{names.get(device, '这盆植物')}的数据与养护状态已经恢复正常",
                    "source": "openclaw_plant_event_monitor",
                        "evidence": {"recovered_from": previous_type},
                    },
                )
    return results


def run_once(group_id: str, dry_run: bool, device_code: str, account_id: str) -> dict[str, Any]:
    code, config, _ = device_config(device_code)
    selected = [(code, config)]
    names = {code: str(cfg.get("user_visible_name") or "这盆植物") for code, cfg in selected}
    statuses = {code: status_for(code) for code, _ in selected}
    state = load_state()
    events = [event for event in read_events(DEFAULT_MEMORY) if str(event.get("device_code")) == code]
    follow_ups = process_follow_ups(events, statuses, names, group_id, dry_run, account_id)
    anomalies = process_anomalies(state, statuses, names, group_id, dry_run, account_id)
    if not dry_run:
        state["last_checked_at"] = now_iso()
        save_state(state)
    payload = {
        "ok": True,
        "checked_at": now_iso(),
        "devices": list(statuses),
        "account_id": account_id,
        "follow_ups": follow_ups,
        "anomalies": anomalies,
        "dry_run": dry_run,
    }
    log_event({"type": "monitor_run", **payload})
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Follow up one plant's care events through its QQbot account.")
    parser.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    parser.add_argument("--device", default="soil3")
    parser.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    configure_runtime_paths(args.device)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        payload = run_once(args.group_id, args.dry_run, args.device, args.account_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json else None))


if __name__ == "__main__":
    main()
