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
DEFAULT_MEMORY = ROOT / "memory" / "plant_daily_memory.jsonl"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_key() -> str:
    return datetime.now().astimezone().date().isoformat()


def load_payload(path: str) -> dict[str, Any]:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def write_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = sorted(records, key=lambda item: item.get("date", ""))
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n", encoding="utf-8")


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def strip_visual_overreach(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    patterns = [
        r"[^，,。；;]*喊渴[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*口渴[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*想喝[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*要水[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*灌水[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*需要额外照顾[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*不需要额外照顾[^，,。；;]*[，,。；;]?",
        r"[^，,。；;]*是否浇水[^，,。；;]*[，,。；;]?",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text)
    return re.sub(r"\s+", " ", text).strip().rstrip("，,。；; ")


def context_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status") or {}
    visual = payload.get("visual") or {}
    combined = payload.get("combined") or {}
    parsed = visual.get("parsed") if isinstance(visual.get("parsed"), dict) else {}
    sensor = status.get("sensor") or {}
    recent_irrigation = status.get("recent_watering_activity") or status.get("recent_irrigation") or {}
    device_code = str(status.get("device_code") or ((payload.get("scope") or {}).get("default_device")) or "soil3")
    humidity = number(sensor.get("humidity"))
    visual_summary = strip_visual_overreach(parsed.get("user_friendly_summary") or parsed.get("visible_leaf_state") or "")
    visual_leaf = parsed.get("visible_leaf_state") or ""
    visual_soil = parsed.get("soil_surface") or ""
    diary = build_diary_sentence(humidity, status, parsed, combined)
    return {
        "device_code": device_code,
        "date": today_key(),
        "first_seen_at": now_iso(),
        "last_seen_at": now_iso(),
        "observations": 1,
        "humidity_min": humidity,
        "humidity_max": humidity,
        "humidity_latest": humidity,
        "drink_line": number((status.get("thresholds") or {}).get("drink_line")),
        "trend_label_latest": (status.get("trend") or {}).get("label"),
        "risk_latest": combined.get("risk_level") or status.get("risk_level"),
        "recommended_action_latest": combined.get("recommended_action") or status.get("recommended_action"),
        "visual_risk_latest": parsed.get("risk_level"),
        "visual_leaf_latest": strip_visual_overreach(visual_leaf),
        "visual_soil_latest": strip_visual_overreach(visual_soil),
        "visual_summary_latest": visual_summary,
        "recent_irrigation_latest": {
            "time": recent_irrigation.get("time"),
            "water_sec": recent_irrigation.get("water_sec"),
            "source": recent_irrigation.get("source"),
            "source_kind": recent_irrigation.get("source_kind"),
            "source_label": recent_irrigation.get("source_label"),
            "status": recent_irrigation.get("status"),
        },
        "care_knowledge_ids_latest": [
            item.get("id")
            for item in ((payload.get("care_knowledge") or {}).get("matches") or [])
            if item.get("id")
        ],
        "diary_latest": diary,
    }


def build_diary_sentence(
    humidity: float | None,
    status: dict[str, Any],
    parsed: dict[str, Any],
    combined: dict[str, Any],
) -> str:
    risk = combined.get("risk_level") or status.get("risk_level")
    leaf = parsed.get("visible_leaf_state")
    if humidity is None:
        first = "我今天的数据有点没拿稳"
    else:
        first = f"我今天根部附近湿度约 {humidity:.1f}%"
    if leaf:
        first += f"，画面里{leaf}"
    if risk == "low":
        return first + "，整体先稳稳观察。"
    if risk == "medium":
        return first + "，有点小状况，先看趋势别急着折腾。"
    if risk == "high":
        return first + "，风险偏高，建议你现场看一眼。"
    return first + "，继续记录变化。"


def merge_record(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(old)
    merged["last_seen_at"] = new["last_seen_at"]
    merged["observations"] = int(old.get("observations") or 0) + 1
    for key in ("humidity_min", "humidity_max"):
        old_value = number(old.get(key))
        new_value = number(new.get(key))
        if old_value is None:
            merged[key] = new_value
        elif new_value is None:
            merged[key] = old_value
        elif key.endswith("min"):
            merged[key] = min(old_value, new_value)
        else:
            merged[key] = max(old_value, new_value)
    for key, value in new.items():
        if key in {"device_code", "date", "first_seen_at", "observations", "humidity_min", "humidity_max"}:
            continue
        if key.startswith("visual_") and (value is None or value == ""):
            continue
        merged[key] = value
    return merged


def upsert_daily(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    records = read_records(path)
    snapshot = context_snapshot(payload)
    replaced = False
    merged_snapshot = snapshot
    updated_records: list[dict[str, Any]] = []
    for record in records:
        old_device = record.get("device_code") or "soil3"
        if old_device == snapshot["device_code"] and record.get("date") == snapshot["date"]:
            merged_snapshot = merge_record(record, snapshot)
            updated_records.append(merged_snapshot)
            replaced = True
        else:
            updated_records.append(record)
    if not replaced:
        updated_records.append(snapshot)
    write_records(path, updated_records[-120:])
    return merged_snapshot


def trend(records: list[dict[str, Any]], limit: int, device_code: str) -> dict[str, Any]:
    matching = [
        item
        for item in records
        if (item.get("device_code") or "soil3") == device_code
    ]
    recent = sorted(matching, key=lambda item: item.get("date", ""))[-limit:]
    points = []
    if len(recent) >= 2:
        prev = recent[-2]
        cur = recent[-1]
        gap_days = (
            datetime.fromisoformat(str(cur.get("date"))).date()
            - datetime.fromisoformat(str(prev.get("date"))).date()
        ).days
        comparison_prefix = "和昨天相比" if gap_days == 1 else f"和上次有记录的 {prev.get('date')} 相比"
        prev_h = number(prev.get("humidity_latest"))
        cur_h = number(cur.get("humidity_latest"))
        if prev_h is not None and cur_h is not None:
            delta = cur_h - prev_h
            if abs(delta) < 1.0:
                points.append(f"{comparison_prefix}，根部附近水分变化不大，属于稳定波动。")
            elif delta > 0:
                points.append(f"{comparison_prefix}，根部附近水分高了约 {delta:.1f} 个点，库存更足。")
            else:
                points.append(f"{comparison_prefix}，根部附近水分低了约 {abs(delta):.1f} 个点，正在慢慢消耗。")
        if cur.get("risk_latest") == prev.get("risk_latest"):
            points.append(f"整体风险还是{risk_text(cur.get('risk_latest'))}，没有明显升级。")
        else:
            points.append(f"整体风险从{risk_text(prev.get('risk_latest'))}变为{risk_text(cur.get('risk_latest'))}。")
        prev_leaf = str(prev.get("visual_leaf_latest") or "").strip()
        cur_leaf = str(cur.get("visual_leaf_latest") or "").strip()
        if prev_leaf and cur_leaf and prev_leaf == cur_leaf:
            points.append("画面里的叶片描述和上次接近，没有明显外观变化。")
    elif recent:
        points.append("我刚开始写长期日记，还需要多攒几天才能更会对比。")
    else:
        points.append("还没有长期日记。")

    if recent:
        latest = recent[-1]
        diary_sentence = latest.get("diary_latest") or "今天继续观察。"
        memory_sentence = diary_sentence
        if len(recent) >= 2:
            memory_sentence = diary_sentence.rstrip("。；; ") + "。" + points[0]
    else:
        memory_sentence = "我还没攒够自己的日记，先按当前数据说话。"
    return {
        "recent_days": recent,
        "trend_points": points[:3],
        "first_person_memory_sentence": memory_sentence,
        "comparison_ready": len(recent) >= 2,
        "stable_comparison_ready": len(recent) >= 3,
        "comparison_basis": "previous_recorded_day",
    }


def risk_text(value: Any) -> str:
    return {"low": "低", "medium": "中等", "high": "高"}.get(str(value), "未知")


def main() -> None:
    parser = argparse.ArgumentParser(description="Maintain daily memory for Plant Talk context.")
    parser.add_argument("--input", default="-", help="Context JSON path, or '-' for stdin")
    parser.add_argument("--memory", default=str(DEFAULT_MEMORY))
    parser.add_argument("--limit", type=int, default=7)
    parser.add_argument("--upsert", action="store_true", help="Upsert today's snapshot before returning trends")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    path = Path(args.memory)
    payload = load_payload(args.input)
    status = payload.get("status") or {}
    device_code = str(status.get("device_code") or ((payload.get("scope") or {}).get("default_device")) or "soil3")
    upserted = None
    if args.upsert:
        upserted = upsert_daily(path, payload)
    records = read_records(path)
    payload_out = {
        "ok": True,
        "source": "openclaw_plant_daily_memory",
        "device_code": device_code,
        "memory_path": str(path),
        "upserted": upserted,
        **trend(records, args.limit, device_code),
        "control_boundary": "Daily memory explains trends only. It must not open pumps or override auto-care.",
    }
    print(json.dumps(payload_out, ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
