import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from analytics import alert_evaluator as evaluator


NOW = datetime.fromisoformat("2026-07-20T08:00:00+08:00")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def quality_with(**states):
    return {
        "freshness": {
            source: {"state": states.get(source, "fresh")}
            for source in ("sensor", "status", "decision", "visual")
        }
    }


class AlertEvaluatorTests(unittest.TestCase):
    def build(self, status, decision, quality):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        status_path = root / "status.json"
        decision_path = root / "decision.json"
        quality_path = root / "quality.json"
        write_json(status_path, status)
        write_json(decision_path, decision)
        write_json(quality_path, quality)
        patches = patch.multiple(
            evaluator,
            STATUS_PATH=status_path,
            DECISION_PATH=decision_path,
            QUALITY_PATH=quality_path,
        )
        return directory, patches

    def test_safety_flags_are_ordered_and_keep_original_reasons(self):
        status = {
            "safety": {
                "water_delivery_suspect": {"active": True, "reason": "连续补水未恢复"},
                "reservoir_empty_suspect": {"active": True, "reason": "水箱读数为空"},
                "low_wet_recovery_suspect": {"active": True, "reason": "湿度恢复偏慢"},
            }
        }
        directory, patches = self.build(status, {"decision": {"action": "observe"}}, quality_with())
        with directory, patches:
            report = evaluator.build_checklist(now=NOW)
        self.assertEqual(
            [item["code"] for item in report["checks"]],
            ["water_delivery_suspect", "reservoir_empty_suspect", "low_wet_recovery_suspect"],
        )
        self.assertEqual(report["checks"][0]["evidence"], "连续补水未恢复")
        self.assertEqual(report["checks"][0]["recommended_checks"], ["检查水箱余量", "检查水路是否通畅", "检查泵出水状态"])

    def test_manual_check_precedes_quality_and_uses_decision_reasoning(self):
        status = {"safety": {}}
        decision = {"decision": {"action": "manual_check", "reasoning": ["疑似病害", "视觉状态异常，需要人工检查"]}}
        quality = quality_with(sensor="stale", visual="unavailable")
        directory, patches = self.build(status, decision, quality)
        with directory, patches:
            report = evaluator.build_checklist(now=NOW)
        self.assertEqual([item["code"] for item in report["checks"]], ["manual_check", "sensor_stale", "visual_unavailable"])
        self.assertEqual(report["checks"][0]["evidence"], "疑似病害；视觉状态异常，需要人工检查")
        self.assertEqual(report["overall"], "warning")

    def test_clear_report_has_no_checks(self):
        directory, patches = self.build(
            {"safety": {}},
            {"decision": {"action": "observe", "reasoning": []}},
            quality_with(),
        )
        with directory, patches:
            report = evaluator.build_checklist(now=NOW)
        self.assertEqual(report["overall"], "clear")
        self.assertEqual(report["checks"], [])

    def test_missing_or_invalid_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(
                evaluator,
                STATUS_PATH=root / "missing-status.json",
                DECISION_PATH=root / "missing-decision.json",
                QUALITY_PATH=root / "missing-quality.json",
            ):
                with self.assertRaises(RuntimeError):
                    evaluator.build_checklist(now=NOW)
        with self.assertRaises(ValueError):
            evaluator.build_checklist(device_code="soil3", now=NOW)


if __name__ == "__main__":
    unittest.main()
