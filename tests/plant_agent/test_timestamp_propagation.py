"""Runnable regression tests for visual observation timestamps."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import plant_state_builder as builder
from agent.vision_agent import current_observed_at


class TimestampPropagationTests(unittest.TestCase):
    def build_status(self, vision_data):
        patches = [
            patch.object(builder, "get_phase3_state", return_value={}),
            patch.object(builder, "get_irrigation_profile", return_value={}),
            patch.object(
                builder,
                "get_latest_sensor",
                return_value={"recv_time": None},
            ),
            patch.object(builder, "get_recent_irrigation_events", return_value=[]),
            patch.object(builder, "first_real_irrigation_event", return_value=None),
            patch.object(builder, "load_visual_status", return_value=vision_data),
            patch.object(builder, "build_control_summary", return_value={}),
            patch.object(builder, "build_safety_summary", return_value={}),
            patch.object(builder, "build_learning_summary", return_value={}),
            patch.object(builder, "calculate_sensor_freshness", return_value={}),
            patch.object(builder, "build_health_assessment", return_value={}),
            patch.object(builder, "build_primary_message", return_value={}),
        ]

        for active_patch in patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

        return builder.build_plant_status("soil2")

    def test_status_preserves_vision_observed_at(self):
        observed_at = "2026-07-16T18:00:00+08:00"

        status = self.build_status(
            {
                "observed_at": observed_at,
                "visual": {"plant_health": "normal"},
            }
        )

        self.assertEqual(status["visual"]["observed_at"], observed_at)

    def test_missing_vision_timestamp_remains_null(self):
        status = self.build_status({"visual": {"plant_health": "normal"}})

        self.assertIsNone(status["visual"].get("observed_at"))

    def test_missing_vision_document_keeps_status_buildable(self):
        status = self.build_status(None)

        self.assertIsNone(status["visual"].get("observed_at"))

    def test_new_vision_timestamp_is_shanghai_timezone_aware(self):
        observed_at = current_observed_at()
        parsed = datetime.fromisoformat(observed_at)

        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))
        self.assertTrue(observed_at.endswith("+08:00"))


if __name__ == "__main__":
    unittest.main()
