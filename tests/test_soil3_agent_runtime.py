import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class StateProducerTests(unittest.TestCase):
    def runtime_config_value(self, root: Path):
        return {
            "device_code": "soil3",
            "provider_mode": "offline_fixture",
            "exploration_requested": False,
            "phase3_state_path": str(root / "phase3" / "system_state.json"),
            "sensor_log_path": str(root / "phase3" / "sensor_log.csv"),
            "irrigation_trials_path": str(root / "phase3" / "irrigation_trials.json"),
            "phase3_service_unit": "phase3_soil3.service",
            "runtime_root": str(root / "agent_chain"),
            "state_output": str(root / "agent_chain" / "state" / "latest.json"),
            "prompt_path": "services/soil3/cloud_strategy/prompts/strategy_v1.txt",
            "strategy_validator": {
                "max_actions": 12,
                "max_pump_seconds": 120,
                "max_wait_seconds": 86400,
                "max_total_pump_seconds": 240,
                "max_total_seconds": 86400,
            },
            "gate_policy": {
                "warning_age_seconds": 900,
                "deny_age_seconds": 18000,
                "window_seconds": 86400,
                "max_exploration_water_seconds": 0,
            },
        }

    def health_snapshot(self):
        return {
            "observed_at": "2026-09-20T12:00:00Z",
            "state_file": {"age_seconds": 4.0},
            "sensor_readings": [
                {
                    "timestamp": "2026-09-20T12:00:00Z",
                    "humidity": 31.5,
                    "temperature": 23.0,
                    "ec_raw": 500.0,
                }
            ],
            "watering_history": [],
            "system_state": {
                "pump_active": False,
                "predictor_circuit": {"state": "OPEN"},
            },
            "environment": {"air": {"age_seconds": 9.0}},
        }

    def test_state_writer_preserves_missing_phase3_safety_facts(self):
        """Fails if the runtime invents pending_soak or cloud_protection."""
        from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig, write_state_snapshot

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RuntimeConfig.from_dict(self.runtime_config_value(root))
            with mock.patch(
                "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                return_value=self.health_snapshot(),
            ):
                state = write_state_snapshot(config)

            stored = config.state_output.read_text(encoding="utf-8")
            self.assertIn('"schema_version": "state.v1"', stored)
            self.assertEqual("soil3", state["device_code"])
            self.assertEqual(
                {
                    "pump_active": False,
                    "predictor_circuit": {"state": "OPEN"},
                },
                state["safety"]["flags"],
            )
            self.assertNotIn("pending_soak", state["safety"]["flags"])
            self.assertNotIn("cloud_protection", state["safety"]["flags"])

    def test_runtime_config_rejects_non_soil3_or_exploration(self):
        """Fails if a future runtime config broadens this deployment's boundary."""
        from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong_device = self.runtime_config_value(root)
            wrong_device["device_code"] = "soil2"
            with self.assertRaisesRegex(ValueError, "device_code"):
                RuntimeConfig.from_dict(wrong_device)

            exploration = self.runtime_config_value(root)
            exploration["exploration_requested"] = True
            with self.assertRaisesRegex(ValueError, "exploration_requested"):
                RuntimeConfig.from_dict(exploration)

    def test_state_cli_requires_explicit_config_and_reports_written_snapshot(self):
        """Fails if the service silently chooses a production config or omits its output."""
        from services.soil3.agent_runtime import service

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "runtime.json"
            config_path.write_text(
                json.dumps(self.runtime_config_value(root)), encoding="utf-8"
            )
            with mock.patch(
                "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                return_value=self.health_snapshot(),
            ):
                summary = service.run(["state", "--config", str(config_path)])

            self.assertEqual("state.v1", summary["schema_version"])
            self.assertEqual(str(root / "agent_chain" / "state" / "latest.json"), summary["path"])
            self.assertEqual(["predictor_circuit", "pump_active"], summary["safety_flags"])


if __name__ == "__main__":
    unittest.main()
