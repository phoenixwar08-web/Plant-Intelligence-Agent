import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.soil3.state.state_v1 import StateBuilder
from services.soil3.telemetry import events


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ROOT / "config" / "soil3_agent_runtime.example.json"
UNIT_FILES = (
    ROOT / "deploy" / "systemd" / "plant-agent-soil3-state.service",
    ROOT / "deploy" / "systemd" / "plant-agent-soil3-state.timer",
    ROOT / "deploy" / "systemd" / "plant-agent-soil3-pipeline.service",
    ROOT / "deploy" / "systemd" / "plant-agent-soil3-pipeline.timer",
)


class DeploymentAssetTests(unittest.TestCase):
    def test_example_config_is_nonsecret_offline_fixture(self):
        """Fails if the deployable baseline gains a real provider or secret."""
        value = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual("offline_fixture", value["provider_mode"])
        self.assertFalse(value["exploration_requested"])
        self.assertNotIn("api_key", json.dumps(value).lower())

    def test_systemd_units_are_oneshot_and_do_not_name_control_paths(self):
        """Fails if scheduling assets acquire a control or broker dependency."""
        source = "\n".join(path.read_text(encoding="utf-8") for path in UNIT_FILES)
        self.assertIn("Type=oneshot", source)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", source)
        self.assertIn("Requires=plant-agent-soil3-state.service", source)
        self.assertIn("After=plant-agent-soil3-state.service", source)
        self.assertEqual(2, source.count("ReadOnlyPaths=/root/water"))
        self.assertEqual(
            2,
            source.count(
                "ReadWritePaths=/root/water/runtime/instances/soil3/agent_chain"
            ),
        )
        self.assertNotIn("mqtt", source.lower())
        self.assertNotIn("manual_water", source.lower())
        self.assertNotIn("phase3/main.py", source)


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


class CanonicalSoilTelemetryTests(unittest.TestCase):
    def _snapshot(self, root, *, canonical_reading, local_sensor_content=None):
        state_path = root / "phase3" / "system_state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text('{"pump_active": false}', encoding="utf-8")
        sensor_path = root / "phase3" / "sensor_log.csv"
        if local_sensor_content is not None:
            sensor_path.write_text(local_sensor_content, encoding="utf-8")
        with mock.patch.object(
            events, "_service_status", return_value={"status": "active"}
        ), mock.patch.object(
            events,
            "_opengauss_health",
            return_value={
                "latest_row_age_seconds": 5.0,
                "latest_air_age_seconds": None,
            },
        ), mock.patch.object(
            events,
            "_opengauss_latest_soil_reading",
            return_value=canonical_reading,
        ):
            return events.build_health_snapshot(
                "soil3",
                str(state_path),
                sensor_log_path=str(sensor_path),
                now=1789955700,
            )

    def test_reads_valid_opengauss_soil_row(self):
        """A real canonical row must expose its values to the state producer."""

        class Completed:
            returncode = 0
            stdout = "2026-09-21 01:53:00+00|35.0|23.2|610\n"
            stderr = ""

        reader = getattr(events, "_opengauss_latest_soil_reading", None)
        self.assertIsNotNone(reader)
        reading = reader("soil3", runner=lambda *args, **kwargs: Completed())
        self.assertEqual(
            {
                "timestamp": "2026-09-21 01:53:00+00",
                "humidity": 35.0,
                "temperature": 23.2,
                "ec_raw": 610.0,
            },
            reading,
        )

    def test_rejects_invalid_or_unparseable_canonical_soil_row(self):
        """Unsafe database values must remain absent rather than become facts."""

        class Completed:
            returncode = 0
            stderr = ""

            def __init__(self, stdout):
                self.stdout = stdout

        reader = events._opengauss_latest_soil_reading
        for row in (
            "2026-09-21 01:53:00+00|5|23.2|610\n",
            "2026-09-21 01:53:00+00|35|46|610\n",
            "not-a-timestamp|35|23.2|610\n",
        ):
            with self.subTest(row=row):
                reading = reader("soil3", runner=lambda *args, **kwargs: Completed(row))
                self.assertIsNone(reading)

    def test_snapshot_uses_valid_canonical_soil_fact(self):
        """The state producer must use the database fact, not a local substitute."""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._snapshot(
                Path(directory),
                canonical_reading={
                    "timestamp": "2026-09-21 01:53:00+00",
                    "humidity": 35.0,
                    "temperature": 23.2,
                    "ec_raw": 610.0,
                },
            )
        state = StateBuilder("soil3").build_from_health_snapshot(snapshot)
        self.assertEqual(35.0, state["soil"]["humidity_percent"])
        self.assertEqual(
            "2026-09-21 01:53:00+00",
            state["source_timestamps"]["soil"],
        )

    def test_snapshot_keeps_soil_missing_when_canonical_query_has_no_fact(self):
        """A stale local CSV must not replace a missing canonical database fact."""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._snapshot(
                Path(directory),
                canonical_reading=None,
                local_sensor_content=(
                    "timestamp,humidity,temperature,ec_raw\n"
                    "2026-09-21T01:53:00Z,35,23.2,610\n"
                ),
            )
        state = StateBuilder("soil3").build_from_health_snapshot(snapshot)
        self.assertIsNone(state["soil"]["humidity_percent"])
        self.assertIsNone(state["data_quality"]["soil_age_sec"])


