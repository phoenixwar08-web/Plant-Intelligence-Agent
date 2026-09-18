import copy
import json
import unittest
from pathlib import Path

from services.soil3.replay.replay_v1 import ReplayBuilder


ROOT = Path(__file__).resolve().parents[1]


def sensor(timestamp, humidity, *, temperature=25.0, ec_raw=420.0, lux=100.0):
    return {
        "timestamp": timestamp,
        "humidity": humidity,
        "temperature": temperature,
        "ec_raw": ec_raw,
        "lux": lux,
        "air_humidity": 55.0,
        "air_temperature": 26.0,
    }


class ReplayV1Tests(unittest.TestCase):
    def setUp(self):
        self.builder = ReplayBuilder(max_gap_seconds=900, response_window_seconds=3600)
        self.cutoff = "2026-09-16T10:15:00Z"
        self.sensors = [
            sensor("2026-09-16T10:00:00Z", 30.0),
            sensor("2026-09-16T10:05:00Z", 30.5),
            sensor("2026-09-16T10:10:00Z", 31.0),
            sensor("2026-09-16T10:15:00Z", 31.2),
        ]
        self.watering = [
            {"timestamp": "2026-09-16T10:07:00Z", "water_sec": 3.0, "status": "accepted"}
        ]
        self.states = [
            {
                "timestamp": "2026-09-16T10:14:00Z",
                "value": {"pump_active": False, "pump_total_cycles": 4},
            }
        ]
        self.parameters = [
            {
                "timestamp": "2026-09-16T09:00:00Z",
                "value": {"FC": 45.7, "TARGET_LOW": 32.0, "HARD_SAFETY_LOW": 28.0, "K_P": 1.4},
            }
        ]

    def build(self, **overrides):
        arguments = {
            "replay_at": self.cutoff,
            "sensor_readings": self.sensors,
            "watering_history": self.watering,
            "state_history": self.states,
            "parameter_history": self.parameters,
        }
        arguments.update(overrides)
        return self.builder.build(**arguments)

    def test_builds_repeatable_state_v1_and_good_quality(self):
        sample, quality = self.build()
        repeated, repeated_quality = self.build()
        self.assertEqual(sample, repeated)
        self.assertEqual(quality, repeated_quality)
        self.assertEqual("replay_sample.v1", sample["schema_version"])
        self.assertEqual("state.v1", sample["state"]["schema_version"])
        self.assertEqual(64, len(sample["fact_digest"]))
        self.assertEqual(self.cutoff, sample["state"]["generated_at"])
        self.assertEqual(31.2, sample["state"]["soil"]["humidity_percent"])
        self.assertEqual("good", quality["status"])

    def test_future_rows_never_change_sample_or_sample_id(self):
        baseline, _ = self.build()
        future_sensors = self.sensors + [sensor("2026-09-16T11:00:00Z", 99.0)]
        future_watering = self.watering + [
            {"timestamp": "2026-09-16T11:01:00Z", "water_sec": 999.0}
        ]
        with_future, quality = self.build(
            sensor_readings=future_sensors,
            watering_history=future_watering,
        )
        self.assertEqual(baseline, with_future)
        self.assertEqual(1, quality["source_summary"]["sensor"]["excluded_after_cutoff"])
        self.assertEqual(1, quality["source_summary"]["watering"]["excluded_after_cutoff"])
        self.assertNotIn("99.0", json.dumps(with_future))
        self.assertNotIn("999.0", json.dumps(with_future))

    def test_continuity_gap_is_reported(self):
        sensors = self.sensors[:3] + [sensor("2026-09-16T11:00:00Z", 32.0)]
        _, quality = self.build(replay_at="2026-09-16T11:00:00Z", sensor_readings=sensors)
        self.assertEqual("warning", quality["status"])
        self.assertEqual(1, len(quality["continuity"]["gaps"]))
        self.assertEqual(3000.0, quality["continuity"]["gaps"][0]["duration_seconds"])

    def test_invalid_and_missing_timestamps_are_reported(self):
        sensors = self.sensors + [
            sensor("not-a-time", 40.0),
            sensor(None, 41.0),
            sensor("14", 42.0),
        ]
        sample, quality = self.build(sensor_readings=sensors)
        self.assertEqual(4, sample["fact_window"]["sensor_count"])
        self.assertEqual(3, quality["timestamp_quality"]["sensor"]["invalid_or_missing_timestamp"])
        self.assertEqual("warning", quality["status"])

    def test_source_order_regression_and_duplicate_are_reported(self):
        sensors = [self.sensors[1], self.sensors[0], self.sensors[0], *self.sensors[2:]]
        sample, quality = self.build(sensor_readings=sensors)
        details = quality["timestamp_quality"]["sensor"]
        self.assertEqual(1, details["source_order_regressions"])
        self.assertEqual(1, details["duplicate_records"])
        self.assertEqual(4, sample["fact_window"]["sensor_count"])

    def test_missing_required_sensor_field_is_counted(self):
        sensors = copy.deepcopy(self.sensors)
        sensors[1]["ec_raw"] = None
        _, quality = self.build(sensor_readings=sensors)
        self.assertEqual(1, quality["missing_sensor_fields"]["ec_raw"])
        self.assertEqual("warning", quality["status"])

    def test_watering_is_matched_to_before_and_after_sensor(self):
        _, quality = self.build()
        relationship = quality["watering_sensor_relationships"][0]
        self.assertEqual("matched", relationship["status"])
        self.assertEqual("2026-09-16T10:05:00Z", relationship["before_sensor_at"])
        self.assertEqual("2026-09-16T10:10:00Z", relationship["after_sensor_at"])
        self.assertEqual(0.5, relationship["humidity_delta"])

    def test_missing_post_watering_sensor_is_explicit(self):
        watering = [{"timestamp": "2026-09-16T10:15:00Z", "water_sec": 3.0}]
        _, quality = self.build(watering_history=watering)
        relationship = quality["watering_sensor_relationships"][0]
        self.assertEqual("missing_after_sensor", relationship["status"])
        self.assertIsNone(relationship["after_sensor_at"])
        self.assertEqual("warning", quality["status"])

    def test_malformed_watering_record_is_counted(self):
        sample, quality = self.build(watering_history=[*self.watering, "broken"])
        self.assertEqual(1, sample["fact_window"]["watering_count"])
        self.assertEqual(
            1,
            quality["timestamp_quality"]["watering"]["invalid_or_missing_timestamp"],
        )

    def test_absent_optional_histories_are_reported_without_invention(self):
        sample, quality = self.build(
            watering_history=[],
            state_history=[],
            parameter_history=[],
        )
        self.assertEqual([], sample["state"]["irrigation"]["recent_results"])
        self.assertIsNone(sample["state"]["safety"]["field_capacity"])
        self.assertEqual(
            [
                "phase3_state_history_unavailable",
                "parameter_history_unavailable",
                "watering_history_unavailable",
            ],
            quality["limitations"],
        )
        self.assertEqual("warning", quality["status"])

    def test_no_sensor_history_is_unusable(self):
        sample, quality = self.build(sensor_readings=[])
        self.assertEqual("unusable", quality["status"])
        self.assertIsNone(sample["state"]["soil"]["humidity_percent"])

    def test_naive_source_time_uses_declared_timezone(self):
        sample, _ = self.build(
            replay_at="2026-09-16T10:00:00Z",
            sensor_readings=[sensor("2026-09-16 18:00:00", 35.0)],
            watering_history=[],
        )
        self.assertEqual("2026-09-16T10:00:00Z", sample["fact_window"]["last_sensor_at"])

    def test_inputs_are_not_mutated(self):
        sources = copy.deepcopy(self.sensors)
        self.build(sensor_readings=sources)
        self.assertEqual(self.sensors, sources)


class ReplayBoundaryTests(unittest.TestCase):
    def test_schemas_are_valid_json(self):
        replay_dir = ROOT / "services" / "soil3" / "replay"
        for path in replay_dir.glob("*.schema.json"):
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_module_has_no_control_or_training_path(self):
        replay_dir = ROOT / "services" / "soil3" / "replay"
        source = "\n".join(path.read_text(encoding="utf-8") for path in replay_dir.glob("*.py"))
        self.assertNotIn(".publish(", source)
        self.assertNotIn("mosquitto_pub", source)
        self.assertNotIn("manual_water", source)
        self.assertNotIn("ActuatorLayer", source)
        self.assertNotIn("optimizer.step", source)


if __name__ == "__main__":
    unittest.main()
