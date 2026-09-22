import copy
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services.soil3.cloud_gate.gate_v1 import GatePolicy, evaluate_gate
from services.soil3.cloud_gate.gate_v2 import evaluate_gate_v2
from services.soil3.cloud_strategy.validator import PROMPT_VERSION, fingerprint
from services.soil3.phase3_bridge.bridge_v1 import FORMAL_PHASE3_ENTRYPOINT, Phase3Bridge
from services.soil3.runner.runner_v1 import DryRunRunner, RunnerStore
from services.soil3.state.state_v1 import PHASE3_SAFETY_FLAG_KEYS


ROOT = Path(__file__).resolve().parents[1]


def state_snapshot():
    flags = {key: False for key in PHASE3_SAFETY_FLAG_KEYS}
    flags["predictor_circuit"] = {"state": "CLOSED", "active": False}
    return {
        "schema_version": "state.v1",
        "device_code": "soil3",
        "observed_at": "2026-09-21T08:00:00Z",
        "generated_at": "2026-09-21T08:00:01Z",
        "soil": {"humidity_percent": 30.0},
        "data_quality": {"soil_age_sec": 1.0, "phase3_state_age_sec": 1.0},
        "irrigation": {"pump_active": False},
        "safety": {"flags": flags},
    }


def strategy_for(state, actions=None):
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "device_code": "soil3",
        "state_observed_at": state["observed_at"],
        "state_generated_at": state["generated_at"],
        "state_sha256": fingerprint(state),
        "created_at": "2026-09-21T08:00:02Z",
        "actions": actions or [
            {"action_id": "a1", "type": "water", "pump_seconds": 5},
            {"action_id": "a2", "type": "observe"},
        ],
        "reason_summary": ["offline fixture"],
        "expected_outcome": {"soil_moisture": "increase", "risk_notes": []},
        "confidence": 0.8,
        "model": {"provider": "test", "name": "fixture", "prompt_version": PROMPT_VERSION},
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }


def policy():
    return GatePolicy(
        warning_age_seconds=60.0,
        deny_age_seconds=120.0,
        window_seconds=86400.0,
        max_exploration_water_seconds=20.0,
    )


class FixedClock:
    def __call__(self):
        return datetime(2026, 9, 21, 8, 0, 3, tzinfo=timezone.utc)