class PipelineTests(StateProducerTests):
    def test_pipeline_cli_requires_explicit_config_and_reports_non_execution(self):
        """Fails if the pipeline silently acquires a config or claims physical work."""
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
                summary = service.run(["pipeline", "--config", str(config_path)])

            self.assertEqual("deny", summary["gate_decision"])
            self.assertEqual("skipped_due_to_gate_deny", summary["runner_status"])
            self.assertEqual(
                {"physical_actions_performed": False, "phase3_called": False},
                summary["execution"],
            )

    def test_gate_deny_skips_runner_and_closes_episode_with_missing_facts(self):
        """Fails if a deny is ever treated as permission to start dry-run work."""
        from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig, run_pipeline
        from services.soil3.episode.episode_v1 import EpisodeStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RuntimeConfig.from_dict(self.runtime_config_value(root))
            with mock.patch(
                "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                return_value=self.health_snapshot(),
            ):
                record = run_pipeline(config)

            self.assertEqual("offline_fixture", record["provider_mode"])
            self.assertEqual("deny", record["gate_decision"])
            self.assertEqual("skipped_due_to_gate_deny", record["runner_status"])
            self.assertIsNone(record["runner_path"])
            self.assertEqual(
                {"physical_actions_performed": False, "phase3_called": False},
                record["execution"],
            )
            episode = EpisodeStore(config.episode_dir).read(record["episode_id"])
            self.assertEqual("closed", episode["status"])
            self.assertIn("executed_actions", episode["missing_facts"])
            self.assertIn("feedback", episode["missing_facts"])
            self.assertIn("outcome", episode["missing_facts"])

    def test_gate_allow_records_runner_dry_run_without_physical_action(self):
        """Fails if an admitted proposal can gain a Phase3 or physical side effect."""
        from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig, run_pipeline
        from services.soil3.episode.episode_v1 import EpisodeStore

        allowed_gate = {
            "schema_version": "gate.v1",
            "decision": "allow",
            "reason_codes": [],
            "warning_codes": [],
            "execution": {
                "mode": "admission_only",
                "actuator_commands_allowed": False,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RuntimeConfig.from_dict(self.runtime_config_value(root))
            with mock.patch(
                "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                return_value=self.health_snapshot(),
            ), mock.patch(
                "services.soil3.agent_runtime.runtime_v1.evaluate_gate",
                return_value=allowed_gate,
            ):
                record = run_pipeline(config)

            runner = json.loads(Path(record["runner_path"]).read_text(encoding="utf-8"))
            self.assertEqual("dry_run", runner["mode"])
            self.assertFalse(runner["execution"]["physical_actions_performed"])
            self.assertFalse(runner["execution"]["phase3_called"])
            episode = EpisodeStore(config.episode_dir).read(record["episode_id"])
            self.assertNotIn("executed_actions", episode["missing_facts"])
            self.assertEqual("dry_run_stop", episode["executed_actions"][0]["kind"])


if __name__ == "__main__":
    unittest.main()
