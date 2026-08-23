#!/usr/bin/env python3
"""Strict parsing and canonical payloads for confirmed manual checks."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEVICE_CODE = "soil2"
MAX_NOTE_LENGTH = 200
MAX_EVIDENCE_LENGTH = 2000
CHECK_RESULT_NO_ISSUE = "no_issue"
CHECK_RESULT_ISSUE_FOUND = "issue_found"
CHECK_RESULT_NOT_COMPLETED = "not_completed"

RESULT_LABELS = {
    "未发现异常": CHECK_RESULT_NO_ISSUE,
    "发现异常": CHECK_RESULT_ISSUE_FOUND,
    "无法完成检查": CHECK_RESULT_NOT_COMPLETED,
}
RESULT_TEXT = {value: key for key, value in RESULT_LABELS.items()}
CHECK_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
COMMAND_PATTERN = re.compile(
    r"^记录(?:\s+(?P<device>soil\d+))?\s+人工检查\s+"
    r"检查项=(?P<check_code>[A-Za-z0-9_]+)\s+"
    r"结果=(?P<result>未发现异常|发现异常|无法完成检查)"
    r"(?:\s+时间=(?P<occurred_at>\S+))?"
    r"(?:\s+备注=(?P<note>.*))?\s*$",
    re.IGNORECASE,
)


class ValidationError(ValueError):
    """Raised when an untrusted manual-check message is outside its contract."""


@dataclass(frozen=True)
class ManualCheckEvent:
    device_code: str
    check_code: str
    result: str
    note: Optional[str]
    occurred_at: str
    check_source: Optional[str] = None
    check_evidence: Optional[str] = None
    check_generated_at: Optional[str] = None

    def payload(self) -> Dict[str, Any]:
        return asdict(self)

    def with_snapshot(self, check: Dict[str, Any], generated_at: str) -> "ManualCheckEvent":
        return replace(
            self,
            check_source=_validate_snapshot_source(check.get("source")),
            check_evidence=_validate_snapshot_evidence(check.get("evidence")),
            check_generated_at=_parse_shanghai_datetime(generated_at, "检查清单生成时间"),
        )


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validate_note(note: Optional[str]) -> Optional[str]:
    if note is None:
        return None
    value = note.strip()
    if not value:
        raise ValidationError("备注不能为空")
    if len(value) > MAX_NOTE_LENGTH or _has_control_characters(value):
        raise ValidationError("备注格式无效")
    return value


def _validate_check_code(value: Any) -> str:
    if not isinstance(value, str) or not CHECK_CODE_PATTERN.fullmatch(value):
        raise ValidationError("检查项无效")
    return value


def _validate_result(value: Any) -> str:
    if value not in RESULT_TEXT:
        raise ValidationError("检查结果无效")
    return str(value)


def _parse_shanghai_datetime(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("+08:00"):
        raise ValidationError(f"{label}必须使用 +08:00 时间")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValidationError(f"{label}无效") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 8 * 3600:
        raise ValidationError(f"{label}必须使用 +08:00 时间")
    return parsed.astimezone(SHANGHAI).isoformat()


def _validate_snapshot_source(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64 or _has_control_characters(value):
        raise ValidationError("检查项来源无效")
    return value


def _validate_snapshot_evidence(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_EVIDENCE_LENGTH or _has_control_characters(value):
        raise ValidationError("检查项依据无效")
    return value


def parse_manual_check_command(text: str, now: Optional[datetime] = None) -> ManualCheckEvent:
    """Parse only the documented check command; no LLM interpretation is involved."""
    if not isinstance(text, str):
        raise ValidationError("人工检查命令必须是文本")
    match = COMMAND_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ValidationError("人工检查格式不正确")
    device_code = (match.group("device") or DEVICE_CODE).lower()
    if device_code != DEVICE_CODE:
        raise ValidationError("仅支持 soil2 人工检查")
    reference_time = now or now_shanghai()
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=SHANGHAI)
    requested_time = match.group("occurred_at")
    if requested_time not in (None, "现在"):
        raise ValidationError("时间仅支持 时间=现在")
    result = RESULT_LABELS[match.group("result")]
    note = _validate_note(match.group("note"))
    if result in (CHECK_RESULT_ISSUE_FOUND, CHECK_RESULT_NOT_COMPLETED) and note is None:
        raise ValidationError("发现异常或无法完成检查时必须填写备注")
    return ManualCheckEvent(
        device_code=DEVICE_CODE,
        check_code=_validate_check_code(match.group("check_code").lower()),
        result=result,
        note=note,
        occurred_at=reference_time.astimezone(SHANGHAI).isoformat(),
    )


def event_from_payload(payload: Dict[str, Any]) -> ManualCheckEvent:
    """Rehydrate only a canonical, already-previewed payload for confirmation."""
    if not isinstance(payload, dict):
        raise ValidationError("人工检查参数无效")
    if payload.get("device_code") != DEVICE_CODE:
        raise ValidationError("仅支持 soil2 人工检查")
    result = _validate_result(payload.get("result"))
    note = _validate_note(payload.get("note"))
    if result in (CHECK_RESULT_ISSUE_FOUND, CHECK_RESULT_NOT_COMPLETED) and note is None:
        raise ValidationError("人工检查备注无效")
    return ManualCheckEvent(
        device_code=DEVICE_CODE,
        check_code=_validate_check_code(payload.get("check_code")),
        result=result,
        note=note,
        occurred_at=_parse_shanghai_datetime(payload.get("occurred_at"), "检查时间"),
        check_source=_validate_snapshot_source(payload.get("check_source")),
        check_evidence=_validate_snapshot_evidence(payload.get("check_evidence")),
        check_generated_at=_parse_shanghai_datetime(payload.get("check_generated_at"), "检查清单生成时间"),
    )


def canonical_payload(event: ManualCheckEvent) -> tuple[str, str]:
    encoded = json.dumps(event.payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def result_text(value: str) -> str:
    return RESULT_TEXT[_validate_result(value)]

