"""Phase 4–5 regression coverage for confirmed human-watering facts."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
import plant_state_builder as builder
from analytics.trend_analyzer import analyze_human_watering_events
from agent.reasoning_agent import reasoning

class Phase45FactTests(unittest.TestCase):
    def test_confirmed_human_timestamp_is_normalized_to_shanghai(self):
        row = [["2", "2026-07-18 09:52:39.256339+00", "30", "100", "验收", "id", "name", "qqbot", "confirmed", "legacy_verified"]]
        with patch.object(builder, "run_gsql", return_value=row):
            result = builder.get_recent_human_watering_events("soil2")
        self.assertEqual(result[0]["occurred_at"], "2026-07-18T17:52:39.256339+08:00")
    def test_confirmed_event_two_is_counted_in_its_24_hour_window(self):
        now = datetime.fromisoformat("2026-07-18T18:00:00+08:00")
        result = analyze_human_watering_events([{"id":2,"device_code":"soil2","event_type":"manual_watering","source":"qqbot","confirmation_status":"confirmed","trust_status":"legacy_verified","occurred_at":"2026-07-18T09:52:39.256339+00","duration_sec":30,"volume_ml":100,"note":"阶段2验收测试"}], now)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["last_human_watering_at"], "2026-07-18T17:52:39.256339+08:00")
    def test_human_fact_does_not_change_control_decision(self):
        base={"device_code":"soil2","sensor":{"soil_humidity":40},"visual":{},"control":{"decision":"observe","decision_reason":"normal"},"safety":{},"history":{},"human_events":{"last_external_watering":None}}
        with_fact={**base,"human_events":{"last_external_watering":{"occurred_at":"2026-07-18T17:52:39.256339+08:00","duration_sec":30.0,"volume_ml":100.0,"note":"阶段2验收测试","source":"qqbot","confirmation_status":"confirmed","trust_status":"legacy_verified"}}}
        without=reasoning(base)["decision"]; explained=reasoning(with_fact)["decision"]
        self.assertEqual(explained["action"], without["action"]); self.assertEqual(explained["water_sec"], without["water_sec"])
        self.assertIn("最近人工浇水记录", explained["reasoning"][-1])
