#!/usr/bin/env python3
"""Persistent, identity-bound confirmation state machine for manual checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from analytics.alert_evaluator import build_checklist
from services.database_access import run_human_event_writer_sql
from services.manual_check_service import (
    DEVICE_CODE,
    ManualCheckEvent,
    ValidationError,
    canonical_payload,
    event_from_payload,
    parse_manual_check_command,
    result_text,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
PENDING_TTL = timedelta(minutes=10)
MAX_CONVERSATION_LENGTH = 256
MAX_QQ_MESSAGE_ID_LENGTH = 256


class ConfirmationState(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PendingAlreadyExists(RuntimeError):
    """A different manual check is already awaiting this sender's confirmation."""


@dataclass(frozen=True)
class RecordContext:
    channel: str
    agent_id: str
    conversation_id: str
    sender_id: str
    sender_name: Optional[str]
    source_message_id: str


@dataclass(frozen=True)
class PendingResult:
    pending_id: int
    status: ConfirmationState
    expires_at: Optional[str]
    manual_check_event_id: Optional[int]
    payload_hash: str


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def _validate_identity(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label}无效")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{label}无效")
    return value


def validate_context(context: RecordContext) -> RecordContext:
    channel = _validate_identity(context.channel, "渠道", 32)
    agent_id = _validate_identity(context.agent_id, "Agent", 64)
    if channel != "qqbot" or agent_id != "qqbot4":
        raise ValueError("人工检查仅支持 QQBot4 QQ 渠道")
    conversation_id = _validate_identity(context.conversation_id, "会话", MAX_CONVERSATION_LENGTH)
    sender_id = _validate_identity(context.sender_id, "发送者", 128)
    source_message_id = _validate_identity(context.source_message_id, "消息 ID", MAX_QQ_MESSAGE_ID_LENGTH)
    sender_name = None
    if context.sender_name is not None:
        sender_name = _validate_identity(context.sender_name, "发送者名称", 200)
    return RecordContext(channel, agent_id, conversation_id, sender_id, sender_name, source_message_id)


