import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
from analytics import daily_summary

NOW = datetime.fromisoformat("2026-07-19T08:00:00+08:00")

class DailySummaryTests(unittest.TestCase):
    def test_build_keeps_mqtt_and_human_watering_separate(self):
        status={"generated_at":"2026-07-19T07:55:00+08:00","sensor":{"soil_humidity":38,"soil_temperature":27,"air_humidity":45,"lux":200},"visual":{"observed_at":"2026-07-18T09:00:00+08:00","plant_health":"normal","yellow_leaf_ratio":0.02},"control":{"decision":"observe"},"safety":{"blocking_reasons":[]}}
        trend={"generated_at":"2026-07-19T08:00:00+08:00","soil_humidity":{"trend":"stable"},"irrigation":{"count":0},"human_watering":{"count":1,"total_volume_ml":100},"visual":{}}
        decision={"generated_at":"2026-07-19T07:55:00+08:00","decision":{"action":"observe","water_sec":0,"reasoning":["事实"]}}
        with patch.object(daily_summary, "write_trend"), patch.object(daily_summary, "_load", side_effect=[status,trend,decision]):
            result=daily_summary.build_daily_summary(now=NOW)
        self.assertEqual(result["trend"]["irrigation"]["count"],0); self.assertEqual(result["trend"]["human_watering"]["count"],1)
        self.assertEqual(result["data_quality"]["visual"],"fresh")
    def test_write_is_atomic_and_history_is_appended(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); payload={"schema_version":1,"device_code":"soil2"}
            with patch.object(daily_summary,"SUMMARY_PATH",root/"summary.json"), patch.object(daily_summary,"HISTORY_PATH",root/"history"/"soil2.jsonl"), patch.object(daily_summary,"build_daily_summary",return_value=payload):
                daily_summary.write_daily_summary(); daily_summary.write_daily_summary()
            self.assertEqual(json.loads((root/"summary.json").read_text())["device_code"],"soil2")
            self.assertEqual(len((root/"history"/"soil2.jsonl").read_text().splitlines()),2)

if __name__ == "__main__": unittest.main()
