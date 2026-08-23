"""Guard the Phase3 integration boundary for cross-plant validation."""

from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "decision_brain.py"


class ExperienceWiringTests(unittest.TestCase):
    def test_bridge_is_limited_to_the_normal_final_watering_path(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        final_path = source.index("self._consume_exploration_budget(best_plan)")
        claim = source.index("experience_claim = None", final_path)
        pump = source.index("self.actuator.execute_pump(", claim)
        record = source.index("self._experience_validation.record_execution(", pump)

        self.assertLess(claim, pump)
        self.assertLess(pump, record)
        self.assertIn("action_sec=action_sec", source[claim:pump])
        self.assertIn("is_emergency=False", source[pump:record])

    def test_bridge_never_supplies_a_duration_or_mqtt_command(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        bridge_path = Path(__file__).resolve().parents[1] / "experience_validation.py"
        bridge = bridge_path.read_text(encoding="utf-8")

        self.assertNotIn("execute_pump", bridge)
        self.assertNotIn("publish(", bridge)
        self.assertNotIn("water_sec=", bridge)
        self.assertIn("action_sec=action_sec", source)


if __name__ == "__main__":
    unittest.main()
