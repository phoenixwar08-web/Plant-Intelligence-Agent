import copy
import json
import unittest
from pathlib import Path

from services.soil3.cloud_gate.gate_v1 import evaluate_gate
from services.soil3.cloud_strategy.validator import fingerprint
from services.soil3.state.state_v1 import PHASE3_REQUIRED_SAFETY_FLAG_KEYS
from tests.test_cloud_gate import fresh_state, policy, valid_strategy

try:
    from services.soil3.cloud_gate.gate_v2 import evaluate_gate_v2
except ImportError:
    evaluate_gate_v2 = None


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "services" / "soil3" / "cloud_gate" / "gate.v2.schema.json"


class CloudGateV2Tests(unittest.TestCase):
    def test_incomplete_pending_soak_is_invalid_in_both_gate_versions(self):
        state = fresh_state(
            flags={
                "pending_soak": {
                    "pump_end_time_wall": 1791806400.0,
                    "soak_duration": 1200.0,
                }
            }
        )
        strategy = valid_strategy(state)

        v1 = evaluate_gate(state, strategy, policy(), False)
        v2 = evaluate_gate_v2(state, strategy, policy(), False)

        self.assertEqual(["safety_flag_invalid:pending_soak"], v1["reason_codes"])
        self.assertEqual(v1["reason_codes"], v2["reason_codes"])

    def test_optional_safety_compatibility_matches_gate_v1(self):
        cases = (
            ("optional facts absent", {}, "allow"),
            ("pending soak null", {"pending_soak": None}, "allow"),
            (
                "pending soak persisted",
                {"pending_soak": {"pump_end_time_wall": 1791806399.0, "soak_duration": 0.0, "water_sec": 5.0, "pre_humidity": 34.0}},
                "deny",
            ),
            ("cloud protection inactive", {"cloud_protection": {"active": False}}, "allow"),
            ("cloud protection active", {"cloud_protection": {"active": True}}, "deny"),
            (
                "cloud protection malformed",
                {"cloud_protection": {"active": "unknown"}},
                "deny",
            ),
        )
        for label, flags, expected_decision in cases:
            with self.subTest(case=label):
                state = fresh_state(flags=flags)
                strategy = valid_strategy(state)
                v1 = evaluate_gate(state, strategy, policy(), False)
                v2 = evaluate_gate_v2(state, strategy, policy(), False)

                self.assertEqual(expected_decision, v1["decision"])
                self.assertEqual(v1["decision"], v2["decision"])
                self.assertEqual(v1["reason_codes"], v2["reason_codes"])
                self.assertEqual(v1["warning_codes"], v2["warning_codes"])

    def test_gate_v2_keeps_every_other_safety_fact_required(self):
        for name in sorted(PHASE3_REQUIRED_SAFETY_FLAG_KEYS):
            with self.subTest(name=name):
                state = fresh_state()
                state["safety"]["flags"].pop(name)
                record = evaluate_gate_v2(
                    state, valid_strategy(state), policy(), False
                )

                self.assertEqual("deny", record["decision"])
                self.assertIn(
                    f"safety_flag_unavailable:{name}", record["reason_codes"]
                )

    def test_valid_strategy_hash_is_retained_for_allow_warning_and_deny(self):
        self.assertIsNotNone(evaluate_gate_v2)
        cases = (
            (fresh_state(), "allow"),
            (fresh_state(soil_age_sec=900), "allow_with_warning"),
            (fresh_state(flags={"sensor_fault": {"active": True}}), "deny"),
        )
        for state, expected_decision in cases:
            with self.subTest(decision=expected_decision):
                strategy = valid_strategy(state)
                record = evaluate_gate_v2(state, strategy, policy(), False)
                self.assertEqual("gate.v2", record["schema_version"])
                self.assertEqual(expected_decision, record["decision"])
                self.assertEqual(fingerprint(strategy), record["strategy_sha256"])

    def test_invalid_strategy_has_null_content_binding(self):
        self.assertIsNotNone(evaluate_gate_v2)
        state = fresh_state()
        invalid = valid_strategy(
            state, [{"action_id": "water", "type": "water", "pump_seconds": 121}]
        )

        record = evaluate_gate_v2(state, invalid, policy(), False)

        self.assertEqual("deny", record["decision"])
        self.assertIsNone(record["strategy_sha256"])

    def test_gate_v1_shape_remains_unchanged(self):
        state = fresh_state()
        record = evaluate_gate(state, valid_strategy(state), policy(), False)

        self.assertEqual("gate.v1", record["schema_version"])
        self.assertNotIn("strategy_sha256", record)

    def test_schema_requires_nullable_strategy_hash(self):
        self.assertTrue(SCHEMA_PATH.is_file())
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual("gate.v2", schema["properties"]["schema_version"]["const"])
        self.assertIn("strategy_sha256", schema["required"])
        self.assertEqual(
            ["string", "null"], schema["properties"]["strategy_sha256"]["type"]
        )
        self.assertEqual(
            "^[0-9a-f]{64}$", schema["properties"]["strategy_sha256"]["pattern"]
        )


if __name__ == "__main__":
    unittest.main()
