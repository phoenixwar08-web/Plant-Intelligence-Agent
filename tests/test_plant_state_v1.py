import unittest

from services.soil3.state.state_v1 import StateBuilder


class StateV1Tests(unittest.TestCase):
    def setUp(self):
        self.builder = StateBuilder(device_code="soil3")
        self.facts = {
            "observed_at": "2026-09-16T12:00:00Z",
            "generated_at": "2026-09-16T12:01:00Z",
            "sensor_readings": [
                {"timestamp": "2026-09-16T06:00:00Z", "humidity": 30.0, "temperature": 25.0, "ec_raw": 410.0, "lux": 10.0},
                {"timestamp": "2026-09-16T11:00:00Z", "humidity": 32.0, "temperature": 26.0, "ec_raw": 420.0, "lux": 20.0},
                {"timestamp": "2026-09-16T12:00:00Z", "humidity": 33.0, "temperature": 27.0, "ec_raw": 430.0, "lux": 30.0},
            ],
            "system_state": {
                "pump_active": False,
                "pump_total_cycles": 12,
                "total_water_sec_dispensed": 48.0,
                "sensor_fault": {"active": False},
                "dynamic_cooldown": {"active": True, "cooldown_sec": 28800},
                "predictor_circuit": {"state": "HALF_OPEN"},
                "water_delivery_suspect": {"active": False},
            },
            "parameters": {"FC": 45.775, "TARGET_LOW": 32.0, "HARD_SAFETY_LOW": 28.0, "K_P": 1.484},
            "watering_history": [{"timestamp": "2026-09-16T10:00:00Z", "water_sec": 3.0, "status": "accepted"}],
            "environment": {"air": {
                "observed_at": "2026-09-16T12:00:00Z",
                "humidity_percent": 55.0,
                "temperature_c": 26.0,
                "source": "opengauss",
            }},
            "source_timestamps": {"phase3_state": "2026-09-16T11:59:00Z"},
        }

    def test_builds_repeatable_state_with_declared_fact_sources(self):
        state = self.builder.build(self.facts)
        self.assertEqual(state["schema_version"], "state.v1")
        self.assertEqual(state["device_code"], "soil3")
        self.assertEqual(state["soil"]["humidity_percent"], 33.0)
        self.assertEqual(state["generated_at"], "2026-09-16T12:01:00Z")
        self.assertEqual(state["safety"]["target_low"], 32.0)
        self.assertEqual(state["irrigation"]["last_water_sec"], 3.0)
        self.assertEqual(state["irrigation"]["recent_results"], [])
        self.assertEqual(state["fact_sources"]["soil"], "sensor_readings")
        self.assertIsNone(state["vision"]["wilting"])
        self.assertEqual(state["extensions"]["pot_weight_g"], None)

    def test_computes_real_source_freshness_and_preserves_unknown_as_null(self):
        state = self.builder.build(self.facts)
        self.assertEqual(state["data_quality"], {
            "soil_age_sec": 60.0,
            "air_age_sec": 60.0,
            "phase3_state_age_sec": 120.0,
            "watering_history_age_sec": 7260.0,
        })
        self.assertEqual(state["source_timestamps"]["soil"], "2026-09-16T12:00:00Z")
        self.assertEqual(state["source_timestamps"]["phase3_state"], "2026-09-16T11:59:00Z")

        unknown = self.builder.build({
            "observed_at": "2026-09-16T12:00:00Z",
            "generated_at": "2026-09-16T12:01:00Z",
        })
        self.assertEqual(unknown["data_quality"], {
            "soil_age_sec": None,
            "air_age_sec": None,
            "phase3_state_age_sec": None,
            "watering_history_age_sec": None,
        })

    def test_maps_only_existing_phase3_safety_flags(self):
        state = self.builder.build(self.facts)
        self.assertEqual(state["safety"]["flags"], {
            "pump_active": False,
            "water_delivery_suspect": {"active": False},
            "sensor_fault": {"active": False},
            "dynamic_cooldown": {"active": True, "cooldown_sec": 28800},
            "predictor_circuit": {"state": "HALF_OPEN"},
        })
        self.assertNotIn("pending_soak", state["safety"]["flags"])

    def test_accepts_real_unix_watering_timestamps_for_freshness(self):
        state = self.builder.build({
            "observed_at": "2026-09-16T12:00:00Z",
            "generated_at": "2026-09-16T12:01:00Z",
            "watering_history": [{"timestamp": 1789560000, "water_sec": 4}],
        })
        self.assertEqual(state["data_quality"]["watering_history_age_sec"], 60.0)

    def test_defines_trends_as_current_minus_reading_n_hours_ago(self):
        state = self.builder.build(self.facts)
        self.assertEqual(state["trends"], {
            "humidity_1h": 1.0,
            "humidity_3h": None,
            "humidity_6h": 3.0,
        })

    def test_uses_nulls_when_optional_future_facts_are_absent(self):
        state = self.builder.build({
            "observed_at": "2026-09-16T12:00:00Z",
            "generated_at": "2026-09-16T12:01:00Z",
        })
        self.assertIsNone(state["soil"]["humidity_percent"])
        self.assertEqual(state["trends"], {"humidity_1h": None, "humidity_3h": None, "humidity_6h": None})

    def test_adapts_existing_health_snapshot_without_control_side_effects(self):
        state = self.builder.build_from_health_snapshot(self.facts, {"FC": 45.775})
        self.assertEqual(state["soil"]["humidity_percent"], 33.0)
        self.assertEqual(state["safety"]["field_capacity"], 45.775)

    def test_preserves_snapshot_source_ages_and_phase3_flags(self):
        snapshot = {
            "observed_at": "2026-09-16T12:00:00Z",
            "sensor_readings": self.facts["sensor_readings"],
            "system_state": {"predictor_circuit": {"state": "OPEN"}},
            "state_file": {"age_seconds": 4.2},
            "environment": {"air": {"age_seconds": 7.1}},
        }
        state = self.builder.build_from_health_snapshot(snapshot)
        self.assertEqual(state["data_quality"]["air_age_sec"], 7.1)
        self.assertEqual(state["data_quality"]["phase3_state_age_sec"], 4.2)
        self.assertEqual(state["safety"]["flags"], {"predictor_circuit": {"state": "OPEN"}})


if __name__ == "__main__":
    unittest.main()
