#!/usr/bin/env python3
"""Validated, idempotent storage for QQBot4 manual-watering records."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from plant_state_builder import run_gsql


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEVICE_CODE = "soil2"
EVENT_TYPE = "manual_watering"
SOURCE = "qqbot"
MAX_NOTE_LENGTH = 200
MAX_QQ_MESSAGE_ID_LENGTH = 256
COMMAND_PATTERN = re.compile(
    r"^记录(?:\s+(?P<device>soil\d+))?\s+人工浇水\s+时间=(?P<occurred_at>\S+)"
    r"(?:\s+时长=(?P<duration>\S+))?"
    r"(?:\s+水量=(?P<volume>\S+))?"
    r"(?:\s+备注=(?P<note>.*))?\s*$",
    re.IGNORECASE,
)
POSITIVE_NUMBER = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")


class ValidationError(ValueError):
    """Raised when untrusted QQ content is outside the strict record contract."""


@dataclass(frozen=True)
class ManualWateringEvent:
    device_code: str
    event_type: str
    occurred_at: str
    duration_sec: Optional[float]
    volume_ml: Optional[float]
    note: Optional[str]

    def payload(self) -> Dict[str, Any]:
        return asdict(self)


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def _decimal_value(value: str, unit: str, label: str) -> float:
    normalized = value[:-len(unit)] if value.lower().endswith(unit) else value
    if not normalized or not POSITIVE_NUMBER.fullmatch(normalized):
        raise ValidationError(f"{label}必须是正数{unit}")
    try:
        number = Decimal(normalized)
    except InvalidOperation as error:
        raise ValidationError(f"{label}必须是正数{unit}") from error
    if number <= 0 or not number.is_finite():
        raise ValidationError(f"{label}必须是正数{unit}")
    return float(number)


def _validate_note(note: Optional[str]) -> Optional[str]:
    if note is None:
        return None
    value = note.strip()
    if not value:
        raise ValidationError("备注不能为空")
    if len(value) > MAX_NOTE_LENGTH:
        raise ValidationError(f"备注不能超过{MAX_NOTE_LENGTH}字")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValidationError("备注不能包含控制字符")
    return value


def _parse_occurred_at(value: str, now: datetime) -> str:
    if value == "现在":
        return now.astimezone(SHANGHAI).isoformat()
    if not value.endswith("+08:00"):
        raise ValidationError("时间仅支持“现在”或带+08:00的ISO 8601时间")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValidationError("时间格式无效") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("时间必须带+08:00时区")
    if parsed.utcoffset().total_seconds() != 8 * 60 * 60:
        raise ValidationError("时间必须使用+08:00时区")
    return parsed.astimezone(SHANGHAI).isoformat()


def parse_manual_watering_command(text: str, now: Optional[datetime] = None) -> ManualWateringEvent:
    """Parse only the documented record command; no LLM interpretation is involved."""
    if not isinstance(text, str):
        raise ValidationError("记录命令必须是文本")
    match = COMMAND_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ValidationError("记录格式不正确")

    device = (match.group("device") or DEVICE_CODE).lower()
    if device != DEVICE_CODE:
        raise ValidationError("仅支持记录soil2人工浇水")

    reference_time = now or now_shanghai()
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=SHANGHAI)
    duration = match.group("duration")
    volume = match.group("volume")
    return ManualWateringEvent(
        device_code=DEVICE_CODE,
        event_type=EVENT_TYPE,
        occurred_at=_parse_occurred_at(match.group("occurred_at"), reference_time),
        duration_sec=_decimal_value(duration, "秒", "时长") if duration else None,
        volume_ml=_decimal_value(volume, "ml", "水量") if volume else None,
        note=_validate_note(match.group("note")),
    )


def _validated_identity(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError(f"{label}无效")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValidationError(f"{label}无效")
    return value


def event_from_payload(payload: Dict[str, Any]) -> ManualWateringEvent:
    if not isinstance(payload, dict):
        raise ValidationError("记录参数无效")
    return parse_manual_watering_command(
        "记录 soil2 人工浇水 时间=" + str(payload.get("occurred_at", ""))
        + (f" 时长={payload['duration_sec']}秒" if payload.get("duration_sec") is not None else "")
        + (f" 水量={payload['volume_ml']}ml" if payload.get("volume_ml") is not None else "")
        + (f" 备注={payload['note']}" if payload.get("note") is not None else ""),
        now=now_shanghai(),
    )


def sql_literal(value: str) -> str:
    """Safely emit a SQL scalar after boundary validation; identifiers are never user supplied."""
    if "\x00" in value:
        raise ValidationError("文本包含非法字符")
    return "'" + value.replace("'", "''") + "'"


def record_manual_watering(
    event: ManualWateringEvent,
    *,
    source_message_id: str,
    operator_id: str,
    operator_name: Optional[str],
    query: Callable[[str], list] = run_gsql,
) -> Dict[str, Any]:
    """Insert once by QQ source_message_id and return the stable audit result."""
    message_id = _validated_identity(source_message_id, "消息ID", MAX_QQ_MESSAGE_ID_LENGTH)
    actor_id = _validated_identity(operator_id, "操作者", 128)
    actor_name = None if operator_name is None else _validated_identity(operator_name, "操作者名称", 200)
    values = event.payload()
    occurred_at = _parse_occurred_at(values["occurred_at"], now_shanghai())
    duration = values["duration_sec"]
    volume = values["volume_ml"]
    note = values["note"]

    insert_sql = """
        INSERT INTO human_events (
            device_code, event_type, occurred_at, duration_sec, volume_ml, note,
            source, operator_id, operator_name, source_message_id
        ) VALUES (
            {device_code}, {event_type}, {occurred_at}, {duration_sec}, {volume_ml}, {note},
            'qqbot', {operator_id}, {operator_name}, {source_message_id}
        ) ON CONFLICT (source_message_id) DO NOTHING
        RETURNING id
    """.format(
        device_code=sql_literal(DEVICE_CODE),
        event_type=sql_literal(EVENT_TYPE),
        occurred_at=sql_literal(occurred_at),
        duration_sec=str(duration) if duration is not None else "NULL",
        volume_ml=str(volume) if volume is not None else "NULL",
        note=sql_literal(note) if note is not None else "NULL",
        operator_id=sql_literal(actor_id),
        operator_name=sql_literal(actor_name) if actor_name is not None else "NULL",
        source_message_id=sql_literal(message_id),
    )
    rows = query(insert_sql)
    if rows and rows[0] and rows[0][0]:
        return {"id": int(rows[0][0]), "created": True, "event": values}

    existing_rows = query(
        "SELECT id FROM human_events WHERE source_message_id = "
        + sql_literal(message_id)
        + " LIMIT 1"
    )
    if not existing_rows or not existing_rows[0] or not existing_rows[0][0]:
        raise RuntimeError("人工浇水记录写入未确认")
    return {"id": int(existing_rows[0][0]), "created": False, "event": values}


def preview_text(event: ManualWateringEvent) -> str:
    fields = [
        "人工浇水记录预览（尚未写入）",
        f"设备：{event.device_code}",
        f"发生时间：{event.occurred_at}",
        f"时长：{event.duration_sec:g} 秒" if event.duration_sec is not None else "时长：未提供",
        f"水量：{event.volume_ml:g} ml" if event.volume_ml is not None else "水量：未提供",
        f"备注：{event.note}" if event.note else "备注：未提供",
        "请在 10 分钟内回复“确认记录”写入；回复“取消”可放弃。",
    ]
    return "\n".join(fields)


def success_text(result: Dict[str, Any]) -> str:
    event = result["event"]
    status = "已记录" if result["created"] else "该消息已记录，无重复写入"
    return "\n".join([
        f"人工浇水{status}",
        f"设备：{event['device_code']}",
        f"发生时间：{event['occurred_at']}",
        f"记录编号：{result['id']}",
    ])


def _emit(payload: Dict[str, Any]) -> None:
    print(json.dumps({"schema_version": 1, **payload}, ensure_ascii=False, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser(description="人工浇水记录验证工具")
    parser.add_argument("--text", required=True)
    args = parser.parse_args()
    try:
        event = parse_manual_watering_command(args.text)
        _emit({"ok": True, "event": event.payload(), "text": preview_text(event)})
    except ValidationError:
        _emit({"ok": False, "text": "记录格式不正确。请使用：记录 soil2 人工浇水 时间=现在 时长=30秒 水量=100ml 备注=手浇"})


if __name__ == "__main__":
    main()
