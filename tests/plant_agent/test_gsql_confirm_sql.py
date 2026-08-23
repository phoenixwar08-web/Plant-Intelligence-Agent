import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from services.human_event_service import parse_manual_watering_command
from services.human_record_gateway import GsqlPendingStore, RecordContext


class GsqlConfirmSqlTests(unittest.TestCase):
    def test_confirm_uses_open_gauss_compatible_idempotent_insert(self):
        store = GsqlPendingStore()
        record = {"id": 1, "payload_hash": "a" * 64, "source_message_id": "source-message"}
        context = RecordContext("qqbot", "qqbot4", "group", "sender", "Tester", "confirmation-message")
        event = parse_manual_watering_command("记录 soil2 人工浇水 时间=现在 时长=30秒 水量=100ml 备注=test")
        statements = []

        with patch("services.human_record_gateway.run_gsql_transaction", side_effect=lambda sql: statements.append(sql)), \
             patch.object(store, "_find", return_value={"id": "1", "status": "confirmed", "expires_at": "", "human_event_id": "1", "payload_hash": "a" * 64}):
            store.confirm(
                record=record, context=context, confirmation_message_id="confirmation-message",
                event=event, now=datetime.now(ZoneInfo("Asia/Shanghai")), owner_hash="b" * 64,
            )

        self.assertEqual(len(statements), 1)
        self.assertNotIn("ON CONFLICT", statements[0])
        self.assertIn("NOT EXISTS", statements[0])


if __name__ == "__main__":
    unittest.main()
