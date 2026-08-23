"""Manual-check command validation contract."""

import sys
import unittest
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from services.manual_check_service import (
    CHECK_RESULT_NO_ISSUE,
    CHECK_RESULT_ISSUE_FOUND,
    CHECK_RESULT_NOT_COMPLETED,
    ValidationError,
    parse_manual_check_command,
)


NOW = datetime.fromisoformat("2026-07-21T15:30:00+08:00")


class ManualCheckServiceTests(unittest.TestCase):
    def test_parses_soil2_check_with_default_now(self):
        event = parse_manual_check_command(
            "记录 soil2 人工检查 检查项=sensor_stale 结果=未发现异常 备注=探针连接正常",
            now=NOW,
        )

        self.assertEqual("soil2", event.device_code)
        self.assertEqual("sensor_stale", event.check_code)
        self.assertEqual(CHECK_RESULT_NO_ISSUE, event.result)
        self.assertEqual("2026-07-21T15:30:00+08:00", event.occurred_at)
        self.assertEqual("探针连接正常", event.note)

    def test_requires_note_for_issue_or_incomplete_results(self):
        for result in ("发现异常", "无法完成检查"):
            with self.subTest(result=result):
                with self.assertRaises(ValidationError):
                    parse_manual_check_command(
                        f"记录 soil2 人工检查 检查项=sensor_stale 结果={result}",
                        now=NOW,
                    )

        found = parse_manual_check_command(
            "记录 人工检查 检查项=sensor_stale 结果=发现异常 备注=探针松动",
            now=NOW,
        )
        incomplete = parse_manual_check_command(
            "记录 人工检查 检查项=sensor_stale 结果=无法完成检查 备注=现场无法进入",
            now=NOW,
        )
        self.assertEqual(CHECK_RESULT_ISSUE_FOUND, found.result)
        self.assertEqual(CHECK_RESULT_NOT_COMPLETED, incomplete.result)

    def test_rejects_other_device_free_time_and_control_characters(self):
        invalid_commands = (
            "记录 soil3 人工检查 检查项=sensor_stale 结果=未发现异常",
            "记录 soil2 人工检查 检查项=sensor_stale 结果=未发现异常 时间=2026-07-21T15:30:00+08:00",
            "记录 soil2 人工检查 检查项=sensor_stale 结果=未发现异常 备注=正常\n伪造字段",
        )
        for text in invalid_commands:
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_manual_check_command(text, now=NOW)


if __name__ == "__main__":
    unittest.main()
