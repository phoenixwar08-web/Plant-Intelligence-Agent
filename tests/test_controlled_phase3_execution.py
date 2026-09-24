import ast
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.soil3.controlled_execution import (
    ControlledExecutionError,
    ControlledPhase3Executor,
)
from services.soil3.phase3_bridge.bridge_v1 import FORMAL_PHASE3_ENTRYPOINT


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
NO_EXECUTION = {
    "mode": "verification_only",
    "phase3_called": False,
    "physical_actions_performed": False,
}


def bridge_response():
    return {
        "schema_version": "phase3_bridge_response.v1",
        "accepted": True,
        "checked_at": "2026-09-24T07:59:00Z",
        "reason_codes": [],
        "handoff": {
            "schema_version": "phase3_bridge_request.v1",
            "request_id": str(uuid.uuid4()),
            "created_at": "2026-09-24T07:59:00Z",
            "device_code": "soil3",
            "operation": "run_cycle",
            "bindings": {
                "state_sha256": "1" * 64,
                "strategy_id": str(uuid.uuid4()),
                "strategy_sha256": "2" * 64,
                "gate_id": str(uuid.uuid4()),
            },
            "phase3_interface": {
                "entrypoint": FORMAL_PHASE3_ENTRYPOINT,
                "arguments": [],
            },
            "execution": dict(NO_EXECUTION),
        },
        "execution": dict(NO_EXECUTION),
    }


def approval(bridge):
    handoff = bridge["handoff"]
    return {
        "schema_version": "controlled_scenario_approval.v1",
        "approval_id": str(uuid.uuid4()),
        "scenario_id": "soil3-conservative-observation-001",
        "device_code": "soil3",
        "approved_by": "owner",
        "approved_at": "2026-09-24T07:55:00Z",
        "expires_at": "2026-09-24T08:05:00Z",
        "scope": "phase3_run_cycle_once",
        "max_runs": 1,
        "bridge_request_id": handoff["request_id"],
        "bridge_bindings": dict(handoff["bindings"]),
        "trace_id": "tr-" + "a" * 24,
        "episode_id": "ep-" + "b" * 24,
    }


def phase3_result(action_sec=0.0):
    return SimpleNamespace(
        zone=SimpleNamespace(name="SAFE_SLEEP"),
        chosen_plan=(
            SimpleNamespace(label="phase3_safe_sleep") if action_sec else None
        ),
        action_sec=action_sec,
        notes="Phase3 final decision",
        reading=SimpleNamespace(humidity=42.0, temperature=24.0, ec_raw=510.0),
    )


