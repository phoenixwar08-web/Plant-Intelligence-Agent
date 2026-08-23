"""Trusted host ingress accepts only fixed QQ event requests."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from services.plant_event_ingress import INVALID_TEXT, PlantEventIngress


CONTEXT = {
    "channel": "qqbot", "agent_id": "qqbot4", "conversation_id": "group-1",
    "sender_id": "sender-1", "sender_name": "Tester", "source_message_id": "source-1",
}


class WaterGateway:
    def __init__(self):
        self.preview_calls = []

    def preview(self, context, text):
        self.preview_calls.append((context, text))
        return SimpleNamespace(pending_id=1)

    def confirm(self, _context, _message_id):
        return SimpleNamespace(human_event_id=7)

    def cancel(self, _context):
        return SimpleNamespace()


class CheckGateway:
    def __init__(self):
        self.store = SimpleNamespace(_find=lambda _where: {
            "event_payload_json": '{"device_code":"soil2","check_code":"sensor_stale","result":"no_issue","note":"ok","occurred_at":"2026-07-21T16:00:00+08:00","check_source":"sensor","check_evidence":"stale","check_generated_at":"2026-07-21T16:00:00+08:00"}'
        })

    def preview(self, _context, _text):
        return SimpleNamespace(pending_id=3)

    def confirm(self, _context, _message_id):
        return SimpleNamespace(manual_check_event_id=9)

    def cancel(self, _context):
        return SimpleNamespace()


class PlantEventIngressTests(unittest.TestCase):
    def setUp(self):
        self.water = WaterGateway()
        self.check = CheckGateway()
        self.ingress = PlantEventIngress(self.water, self.check)

    def test_watering_preview_uses_fixed_request_shape(self):
        response = self.ingress.handle({
            "schema_version": 1, "kind": "watering", "operation": "preview",
            "context": CONTEXT,
            "text": "记录 soil2 人工浇水 时间=现在 时长=30秒 水量=100ml 备注=验收",
        })
        self.assertTrue(response["ok"])
        self.assertIn("请在 10 分钟内回复“确认记录”", response["text"])
        self.assertEqual(1, len(self.water.preview_calls))

    def test_manual_check_confirmation_returns_only_fixed_event_result(self):
        response = self.ingress.handle({
            "schema_version": 1, "kind": "manual_check", "operation": "confirm",
            "context": CONTEXT, "confirmation_message_id": "confirm-1",
        })
        self.assertEqual({"schema_version": 1, "ok": True, "text": "soil2 人工检查记录已保存 ✅\n事件编号：9"}, response)

    def test_rejects_extra_fields_and_never_dispatches(self):
        response = self.ingress.handle({
            "schema_version": 1, "kind": "watering", "operation": "cancel",
            "context": CONTEXT, "sql": "DELETE FROM human_events",
        })
        self.assertEqual({"schema_version": 1, "ok": False, "text": INVALID_TEXT}, response)
        self.assertEqual([], self.water.preview_calls)


if __name__ == "__main__":
    unittest.main()
