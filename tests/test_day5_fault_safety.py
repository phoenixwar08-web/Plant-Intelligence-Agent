import ast
import copy
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig, run_pipeline
from services.soil3.cloud_gate.gate_v2 import evaluate_gate_v2
from services.soil3.cloud_strategy.validator import PROMPT_VERSION, fingerprint
from services.soil3.runner.runner_v1 import DryRunRunner, RunnerStore
from services.soil3.state.state_v1 import PHASE3_SAFETY_FLAG_KEYS
from services.soil3.trace.trace_v1 import TraceError, TraceStore


ROOT = Path(__file__).resolve().parents[1]
NO_EXECUTION = {"phase3_called": False, "physical_actions_performed": False}


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def complete_safety_flags():
    flags = {name: False for name in PHASE3_SAFETY_FLAG_KEYS}
    flags["predictor_circuit"] = {"state": "CLOSED", "active": False}
    return flags


def health_snapshot(*, soil_age_seconds=1, include_sensor=True, complete_safety=True):
    now = datetime.now(timezone.utc)
    system = complete_safety_flags() if complete_safety else {"pump_active": False}
    system["pump_active"] = False
    return {
        "observed_at": utc_text(now),
        "state_file": {"age_seconds": 1.0},
        "sensor_readings": (
            [{
                "timestamp": utc_text(now - timedelta(seconds=soil_age_seconds)),
                "humidity": 31.5,
                "temperature": 23.0,
                "ec_raw": 500.0,
            }]
            if include_sensor
            else []
        ),
        "watering_history": [],
        "system_state": system,
        "environment": {"air": {"age_seconds": 9.0}},
    }


def runtime_config(root):
    return RuntimeConfig.from_dict({
        "device_code": "soil3",
        "provider_mode": "offline_fixture",
        "provider": {},
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
    })


def admitted_gate(state, strategy, policy, exploration_requested):
    gate = evaluate_gate_v2(state, strategy, policy, exploration_requested)
    gate["decision"] = "allow"
    gate["reason_codes"] = []
    gate["warning_codes"] = []
    return gate


def runner_state():
    return {
        "device_code": "soil3",
        "observed_at": "2026-09-24T08:00:00Z",
    }


