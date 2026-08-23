#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from openclaw_plant_registry import device_config, report_devices


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/root/.openclaw/workspace"))
CONTEXT_TOOL = ROOT / "tools" / "openclaw_plant_context.py"
STATUS_TOOL = ROOT / "tools" / "openclaw_plant_status_summary.py"
EVENT_TOOL = ROOT / "tools" / "openclaw_plant_event_memory.py"


def run_json(cmd: list[str], timeout: int = 180) -> dict[str, Any]:
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout)[:1200]}
    return json.loads(result.stdout)


def plant_overview(code: str, config: dict[str, Any]) -> dict[str, Any]:
    status = run_json([str(STATUS_TOOL), "--device", code], timeout=45)
    sensor = status.get("sensor") or {}
    activity = status.get("recent_watering_activity") or {}
    return {
        "device_code": code,
        "display_name": config.get("user_visible_name") or "这盆植物",
        "ok": status.get("ok", False),
        "humidity": sensor.get("humidity"),
        "fresh": sensor.get("fresh"),
        "risk_level": status.get("risk_level"),
        "recommended_action": status.get("recommended_action"),
        "recent_watering": {
            "time": activity.get("time"),
            "water_sec": activity.get("water_sec"),
            "source_kind": activity.get("source_kind"),
            "source_label": activity.get("source_label"),
        } if activity else None,
    }


def build_snapshot(device_code: str | None, include_visual: bool) -> dict[str, Any]:
    selected, selected_config, registry = device_config(device_code)
    context_cmd = [str(CONTEXT_TOOL), "--device", selected]
    if not include_visual:
        context_cmd.append("--no-visual")
    context = run_json(context_cmd)
    events = run_json([str(EVENT_TOOL), "--device", selected, "--limit", "8"], timeout=30)
    overviews = [
        plant_overview(code, config)
        for code, config in report_devices()
    ]
    return {
        "ok": bool(context.get("ok")),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "identity": registry.get("identity") or {},
        "selected_plant": {
            "device_code": selected,
            "display_name": selected_config.get("user_visible_name") or "这盆植物",
            "first_person_reply": (context.get("combined") or {}).get("first_person_reply"),
            "visual_points": (context.get("combined") or {}).get("visual_points") or [],
            "daily_memory_points": (context.get("combined") or {}).get("memory_points") or [],
            "event_memory_points": (context.get("combined") or {}).get("event_memory_points") or [],
        },
        "all_plants": overviews,
        "recent_events": events.get("recent_events") or [],
        "safety_boundary": [
            "视觉、知识和记忆用于解释，不能单独决定开泵",
            "用户确认浇水由 watcher 解释并交给受限网关复核",
            "自动养护继续负责常规自治与硬安全保护",
        ],
    }


def markdown(snapshot: dict[str, Any]) -> str:
    selected = snapshot.get("selected_plant") or {}
    lines = [
        "# 植境智养 Plant Talk 演示快照",
        "",
        f"生成时间：{snapshot.get('generated_at')}",
        "",
        "## 当前盆栽怎么说",
        "",
        str(selected.get("first_person_reply") or "当前状态暂不可用。"),
        "",
        "## 全部盆栽概览",
        "",
        "| 盆栽 | 根部附近水分 | 状态 | 最近浇水来源 |",
        "| --- | ---: | --- | --- |",
    ]
    for item in snapshot.get("all_plants") or []:
        humidity = item.get("humidity")
        humidity_text = f"{humidity:.1f}%" if isinstance(humidity, (int, float)) else "暂无"
        activity = item.get("recent_watering") or {}
        lines.append(
            f"| {item.get('display_name')} | {humidity_text} | {item.get('risk_level') or '未知'} "
            f"| {activity.get('source_label') or '暂无'} |"
        )
    lines.extend(["", "## 近期重要事件", ""])
    events = snapshot.get("recent_events") or []
    if not events:
        lines.append("- 暂无重要事件。")
    else:
        for event in reversed(events[-6:]):
            lines.append(f"- {event.get('occurred_at')}：{event.get('summary')}")
    lines.extend(["", "## 安全边界", ""])
    for item in snapshot.get("safety_boundary") or []:
        lines.append(f"- {item}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only Plant Talk competition demo snapshot.")
    parser.add_argument("--device")
    parser.add_argument("--no-visual", action="store_true")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    args = parser.parse_args()
    snapshot = build_snapshot(args.device, include_visual=not args.no_visual)
    if args.format == "markdown":
        print(markdown(snapshot))
    else:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