class ControlledPhase3ExecutionTests(unittest.TestCase):
    def executor(self, directory):
        return ControlledPhase3Executor(Path(directory) / "receipts", clock=lambda: NOW)

    def test_missing_expired_or_mismatched_approval_never_calls_phase3(self):
        bridge = bridge_response()
        valid = approval(bridge)
        cases = {
            "missing": None,
            "expired": {**valid, "expires_at": "2026-09-24T07:59:59Z"},
            "future": {**valid, "approved_at": "2026-09-24T08:01:00Z"},
            "too_long": {**valid, "expires_at": "2026-09-24T08:20:01Z"},
            "wrong_request": {**valid, "bridge_request_id": str(uuid.uuid4())},
            "wrong_bindings": {**valid, "bridge_bindings": {**valid["bridge_bindings"], "state_sha256": "0" * 64}},
            "multiple_runs": {**valid, "max_runs": 2},
        }
        for label, candidate in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                phase3 = mock.Mock(return_value=phase3_result())
                with self.assertRaises(ControlledExecutionError):
                    self.executor(directory).execute(bridge, candidate, phase3)
                phase3.assert_not_called()
                self.assertEqual([], list((Path(directory) / "receipts").glob("*.json")))

    def test_rejected_or_mutated_bridge_never_calls_phase3(self):
        base = bridge_response()
        cases = {}
        rejected = {**base, "accepted": False, "reason_codes": ["gate_not_admitted"], "handoff": None}
        cases["rejected"] = rejected
        with_arguments = json.loads(json.dumps(base))
        with_arguments["handoff"]["phase3_interface"]["arguments"] = ["water", 10]
        cases["arguments"] = with_arguments
        physical_claim = json.loads(json.dumps(base))
        physical_claim["handoff"]["execution"]["phase3_called"] = True
        cases["physical_claim"] = physical_claim

        for label, candidate in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                phase3 = mock.Mock(return_value=phase3_result())
                with self.assertRaises(ControlledExecutionError):
                    self.executor(directory).execute(candidate, approval(base), phase3)
                phase3.assert_not_called()

    def test_owner_approved_bridge_calls_only_zero_argument_phase3_and_records_result(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = bridge_response()
            permit = approval(bridge)
            phase3 = mock.Mock(return_value=phase3_result(0.0))
            receipt = self.executor(directory).execute(bridge, permit, phase3)

            phase3.assert_called_once_with()
            self.assertEqual("completed", receipt["status"])
            self.assertTrue(receipt["phase3_called"])
            self.assertFalse(receipt["physical_actions_performed"])
            self.assertEqual("SAFE_SLEEP", receipt["phase3_decision"]["zone"])
            self.assertEqual(0.0, receipt["phase3_decision"]["action_sec"])
            self.assertEqual("pending", receipt["feedback_entry"]["status"])
            stored = self.executor(directory).read(permit["approval_id"])
            self.assertEqual(receipt, stored)

    def test_phase3_positive_action_is_reported_as_physical_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = bridge_response()
            permit = approval(bridge)
            receipt = self.executor(directory).execute(
                bridge,
                permit,
                lambda: phase3_result(2.0),
            )
            self.assertTrue(receipt["physical_actions_performed"])
            self.assertEqual(2.0, receipt["phase3_decision"]["action_sec"])

    def test_approval_is_at_most_once_across_restart_and_duplicate_request(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = bridge_response()
            permit = approval(bridge)
            phase3 = mock.Mock(return_value=phase3_result())
            self.executor(directory).execute(bridge, permit, phase3)

            restarted = self.executor(directory)
            with self.assertRaisesRegex(ControlledExecutionError, "approval_already_consumed"):
                restarted.execute(bridge, permit, phase3)
            phase3.assert_called_once_with()

    def test_phase3_error_is_recorded_unknown_and_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = bridge_response()
            permit = approval(bridge)
            phase3 = mock.Mock(side_effect=RuntimeError("hardware status unavailable"))
            executor = self.executor(directory)
            with self.assertRaisesRegex(ControlledExecutionError, "phase3_cycle_failed"):
                executor.execute(bridge, permit, phase3)

            receipt = executor.read(permit["approval_id"])
            self.assertEqual("phase3_error", receipt["status"])
            self.assertTrue(receipt["phase3_called"])
            self.assertIsNone(receipt["physical_actions_performed"])
            self.assertEqual("RuntimeError", receipt["error_type"])
            with self.assertRaisesRegex(ControlledExecutionError, "approval_already_consumed"):
                executor.execute(bridge, permit, phase3)
            phase3.assert_called_once_with()

    def test_invalid_phase3_result_is_recorded_unknown_and_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = bridge_response()
            permit = approval(bridge)
            executor = self.executor(directory)
            with self.assertRaisesRegex(ControlledExecutionError, "phase3_result_invalid"):
                executor.execute(
                    bridge,
                    permit,
                    lambda: SimpleNamespace(
                        zone=SimpleNamespace(name="SAFE_SLEEP"),
                        chosen_plan=None,
                        action_sec=-1,
                        notes="invalid fixture",
                        reading=SimpleNamespace(
                            humidity=42.0,
                            temperature=24.0,
                            ec_raw=510.0,
                        ),
                    ),
                )
            receipt = executor.read(permit["approval_id"])
            self.assertEqual("phase3_result_invalid", receipt["status"])
            self.assertTrue(receipt["phase3_called"])
            self.assertIsNone(receipt["physical_actions_performed"])
            with self.assertRaisesRegex(ControlledExecutionError, "approval_already_consumed"):
                executor.execute(bridge, permit, lambda: phase3_result())

    def test_claim_written_before_phase3_call_survives_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = bridge_response()
            permit = approval(bridge)
            receipt_path = Path(directory) / "receipts" / f'{permit["approval_id"]}.json'

            def interrupted():
                claimed = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertEqual("started", claimed["status"])
                self.assertTrue(claimed["phase3_called"])
                self.assertIsNone(claimed["physical_actions_performed"])
                raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                self.executor(directory).execute(bridge, permit, interrupted)
            self.assertTrue(receipt_path.exists())
            with self.assertRaisesRegex(ControlledExecutionError, "approval_already_consumed"):
                self.executor(directory).execute(bridge, permit, lambda: phase3_result())

    def test_adapter_has_no_direct_actuator_mqtt_or_manual_water_dependency(self):
        source_path = (
            ROOT
            / "services"
            / "soil3"
            / "controlled_execution"
            / "controlled_v1.py"
        )
        source = source_path.read_text(encoding="utf-8")
        imports = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        forbidden = (
            "services.soil3.phase3",
            "services.soil3.phase1",
            "paho.mqtt",
        )
        self.assertFalse([
            name
            for name in imports
            if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
        ])
        symbols = {
            node.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Name)
        } | {
            node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute)
        }
        self.assertTrue({"manual_water", "publish", "ActuatorLayer"}.isdisjoint(symbols))


if __name__ == "__main__":
    unittest.main()
