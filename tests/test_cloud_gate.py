import tempfile
import unittest
import uuid
import json
from pathlib import Path
from unittest.mock import patch

from services.soil3.cloud_gate.budget import BudgetLedger, BudgetLedgerError
from services.soil3.cloud_gate import service
from services.soil3.cloud_gate.gate_v1 import GatePolicy, evaluate_gate
from services.soil3.cloud_strategy.validator import PROMPT_VERSION, fingerprint
from services.soil3.state.state_v1 import StateBuilder


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


def fresh_state(*, soil_age_sec=60, phase3_state_age_sec=60, flags=None):
    phase3_flags = {**SAFE_PHASE3_FLAGS, **(flags or {})}
    state = StateBuilder("soil3").build(
        {
            "observed_at": "2026-09-20T10:00:00Z",
            "generated_at": "2026-09-20T10:00:00Z",
            "sensor_readings": [
                {"timestamp": "2026-09-20T09:59:00Z", "humidity": 31.4, "temperature": 24.1}
            ],
            "source_timestamps": {"phase3_state": "2026-09-20T09:59:00Z"},
            "system_state": phase3_flags,
        }
    )
    state["data_quality"]["soil_age_sec"] = soil_age_sec
    state["data_quality"]["phase3_state_age_sec"] = phase3_state_age_sec
    return state


def valid_strategy(state, actions=None):
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "device_code": "soil3",
        "state_observed_at": state["observed_at"],
        "state_generated_at": state["generated_at"],
        "state_sha256": fingerprint(state),
        "created_at": "2026-09-20T10:00:00Z",
        "actions": actions or [{"action_id": "observe", "type": "observe"}],
        "reason_summary": ["observe current state"],
        "expected_outcome": {"soil_moisture": "observe", "risk_notes": []},
        "confidence": 0.8,
        "model": {"provider": "fixture", "name": "fixture", "prompt_version": PROMPT_VERSION},
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }


def policy(limit=10):
    return GatePolicy.from_dict(
        {
            "warning_age_seconds": 900,
            "deny_age_seconds": 18000,
            "window_seconds": 86400,
            "max_exploration_water_seconds": limit,
        }
    )


class CloudGatePolicyTests(unittest.TestCase):
    def test_policy_rejects_unordered_freshness_bounds_and_negative_budget(self):
        with self.assertRaisesRegex(ValueError, "warning_age_seconds"):
            GatePolicy.from_dict(
                {
                    "warning_age_seconds": 18001,
                    "deny_age_seconds": 18000,
                    "window_seconds": 86400,
                    "max_exploration_water_seconds": 0,
                }
            )
        with self.assertRaisesRegex(ValueError, "max_exploration_water_seconds"):
            GatePolicy.from_dict(
                {
                    "warning_age_seconds": 900,
                    "deny_age_seconds": 18000,
                    "window_seconds": 86400,
                    "max_exploration_water_seconds": -1,
                }
            )


