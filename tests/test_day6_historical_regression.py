import json
import tempfile
import unittest
from pathlib import Path

from services.soil3.state.state_v1 import StateBuilder


try:
    from services.soil3.historical_regression.batch_v1 import (
        load_batch_manifest,
        load_case_input,
    )
except ImportError:
    load_batch_manifest = None
    load_case_input = None


class HistoricalRegressionManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = StateBuilder("soil3").build(
            {
                "observed_at": "2026-09-16T10:00:00Z",
                "generated_at": "2026-09-16T10:00:01Z",
                "sensor_readings": [
                    {
                        "timestamp": "2026-09-16T10:00:00Z",
                        "humidity": 31.0,
                        "temperature": 24.0,
                        "ec_raw": 500.0,
                    }
                ],
                "system_state": {
                    "pump_active": False,
                    "sensor_fault": 0,
                    "hard_safety_low_guard": 1,
                },
                "watering_history": [],
                "parameters": {
                    "FC": 38.0,
                    "TARGET_LOW": 40.0,
                    "HARD_SAFETY_LOW": 25.0,
                },
            }
        )
        self.sample = {
            "schema_version": "replay_sample.v1",
            "sample_id": "replay-example",
            "device_code": "soil3",
            "replay_at": "2026-09-16T10:00:01Z",
            "state": self.state,
        }
        (self.root / "sample.json").write_text(
            json.dumps(self.sample), encoding="utf-8"
        )
        (self.root / "response.txt").write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def write_manifest(self, value):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def valid_manifest(self):
        return {
            "schema_version": "historical_regression_manifest.v1",
            "cases": [
                {
                    "case_id": "baseline-1",
                    "replay_sample": "sample.json",
                    "fixture_response": "response.txt",
                    "labels": ["baseline"],
                }
            ],
        }

    def test_public_manifest_loader_exists(self):
        self.assertIsNotNone(load_batch_manifest)

    def test_loads_ordered_fixture_case_relative_to_manifest(self):
        manifest = load_batch_manifest(self.write_manifest(self.valid_manifest()))

        self.assertEqual("historical_regression_manifest.v1", manifest.schema_version)
        self.assertEqual(1, len(manifest.cases))
        case = manifest.cases[0]
        self.assertEqual("baseline-1", case.case_id)
        self.assertEqual((self.root / "sample.json").resolve(), case.replay_sample_path)
        self.assertEqual((self.root / "response.txt").resolve(), case.fixture_response_path)
        self.assertEqual(("baseline",), case.labels)
        loaded = load_case_input(case)
        self.assertEqual("replay-example", loaded.sample_id)
        self.assertEqual("state.v1", loaded.state["schema_version"])
        self.assertEqual(64, len(loaded.sample_sha256))

    def test_rejects_duplicate_case_ids(self):
        value = self.valid_manifest()
        value["cases"].append(dict(value["cases"][0]))

        with self.assertRaisesRegex(ValueError, "duplicate case_id"):
            load_batch_manifest(self.write_manifest(value))

    def test_fixture_mode_requires_existing_fixture_response(self):
        value = self.valid_manifest()
        value["cases"][0].pop("fixture_response")

        with self.assertRaisesRegex(ValueError, "fixture_response"):
            load_batch_manifest(self.write_manifest(value))

    def test_live_mode_does_not_require_fixture_response(self):
        value = self.valid_manifest()
        value["cases"][0].pop("fixture_response")

        manifest = load_batch_manifest(
            self.write_manifest(value), mode="live-provider"
        )

        self.assertIsNone(manifest.cases[0].fixture_response_path)

    def test_rejects_unknown_manifest_and_case_fields(self):
        manifest_value = self.valid_manifest()
        manifest_value["unknown"] = True
        with self.assertRaisesRegex(ValueError, "manifest fields"):
            load_batch_manifest(self.write_manifest(manifest_value))

        case_value = self.valid_manifest()
        case_value["cases"][0]["unknown"] = True
        with self.assertRaisesRegex(ValueError, "case fields"):
            load_batch_manifest(self.write_manifest(case_value))

    def test_rejects_non_replay_or_non_soil3_state(self):
        broken = dict(self.sample)
        broken["schema_version"] = "other"
        (self.root / "sample.json").write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "replay_sample.v1"):
            manifest = load_batch_manifest(self.write_manifest(self.valid_manifest()))
            load_case_input(manifest.cases[0])

        broken = dict(self.sample)
        broken["state"] = dict(self.state, device_code="soil2")
        (self.root / "sample.json").write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "soil3 state.v1"):
            manifest = load_batch_manifest(self.write_manifest(self.valid_manifest()))
            load_case_input(manifest.cases[0])


if __name__ == "__main__":
    unittest.main()