def runner_strategy(state):
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "device_code": "soil3",
        "state_observed_at": state["observed_at"],
        "state_generated_at": "2026-09-24T08:00:01Z",
        "state_sha256": fingerprint(state),
        "created_at": "2026-09-24T08:00:02Z",
        "actions": [
            {"action_id": "water", "type": "water", "pump_seconds": 2},
            {"action_id": "wait", "type": "wait", "seconds": 10},
            {"action_id": "stop", "type": "stop"},
        ],
        "reason_summary": ["fault test fixture"],
        "expected_outcome": {"soil_moisture": "increase", "risk_notes": []},
        "confidence": 0.8,
        "model": {
            "provider": "test",
            "name": "fixture",
            "prompt_version": PROMPT_VERSION,
        },
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 9, 24, 8, 0, 3, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class Day5FaultSafetyTests(unittest.TestCase):
    def assert_stopped_before_bridge(self, record):
        self.assertIsNone(record["runner_path"])
        self.assertIsNone(record["bridge_path"])
        self.assertEqual(NO_EXECUTION, record["execution"])

    def test_stale_missing_sensor_and_incomplete_safety_facts_fail_closed(self):
        cases = {
            "stale_sensor": health_snapshot(soil_age_seconds=18001),
            "missing_sensor": health_snapshot(include_sensor=False),
            "incomplete_safety": health_snapshot(complete_safety=False),
        }
        for label, snapshot in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                config = runtime_config(Path(directory))
                with mock.patch(
                    "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                    return_value=snapshot,
                ):
                    record = run_pipeline(config)

                self.assertEqual("deny", record["gate_decision"])
                self.assertEqual("skipped_due_to_gate_deny", record["runner_status"])
                self.assertEqual("not_started_gate_deny", record["bridge_status"])
                self.assert_stopped_before_bridge(record)
                trace = TraceStore(config.trace_dir).read(record["trace_id"])
                self.assertEqual("deny", trace["decision"]["gate_decision"])
                self.assertEqual(NO_EXECUTION, trace["execution"])

    def test_vision_and_experience_absence_is_explicit_and_never_fabricated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "traces")
            trace_id = store.create()["trace_id"]
            updated = store.set_decision(
                trace_id,
                vision={"availability": "unavailable", "ref": None},
                experience={"availability": "not_requested", "ref": None},
            )

            self.assertEqual(
                {"availability": "unavailable", "ref": None},
                updated["decision"]["vision"],
            )
            self.assertEqual(
                {"availability": "not_requested", "ref": None},
                updated["decision"]["experience"],
            )
            self.assertEqual(NO_EXECUTION, updated["execution"])

    def test_model_timeout_failure_invalid_json_and_validator_rejection_stop_early(self):
        cases = {
            "timeout": {
                "cloud": {"provider": "qwen_dashscope", "model": "qwen3.8-Flash"},
                "failure": {"code": "model_timeout", "message": "timeout", "retryable": True},
                "validation": {"accepted": False, "reason_codes": ["model_timeout"], "strategy": None},
            },
            "invalid_json": {
                "cloud": {"provider": "qwen_dashscope", "model": "qwen3.8-Flash"},
                "parse_error": {"code": "invalid_model_json", "message": "bad json"},
                "validation": {"accepted": False, "reason_codes": ["invalid_model_json"], "strategy": None},
            },
            "validator_rejected": {
                "cloud": {"provider": "qwen_dashscope", "model": "qwen3.8-Flash"},
                "validation": {"accepted": False, "reason_codes": ["unknown_action_type"], "strategy": None},
            },
        }
        for label, result in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                config = runtime_config(Path(directory))
                with mock.patch(
                    "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                    return_value=health_snapshot(),
                ), mock.patch(
                    "services.soil3.agent_runtime.runtime_v1.run_chain",
                    return_value=result,
                ):
                    with self.assertRaisesRegex(RuntimeError, "strategy was rejected"):
                        run_pipeline(config)

                records = list(config.runs_dir.glob("*.json"))
                self.assertEqual(1, len(records))
                record = json.loads(records[0].read_text(encoding="utf-8"))
                self.assertEqual("not_started_provider_or_validation_failure", record["runner_status"])
                self.assertEqual("not_started_provider_or_validation_failure", record["bridge_status"])
                self.assert_stopped_before_bridge(record)
                trace = TraceStore(config.trace_dir).read(record["trace_id"])
                self.assertIsNone(trace["decision"]["strategy_ref"])
                self.assertIsNone(trace["decision"]["gate_ref"])
                self.assertIsNone(trace["decision"]["bridge_ref"])

    def test_gate_deny_and_strategy_hash_mismatch_do_not_reach_bridge(self):
        def mismatched_gate(state, strategy, policy, exploration_requested):
            gate = admitted_gate(state, strategy, policy, exploration_requested)
            gate["strategy_sha256"] = "0" * 64
            return gate

        for label, evaluator in (
            ("deny", evaluate_gate_v2),
            ("hash_mismatch", mismatched_gate),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                config = runtime_config(Path(directory))
                with mock.patch(
                    "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                    return_value=(
                        health_snapshot(soil_age_seconds=18001)
                        if label == "deny"
                        else health_snapshot()
                    ),
                ), mock.patch(
                    "services.soil3.agent_runtime.runtime_v1.evaluate_gate_v2",
                    side_effect=evaluator,
                ):
                    record = run_pipeline(config)

                self.assert_stopped_before_bridge(record)
                self.assertNotEqual("verified", record["bridge_status"])

    def test_runner_hash_mismatch_and_non_dry_run_are_rejected_by_bridge(self):
        original_run = DryRunRunner.run

        def tampered(kind):
            def run(runner, strategy, state):
                result = original_run(runner, strategy, state)
                changed = copy.deepcopy(result)
                if kind == "hash":
                    changed["strategy_sha256"] = "0" * 64
                else:
                    changed["mode"] = "live"
                return changed

            return run

        for label in ("hash", "mode"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                config = runtime_config(Path(directory))
                with mock.patch(
                    "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                    return_value=health_snapshot(),
                ), mock.patch(
                    "services.soil3.agent_runtime.runtime_v1.evaluate_gate_v2",
                    side_effect=admitted_gate,
                ), mock.patch(
                    "services.soil3.agent_runtime.runtime_v1.DryRunRunner.run",
                    new=tampered(label),
                ):
                    with self.assertRaisesRegex(RuntimeError, "bridge verification"):
                        run_pipeline(config)

                record_path = next(config.runs_dir.glob("*.json"))
                record = json.loads(record_path.read_text(encoding="utf-8"))
                self.assertEqual("rejected", record["bridge_status"])
                self.assertEqual(NO_EXECUTION, record["execution"])
                bridge = json.loads(Path(record["bridge_path"]).read_text(encoding="utf-8"))
                self.assertFalse(bridge["accepted"])
                self.assertEqual(NO_EXECUTION, {
                    "phase3_called": bridge["execution"]["phase3_called"],
                    "physical_actions_performed": bridge["execution"]["physical_actions_performed"],
                })

    def test_invalid_trace_stops_before_strategy_gate_runner_and_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_config(Path(directory))
            with mock.patch(
                "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                return_value=health_snapshot(),
            ), mock.patch(
                "services.soil3.agent_runtime.runtime_v1.TraceStore.set_decision",
                side_effect=TraceError("trace_invalid"),
            ), mock.patch(
                "services.soil3.agent_runtime.runtime_v1.run_chain"
            ) as strategy_call, mock.patch(
                "services.soil3.agent_runtime.runtime_v1.evaluate_gate_v2"
            ) as gate_call, mock.patch(
                "services.soil3.agent_runtime.runtime_v1.Phase3Bridge.verify"
            ) as bridge_call:
                with self.assertRaises(TraceError):
                    run_pipeline(config)

            strategy_call.assert_not_called()
            gate_call.assert_not_called()
            bridge_call.assert_not_called()
            self.assertFalse(config.runner_dir.exists())
            self.assertFalse(config.bridge_dir.exists())
            trace_path = next(config.trace_dir.glob("*.json"))
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            self.assertEqual(NO_EXECUTION, trace["execution"])

    def test_restart_duplicate_request_and_interruption_do_not_duplicate_water(self):
        with tempfile.TemporaryDirectory() as directory:
            state = runner_state()
            strategy = runner_strategy(state)
            store = RunnerStore(Path(directory) / "runner")
            clock = MutableClock()

            interrupted = DryRunRunner(store, clock=clock).run(strategy, state)
            self.assertEqual("waiting", interrupted["status"])
            repeated = DryRunRunner(store, clock=clock).run(strategy, state)
            self.assertEqual(interrupted, repeated)

            clock.advance(10)
            recovered = DryRunRunner(store, clock=clock).resume(strategy["strategy_id"])
            replayed = DryRunRunner(store, clock=clock).run(strategy, state)
            self.assertEqual("stopped", recovered["status"])
            self.assertEqual(recovered, replayed)
            water_results = [
                step["result"]
                for step in replayed["steps"]
                if isinstance(step.get("result"), dict)
                and step["result"].get("kind") == "dry_run_water"
            ]
            self.assertEqual(1, len(water_results))
            self.assertFalse(water_results[0]["physical_action_performed"])
            self.assertEqual(NO_EXECUTION, replayed["execution"])

    def test_fault_suite_and_runtime_have_no_control_path_imports(self):
        paths = (
            ROOT / "services" / "soil3" / "agent_runtime" / "runtime_v1.py",
            Path(__file__),
        )
        imported = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
        forbidden = (
            "services.soil3.phase3",
            "services.soil3.phase1",
            "paho.mqtt",
        )
        self.assertFalse([
            name
            for name in imported
            if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
        ])


if __name__ == "__main__":
    unittest.main()
