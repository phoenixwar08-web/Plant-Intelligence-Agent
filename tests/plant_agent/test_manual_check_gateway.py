"""Persistent confirmation behavior for manual-check feedback."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from services.manual_check_gateway import (
    ConfirmationState,
    ManualCheckGateway,
    PendingAlreadyExists,
    RecordContext,
    _load_request,
)


NOW = datetime.fromisoformat("2026-07-21T15:30:00+08:00")
TEXT = "记录 soil2 人工检查 检查项=sensor_stale 结果=未发现异常 备注=探针连接正常"


def checklist():
    return {
        "device_code": "soil2",
        "generated_at": "2026-07-21T15:29:00+08:00",
        "checks": [{
            "code": "sensor_stale",
            "source": "sensor",
            "evidence": "数据质量审计标记为 stale；最近时间：2026-07-21T15:00:00+08:00",
        }],
    }


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
            "event_payload_json": payload_json,
            "payload_hash": payload_hash,
            "owner_hash": owner_hash,
            "status": "pending",
            "expires_at": expires_at,
            "manual_check_event_id": None,
            "confirmation_message_id": None,
        }
        self.records[pending_id] = record
        self.records[context.source_message_id] = record
        self.active_by_owner[owner_hash] = pending_id
        return record

    def find_active(self, _context, owner_hash):
        pending_id = self.active_by_owner.get(owner_hash)
        return self.records[pending_id] if pending_id is not None else None

    def find_by_confirmation(self, context, confirmation_message_id):
        for record in self.records.values():
            if isinstance(record, dict) and record.get("context") == context and record.get("confirmation_message_id") == confirmation_message_id:
                return record
        return None

    def find_latest_confirmed(self, context):
        for record in self.records.values():
            if isinstance(record, dict) and record.get("context") == context and record.get("status") == "confirmed":
                return record
        return None

    def confirm(self, *, record, context, confirmation_message_id, event, owner_hash, now):
        pending_id = self.active_by_owner.get(owner_hash)
        record = self.records[pending_id] if pending_id is not None else record
        if record["expires_at"] <= now:
            record["status"] = "expired"
            self.active_by_owner.pop(owner_hash, None)
            return record
        if self.fail_confirmation:
            raise RuntimeError("simulated transaction failure")
        event_id = self.next_event_id
        self.next_event_id += 1
        self.events[event_id] = event
        record["manual_check_event_id"] = event_id
        record["confirmation_message_id"] = confirmation_message_id
        record["status"] = "confirmed"
        self.active_by_owner.pop(owner_hash, None)
        return record

    def cancel(self, *, record, context, owner_hash, now):
        if record["expires_at"] <= now:
            record["status"] = "expired"
        else:
            record["status"] = "cancelled"
        self.active_by_owner.pop(owner_hash, None)
        return record

    def status(self, *, record, context, owner_hash, now):
        if record["expires_at"] <= now and record["status"] == "pending":
            record["status"] = "expired"
            self.active_by_owner.pop(owner_hash, None)
        return record


class ManualCheckGatewayTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.gateway = ManualCheckGateway(self.store, now=lambda: NOW, checklist_builder=lambda *_args, **_kwargs: checklist())
        self.context = RecordContext(
            channel="qqbot", agent_id="qqbot4", conversation_id="group-1",
            sender_id="sender-1", sender_name="Tester", source_message_id="message-1",
        )

    def test_preview_snapshots_current_check_without_writing_final_event(self):
        result = self.gateway.preview(self.context, TEXT)

        self.assertEqual(ConfirmationState.PENDING, result.status)
        self.assertEqual({}, self.store.events)
        payload = self.store.records["message-1"]["event_payload_json"]
        self.assertIn('"check_code":"sensor_stale"', payload)
        self.assertIn('"check_source":"sensor"', payload)

    def test_unknown_check_is_rejected_and_same_sender_cannot_replace_pending(self):
        with self.assertRaises(ValueError):
            self.gateway.preview(self.context, TEXT.replace("sensor_stale", "status_stale"))
        self.gateway.preview(self.context, TEXT)
        next_context = RecordContext(**{**self.context.__dict__, "source_message_id": "message-2"})
        with self.assertRaises(PendingAlreadyExists):
            self.gateway.preview(next_context, TEXT.replace("探针连接正常", "再次检查"))

    def test_confirm_is_idempotent_and_writes_exactly_one_event(self):
        self.gateway.preview(self.context, TEXT)
        first = self.gateway.confirm(self.context, "confirm-1")
        repeated = self.gateway.confirm(self.context, "confirm-2")

        self.assertEqual(ConfirmationState.CONFIRMED, first.status)
        self.assertEqual(first.manual_check_event_id, repeated.manual_check_event_id)
        self.assertEqual(1, len(self.store.events))

    def test_expiry_and_transaction_failure_do_not_write_event(self):
        self.gateway.preview(self.context, TEXT)
        expired = ManualCheckGateway(self.store, now=lambda: NOW + timedelta(minutes=10), checklist_builder=lambda *_args, **_kwargs: checklist())
        self.assertEqual(ConfirmationState.EXPIRED, expired.confirm(self.context, "confirm-1").status)
        self.assertEqual({}, self.store.events)

        fresh_store = InMemoryStore()
        fresh_gateway = ManualCheckGateway(fresh_store, now=lambda: NOW, checklist_builder=lambda *_args, **_kwargs: checklist())
        fresh_gateway.preview(self.context, TEXT)
        fresh_store.fail_confirmation = True
        with self.assertRaises(RuntimeError):
            fresh_gateway.confirm(self.context, "confirm-2")
        self.assertEqual({}, fresh_store.events)
        self.assertEqual(ConfirmationState.PENDING, fresh_gateway.status(self.context).status)

    def test_cli_request_is_one_json_line_and_not_a_command_interface(self):
        self.assertEqual("status", _load_request('{"operation":"status"}', False)["operation"])
        with self.assertRaises(ValueError):
            _load_request('{"operation":"status"}\n{}', False)


if __name__ == "__main__":
    unittest.main()
