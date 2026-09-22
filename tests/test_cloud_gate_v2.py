import copy
import json
import unittest
from pathlib import Path

from services.soil3.cloud_gate.gate_v1 import evaluate_gate
from services.soil3.cloud_strategy.validator import fingerprint
from tests.test_cloud_gate import fresh_state, policy, valid_strategy

try:
    from services.soil3.cloud_gate.gate_v2 import evaluate_gate_v2
except ImportError:
    evaluate_gate_v2 = None


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "services" / "soil3" / "cloud_gate" / "gate.v2.schema.json"


class CloudGateV2Tests(unittest.TestCase):
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
