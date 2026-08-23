#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path("/root/.openclaw/workspace")
DEFAULT_KNOWLEDGE = ROOT / "knowledge" / "plant_care_knowledge.json"


def load_payload(path: str) -> dict[str, Any]:
    if path == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_knowledge(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def text_has(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def derive_humidity_relation(status: dict[str, Any]) -> str:
    sensor = status.get("sensor") or {}
    thresholds = status.get("thresholds") or {}
    humidity = sensor.get("humidity")
    drink_line = thresholds.get("drink_line")
    if not isinstance(humidity, (int, float)) or not isinstance(drink_line, (int, float)):
        return "unknown"
    margin = humidity - drink_line
    if margin >= 2.0:
        return "above_drink_line"
    if margin >= -1.5:
        return "near_drink_line"
    return "below_drink_line"


def derive_light_relation(status: dict[str, Any]) -> str:
    sensor = status.get("sensor") or {}
    lux = sensor.get("lux")
    if not isinstance(lux, (int, float)):
        return "unknown"
    hour = datetime.now().astimezone().hour
    if hour < 7 or hour > 19:
        return "night_or_lights_off"
    if lux < 200:
        return "low_light"
    if lux > 5000:
        return "high_light"
    return "normal_light"


def derive_visual_tags(parsed_visual: dict[str, Any] | None) -> set[str]:
    if not parsed_visual:
        return set()
    text = " ".join(str(v) for v in parsed_visual.values() if isinstance(v, str))
    leaf_text = " ".join(
        str(parsed_visual.get(key) or "")
        for key in ("visible_leaf_state", "wilt_or_droop", "yellowing_or_browning", "overall_visual_state")
    )
    soil_text = str(parsed_visual.get("soil_surface") or "")
    tube_text = str(parsed_visual.get("pot_probe_tube_visible") or "")
    tags: set[str] = set()
    if text_has(leaf_text, [r"黄", r"褐", r"棕", r"枯黄", r"发黄", r"browning", r"yellow"]):
        tags.add("yellowing_or_browning")
    if text_has(leaf_text, [r"下垂", r"萎蔫", r"蔫", r"软塌", r"droop", r"wilt"]):
        tags.add("wilt_or_droop")
    if text_has(leaf_text, [r"严重萎蔫", r"明显萎蔫", r"严重下垂", r"大面积下垂"]):
        tags.add("severe_wilt")
    soil_negates_wet = text_has(soil_text, [r"无积水", r"未见积水", r"没有积水", r"无明显积水"])
    soil_mentions_wet = text_has(soil_text, [r"湿润", r"潮湿", r"积水", r"水渍", r"泥泞"]) and not soil_negates_wet
    soil_negates_dry = text_has(soil_text, [r"无.*干裂", r"未见.*干裂", r"没有.*干裂"])
    if text_has(soil_text, [r"土壤表面干", r"表面干燥", r"土面干", r"干裂", r"裸露.*干"]) and not soil_negates_dry and not soil_mentions_wet:
        tags.add("soil_surface_dry")
    if soil_mentions_wet:
        tags.add("soil_surface_wet")
    if text_has(tube_text, [r"探针", r"传感器", r"水管", r"滴灌管", r"细线"]):
        tags.add("probe_or_tube_visible")
    return tags


def recent_irrigation_hours(status: dict[str, Any]) -> float | None:
    recent = status.get("recent_irrigation") or {}
    age = recent.get("age_sec")
    if isinstance(age, (int, float)) and age >= 0:
        return age / 3600.0
    return None


def rule_matches(rule: dict[str, Any], facts: dict[str, Any]) -> bool:
    when = rule.get("when") or {}
    if "humidity_relation" in when and facts["humidity_relation"] not in set(when["humidity_relation"]):
        return False
    if "light_relation" in when and facts["light_relation"] not in set(when["light_relation"]):
        return False
    if "visual_available" in when and bool(facts["visual_available"]) != bool(when["visual_available"]):
        return False

    required_tags = set(when.get("visual_tags") or [])
    if required_tags and not required_tags.issubset(facts["visual_tags"]):
        return False
    excluded_tags = set(when.get("exclude_visual_tags") or [])
    if excluded_tags and excluded_tags.intersection(facts["visual_tags"]):
        return False

    required_flags = set(when.get("flags") or [])
    if required_flags and not required_flags.issubset(facts["flags"]):
        return False

    if "recent_irrigation_within_hours" in when:
        hours = facts.get("recent_irrigation_hours")
        if not isinstance(hours, (int, float)) or hours > float(when["recent_irrigation_within_hours"]):
            return False
    return True


def build_facts(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status") or {}
    visual = payload.get("visual") or {}
    parsed_visual = visual.get("parsed") if isinstance(visual.get("parsed"), dict) else None
    flags = {key for key, value in (status.get("flags") or {}).items() if value}
    return {
        "humidity_relation": derive_humidity_relation(status),
        "light_relation": derive_light_relation(status),
        "visual_available": bool(parsed_visual),
        "visual_tags": derive_visual_tags(parsed_visual),
        "flags": flags,
        "recent_irrigation_hours": recent_irrigation_hours(status),
    }


def match_knowledge(payload: dict[str, Any], knowledge: dict[str, Any], limit: int) -> dict[str, Any]:
    facts = build_facts(payload)
    matches = []
    for rule in knowledge.get("rules", []):
        if rule_matches(rule, facts):
            matches.append(
                {
                    "id": rule.get("id"),
                    "title": rule.get("title"),
                    "priority": rule.get("priority", 0),
                    "explanation": rule.get("explanation"),
                    "advice": rule.get("advice") or [],
                    "control_policy": rule.get("control_policy", "explain_only"),
                }
            )
    matches.sort(key=lambda item: item.get("priority", 0), reverse=True)
    return {
        "ok": True,
        "source": "openclaw_plant_care_knowledge",
        "knowledge_version": knowledge.get("version"),
        "scope": knowledge.get("scope"),
        "facts": {
            **facts,
            "visual_tags": sorted(facts["visual_tags"]),
            "flags": sorted(facts["flags"]),
        },
        "matches": matches[:limit],
        "control_boundary": knowledge.get("control_boundary"),
        "sources": knowledge.get("sources", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Match generic indoor plant-care knowledge against Plant Talk context.")
    parser.add_argument("--input", default="-", help="Context JSON path, or '-' for stdin")
    parser.add_argument("--knowledge", default=str(DEFAULT_KNOWLEDGE))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    payload = load_payload(args.input)
    knowledge = load_knowledge(Path(args.knowledge))
    result = match_knowledge(payload, knowledge, args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
