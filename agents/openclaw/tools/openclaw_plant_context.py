#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from openclaw_plant_registry import device_config

ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/root/.openclaw/workspace"))
STATUS_TOOL = ROOT / "tools" / "openclaw_plant_status_summary.py"
VISUAL_TOOL = ROOT / "tools" / "openclaw_plant_visual_observation.py"
CARE_KNOWLEDGE_TOOL = ROOT / "tools" / "openclaw_plant_care_knowledge.py"
DAILY_MEMORY_TOOL = ROOT / "tools" / "openclaw_plant_daily_memory.py"
EVENT_MEMORY_TOOL = ROOT / "tools" / "openclaw_plant_event_memory.py"


INTERNAL_TERMS = [
    "Phase3",
    "soil3",
    "soil1",
    "soil2",
    "soil_test",
    "GaussDB",
    "MQTT",
    "watcher",
    "pending_soak",
    "water_delivery_suspect",
]

BACKGROUND_TERMS = [
    "左侧盆",
    "左边",
    "右侧盆",
    "右边",
    "其它盆",
    "其他盆",
]

TARGET_TERMS = [
    "中间盆",
    "中间那盆",
    "目标盆",
    "目标盆位",
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_json(cmd: list[str], timeout: int, input_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    input_text = json.dumps(input_payload, ensure_ascii=False) if input_payload is not None else None
    result = subprocess.run(cmd, text=True, input=input_text, capture_output=True, timeout=timeout)
    out = result.stdout.strip()
    if result.returncode != 0:
        return {
            "ok": False,
            "error": "tool_failed",
            "cmd": cmd,
            "exit_code": result.returncode,
            "detail": (out or result.stderr.strip())[:2000],
        }
    try:
        return json.loads(out)
    except Exception as exc:
        return {
            "ok": False,
            "error": "json_parse_failed",
            "cmd": cmd,
            "detail": f"{type(exc).__name__}: {exc}",
            "raw": out[:2000],
        }


def risk_rank(value: str | None) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get((value or "").lower(), 0)


def max_risk(*values: str | None) -> str:
    ranked = sorted(((risk_rank(v), v or "unknown") for v in values), reverse=True)
    return ranked[0][1] if ranked and ranked[0][0] else "unknown"


def risk_text(value: str | None) -> str:
    return {"low": "低", "medium": "中等", "high": "高"}.get((value or "").lower(), "未知")


def strip_internal_terms(text: str) -> str:
    for term in INTERNAL_TERMS:
        text = text.replace(term, "自动养护")
    return text


def strip_watering_claims(text: str) -> str:
    patterns = [
        r"别急着给我浇水[，,。]?",
        r"别乱浇水[，,。]?",
        r"我还没喊渴[，,。]?",
        r"我没喊渴[，,。]?",
        r"也不喊渴[，,。]?",
        r"不喊渴[，,。]?",
        r"我真不渴[，,。]?",
        r"我不渴[，,。]?",
        r"我想喝水[，,。]?",
        r"[^，,。；;]*渴不渴[^，,。；;]*[，,。；;]?",
        r"我需要浇水[，,。]?",
        r"需要浇水[，,。]?",
        r"该浇水[，,。]?",
        r"[^，,。；;]*喊渴[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*口渴[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*要水[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*想喝[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*灌水[^，,。；;]*[，,。；;]?",
        r"比昨天[^，,。；;]*[，,。；;]?",
        r"比前[^，,。；;]*[，,。；;]?",
        r"越来越[^，,。；;]*[，,。；;]?",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip("，,。；; ")


def focus_target_pot_text(text: str) -> str:
    parts = re.split(r"([；;。])", text)
    kept: list[str] = []
    for idx in range(0, len(parts), 2):
        clause = parts[idx].strip()
        sep = parts[idx + 1] if idx + 1 < len(parts) else ""
        if not clause:
            continue
        if any(term in clause for term in BACKGROUND_TERMS) and not any(term in clause for term in TARGET_TERMS):
            continue
        for term in TARGET_TERMS:
            clause = clause.replace(term, "我")
        kept.append(clause + sep)
    focused = "".join(kept).strip()
    if not focused:
        focused = text
        for term in TARGET_TERMS:
            focused = focused.replace(term, "我")
    return focused.rstrip("，,。；; ")


def visual_parsed(visual: dict[str, Any] | None) -> dict[str, Any] | None:
    if not visual or not visual.get("ok"):
        return None
    parsed = visual.get("parsed")
    if not isinstance(parsed, dict):
        return None
    sanitized = dict(parsed)
    for key, value in list(sanitized.items()):
        value = sanitized.get(key)
        if isinstance(value, str):
            value = focus_target_pot_text(value)
            if key in ("user_friendly_summary", "caveats"):
                value = strip_watering_claims(value)
            sanitized[key] = value
    return sanitized


def compact_visual(visual: dict[str, Any] | None, parsed: dict[str, Any] | None, include_raw: bool) -> dict[str, Any] | None:
    if visual is None:
        return None
    if not visual.get("ok"):
        return visual
    compact = {
        "ok": True,
        "source": visual.get("source"),
        "image_path": visual.get("image_path"),
        "image_size_bytes": visual.get("image_size_bytes"),
        "model": visual.get("model"),
        "parsed": parsed,
        "parse_error": visual.get("parse_error"),
        "raw_content_omitted": not include_raw,
    }
    if include_raw:
        compact["raw_content"] = visual.get("raw_content")
    return compact


def visual_points(parsed: dict[str, Any] | None) -> list[str]:
    if not parsed:
        return []
    points: list[str] = []
    for key in ("visible_leaf_state", "wilt_or_droop", "yellowing_or_browning", "soil_surface"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            if value.strip() in {"无", "没有", "未见", "无明显", "不明显"}:
                continue
            points.append(value.strip())
    return points[:4]


def care_knowledge_points(care: dict[str, Any] | None) -> list[str]:
    if not care or not care.get("ok"):
        return []
    points: list[str] = []
    for match in care.get("matches") or []:
        title = match.get("title")
        explanation = match.get("explanation")
        if title and explanation:
            points.append(f"{title}: {explanation}")
    return points[:3]


def memory_points(memory: dict[str, Any] | None) -> list[str]:
    if not memory or not memory.get("ok"):
        return []
    return [
        point
        for point in (memory.get("trend_points") or [])
        if isinstance(point, str) and point.strip()
    ][:3]


def event_memory_points(memory: dict[str, Any] | None) -> list[str]:
    if not memory or not memory.get("ok"):
        return []
    return [
        point
        for point in (memory.get("memory_points") or [])
        if isinstance(point, str) and point.strip()
    ][:4]


def action_label(status: dict[str, Any], visual: dict[str, Any] | None) -> str:
    flags = status.get("flags") or {}
    recommended = status.get("recommended_action")
    water_status = status.get("water_status")
    visual_risk = (visual or {}).get("risk_level")

    if flags.get("water_delivery_suspect"):
        return "check_water_path"
    if flags.get("pending_soak"):
        return "wait_soak"
    if recommended == "consider_watering":
        return "let_auto_care_decide_watering"
    if risk_rank(visual_risk) >= 3:
        return "inspect_visual_issue"
    if water_status == "not_thirsty":
        return "observe"
    return recommended or "observe"


def user_action_advice(action: str) -> str:
    if action == "observe":
        return "所以我先不加餐，让自动养护继续盯着就好。"
    if action == "let_auto_care_decide_watering":
        return "我可能快到喝水窗口了，但要不要开泵还是交给自动养护做安全判断。"
    if action == "wait_soak":
        return "刚喝过的水还在土里散开，先让我消化一下，别急着续杯。"
    if action == "check_water_path":
        return "你有空帮我看一眼水箱、水管、出水口和探针位置，像是有水没真正送到根部附近。"
    if action == "inspect_visual_issue":
        return "画面里有点不对劲，建议你现场看一眼叶片、盆土和摄像头角度。"
    return "先继续观察，有异常我再喊你。"


def abnormal_hint(status: dict[str, Any], parsed_visual: dict[str, Any] | None) -> str:
    flags = status.get("flags") or {}
    recent = status.get("recent_irrigation") or {}
    trend = status.get("trend") or {}
    visual_text = " ".join(
        str((parsed_visual or {}).get(key) or "")
        for key in ("pot_probe_tube_visible", "soil_surface", "caveats")
    )
    if flags.get("water_delivery_suspect"):
        return "如果你在现场，优先看水箱有没有水、水管有没有折住、出水口有没有偏到盆外；这些比继续浇更重要。"
    if flags.get("pending_soak"):
        return "刚浇完马上看湿度可能会冤枉我，水要一点时间才会扩散到探针附近。"
    if "探针" in visual_text and any(word in visual_text for word in ("偏", "歪", "松", "不清楚", "遮挡")):
        return "探针位置如果动了，读数会突然不像平时；你可以轻轻确认它还在原来的土层里。"
    if recent.get("water_sec") and trend.get("label") in {"falling", "flat_low"}:
        return "如果刚浇过但水分没明显回升，先检查水有没有进盆，再看探针附近是不是太偏干或太偏湿。"
    return ""


def synthesize_reply(
    status: dict[str, Any],
    parsed_visual: dict[str, Any] | None,
    care: dict[str, Any] | None,
    memory: dict[str, Any] | None,
    event_memory: dict[str, Any] | None,
) -> str:
    sensor = status.get("sensor") or {}
    thresholds = status.get("thresholds") or {}
    humidity = sensor.get("humidity")
    drink_line = thresholds.get("drink_line")
    trend = status.get("trend") or {}
    visual_risk = (parsed_visual or {}).get("risk_level", "unknown")

    if isinstance(humidity, (int, float)) and isinstance(drink_line, (int, float)):
        if humidity >= drink_line:
            first = f"我现在根部附近还有库存，湿度 {humidity:.1f}%，比参考喝水线 {drink_line:.1f}% 高。"
        else:
            first = f"我根部附近有点接近喝水线了，湿度 {humidity:.1f}%，参考线是 {drink_line:.1f}%。"
    else:
        first = "我这会儿没拿到足够新的根部数据，先别让我凭感觉乱喝水。"

    visual_sentence = ""
    if parsed_visual:
        leaf = str(parsed_visual.get("visible_leaf_state") or "叶片状态可见").rstrip("。；; ")
        wilt = str(parsed_visual.get("wilt_or_droop") or "").rstrip("。；; ")
        if not (leaf.startswith("叶") or leaf.startswith("绿叶")):
            leaf = "叶片" + leaf
        visual_sentence = f"画面里看，{leaf}"
        if wilt and wilt not in {"无", "没有", "未见", "无明显", "不明显"}:
            visual_sentence += f"；{wilt}"
        visual_sentence += "。"

    trend_desc = trend.get("description") or "趋势还要继续观察"
    action = action_label(status, parsed_visual)
    advice = user_action_advice(action)

    parts = [first]
    if visual_sentence:
        parts.append(visual_sentence)
    if parsed_visual:
        parts.append(f"{trend_desc}，视觉风险{risk_text(visual_risk)}。{advice}")
    else:
        parts.append(f"{trend_desc}。{advice}")

    matches = [
        match
        for match in ((care or {}).get("matches") or [])
        if match.get("id") != "vision_cannot_decide_watering"
    ]
    if matches:
        top = matches[0]
        explanation = str(top.get("explanation") or "").rstrip("。；; ")
        explanation = explanation.replace("根区传感器", "根部附近数据")
        explanation = explanation.replace("根区数据", "根部附近数据")
        advice_items = top.get("advice") or []
        if explanation:
            parts.append(f"按养护经验，{explanation}。")
        if advice_items:
            advice_text = str(advice_items[0]).rstrip("。；; ")
            parts.append(f"小建议：{advice_text}。")
    hint = abnormal_hint(status, parsed_visual)
    if hint:
        parts.append(hint)
    memory_sentence = (memory or {}).get("first_person_memory_sentence")
    if isinstance(memory_sentence, str) and memory_sentence.strip():
        parts.append("我的小日记：" + memory_sentence.strip().rstrip("。") + "。")
    event_points = event_memory_points(event_memory)
    if event_points:
        parts.append("我还记得：" + event_points[0].rstrip("。") + "。")
    return strip_internal_terms("".join(parts))


def build_context(
    include_visual: bool,
    include_raw_visual: bool,
    include_memory: bool,
    device_code: str | None,
    config_path: str | None,
) -> dict[str, Any]:
    selected, config, registry = device_config(device_code, config_path)
    status_cmd = [str(STATUS_TOOL), "--device", selected]
    if config_path:
        status_cmd.extend(["--config", config_path])
    status = run_json(status_cmd, timeout=45)
    visual = None
    visual_allowed = bool(config.get("visual_enabled", False))
    if include_visual and visual_allowed:
        visual = run_json([str(VISUAL_TOOL), "--device", selected], timeout=120)
    parsed = visual_parsed(visual)
    visual_compact = compact_visual(visual, parsed, include_raw_visual)
    care_payload = {
        "status": status,
        "visual": visual_compact,
    }
    care = run_json([str(CARE_KNOWLEDGE_TOOL), "--input", "-", "--limit", "3"], timeout=20, input_payload=care_payload)
    status_ok = bool(status.get("ok"))
    visual_ok = bool(visual and visual.get("ok") and parsed)

    context: dict[str, Any] = {
        "ok": status_ok,
        "source": "openclaw_plant_context",
        "checked_at": now_iso(),
        "scope": {
            "default_device": selected,
            "selected_device": selected,
            "user_visible_name": config.get("user_visible_name") or "这盆植物",
            "identity": (registry.get("identity") or {}).get("name") or "植境智养",
            "species_policy": (registry.get("identity") or {}).get("species_policy") or "unknown_and_do_not_guess",
        },
        "status": status,
        "visual": visual_compact,
        "care_knowledge": care,
        "plant_memory": None,
        "event_memory": None,
        "combined": {
            "risk_level": max_risk(status.get("risk_level"), parsed.get("risk_level") if parsed else None),
            "recommended_action": action_label(status, parsed),
            "status_points": status.get("human_message_points") or [],
            "visual_points": visual_points(parsed),
            "care_knowledge_points": care_knowledge_points(care),
            "memory_points": [],
            "event_memory_points": [],
            "guardrails": [
                "visual_observation_cannot_decide_watering",
                "watering_decision_must_use_sensor_and_auto_care",
                "care_knowledge_cannot_override_auto_care",
                "daily_memory_cannot_override_auto_care",
                "event_memory_cannot_override_auto_care",
                "hide_internal_names_for_normal_users",
            ],
            "first_person_reply": "",
        },
    }
    if include_memory and status_ok:
        memory = run_json([str(DAILY_MEMORY_TOOL), "--input", "-", "--upsert", "--limit", "7"], timeout=20, input_payload=context)
        context["plant_memory"] = memory
        context["combined"]["memory_points"] = memory_points(memory)
    else:
        memory = None
    event_memory = run_json(
        [str(EVENT_MEMORY_TOOL), "--device", selected, "--limit", "12"],
        timeout=20,
    )
    context["event_memory"] = event_memory
    context["combined"]["event_memory_points"] = event_memory_points(event_memory)
    context["combined"]["first_person_reply"] = (
        synthesize_reply(status, parsed, care, memory, event_memory)
        if status_ok
        else "我这会儿状态数据没拿稳，先别让我乱说。麻烦稍后再问一次。"
    )
    if include_visual and visual_allowed and not visual_ok:
        context["combined"]["visual_unavailable_reason"] = (visual or {}).get("error") or (visual or {}).get("detail") or "unknown"
    if include_visual and not visual_allowed:
        context["combined"]["visual_unavailable_reason"] = "visual_not_configured_for_this_plant"
    return context


def main() -> None:
    parser = argparse.ArgumentParser(description="Build unified Plant Talk context for OpenClaw.")
    parser.add_argument("--no-visual", action="store_true", help="Skip imageModel visual observation")
    parser.add_argument("--no-memory", action="store_true", help="Skip daily memory upsert and trend explanation")
    parser.add_argument("--device")
    parser.add_argument("--config")
    parser.add_argument("--include-raw-visual", action="store_true", help="Include raw imageModel text in output")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    payload = build_context(
        include_visual=not args.no_visual,
        include_raw_visual=args.include_raw_visual,
        include_memory=not args.no_memory,
        device_code=args.device,
        config_path=args.config,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
