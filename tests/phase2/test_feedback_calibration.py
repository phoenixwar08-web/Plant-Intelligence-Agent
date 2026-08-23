import copy
import json
import os
import tempfile
import unittest

from phase2_predictor.calibration import (
    PeakCalibrationStore,
    annotate_delivery_quality,
    build_peak_calibration,
)
from phase2_predictor.config import DEFAULT_CONFIG
from phase2_predictor.offline_training import load_historical_rows


def row(timestamp, humidity=30.0, water=0.0):
    metadata = {
        "humidity": humidity,
        "watered": int(water > 0),
        "water_seconds": water,
    }
    return (timestamp, [0.0] * 9, humidity, metadata)


class RuntimeStub:
    def __init__(self, peak):
        self.peak = peak

    def predict(self, _sequence):
        return [self.peak] * 12, self.peak


class FeedbackQualityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        for key in ("phase3_state", "phase3_trials", "phase3_evolving_params", "phase3_irrigation_profile"):
            self.config["paths"][key] = os.path.join(self.folder.name, key + ".json")
        self.config["paths"]["peak_calibration"] = os.path.join(self.folder.name, "calibration.json")

    def tearDown(self):
        self.folder.cleanup()

    def write(self, key, value):
        with open(self.config["paths"][key], "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    def test_fault_interval_is_excluded_but_recovered_retest_is_kept(self):
        self.write("phase3_state", {"water_delivery_suspect": {
            "first_seen_at": 100, "cleared_at": 300,
            "repair_retest_started_at": 250, "repair_retest_completed_at": 280,
            "repair_retest_result": "recovered",
        }})
        rows = [row(90, water=3), row(150, water=3), row(260, water=3), row(310, water=3)]
        report = annotate_delivery_quality(rows, self.config)
        self.assertTrue(rows[0][3]["training_eligible"])
        self.assertFalse(rows[1][3]["training_eligible"])
        self.assertEqual(rows[1][3]["training_label"], "hardware_suspect")
        self.assertTrue(rows[2][3]["training_eligible"])
        self.assertEqual(rows[2][3]["training_label"], "recovered_retest")
        self.assertEqual(report["excluded_rows"], 1)

    def test_calibration_is_bounded_and_requires_samples(self):
        self.write("phase3_evolving_params", {"TARGET_LOW": 33, "M_SAFE_SLEEP": 42})
        self.write("phase3_irrigation_profile", {"zones": {"low": {"kp_ema": 1.5, "stable_success": 10}}})
        samples = [([0.0] * 9, 39.0, {"humidity": 34.0, "training_eligible": True}) for _ in range(6)]
        value = build_peak_calibration(samples, RuntimeStub(35.0), self.config)
        self.assertTrue(value["global"]["active"])
        self.assertLessEqual(
            abs(value["global"]["bias"]),
            self.config["calibration"]["maximum_absolute_bias"],
        )
        self.assertTrue(value["zones"]["low"]["active"])

    def test_consistent_large_residual_is_measured_in_shadow(self):
        samples = [([0.0] * 9, 20.0, {"humidity": 34.0, "training_eligible": True}) for _ in range(6)]
        value = build_peak_calibration(samples, RuntimeStub(35.0), self.config)
        self.assertTrue(value["global"]["active"])
        self.assertEqual(value["global"]["bias"], -10.0)
        self.assertEqual(value["mode"], "shadow")

    def test_unstable_residual_disables_bias(self):
        samples = []
        for actual in (20.0, 50.0, 21.0, 49.0, 22.0, 48.0):
            samples.append(([0.0] * 9, actual, {
                "humidity": 34.0, "training_eligible": True,
            }))
        value = build_peak_calibration(samples, RuntimeStub(35.0), self.config)
        self.assertFalse(value["global"]["active"])
        self.assertEqual(value["global"]["bias"], 0.0)

    def test_store_applies_bounded_residual_and_kp_prior(self):
        self.config["calibration"]["mode"] = "apply"
        value = {
            "bounds": {"low_max": 36.0, "high_min": 40.0},
            "global": {"active": True, "bias": 1.0},
            "zones": {"low": {"active": True, "bias": 1.0}},
            "kp_by_zone": {"low": {"value": 1.5, "samples": 10}},
        }
        self.write("peak_calibration", value)
        store = PeakCalibrationStore(self.config)
        trajectory, peak, info = store.apply([36.0] * 12, 36.0, 34.0, 3.0)
        self.assertEqual(info["status"], "applied")
        self.assertLessEqual(info["total_adjustment"], 3.0)
        self.assertGreater(peak, 36.0)
        self.assertGreater(trajectory[0], trajectory[-1])

    def test_store_does_not_change_predictions_in_shadow_mode(self):
        self.write("peak_calibration", {
            "global": {"active": True, "bias": -10.0},
            "zones": {}, "kp_by_zone": {},
        })
        store = PeakCalibrationStore(self.config)
        trajectory, peak, info = store.apply([45.0] * 12, 45.0, 34.0, 3.0)
        self.assertEqual(info["status"], "shadow_only")
        self.assertEqual(peak, 45.0)
        self.assertEqual(trajectory, [45.0] * 12)

    def test_watering_row_records_pre_command_humidity_without_schema_switch(self):
        soil_path = os.path.join(self.folder.name, "soil.csv")
        with open(soil_path, "w", encoding="utf-8") as handle:
            handle.write("time,id,temp,humidity,ec,watered,light,seconds\n")
            handle.write("2026/06/21 15:57:37,id,26.3,36.1,402,0,1000,0\n")
            handle.write("2026/06/21 16:02:38,id,26.1,43.5,431,1,994,3\n")
        self.config["paths"]["soil_csv"] = soil_path
        rows = load_historical_rows(self.config)
        self.assertEqual(rows[1][2], 43.5)
        self.assertEqual(rows[1][3]["pre_watering_humidity"], 36.1)
        self.assertAlmostEqual(rows[1][1][2], 0.435)
        self.assertAlmostEqual(rows[1][1][5], 3.0 / 120.0)


if __name__ == "__main__":
    unittest.main()
