#!/usr/bin/env python3
"""Persistent, identity-bound confirmation state machine for manual watering."""

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
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from services.database_access import run_human_event_writer_sql
from services.human_event_service import (
    DEVICE_CODE,
    EVENT_TYPE,
    ManualWateringEvent,
    ValidationError,
    event_from_payload,
    now_shanghai,
    parse_manual_watering_command,
    sql_literal,
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
    """A different record is already awaiting this sender's confirmation."""


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
    human_event_id: Optional[int]
    payload_hash: str


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
        raise ValueError("人工浇水记录仅支持QQBot4渠道")
    conversation_id = _validate_identity(context.conversation_id, "会话", MAX_CONVERSATION_LENGTH)
    sender_id = _validate_identity(context.sender_id, "发送者", 128)
    source_message_id = _validate_identity(context.source_message_id, "消息ID", MAX_QQ_MESSAGE_ID_LENGTH)
    sender_name = None
    if context.sender_name is not None:
        sender_name = _validate_identity(context.sender_name, "发送者名称", 200)
    return RecordContext(channel, agent_id, conversation_id, sender_id, sender_name, source_message_id)


def owner_hash(context: RecordContext) -> str:
    identity = "\x00".join((context.channel, context.agent_id, context.conversation_id, context.sender_id))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def canonical_payload(event: ManualWateringEvent) -> tuple[str, str]:
    payload = event.payload()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def run_gsql_transaction(sql: str) -> List[List[str]]:
    """Execute a fixed, internally generated transaction with the writer role."""
    return run_human_event_writer_sql(sql)


class GsqlPendingStore:
    """Storage adapter. SQL is generated from validated fields only."""

    columns = (
        "id, status, expires_at, human_event_id, payload_hash, event_payload_json, "
        "source_message_id, confirmation_message_id"
    )

    @staticmethod
    def _record(rows: List[List[str]]) -> Optional[Dict[str, Any]]:
        if not rows or len(rows[0]) < 8:
            return None
        row = rows[0]
        return {
            "id": int(row[0]), "status": row[1], "expires_at": row[2] or None,
            "human_event_id": int(row[3]) if row[3] else None, "payload_hash": row[4],
            "event_payload_json": row[5], "source_message_id": row[6],
            "confirmation_message_id": row[7] or None,
        }

    def _find(self, where_sql: str) -> Optional[Dict[str, Any]]:
        sql = f"SELECT {self.columns} FROM pending_human_events WHERE {where_sql} ORDER BY id DESC LIMIT 1"
        rows = run_human_event_writer_sql(sql)
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

    def preview(self, *, context: RecordContext, event: ManualWateringEvent, payload_json: str, payload_hash: str, owner_hash: str, expires_at: datetime) -> Dict[str, Any]:
        sql = """
BEGIN;
LOCK TABLE pending_human_events IN SHARE ROW EXCLUSIVE MODE;
UPDATE pending_human_events
SET status = 'expired', active_owner_hash = NULL
WHERE active_owner_hash = {owner_hash}
  AND status = 'pending'
  AND expires_at <= CURRENT_TIMESTAMP;
INSERT INTO pending_human_events (
  channel, agent_id, conversation_id, sender_id, sender_name, source_message_id,
  event_payload_json, payload_hash, active_owner_hash, status, expires_at
)
SELECT {channel}, {agent_id}, {conversation_id}, {sender_id}, {sender_name}, {source_message_id},
       {payload_json}, {payload_hash}, {owner_hash}, 'pending', {expires_at}
WHERE NOT EXISTS (SELECT 1 FROM pending_human_events WHERE source_message_id = {source_message_id})
  AND NOT EXISTS (SELECT 1 FROM pending_human_events WHERE active_owner_hash = {owner_hash});
COMMIT;
""".format(
            channel=sql_literal(context.channel), agent_id=sql_literal(context.agent_id),
            conversation_id=sql_literal(context.conversation_id), sender_id=sql_literal(context.sender_id),
            sender_name=sql_literal(context.sender_name) if context.sender_name else "NULL",
            source_message_id=sql_literal(context.source_message_id), payload_json=sql_literal(payload_json),
            payload_hash=sql_literal(payload_hash), owner_hash=sql_literal(owner_hash),
            expires_at=sql_literal(expires_at.astimezone(SHANGHAI).isoformat()),
        )
        run_gsql_transaction(sql)
        return self._find("source_message_id = " + sql_literal(context.source_message_id)) or self.find_active(context, owner_hash)

    def confirm(self, *, record: Dict[str, Any], context: RecordContext, confirmation_message_id: str, event: ManualWateringEvent, now: datetime, owner_hash: str) -> Dict[str, Any]:
        event_values = event.payload()
        sql = """
BEGIN;
LOCK TABLE pending_human_events IN SHARE ROW EXCLUSIVE MODE;
UPDATE pending_human_events
SET status = 'expired', active_owner_hash = NULL
WHERE id = {pending_id} AND status = 'pending' AND expires_at <= CURRENT_TIMESTAMP;
INSERT INTO human_events (
  device_code, event_type, occurred_at, duration_sec, volume_ml, note, source,
  operator_id, operator_name, source_message_id, confirmation_status, trust_status
)
SELECT {device_code}, {event_type}, {occurred_at}, {duration_sec}, {volume_ml}, {note}, 'qqbot',
       {operator_id}, {operator_name}, {source_message_id}, 'confirmed', 'attested'
WHERE EXISTS (
  SELECT 1 FROM pending_human_events
  WHERE id = {pending_id} AND status = 'pending' AND expires_at > CURRENT_TIMESTAMP
    AND payload_hash = {payload_hash} AND active_owner_hash = {owner_hash}
)
AND NOT EXISTS (
  SELECT 1 FROM human_events
  WHERE source_message_id = {source_message_id}
);
UPDATE pending_human_events
SET status = 'confirmed', active_owner_hash = NULL, confirmed_at = CURRENT_TIMESTAMP,
    confirmation_message_id = {confirmation_message_id},
    human_event_id = (SELECT id FROM human_events WHERE source_message_id = {source_message_id})
WHERE id = {pending_id} AND status = 'pending' AND expires_at > CURRENT_TIMESTAMP
  AND payload_hash = {payload_hash} AND active_owner_hash = {owner_hash};
COMMIT;
""".format(
            pending_id=int(record["id"]), payload_hash=sql_literal(record["payload_hash"]),
            owner_hash=sql_literal(owner_hash), device_code=sql_literal(DEVICE_CODE), event_type=sql_literal(EVENT_TYPE),
            occurred_at=sql_literal(event_values["occurred_at"]),
            duration_sec=str(event_values["duration_sec"]) if event_values["duration_sec"] is not None else "NULL",
            volume_ml=str(event_values["volume_ml"]) if event_values["volume_ml"] is not None else "NULL",
            note=sql_literal(event_values["note"]) if event_values["note"] is not None else "NULL",
            operator_id=sql_literal(context.sender_id),
            operator_name=sql_literal(context.sender_name) if context.sender_name else "NULL",
            source_message_id=sql_literal(record["source_message_id"]),
            confirmation_message_id=sql_literal(confirmation_message_id),
        )
        run_gsql_transaction(sql)
        return self._find("id = " + str(int(record["id"])))

    def cancel(self, *, record: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        sql = """
BEGIN;
LOCK TABLE pending_human_events IN SHARE ROW EXCLUSIVE MODE;
UPDATE pending_human_events
SET status = CASE WHEN expires_at <= CURRENT_TIMESTAMP THEN 'expired' ELSE 'cancelled' END,
    active_owner_hash = NULL
WHERE id = {pending_id} AND status = 'pending';
COMMIT;
""".format(pending_id=int(record["id"]))
        run_gsql_transaction(sql)
        return self._find("id = " + str(int(record["id"])))

    def status(self, *, record: Dict[str, Any]) -> Dict[str, Any]:
        run_gsql_transaction(
            "BEGIN; UPDATE pending_human_events SET status = 'expired', active_owner_hash = NULL "
            "WHERE id = " + str(int(record["id"])) + " AND status = 'pending' AND expires_at <= CURRENT_TIMESTAMP; COMMIT;"
        )
        return self._find("id = " + str(int(record["id"])))


class HumanRecordGateway:
    def __init__(self, store: Any, now=now_shanghai):
        self.store = store
        self.now = now

    @staticmethod
    def _result(record: Dict[str, Any]) -> PendingResult:
        return PendingResult(
            pending_id=int(record["id"]), status=ConfirmationState(record["status"]),
            expires_at=record.get("expires_at"), human_event_id=record.get("human_event_id"),
            payload_hash=record["payload_hash"],
        )

    def preview(self, context: RecordContext, text: str) -> PendingResult:
        context = validate_context(context)
        event = parse_manual_watering_command(text, now=self.now())
        payload_json, payload_hash = canonical_payload(event)
        record = self.store.preview(
            context=context, event=event, payload_json=payload_json, payload_hash=payload_hash,
            owner_hash=owner_hash(context), expires_at=self.now().astimezone(SHANGHAI) + PENDING_TTL,
        )
        if record["payload_hash"] != payload_hash:
            raise PendingAlreadyExists("已有待确认的人工浇水记录，请先确认或取消")
        return self._result(record)

    def confirm(self, context: RecordContext, confirmation_message_id: str) -> Optional[PendingResult]:
        context = validate_context(context)
        confirmation_message_id = _validate_identity(confirmation_message_id, "确认消息ID", MAX_QQ_MESSAGE_ID_LENGTH)
        existing = self.store.find_by_confirmation(context, confirmation_message_id) if hasattr(self.store, "find_by_confirmation") else None
        if existing is not None:
            return self._result(existing)
        active_hash = owner_hash(context)
        record = self.store.find_active(context, active_hash) if hasattr(self.store, "find_active") else None
        if record is None:
            existing = self.store.find_latest_confirmed(context) if hasattr(self.store, "find_latest_confirmed") else None
            return self._result(existing) if existing is not None else None
        if record["status"] != "pending":
            return self._result(record)
        event = event_from_payload(json.loads(record["event_payload_json"]))
        result = self.store.confirm(
            record=record, context=context, confirmation_message_id=confirmation_message_id,
            event=event, now=self.now(), owner_hash=active_hash,
        )
        return self._result(result)

    def cancel(self, context: RecordContext) -> Optional[PendingResult]:
        context = validate_context(context)
        active_hash = owner_hash(context)
        record = self.store.find_active(context, active_hash) if hasattr(self.store, "find_active") else None
        if record is None:
            return None
        return self._result(self.store.cancel(record=record, context=context, owner_hash=active_hash, now=self.now()))

    def status(self, context: RecordContext) -> Optional[PendingResult]:
        context = validate_context(context)
        active_hash = owner_hash(context)
        record = self.store.find_active(context, active_hash) if hasattr(self.store, "find_active") else None
        if record is None:
            return None
        return self._result(self.store.status(record=record, context=context, owner_hash=active_hash, now=self.now()))


def _emit(
    ok: bool,
    text: str,
    result: Optional[PendingResult] = None,
    normalized_event: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {"schema_version": 1, "ok": ok, "text": text}
    if result is not None:
        payload["pending"] = {
            "id": result.pending_id, "status": result.status.value, "expires_at": result.expires_at,
            "human_event_id": result.human_event_id,
        }
    if normalized_event is not None:
        payload["normalized_event"] = normalized_event
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _load_request(request_json: Optional[str], request_stdin: bool) -> Dict[str, Any]:
    """Accept exactly one structured request source; never parse shell arguments."""
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
    _emit(False, "人工浇水记录服务仅接受可信 QQ 入站请求")
    return
    parser = argparse.ArgumentParser(description="内部人工浇水确认状态机")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--request-json")
    source.add_argument("--request-stdin", action="store_true")
    args = parser.parse_args()
    try:
        request = _load_request(args.request_json, args.request_stdin)
        context = RecordContext(**request["context"])
        gateway = HumanRecordGateway(GsqlPendingStore())
        operation = request.get("operation")
        if operation == "preview":
            normalized_event = parse_manual_watering_command(request["text"])
            result = gateway.preview(context, request["text"])
            _emit(True, "人工浇水记录预览已保存，等待确认", result, normalized_event.payload())
        elif operation == "confirm":
            result = gateway.confirm(context, request["confirmation_message_id"])
            _emit(result is not None, "人工浇水记录已确认" if result else "没有待确认的人工浇水记录", result)
        elif operation == "cancel":
            result = gateway.cancel(context)
            _emit(result is not None, "人工浇水记录已取消" if result else "没有待确认的人工浇水记录", result)
        elif operation == "status":
            result = gateway.status(context)
            _emit(result is not None, "待确认记录状态" if result else "没有待确认的人工浇水记录", result)
        else:
            raise ValueError("operation无效")
    except (KeyError, TypeError, ValueError, ValidationError, PendingAlreadyExists, json.JSONDecodeError):
        _emit(False, "人工浇水记录请求无效")
    except Exception:
        _emit(False, "人工浇水记录处理失败，未写入任何记录")


if __name__ == "__main__":
    main()
