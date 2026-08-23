import os
import unittest
from pathlib import Path
from unittest.mock import patch

if os.name == "nt":
    os.environ.setdefault(
        "PHASE3_IOT_AGENT_ROOT",
        str(Path(__file__).resolve().parents[2] / "iot_stage"),
    )

from cloud_protection import (
    cloud_watering_allowed,
    load_cloud_control,
    protection_mode,
)

if os.name != "nt":
    import decision_brain
else:
    decision_brain = None


PAUSED = {
    "auto_watering_enabled": False,
    "safety_mode": "conservative",
    "risk_level": "critical",
    "reason": "test_pause",
}


class CloudProtectionTests(unittest.TestCase):
    def test_alert_only_mode_never_controls_watering(self):
        with patch.dict(os.environ, {"PHASE3_CLOUD_PROTECTION_ENABLED": "false"}):
            control = load_cloud_control()
            self.assertEqual(protection_mode(control), "normal")
            self.assertEqual(
                cloud_watering_allowed(PAUSED, emergency=False, sensor_trusted=False),
                (True, "cloud_alert_only"),
            )

    def test_paused_policy_blocks_all_watering(self):
        self.assertEqual(protection_mode(PAUSED), "paused")
        self.assertEqual(
            cloud_watering_allowed(PAUSED, emergency=True, sensor_trusted=True),
            (False, "cloud_paused"),
        )

    @unittest.skipIf(os.name == "nt", "final pump gate uses Linux-only fcntl runtime")
    def test_final_pump_gate_blocks_before_publish(self):
        actuator = object.__new__(decision_brain.ActuatorLayer)
        with patch.object(decision_brain, "load_cloud_control", return_value=PAUSED):
            with patch.object(actuator, "_publish_pump") as publish:
                with self.assertRaises(decision_brain.PumpExecutionError):
                    actuator._activate_pump(3.0, is_emergency=True)
                publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
