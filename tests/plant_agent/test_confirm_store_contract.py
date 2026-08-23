import inspect
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from services.human_event_service import parse_manual_watering_command
from services.human_record_gateway import GsqlPendingStore, HumanRecordGateway, PendingResult, RecordContext


class StrictConfirmStore:
    def find_by_confirmation(self, context, confirmation_message_id):
        return None

    def find_active(self, context, owner_hash):
        return {
            "status": "pending",
            "event_payload_json": '{"device_code":"soil2","event_type":"manual_watering","occurred_at":"2026-07-18T17:52:39+08:00","duration_sec":30.0,"volume_ml":100.0,"note":"test"}',
        }

    def confirm(self, *, record, context, confirmation_message_id, event, now, owner_hash):
        assert owner_hash
        return {
            "id": 1,
            "status": "confirmed",
            "expires_at": None,
            "human_event_id": 1,
            "payload_hash": "hash",
        }


class ConfirmStoreContractTests(unittest.TestCase):
    def test_production_confirm_store_accepts_owner_hash_contract(self):
        self.assertIn("owner_hash", inspect.signature(GsqlPendingStore.confirm).parameters)

    def test_confirm_does_not_pass_unsupported_store_arguments(self):
        gateway = HumanRecordGateway(StrictConfirmStore(), now=lambda: datetime(2026, 7, 18, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
        context = RecordContext("qqbot", "qqbot4", "group", "sender", "Tester", "message")

        result = gateway.confirm(context, "confirmation")

        self.assertEqual(result.status.value, "confirmed")
        self.assertEqual(result.human_event_id, 1)


if __name__ == "__main__":
    unittest.main()
