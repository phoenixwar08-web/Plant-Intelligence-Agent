"""Persistent confirmation-state-machine behavior."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from services.human_record_gateway import (
    ConfirmationState,
    HumanRecordGateway,
    PendingAlreadyExists,
    RecordContext,
    _load_request,
)


NOW = datetime.fromisoformat("2026-07-18T10:00:00+08:00")
TEXT = "记录 soil2 人工浇水 时间=现在 时长=30秒 水量=100ml 备注=测试"


class InMemoryStore:
    def __init__(self):
        self.records = {}
        self.active_by_owner = {}
        self.events = {}
        self.next_pending_id = 1
        self.next_event_id = 1
        self.fail_confirmation = False

    def preview(self, *, context, event, payload_json, payload_hash, owner_hash, expires_at):
        existing = self.records.get(context.source_message_id)
        if existing is not None:
            return existing
        active_id = self.active_by_owner.get(owner_hash)
        if active_id is not None:
            return self.records[active_id]
        pending_id = str(self.next_pending_id)
        self.next_pending_id += 1
        record = {
            "id": pending_id,
            "context": context,
            "event": event,
            "payload_json": payload_json,
            "event_payload_json": payload_json,
            "payload_hash": payload_hash,
            "owner_hash": owner_hash,
            "status": "pending",
            "expires_at": expires_at,
            "human_event_id": None,
            "confirmation_message_id": None,
        }
        self.records[pending_id] = record
        self.records[context.source_message_id] = record
        self.active_by_owner[owner_hash] = pending_id
        return record

    def find_active(self, context, owner_hash):
        pending_id = self.active_by_owner.get(owner_hash)
        return self.records[pending_id] if pending_id is not None else None

    def find_by_confirmation(self, context, confirmation_message_id):
        for record in self.records.values():
            if isinstance(record, dict) and record.get("confirmation_message_id") == confirmation_message_id:
                return record
        return None

    def find_latest_confirmed(self, context):
        for record in self.records.values():
            if isinstance(record, dict) and record.get("context") == context and record.get("status") == "confirmed":
                return record
        return None

    def confirm(self, *, record, context, confirmation_message_id, event, owner_hash, now):
        pending_id = self.active_by_owner.get(owner_hash)
        if pending_id is None:
            for record in self.records.values():
                if isinstance(record, dict) and record.get("context") == context and record["status"] == "confirmed":
                    return record
            return None
        record = self.records[pending_id]
        if record["expires_at"] <= now:
            record["status"] = "expired"
            self.active_by_owner.pop(owner_hash, None)
            return record
        if self.fail_confirmation:
            raise RuntimeError("simulated transaction failure")
        event_id = self.next_event_id
        self.next_event_id += 1
        self.events[event_id] = record["event"]
        record["human_event_id"] = event_id
        record["confirmation_message_id"] = confirmation_message_id
        record["status"] = "confirmed"
        self.active_by_owner.pop(owner_hash, None)
        return record

    def cancel(self, *, record, context, owner_hash, now):
        pending_id = self.active_by_owner.get(owner_hash)
        if pending_id is None:
            return None
        record = self.records[pending_id]
        if record["expires_at"] <= now:
            record["status"] = "expired"
        else:
            record["status"] = "cancelled"
        self.active_by_owner.pop(owner_hash, None)
        return record

    def status(self, *, record, context, owner_hash, now):
        pending_id = self.active_by_owner.get(owner_hash)
        if pending_id is None:
            return None
        record = self.records[pending_id]
        if record["expires_at"] <= now:
            record["status"] = "expired"
            self.active_by_owner.pop(owner_hash, None)
        return record


class HumanRecordGatewayTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.gateway = HumanRecordGateway(self.store, now=lambda: NOW)
        self.context = RecordContext(
            channel="qqbot", agent_id="qqbot4", conversation_id="group-1",
            sender_id="sender-1", sender_name="Tester", source_message_id="message-1",
        )

    def test_preview_is_idempotent_and_only_creates_pending(self):
        first = self.gateway.preview(self.context, TEXT)
        repeated = self.gateway.preview(self.context, TEXT)

        self.assertEqual(ConfirmationState.PENDING, first.status)
        self.assertEqual(first.pending_id, repeated.pending_id)
        self.assertEqual({}, self.store.events)

    def test_different_record_is_rejected_until_pending_is_cancelled(self):
        self.gateway.preview(self.context, TEXT)
        different = RecordContext(**{**self.context.__dict__, "source_message_id": "message-2"})
        with self.assertRaises(PendingAlreadyExists):
            self.gateway.preview(different, TEXT.replace("30秒", "31秒"))

        self.assertEqual(ConfirmationState.CANCELLED, self.gateway.cancel(self.context).status)
        self.assertEqual(ConfirmationState.PENDING, self.gateway.preview(different, TEXT.replace("30秒", "31秒")).status)

    def test_confirmation_is_idempotent_and_creates_exactly_one_event(self):
        self.gateway.preview(self.context, TEXT)
        first = self.gateway.confirm(self.context, "confirm-1")
        repeated = self.gateway.confirm(self.context, "confirm-2")

        self.assertEqual(ConfirmationState.CONFIRMED, first.status)
        self.assertEqual(first.human_event_id, repeated.human_event_id)
        self.assertEqual(1, len(self.store.events))

    def test_expired_pending_does_not_write_event(self):
        self.gateway.preview(self.context, TEXT)
        expired_gateway = HumanRecordGateway(self.store, now=lambda: NOW + timedelta(minutes=10))
        result = expired_gateway.confirm(self.context, "confirm-1")

        self.assertEqual(ConfirmationState.EXPIRED, result.status)
        self.assertEqual({}, self.store.events)

    def test_simulated_transaction_failure_leaves_no_event_or_confirmation(self):
        self.gateway.preview(self.context, TEXT)
        self.store.fail_confirmation = True

        with self.assertRaises(RuntimeError):
            self.gateway.confirm(self.context, "confirm-1")

        current = self.gateway.status(self.context)
        self.assertEqual(ConfirmationState.PENDING, current.status)
        self.assertEqual({}, self.store.events)

    def test_non_qq_context_is_rejected_before_storage(self):
        webchat = RecordContext(
            channel="webchat", agent_id="qqbot4", conversation_id="web-1",
            sender_id="sender-1", sender_name="Tester", source_message_id="message-1",
        )
        with self.assertRaises(ValueError):
            self.gateway.preview(webchat, TEXT)
        self.assertEqual({}, self.store.records)

    def test_structured_cli_request_accepts_one_json_line_only(self):
        request = _load_request('{"operation":"status"}', False)
        self.assertEqual("status", request["operation"])
        with self.assertRaises(ValueError):
            _load_request('{"operation":"status"}\n{}', False)
        with self.assertRaises(ValueError):
            _load_request(None, False)


if __name__ == "__main__":
    unittest.main()
