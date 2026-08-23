"""Tests for strict, auditable manual-watering records."""

import sys
import unittest
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from services.human_event_service import (
    ValidationError,
    parse_manual_watering_command,
    record_manual_watering,
)


NOW = datetime.fromisoformat("2026-07-17T18:00:00+08:00")


class HumanEventServiceTests(unittest.TestCase):
    def test_parse_command_defaults_to_soil2_and_returns_shanghai_time(self):
        event = parse_manual_watering_command(
            "记录 人工浇水 时间=现在 时长=30秒 水量=100ml 备注=手浇测试",
            now=NOW,
        )
        self.assertEqual("soil2", event.device_code)
        self.assertEqual("manual_watering", event.event_type)
        self.assertEqual("2026-07-17T18:00:00+08:00", event.occurred_at)
        self.assertEqual(30.0, event.duration_sec)
        self.assertEqual(100.0, event.volume_ml)
        self.assertEqual("手浇测试", event.note)

    def test_parse_command_accepts_only_explicit_shanghai_iso_time(self):
        event = parse_manual_watering_command(
            "记录 soil2 人工浇水 时间=2026-07-17T17:30:00+08:00",
            now=NOW,
        )
        self.assertEqual("2026-07-17T17:30:00+08:00", event.occurred_at)
        with self.assertRaises(ValidationError):
            parse_manual_watering_command("记录 soil2 人工浇水 时间=2026-07-17T17:30:00Z", now=NOW)

    def test_parse_command_rejects_other_device_and_invalid_fields(self):
        for command in (
            "记录 soil3 人工浇水 时间=现在",
            "记录 soil2 人工浇水 时长=30秒",
            "记录 soil2 人工浇水 时间=现在 时长=0秒",
            "记录 soil2 人工浇水 时间=现在 水量=-1ml",
            "记录 soil2 人工浇水 时间=现在 备注=bad" + chr(10) + "text",
            "记录 soil2 人工浇水 时间=现在 未知=1",
        ):
            with self.subTest(command=command), self.assertRaises(ValidationError):
                parse_manual_watering_command(command, now=NOW)

    def test_record_is_idempotent_by_source_message_id_and_escapes_note(self):
        event = parse_manual_watering_command(
            "记录 soil2 人工浇水 时间=现在 时长=2.5秒 备注=O'Reilly",
            now=NOW,
        )
        calls = []

        def first_insert(sql):
            calls.append(sql)
            return [["42"]]

        created = record_manual_watering(
            event, source_message_id="qq-message-1", operator_id="operator-1",
            operator_name="Toumai", query=first_insert,
        )
        self.assertTrue(created["created"])
        self.assertEqual(42, created["id"])
        self.assertIn("O''Reilly", calls[0])
        self.assertIn("ON CONFLICT (source_message_id) DO NOTHING", calls[0])

        def duplicate(sql):
            calls.append(sql)
            return [] if "INSERT INTO" in sql else [["42"]]

        repeated = record_manual_watering(
            event, source_message_id="qq-message-1", operator_id="operator-1",
            operator_name="Toumai", query=duplicate,
        )
        self.assertFalse(repeated["created"])
        self.assertEqual(42, repeated["id"])

    def test_record_rejects_control_characters_in_identity_before_sql(self):
        event = parse_manual_watering_command("记录 soil2 人工浇水 时间=现在", now=NOW)
        with self.assertRaises(ValidationError):
            record_manual_watering(
                event, source_message_id="bad" + chr(10) + "message", operator_id="operator-1",
                operator_name=None, query=lambda sql: self.fail("query must not execute"),
            )


if __name__ == "__main__":
    unittest.main()
