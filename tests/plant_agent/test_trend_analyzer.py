"""Regression tests for the read-only soil2 trend analyzer."""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from analytics import trend_analyzer as trend


NOW = datetime.fromisoformat("2026-07-17T12:00:00+08:00")


class TrendAnalyzerTests(unittest.TestCase):
    def test_database_time_predicates_keep_offset_and_explicitly_interpret_naive_columns_as_shanghai(self):
        start = datetime.fromisoformat("2026-07-16T12:00:00+08:00")
        end = datetime.fromisoformat("2026-07-17T12:00:00+08:00")

        with patch.object(trend, "run_gsql", return_value=[]) as query:
            trend.fetch_sensor_readings(start, end)
        sensor_sql = query.call_args.args[0]
        self.assertIn("recv_time AT TIME ZONE 'Asia/Shanghai'", sensor_sql)
        self.assertIn("2026-07-16T12:00:00+08:00", sensor_sql)
        self.assertIn("2026-07-17T12:00:00+08:00", sensor_sql)

        with patch.object(trend, "run_gsql", return_value=[]) as query:
            trend.fetch_irrigation_events(start, end)
        irrigation_sql = query.call_args.args[0]
        self.assertIn("command_time AT TIME ZONE 'Asia/Shanghai'", irrigation_sql)
        self.assertIn("2026-07-16T12:00:00+08:00", irrigation_sql)

        with patch.object(trend, "run_gsql", return_value=[]) as query:
            trend.fetch_human_watering_events(start, end)
        human_sql = query.call_args.args[0]
        self.assertIn("occurred_at >= '2026-07-16T12:00:00+08:00'::timestamptz", human_sql)
        self.assertIn("confirmation_status = 'confirmed'", human_sql)
        self.assertIn("trust_status IN ('attested', 'legacy_verified')", human_sql)

    def test_sensor_analysis_filters_invalid_values_and_uses_one_percent_stable_band(self):
        readings = [
            {"recv_time": "2026-07-16 12:00:00", "humidity": "40"},
            {"recv_time": "2026-07-16 12:30:00", "humidity": "42"},
            {"recv_time": "2026-07-16 12:45:00", "humidity": "200"},
            {"recv_time": "2026-07-17 11:10:00", "humidity": "41.4"},
            {"recv_time": "2026-07-17 11:50:00", "humidity": "41.6"},
        ]

        result = trend.analyze_sensor_readings(readings, NOW)

        self.assertEqual(result["sample_count"], 4)
        self.assertEqual(result["invalid_sample_count"], 1)
        self.assertEqual(result["start_value"], 41.0)
        self.assertEqual(result["end_value"], 41.5)
        self.assertEqual(result["change"], 0.5)
        self.assertEqual(result["trend"], "stable")

    def test_irrigation_analysis_filters_non_real_events_and_deduplicates_ids(self):
        events = [
            {
                "id": 1,
                "device_code": "soil2",
                "command_time": "2026-07-17 08:00:00",
                "water_sec": 3,
                "reason": "mqtt_watering_event",
                "source": "mqtt",
                "status": "issued",
            },
            {
                "id": 1,
                "device_code": "soil2",
                "command_time": "2026-07-17 08:00:00",
                "water_sec": 3,
                "reason": "mqtt_watering_event",
                "source": "mqtt",
                "status": "issued",
            },
            {
                "id": 2,
                "device_code": "soil2",
                "command_time": "2026-07-17 10:00:00",
                "water_sec": 4,
                "reason": "manual",
                "source": "mqtt",
                "status": "issued",
            },
            {
                "id": 3,
                "device_code": "soil3",
                "command_time": "2026-07-17 11:00:00",
                "water_sec": 5,
                "reason": "mqtt_watering_event",
                "source": "mqtt",
                "status": "issued",
            },
        ]

        result = trend.analyze_irrigation_events(events, NOW)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total_water_sec"], 3.0)
        self.assertEqual(result["last_irrigation_at"], "2026-07-17T08:00:00+08:00")

    def test_human_watering_is_separate_and_keeps_unknown_metrics_unknown(self):
        events = [
            {"id": 10, "device_code": "soil2", "event_type": "manual_watering", "trust_status": "attested", "occurred_at": "2026-07-17T08:00:00+08:00", "duration_sec": 30, "volume_ml": 100, "note": "手浇"},
            {"id": 11, "device_code": "soil2", "event_type": "manual_watering", "trust_status": "attested", "occurred_at": "2026-07-17T10:00:00+08:00", "duration_sec": None, "volume_ml": None, "note": "补水"},
            {"id": 12, "device_code": "soil3", "event_type": "manual_watering", "occurred_at": "2026-07-17T11:00:00+08:00", "duration_sec": 9, "volume_ml": 9, "note": "ignore"},
            {"id": 13, "device_code": "soil2", "event_type": "manual_watering", "occurred_at": "2026-07-17T11:30:00+08:00", "duration_sec": 60, "volume_ml": 200, "note": "legacy", "source": "legacy_unconfirmed"},
            {"id": 14, "device_code": "soil2", "event_type": "manual_watering", "occurred_at": "2026-07-17T11:45:00+08:00", "duration_sec": 60, "volume_ml": 200, "note": "not confirmed", "confirmation_status": "legacy_unconfirmed"},
        ]

        result = trend.analyze_human_watering_events(events, NOW)

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["known_duration_sample_count"], 1)
        self.assertEqual(result["total_duration_sec"], 30.0)
        self.assertEqual(result["known_volume_sample_count"], 1)
        self.assertEqual(result["total_volume_ml"], 100.0)
        self.assertEqual(result["last_human_watering_at"], "2026-07-17T10:00:00+08:00")
        self.assertEqual(result["last_note"], "补水")

    def test_visual_analysis_skips_bad_lines_and_calculates_worsening(self):
        records = [
            {"invalid": True},
            {
                "device_code": "soil2",
                "observed_at": "2026-07-16T14:00:00+08:00",
                "visual": {"yellow_leaf_ratio": 0.02, "plant_health": "normal"},
            },
            {
                "device_code": "soil2",
                "observed_at": "2026-07-17T11:00:00+08:00",
                "visual": {"yellow_leaf_ratio": 0.05, "plant_health": "warning"},
            },
        ]

        result = trend.analyze_visual_observations(records, NOW)

        self.assertEqual(result["observation_count"], 2)
        self.assertEqual(result["yellow_leaf_ratio_change"], 0.03)
        self.assertEqual(result["yellow_leaf_ratio_trend"], "increasing")
        self.assertEqual(result["health_trend"], "worsening")

    def test_build_trend_marks_unavailable_and_single_visual_sample_without_faking_values(self):
        visual = [{
            "device_code": "soil2",
            "observed_at": "2026-07-17T11:00:00+08:00",
            "visual": {"yellow_leaf_ratio": 0.02, "plant_health": "normal"},
        }]

        with patch.object(trend, "fetch_sensor_readings", side_effect=RuntimeError("db down")), \
             patch.object(trend, "fetch_irrigation_events", return_value=[]), \
             patch.object(trend, "fetch_human_watering_events", return_value=[]), \
             patch.object(trend, "load_visual_observations", return_value=visual):
            result = trend.build_trend("soil2", now=NOW)

        self.assertEqual(result["data_quality"]["sensor"], "unavailable")
        self.assertIsNone(result["soil_humidity"]["start_value"])
        self.assertEqual(result["data_quality"]["irrigation"], "ok")
        self.assertEqual(result["irrigation"]["count"], 0)
        self.assertEqual(result["data_quality"]["human_watering"], "ok")
        self.assertEqual(result["human_watering"]["count"], 0)
        self.assertEqual(result["data_quality"]["visual"], "insufficient_data")
        self.assertEqual(result["visual"]["health_trend"], "insufficient_data")

    def test_write_trend_atomically_writes_parseable_json(self):
        expected = {"device_code": "soil2", "generated_at": "2026-07-17T12:00:00+08:00"}

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with patch.object(trend, "OUTPUT_DIR", output_dir), \
                 patch.object(trend, "build_trend", return_value=expected):
                output_path = trend.write_trend("soil2", now=NOW)

            self.assertEqual(output_path, output_dir / "soil2_trend.json")
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8")),
                expected,
            )
            self.assertEqual(list(output_dir.glob("*.tmp")), [])

    def test_other_devices_are_rejected(self):
        with self.assertRaises(ValueError):
            trend.build_trend("soil3", now=NOW)

    def test_cli_can_import_project_helpers_when_run_as_script(self):
        script = Path(trend.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), "soil3"],
            cwd=script.parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("only soil2 is supported", result.stderr)


if __name__ == "__main__":
    unittest.main()