class CloudGateAdmissionTests(unittest.TestCase):
    def test_fresh_safe_valid_strategy_is_allowed_without_device_permission(self):
        state = fresh_state()

        record = evaluate_gate(state, valid_strategy(state), policy(), False)

        self.assertEqual("allow", record["decision"])
        self.assertEqual([], record["reason_codes"])
        self.assertEqual([], record["warning_codes"])
        self.assertFalse(record["execution"]["actuator_commands_allowed"])

    def test_soil_freshness_at_warning_boundary_is_allowed_with_warning(self):
        state = fresh_state(soil_age_sec=900)

        record = evaluate_gate(state, valid_strategy(state), policy(), False)

        self.assertEqual("allow_with_warning", record["decision"])
        self.assertEqual(["soil_data_age_warning"], record["warning_codes"])

    def test_active_sensor_fault_is_denied(self):
        state = fresh_state(flags={"sensor_fault": {"active": True}})

        record = evaluate_gate(state, valid_strategy(state), policy(), False)

        self.assertEqual("deny", record["decision"])
        self.assertEqual(["safety_flag_active:sensor_fault"], record["reason_codes"])

    def test_missing_or_malformed_phase3_safety_facts_fail_closed(self):
        cases = {
            "flags missing": lambda state: state["safety"].pop("flags"),
            "flags empty": lambda state: state["safety"].update({"flags": {}}),
            "predictor missing": lambda state: state["safety"]["flags"].pop("predictor_circuit"),
            "flags wrong type": lambda state: state["safety"].update({"flags": []}),
            "protection malformed": lambda state: state["safety"]["flags"].update(
                {"sensor_fault": {"active": "false"}}
            ),
            "predictor malformed": lambda state: state["safety"]["flags"].update(
                {"predictor_circuit": {"state": "UNKNOWN"}}
            ),
        }
        for required_flag in SAFE_PHASE3_FLAGS:
            cases[f"{required_flag} missing"] = (
                lambda state, key=required_flag: state["safety"]["flags"].pop(key)
            )

        for label, mutate in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                state = fresh_state()
                mutate(state)
                ledger_path = Path(directory) / "budget.json"
                record = evaluate_gate(
                    state,
                    valid_strategy(
                        state,
                        [{"action_id": "water", "type": "water", "pump_seconds": 1}],
                    ),
                    policy(),
                    True,
                    BudgetLedger(ledger_path),
                )

                self.assertEqual("deny", record["decision"])
                self.assertFalse(ledger_path.exists())

    def test_non_boolean_exploration_request_is_denied_without_schema_violation(self):
        state = fresh_state()

        record = evaluate_gate(state, valid_strategy(state), policy(), "yes")

        self.assertEqual("deny", record["decision"])
        self.assertEqual(["invalid_exploration_requested"], record["reason_codes"])
        self.assertFalse(record["budget"]["exploration_requested"])

    def test_malformed_exploration_water_is_denied_without_ledger_write(self):
        state = fresh_state()
        strategy = valid_strategy(state, [{"action_id": "water", "type": "water"}])
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "budget.json"
            record = evaluate_gate(state, strategy, policy(), True, BudgetLedger(ledger_path))

            self.assertFalse(ledger_path.exists())

        self.assertEqual("deny", record["decision"])
        self.assertIn("strategy_invalid:action[0]:invalid_pump_seconds_type", record["reason_codes"])

    def test_invalid_reference_fields_are_rendered_as_null(self):
        state = fresh_state()
        strategy = valid_strategy(state)
        state["device_code"] = {"not": "a string"}
        strategy["strategy_id"] = ["not", "a string"]

        record = evaluate_gate(state, strategy, policy(), False)

        self.assertEqual("deny", record["decision"])
        self.assertIsNone(record["device_code"])
        self.assertIsNone(record["strategy_id"])