class Phase3BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = state_snapshot()
        self.strategy = strategy_for(self.state)
        self.gate = evaluate_gate_v2(self.state, self.strategy, policy(), exploration_requested=False)
        self.runner = DryRunRunner(
            RunnerStore(self.root / "runner"), clock=FixedClock()
        ).run(self.strategy, self.state)

    def verify(self, gate=None, runner=None, strategy=None, state=None):
        return Phase3Bridge().verify(
            self.state if state is None else state,
            self.strategy if strategy is None else strategy,
            self.gate if gate is None else gate,
            self.runner if runner is None else runner,
        )

    def test_valid_chain_produces_only_zero_argument_formal_handoff(self):
        response = self.verify()

        self.assertTrue(response["accepted"])
        self.assertEqual(response["reason_codes"], [])
        handoff = response["handoff"]
        self.assertEqual(handoff["schema_version"], "phase3_bridge_request.v1")
        self.assertEqual(handoff["operation"], "run_cycle")
        self.assertEqual(handoff["phase3_interface"], {
            "entrypoint": FORMAL_PHASE3_ENTRYPOINT,
            "arguments": [],
        })
        self.assertEqual(handoff["bindings"]["state_sha256"], fingerprint(self.state))
        self.assertEqual(handoff["bindings"]["strategy_sha256"], fingerprint(self.strategy))
        self.assertNotIn("actions", handoff)
        self.assertNotIn("pump_seconds", json.dumps(handoff))
        self.assertEqual(response["execution"], {
            "mode": "verification_only",
            "phase3_called": False,
            "physical_actions_performed": False,
        })

    def test_gate_deny_cannot_produce_handoff(self):
        denied = copy.deepcopy(self.gate)
        denied["decision"] = "deny"
        denied["reason_codes"] = ["pump_active"]
        response = self.verify(gate=denied)
        self.assertFalse(response["accepted"])
        self.assertIsNone(response["handoff"])
        self.assertIn("gate_not_admitted", response["reason_codes"])
        self.assertIn("gate_has_denial_reasons", response["reason_codes"])

    def test_gate_binding_and_admission_metadata_cannot_be_forged(self):
        forged = copy.deepcopy(self.gate)
        forged["state_sha256"] = "0" * 64
        forged["strategy_id"] = str(uuid.uuid4())
        forged["execution"]["actuator_commands_allowed"] = True
        forged["unexpected_route"] = "anything"
        response = self.verify(gate=forged)
        for reason in (
            "gate_shape_invalid",
            "gate_strategy_mismatch",
            "gate_state_hash_mismatch",
            "gate_execution_boundary_invalid",
        ):
            self.assertIn(reason, response["reason_codes"])

    def test_gate_v1_is_rejected_without_strategy_id_compatibility_fallback(self):
        legacy_gate = evaluate_gate(
            self.state, self.strategy, policy(), exploration_requested=False
        )

        response = self.verify(gate=legacy_gate)

        self.assertFalse(response["accepted"])
        self.assertIn("gate_schema_invalid", response["reason_codes"])

    def test_gate_approved_water_seconds_cannot_change_with_same_strategy_id(self):
        changed = copy.deepcopy(self.strategy)
        changed["actions"][0]["pump_seconds"] = 6
        changed_runner = DryRunRunner(
            RunnerStore(self.root / "water-seconds-tamper"), clock=FixedClock()
        ).run(changed, self.state)

        response = self.verify(strategy=changed, runner=changed_runner)

        self.assertFalse(response["accepted"])
        self.assertIn("gate_strategy_hash_mismatch", response["reason_codes"])

    def test_gate_approved_strategy_metadata_cannot_change_with_same_strategy_id(self):
        changed = copy.deepcopy(self.strategy)
        changed["reason_summary"] = ["changed but still valid"]
        changed_runner = DryRunRunner(
            RunnerStore(self.root / "metadata-tamper"), clock=FixedClock()
        ).run(changed, self.state)

        response = self.verify(strategy=changed, runner=changed_runner)

        self.assertFalse(response["accepted"])
        self.assertIn("gate_strategy_hash_mismatch", response["reason_codes"])

    def test_runner_must_be_terminal_bound_and_entirely_nonphysical(self):
        forged = copy.deepcopy(self.runner)
        forged["strategy_sha256"] = "0" * 64
        forged["status"] = "running"
        forged["execution"]["phase3_called"] = True
        forged["steps"][0]["result"]["physical_action_performed"] = True
        response = self.verify(runner=forged)
        for reason in (
            "runner_strategy_hash_mismatch",
            "runner_execution_boundary_invalid",
            "runner_not_terminal",
            "runner_step_result_invalid:0",
        ):
            self.assertIn(reason, response["reason_codes"])

    def test_runner_action_copy_cannot_be_changed_after_validation(self):
        forged = copy.deepcopy(self.runner)
        forged["steps"][0]["action"]["pump_seconds"] = 100
        forged["steps"][0]["result"]["pump_seconds"] = 100.0
        response = self.verify(runner=forged)
        self.assertFalse(response["accepted"])
        self.assertIn("runner_step_binding_mismatch:0", response["reason_codes"])

    def test_dry_run_wait_cannot_claim_a_physical_action(self):
        wait_strategy = strategy_for(
            self.state,
            [{"action_id": "wait", "type": "wait", "seconds": 5}],
        )
        wait_gate = evaluate_gate_v2(
            self.state, wait_strategy, policy(), exploration_requested=False
        )
        instants = iter(
            [
                datetime(2026, 9, 21, 8, 0, 3, tzinfo=timezone.utc),
                datetime(2026, 9, 21, 8, 0, 3, tzinfo=timezone.utc),
                datetime(2026, 9, 21, 8, 0, 9, tzinfo=timezone.utc),
            ]
        )
        forged_runner = DryRunRunner(
            RunnerStore(self.root / "wait-physical-forgery"), clock=lambda: next(instants)
        )
        forged_runner.run(wait_strategy, self.state)
        forged_runner = forged_runner.resume(wait_strategy["strategy_id"])
        forged_runner["steps"][0]["result"]["physical_action_performed"] = True

        response = self.verify(gate=wait_gate, runner=forged_runner, strategy=wait_strategy)

        self.assertFalse(response["accepted"])
        self.assertIn("runner_step_result_invalid:0", response["reason_codes"])

    def test_invalid_strategy_is_rejected_even_with_matching_forged_hashes(self):
        changed = copy.deepcopy(self.strategy)
        changed["actions"][0]["pump_seconds"] = 121
        forged_gate = copy.deepcopy(self.gate)
        forged_gate["strategy_id"] = changed["strategy_id"]
        forged_runner = copy.deepcopy(self.runner)
        forged_runner["strategy_sha256"] = fingerprint(changed)
        forged_runner["steps"][0]["action"] = copy.deepcopy(changed["actions"][0])
        forged_runner["steps"][0]["result"]["pump_seconds"] = 121.0
        response = self.verify(strategy=changed, gate=forged_gate, runner=forged_runner)
        self.assertFalse(response["accepted"])
        self.assertTrue(any(reason.startswith("strategy_invalid:") for reason in response["reason_codes"]))

    def test_unreserved_exploration_is_rejected(self):
        forged = copy.deepcopy(self.gate)
        forged["budget"].update({
            "exploration_requested": True,
            "requested_water_seconds": 5.0,
            "reserved_water_seconds": 0.0,
            "remaining_water_seconds": 20.0,
            "reservation_id": None,
        })
        response = self.verify(gate=forged)
        self.assertIn("gate_exploration_not_reserved", response["reason_codes"])

    def test_malformed_numeric_fields_fail_closed_without_exception(self):
        forged_gate = copy.deepcopy(self.gate)
        forged_gate["budget"]["reserved_water_seconds"] = "not-a-number"
        gate_response = self.verify(gate=forged_gate)
        self.assertFalse(gate_response["accepted"])
        self.assertIn("gate_budget_invalid", gate_response["reason_codes"])

        changed_strategy = copy.deepcopy(self.strategy)
        changed_strategy["actions"][0]["pump_seconds"] = "not-a-number"
        forged_runner = copy.deepcopy(self.runner)
        forged_runner["strategy_sha256"] = fingerprint(changed_strategy)
        forged_runner["steps"][0]["action"] = copy.deepcopy(changed_strategy["actions"][0])
        runner_response = self.verify(strategy=changed_strategy, runner=forged_runner)
        self.assertFalse(runner_response["accepted"])

    def test_verification_does_not_import_or_call_phase3(self):
        source = (ROOT / "services" / "soil3" / "phase3_bridge" / "bridge_v1.py").read_text(encoding="utf-8")
        self.assertNotIn("from services.soil3.phase3", source)
        self.assertNotIn("import services.soil3.phase3", source)
        before = copy.deepcopy((self.state, self.strategy, self.gate, self.runner))
        response = self.verify()
        self.assertTrue(response["accepted"])
        self.assertEqual((self.state, self.strategy, self.gate, self.runner), before)
        self.assertFalse(response["execution"]["phase3_called"])

    def test_cli_returns_zero_for_accept_and_two_for_reject(self):
        paths = {}
        for name, value in (
            ("state", self.state), ("strategy", self.strategy),
            ("gate", self.gate), ("runner", self.runner),
        ):
            path = self.root / f"{name}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            paths[name] = path
        command = [
            sys.executable, "-m", "services.soil3.phase3_bridge.service",
            "--state", str(paths["state"]), "--strategy", str(paths["strategy"]),
            "--gate", str(paths["gate"]), "--runner", str(paths["runner"]),
        ]
        accepted = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(json.loads(accepted.stdout)["accepted"])

        denied = copy.deepcopy(self.gate)
        denied["decision"] = "deny"
        denied["reason_codes"] = ["pump_active"]
        paths["gate"].write_text(json.dumps(denied), encoding="utf-8")
        rejected = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(rejected.returncode, 2, rejected.stderr)
        self.assertFalse(json.loads(rejected.stdout)["accepted"])


if __name__ == "__main__":
    unittest.main()
