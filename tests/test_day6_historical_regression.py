import json
import tempfile
import unittest
import uuid
from pathlib import Path

from services.soil3.cloud_gate.gate_v1 import GatePolicy
from services.soil3.cloud_strategy.validator import PROMPT_VERSION
from services.soil3.state.state_v1 import StateBuilder


try:
    from services.soil3.historical_regression.batch_v1 import (
        load_batch_manifest,
        load_case_input,
    )
except ImportError:
    load_batch_manifest = None
    load_case_input = None

try:
    from services.soil3.historical_regression.batch_v1 import run_batch
except ImportError:
    run_batch = None


SAFE_PHASE3_FLAGS = {
    "pump_active": False,
    "pending_soak": False,
    "water_delivery_suspect": {"active": False},
    "reservoir_empty_suspect": {"active": False},
    "low_wet_recovery_suspect": {"active": False},
    "sensor_fault": {"active": False},
    "dynamic_cooldown": {"active": False},
    "predictor_circuit": {"state": "CLOSED"},
    "watering_trigger_guard": {"active": False},
    "recent_response_guard": {"active": False},
    "hard_safety_low_guard": {"active": False},
    "cloud_protection": {"active": False},
}


class _HistoricalRegressionFixture:
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
                "source_timestamps": {"phase3_state": "2026-09-16T10:00:00Z"},
                "system_state": SAFE_PHASE3_FLAGS,
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

    def strategy_response(self, actions=None):
        return {
            "schema_version": "strategy.v1",
            "strategy_id": str(uuid.uuid4()),
            "device_code": "soil3",
            "state_observed_at": self.state["observed_at"],
            "state_generated_at": self.state["generated_at"],
            "created_at": "2026-09-16T10:00:01Z",
            "actions": actions or [{"action_id": "observe", "type": "observe"}],
            "reason_summary": ["historical fixture"],
            "expected_outcome": {"soil_moisture": "observe", "risk_notes": []},
            "confidence": 0.8,
            "model": {
                "provider": "fixture",
                "name": "fixture",
                "prompt_version": PROMPT_VERSION,
            },
            "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
        }

    def strategy_config(self):
        return {
            "enabled": True,
            "provider": "test",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
            "timeout_seconds": 1,
            "max_retries": 0,
            "temperature": 0,
            "max_tokens": 200,
            "json_response_format": True,
            "validator": {
                "max_actions": 12,
                "max_pump_seconds": 120,
                "max_wait_seconds": 86400,
                "max_total_pump_seconds": 240,
                "max_total_seconds": 86400,
            },
        }

    def gate_policy(self):
        return GatePolicy.from_dict(
            {
                "warning_age_seconds": 900,
                "deny_age_seconds": 18000,
                "window_seconds": 86400,
                "max_exploration_water_seconds": 0,
            }
        )


class HistoricalRegressionManifestTests(_HistoricalRegressionFixture, unittest.TestCase):
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


class HistoricalRegressionBatchTests(_HistoricalRegressionFixture, unittest.TestCase):
    def test_fixture_case_runs_through_strategy_validation_and_gate_v2(self):
        (self.root / "response.txt").write_text(
            json.dumps(self.strategy_response()), encoding="utf-8"
        )
        manifest = load_batch_manifest(self.write_manifest(self.valid_manifest()))

        report = run_batch(
            manifest,
            strategy_config=self.strategy_config(),
            prompt="Return strategy.v1 JSON",
            gate_policy=self.gate_policy(),
        )

        self.assertEqual("fixture", report["mode"])
        self.assertTrue(report["deterministic"])
        self.assertEqual(1, len(report["cases"]))
        result = report["cases"][0]
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["strategy"]["accepted"])
        self.assertEqual("gate.v2", result["gate"]["schema_version"])
        self.assertEqual("allow", result["gate"]["decision"])
        self.assertFalse(result["gate"]["budget"]["exploration_requested"])
        self.assertEqual(0.0, result["gate"]["budget"]["reserved_water_seconds"])
        self.assertFalse(result["gate"]["execution"]["actuator_commands_allowed"])

    def test_validator_reject_does_not_create_gate_result(self):
        invalid = self.strategy_response(
            [{"action_id": "water", "type": "water", "pump_seconds": 121}]
        )
        (self.root / "response.txt").write_text(json.dumps(invalid), encoding="utf-8")
        manifest = load_batch_manifest(self.write_manifest(self.valid_manifest()))

        report = run_batch(
            manifest,
            strategy_config=self.strategy_config(),
            prompt="Return strategy.v1 JSON",
            gate_policy=self.gate_policy(),
        )

        result = report["cases"][0]
        self.assertEqual("completed", result["status"])
        self.assertFalse(result["strategy"]["accepted"])
        self.assertIn("action[0]:pump_seconds_extreme", result["strategy"]["reason_codes"])
        self.assertIsNone(result["gate"])

    def test_case_error_is_recorded_and_later_case_still_runs(self):
        value = self.valid_manifest()
        value["cases"] = [
            {
                "case_id": "missing-sample",
                "replay_sample": "missing.json",
                "fixture_response": "response.txt",
            },
            {
                "case_id": "valid-sample",
                "replay_sample": "sample.json",
                "fixture_response": "response.txt",
            },
        ]
        (self.root / "response.txt").write_text(
            json.dumps(self.strategy_response()), encoding="utf-8"
        )
        manifest = load_batch_manifest(self.write_manifest(value))

        report = run_batch(
            manifest,
            strategy_config=self.strategy_config(),
            prompt="Return strategy.v1 JSON",
            gate_policy=self.gate_policy(),
        )

        self.assertEqual("system_error", report["cases"][0]["status"])
        self.assertEqual("case_input_unavailable", report["cases"][0]["error_code"])
        self.assertEqual("completed", report["cases"][1]["status"])


if __name__ == "__main__":
    unittest.main()