def owner_hash(context: RecordContext) -> str:
    identity = "\x00".join((context.channel, context.agent_id, context.conversation_id, context.sender_id))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def sql_literal(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("SQL 文本无效")
    return "'" + value.replace("'", "''") + "'"


class GsqlManualCheckStore:
    """Storage adapter for fixed internal SQL only; no request controls identifiers."""

    columns = (
        "id, status, expires_at, manual_check_event_id, payload_hash, event_payload_json, "
        "source_message_id, confirmation_message_id"
    )

    @staticmethod
    def _record(rows: List[List[str]]) -> Optional[Dict[str, Any]]:
        if not rows or len(rows[0]) < 8:
            return None
        row = rows[0]
        return {
            "id": int(row[0]), "status": row[1], "expires_at": row[2] or None,
            "manual_check_event_id": int(row[3]) if row[3] else None,
            "payload_hash": row[4], "event_payload_json": row[5],
            "source_message_id": row[6], "confirmation_message_id": row[7] or None,
        }

    def _find(self, where_sql: str) -> Optional[Dict[str, Any]]:
        rows = run_human_event_writer_sql(
            f"SELECT {self.columns} FROM pending_manual_check_events WHERE {where_sql} ORDER BY id DESC LIMIT 1"
        )
        return self._record(rows)

    def find_active(self, context: RecordContext, active_hash: str) -> Optional[Dict[str, Any]]:
        return self._find(
            "active_owner_hash = " + sql_literal(active_hash)
            + " AND channel = " + sql_literal(context.channel)
            + " AND agent_id = " + sql_literal(context.agent_id)
            + " AND conversation_id = " + sql_literal(context.conversation_id)
            + " AND sender_id = " + sql_literal(context.sender_id)
        )

    def find_by_confirmation(self, context: RecordContext, confirmation_message_id: str) -> Optional[Dict[str, Any]]:
        return self._find(
            "confirmation_message_id = " + sql_literal(confirmation_message_id)
            + " AND channel = " + sql_literal(context.channel)
            + " AND agent_id = " + sql_literal(context.agent_id)
            + " AND conversation_id = " + sql_literal(context.conversation_id)
            + " AND sender_id = " + sql_literal(context.sender_id)
        )

    def find_latest_confirmed(self, context: RecordContext) -> Optional[Dict[str, Any]]:
        return self._find(
            "status = 'confirmed'"
            + " AND channel = " + sql_literal(context.channel)
            + " AND agent_id = " + sql_literal(context.agent_id)
            + " AND conversation_id = " + sql_literal(context.conversation_id)
            + " AND sender_id = " + sql_literal(context.sender_id)
        )

    def preview(self, *, context: RecordContext, event: ManualCheckEvent, payload_json: str, payload_hash: str, owner_hash: str, expires_at: datetime) -> Optional[Dict[str, Any]]:
        sql = """
BEGIN;
LOCK TABLE pending_manual_check_events IN SHARE ROW EXCLUSIVE MODE;
UPDATE pending_manual_check_events
SET status = 'expired', active_owner_hash = NULL
WHERE active_owner_hash = {owner_hash}
  AND status = 'pending'
  AND expires_at <= CURRENT_TIMESTAMP;
INSERT INTO pending_manual_check_events (
  channel, agent_id, conversation_id, sender_id, sender_name, source_message_id,
  event_payload_json, payload_hash, active_owner_hash, status, expires_at
)
SELECT {channel}, {agent_id}, {conversation_id}, {sender_id}, {sender_name}, {source_message_id},
       {payload_json}, {payload_hash}, {owner_hash}, 'pending', {expires_at}
WHERE NOT EXISTS (SELECT 1 FROM pending_manual_check_events WHERE source_message_id = {source_message_id})
  AND NOT EXISTS (SELECT 1 FROM pending_manual_check_events WHERE active_owner_hash = {owner_hash});
COMMIT;
""".format(
            channel=sql_literal(context.channel), agent_id=sql_literal(context.agent_id),
            conversation_id=sql_literal(context.conversation_id), sender_id=sql_literal(context.sender_id),
            sender_name=sql_literal(context.sender_name) if context.sender_name else "NULL",
            source_message_id=sql_literal(context.source_message_id), payload_json=sql_literal(payload_json),
            payload_hash=sql_literal(payload_hash), owner_hash=sql_literal(owner_hash),
            expires_at=sql_literal(expires_at.astimezone(SHANGHAI).isoformat()),
        )
        run_human_event_writer_sql(sql)
        return self._find("source_message_id = " + sql_literal(context.source_message_id)) or self.find_active(context, owner_hash)

    def confirm(self, *, record: Dict[str, Any], context: RecordContext, confirmation_message_id: str, event: ManualCheckEvent, owner_hash: str, now: datetime) -> Optional[Dict[str, Any]]:
        values = event.payload()
        sql = """
BEGIN;
LOCK TABLE pending_manual_check_events IN SHARE ROW EXCLUSIVE MODE;
UPDATE pending_manual_check_events
SET status = 'expired', active_owner_hash = NULL
WHERE id = {pending_id} AND status = 'pending' AND expires_at <= CURRENT_TIMESTAMP;
INSERT INTO manual_check_events (
  device_code, check_code, check_source, check_evidence, check_generated_at,
  result, note, occurred_at, source, operator_id, operator_name,
  source_message_id, confirmation_status, trust_status
)
SELECT {device_code}, {check_code}, {check_source}, {check_evidence}, {check_generated_at},
       {result}, {note}, {occurred_at}, 'qqbot', {operator_id}, {operator_name},
       {source_message_id}, 'confirmed', 'attested'
WHERE EXISTS (
  SELECT 1 FROM pending_manual_check_events
  WHERE id = {pending_id} AND status = 'pending' AND expires_at > CURRENT_TIMESTAMP
    AND payload_hash = {payload_hash} AND active_owner_hash = {owner_hash}
)
AND NOT EXISTS (
  SELECT 1 FROM manual_check_events WHERE source_message_id = {source_message_id}
);
UPDATE pending_manual_check_events
SET status = 'confirmed', active_owner_hash = NULL, confirmed_at = CURRENT_TIMESTAMP,
    confirmation_message_id = {confirmation_message_id},
    manual_check_event_id = (SELECT id FROM manual_check_events WHERE source_message_id = {source_message_id})
WHERE id = {pending_id} AND status = 'pending' AND expires_at > CURRENT_TIMESTAMP
  AND payload_hash = {payload_hash} AND active_owner_hash = {owner_hash};
COMMIT;
""".format(
            pending_id=int(record["id"]), payload_hash=sql_literal(record["payload_hash"]),
            owner_hash=sql_literal(owner_hash), device_code=sql_literal(DEVICE_CODE),
            check_code=sql_literal(values["check_code"]), check_source=sql_literal(values["check_source"]),
            check_evidence=sql_literal(values["check_evidence"]), check_generated_at=sql_literal(values["check_generated_at"]),
            result=sql_literal(values["result"]), note=sql_literal(values["note"]) if values["note"] is not None else "NULL",
            occurred_at=sql_literal(values["occurred_at"]), operator_id=sql_literal(context.sender_id),
            operator_name=sql_literal(context.sender_name) if context.sender_name else "NULL",
            source_message_id=sql_literal(record["source_message_id"]),
            confirmation_message_id=sql_literal(confirmation_message_id),
        )
        run_human_event_writer_sql(sql)
        return self._find("id = " + str(int(record["id"])))

    def cancel(self, *, record: Dict[str, Any], context: RecordContext, owner_hash: str, now: datetime) -> Optional[Dict[str, Any]]:
        sql = """
BEGIN;
LOCK TABLE pending_manual_check_events IN SHARE ROW EXCLUSIVE MODE;
UPDATE pending_manual_check_events
SET status = CASE WHEN expires_at <= CURRENT_TIMESTAMP THEN 'expired' ELSE 'cancelled' END,
    active_owner_hash = NULL
WHERE id = {pending_id} AND status = 'pending';
COMMIT;
""".format(pending_id=int(record["id"]))
        run_human_event_writer_sql(sql)
        return self._find("id = " + str(int(record["id"])))

    def status(self, *, record: Dict[str, Any], context: RecordContext, owner_hash: str, now: datetime) -> Optional[Dict[str, Any]]:
        run_human_event_writer_sql(
            "BEGIN; UPDATE pending_manual_check_events SET status = 'expired', active_owner_hash = NULL "
            "WHERE id = " + str(int(record["id"])) + " AND status = 'pending' AND expires_at <= CURRENT_TIMESTAMP; COMMIT;"
        )
        return self._find("id = " + str(int(record["id"])))


class ManualCheckGateway:
    def __init__(self, store: Any, now: Callable[[], datetime] = now_shanghai, checklist_builder: Callable[..., Dict[str, Any]] = build_checklist):
        self.store = store
        self.now = now
        self.checklist_builder = checklist_builder

    @staticmethod
    def _result(record: Dict[str, Any]) -> PendingResult:
        return PendingResult(
            pending_id=int(record["id"]), status=ConfirmationState(record["status"]),
            expires_at=str(record.get("expires_at")) if record.get("expires_at") is not None else None,
            manual_check_event_id=record.get("manual_check_event_id"), payload_hash=record["payload_hash"],
        )

    def _snapshot_event(self, event: ManualCheckEvent) -> ManualCheckEvent:
        checklist = self.checklist_builder(DEVICE_CODE, now=self.now())
        if not isinstance(checklist, dict) or checklist.get("device_code") != DEVICE_CODE:
            raise ValueError("检查清单不可用")
        checks = checklist.get("checks")
        if not isinstance(checks, list):
            raise ValueError("检查清单不可用")
        matched = next((item for item in checks if isinstance(item, dict) and item.get("code") == event.check_code), None)
        if matched is None:
            raise ValueError("检查项不在当前检查清单中")
        generated_at = checklist.get("generated_at")
        return event.with_snapshot(matched, generated_at)

    def preview(self, context: RecordContext, text: str) -> PendingResult:
        context = validate_context(context)
        event = self._snapshot_event(parse_manual_check_command(text, now=self.now()))
        payload_json, payload_hash = canonical_payload(event)
        record = self.store.preview(
            context=context, event=event, payload_json=payload_json, payload_hash=payload_hash,
            owner_hash=owner_hash(context), expires_at=self.now().astimezone(SHANGHAI) + PENDING_TTL,
        )
        if record is None or record.get("payload_hash") != payload_hash:
            raise PendingAlreadyExists("已有待确认的人工检查，请先确认或取消")
        return self._result(record)

    def confirm(self, context: RecordContext, confirmation_message_id: str) -> Optional[PendingResult]:
        context = validate_context(context)
        confirmation_message_id = _validate_identity(confirmation_message_id, "确认消息 ID", MAX_QQ_MESSAGE_ID_LENGTH)
        existing = self.store.find_by_confirmation(context, confirmation_message_id)
        if existing is not None:
            return self._result(existing)
        active_hash = owner_hash(context)
        record = self.store.find_active(context, active_hash)
        if record is None:
            existing = self.store.find_latest_confirmed(context)
            return self._result(existing) if existing is not None else None
        if record["status"] != ConfirmationState.PENDING.value:
            return self._result(record)
        event = event_from_payload(json.loads(record["event_payload_json"]))
        result = self.store.confirm(
            record=record, context=context, confirmation_message_id=confirmation_message_id,
            event=event, owner_hash=active_hash, now=self.now(),
        )
        return self._result(result) if result is not None else None

    def cancel(self, context: RecordContext) -> Optional[PendingResult]:
        context = validate_context(context)
        active_hash = owner_hash(context)
        record = self.store.find_active(context, active_hash)
        if record is None:
            return None
        result = self.store.cancel(record=record, context=context, owner_hash=active_hash, now=self.now())
        return self._result(result) if result is not None else None

    def status(self, context: RecordContext) -> Optional[PendingResult]:
        context = validate_context(context)
        active_hash = owner_hash(context)
        record = self.store.find_active(context, active_hash)
        if record is None:
            return None
        result = self.store.status(record=record, context=context, owner_hash=active_hash, now=self.now())
        return self._result(result) if result is not None else None


def _emit(ok: bool, text: str, result: Optional[PendingResult] = None, normalized_event: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"schema_version": 1, "ok": ok, "text": text}
    if result is not None:
        payload["pending"] = {
            "id": result.pending_id, "status": result.status.value, "expires_at": result.expires_at,
            "manual_check_event_id": result.manual_check_event_id,
        }
    if normalized_event is not None:
        payload["normalized_event"] = normalized_event
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _load_request(request_json: Optional[str], request_stdin: bool) -> Dict[str, Any]:
    if request_stdin == (request_json is not None):
        raise ValueError("provide exactly one request source")
    raw = sys.stdin.read() if request_stdin else request_json
    if not isinstance(raw, str) or not raw.strip() or "\n" in raw.strip():
        raise ValueError("request must be one JSON line")
    request = json.loads(raw)
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    return request


def main() -> None:
    # The generic CLI was the forged-context bypass. Only plant_event_ingress
    # may call this module's state-machine classes on the trusted host path.
    _emit(False, "人工检查记录服务仅接受可信 QQ 入站请求")
    return
    parser = argparse.ArgumentParser(description="内部人工检查确认状态机")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--request-json")
    source.add_argument("--request-stdin", action="store_true")
    args = parser.parse_args()
    try:
        request = _load_request(args.request_json, args.request_stdin)
        context = RecordContext(**request["context"])
        gateway = ManualCheckGateway(GsqlManualCheckStore())
        operation = request.get("operation")
        if operation == "preview":
            result = gateway.preview(context, request["text"])
            event = event_from_payload(json.loads(gateway.store._find("id = " + str(result.pending_id))["event_payload_json"]))
            _emit(True, "人工检查记录预览已保存，等待确认", result, event.payload())
        elif operation == "confirm":
            result = gateway.confirm(context, request["confirmation_message_id"])
            _emit(result is not None, "人工检查记录已确认" if result else "没有待确认的人工检查记录", result)
        elif operation == "cancel":
            result = gateway.cancel(context)
            _emit(result is not None, "人工检查记录已取消" if result else "没有待确认的人工检查记录", result)
        elif operation == "status":
            result = gateway.status(context)
            _emit(result is not None, "待确认人工检查状态" if result else "没有待确认的人工检查记录", result)
        else:
            raise ValueError("operation 无效")
    except (KeyError, TypeError, ValueError, ValidationError, PendingAlreadyExists, json.JSONDecodeError):
        _emit(False, "人工检查记录请求无效")
    except Exception:
        _emit(False, "人工检查反馈处理失败，未写入任何记录")


if __name__ == "__main__":
    main()