class CloudGateBudgetTests(unittest.TestCase):
    def test_reopening_ledger_with_same_reservation_does_not_charge_budget_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            first = BudgetLedger(path).reserve(
                "soil3", "same-reservation", "a" * 64, 6, policy(), "2026-09-20T10:00:00Z"
            )
            repeated = BudgetLedger(path).reserve(
                "soil3", "same-reservation", "a" * 64, 6, policy(), "2026-09-20T10:00:00Z"
            )

        self.assertTrue(first.available)
        self.assertEqual(6.0, first.reserved_water_seconds)
        self.assertEqual(first, repeated)

    def test_budget_exhaustion_returns_unavailable_without_new_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger(Path(directory) / "budget.json")
            first = ledger.reserve("soil3", "first", "a" * 64, 6, policy(), "2026-09-20T10:00:00Z")
            exhausted = ledger.reserve("soil3", "second", "b" * 64, 6, policy(), "2026-09-20T10:00:00Z")

        self.assertTrue(first.available)
        self.assertFalse(exhausted.available)
        self.assertEqual(0.0, exhausted.reserved_water_seconds)
        self.assertEqual(4.0, exhausted.remaining_water_seconds)

    def test_budget_denial_leaves_persisted_ledger_bytes_unchanged(self):
        state = fresh_state()
        first_strategy = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 6}]
        )
        denied_strategy = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 1}]
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "budget.json"
            ledger = BudgetLedger(ledger_path)
            allowed = evaluate_gate(state, first_strategy, policy(limit=6), True, ledger)
            before = ledger_path.read_bytes()
            denied = evaluate_gate(state, denied_strategy, policy(limit=6), True, ledger)
            after = ledger_path.read_bytes()

        self.assertEqual("allow", allowed["decision"])
        self.assertEqual("deny", denied["decision"])
        self.assertIn("exploration_budget_exhausted", denied["reason_codes"])
        self.assertEqual(before, after)

    def test_only_allowed_exploration_creates_a_budget_reservation(self):
        state = fresh_state()
        strategy = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 1}]
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "budget.json"
            denied_state = fresh_state()
            denied_state["safety"]["flags"] = {}
            denied = evaluate_gate(
                denied_state,
                valid_strategy(
                    denied_state,
                    [{"action_id": "water", "type": "water", "pump_seconds": 1}],
                ),
                policy(),
                True,
                BudgetLedger(ledger_path),
            )
            self.assertFalse(ledger_path.exists())

            allowed = evaluate_gate(state, strategy, policy(), True, BudgetLedger(ledger_path))
            persisted = json.loads(ledger_path.read_text(encoding="utf-8"))

        self.assertEqual("deny", denied["decision"])
        self.assertEqual("allow", allowed["decision"])
        self.assertEqual(1, len(persisted["reservations"]))

    def test_gate_reserves_water_budget_once_for_repeated_exploration_request(self):
        state = fresh_state()
        strategy = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 6}]
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger(Path(directory) / "budget.json")
            first = evaluate_gate(state, strategy, policy(), True, ledger)
            repeated = evaluate_gate(state, strategy, policy(), True, ledger)

        self.assertEqual("allow", first["decision"])
        self.assertEqual(6.0, first["budget"]["reserved_water_seconds"])
        self.assertEqual(first["budget"], repeated["budget"])

    def test_malformed_persisted_reservation_fails_closed_in_gate(self):
        state = fresh_state()
        strategy = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 6}]
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "budget.json"
            ledger_path.write_text(
                json.dumps(
                    {
                        "schema_version": "cloud-gate-budget.v1",
                        "reservations": [{"device_code": "soil3", "reservation_id": "old"}],
                    }
                ),
                encoding="utf-8",
            )

            record = evaluate_gate(state, strategy, policy(), True, BudgetLedger(ledger_path))

        self.assertEqual("deny", record["decision"])
        self.assertEqual(["budget_ledger_unavailable"], record["reason_codes"])

    def test_changed_water_under_same_strategy_id_is_denied_as_budget_conflict(self):
        state = fresh_state()
        strategy = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 6}]
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger(Path(directory) / "budget.json")
            first = evaluate_gate(state, strategy, policy(), True, ledger)
            strategy["actions"] = [{"action_id": "water", "type": "water", "pump_seconds": 9}]
            conflicting = evaluate_gate(state, strategy, policy(), True, ledger)

        self.assertEqual("allow", first["decision"])
        self.assertEqual("deny", conflicting["decision"])
        self.assertEqual(["budget_reservation_conflict"], conflicting["reason_codes"])

    def test_changed_non_water_content_under_same_strategy_id_is_denied_as_budget_conflict(self):
        state = fresh_state()
        strategy = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 6}]
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger(Path(directory) / "budget.json")
            first = evaluate_gate(state, strategy, policy(), True, ledger)
            strategy["actions"].append(
                {"action_id": "wait", "type": "wait", "seconds": 60}
            )
            conflicting = evaluate_gate(state, strategy, policy(), True, ledger)

        self.assertEqual("allow", first["decision"])
        self.assertEqual("deny", conflicting["decision"])
        self.assertEqual(["budget_reservation_conflict"], conflicting["reason_codes"])

    def test_ledger_directory_error_fails_closed_in_gate(self):
        state = fresh_state()
        strategy = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 6}]
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger(Path(directory) / "budget.json")
            with patch("services.soil3.cloud_gate.budget.Path.mkdir", side_effect=OSError("denied")):
                record = evaluate_gate(state, strategy, policy(), True, ledger)

        self.assertEqual("deny", record["decision"])
        self.assertEqual(["budget_ledger_unavailable"], record["reason_codes"])

    def test_committed_reservation_remains_allowed_when_lock_release_fails(self):
        state = fresh_state()
        strategy = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 6}]
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger(Path(directory) / "budget.json")
            with patch.object(ledger, "_release_lock", side_effect=BudgetLedgerError("stuck")):
                record = evaluate_gate(state, strategy, policy(), True, ledger)

            persisted = json.loads(ledger.path.read_text(encoding="utf-8"))

        self.assertEqual("allow", record["decision"])
        self.assertEqual(6.0, record["budget"]["reserved_water_seconds"])
        self.assertEqual(1, len(persisted["reservations"]))


class CloudGateCliTests(unittest.TestCase):
    def test_cli_requires_explicit_input_output_and_ledger_paths(self):
        with self.assertRaises(SystemExit):
            service.main([])

    def test_cli_writes_nonexecuting_gate_record_atomically(self):
        state = fresh_state()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            strategy_path = root / "strategy.json"
            config_path = root / "config.json"
            ledger_path = root / "budget.json"
            output_path = root / "output.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            strategy_path.write_text(json.dumps(valid_strategy(state)), encoding="utf-8")
            config_path.write_text(
                json.dumps(
                    {
                        "warning_age_seconds": 900,
                        "deny_age_seconds": 18000,
                        "window_seconds": 86400,
                        "max_exploration_water_seconds": 0,
                    }
                ),
                encoding="utf-8",
            )

            service.main(
                [
                    "--state", str(state_path),
                    "--strategy", str(strategy_path),
                    "--config", str(config_path),
                    "--budget-ledger", str(ledger_path),
                    "--output", str(output_path),
                ]
            )

            record = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual("gate.v1", record["schema_version"])
        self.assertFalse(record["execution"]["actuator_commands_allowed"])
        self.assertFalse(output_path.with_name(output_path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
