import unittest

from services.soil3.cloud_gate.gate_v1 import GatePolicy


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


if __name__ == "__main__":
    unittest.main()
