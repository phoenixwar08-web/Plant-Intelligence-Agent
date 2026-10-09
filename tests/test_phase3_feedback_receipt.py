import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.soil3.feedback_collector.action_receipt_v1 import ActionReceiptStore
from services.soil3.state.state_v1 import StateBuilder


ROOT = Path(__file__).resolve().parents[1]


def action_state():
    return StateBuilder("soil3").build(
        {
            "observed_at": "2026-10-09T10:00:00Z",
            "generated_at": "2026-10-09T10:00:01Z",
            "sensor_readings": [{"timestamp": "2026-10-09T10:00:00Z", "humidity": 31.0}],
            "parameters": {"FC": 40.0, "TARGET_LOW": 35.0, "HARD_SAFETY_LOW": 25.0},
        }
    )


class NativePhase3ReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fcntl = types.ModuleType("fcntl")
        fcntl.LOCK_SH = 1
        fcntl.LOCK_EX = 2
        fcntl.LOCK_UN = 8
        fcntl.flock = lambda *args: None
        cls.previous_fcntl = sys.modules.get("fcntl")
        sys.modules["fcntl"] = fcntl
        cls.phase3_dir = str((ROOT / "services" / "soil3" / "phase3").resolve())
        sys.path.insert(0, cls.phase3_dir)
        import decision_brain
        cls.decision_brain = decision_brain

    @classmethod
    def tearDownClass(cls):
        if cls.phase3_dir in sys.path:
            sys.path.remove(cls.phase3_dir)
        if cls.previous_fcntl is None:
            sys.modules.pop("fcntl", None)
        else:
            sys.modules["fcntl"] = cls.previous_fcntl

    def test_receipt_is_durable_before_native_pump_activation(self):
        """Removing pre-activation persistence would expose an untraceable watering command."""
        with tempfile.TemporaryDirectory() as directory:
            actuator = object.__new__(self.decision_brain.ActuatorLayer)
            store = ActionReceiptStore(Path(directory) / "receipts")
            actuator._receipt_store = store
            actuator._capture_action_state = action_state
            actuator.cfg = SimpleNamespace(
                LARGE_WATER_THRESHOLD=30.0,
                get_constant=lambda _name: 1800,
            )
            receipts_visible_at_activation = []

            def activate(_seconds):
                receipts_visible_at_activation.append(len(list(store.iter_receipts())))
                return {
                    "mqtt_on_published_at": "2026-10-09T10:00:00Z",
                    "mqtt_off_published_at": "2026-10-09T10:00:08Z",
                }

            actuator._activate_pump = activate
            with mock.patch.object(
                self.decision_brain,
                "_update_system_state",
                side_effect=lambda mutator: mutator({}) or {},
            ):
                actuator.execute_pump(8.0, 31.0)

            self.assertEqual([1], receipts_visible_at_activation)
            receipt = next(store.iter_receipts())
            self.assertEqual("phase3_native", receipt["action_source"])
            self.assertEqual("command_completed", receipt["command_status"])
            self.assertFalse(receipt["execution_evidence"]["physical_action_confirmed"])

    def test_bound_controlled_receipt_replaces_native_receipt_for_one_action(self):
        """A controlled run must not create a second independent feedback action for its same pump command."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actuator = object.__new__(self.decision_brain.ActuatorLayer)
            native_store = ActionReceiptStore(root / "native")
            controlled_store = ActionReceiptStore(root / "controlled")
            action_id = "act-0123456789abcdef01234567"
            controlled_store.prepare_controlled(
                action_id,
                action_state(),
                "tr-0123456789abcdef01234567",
                "ep-0123456789abcdef01234567",
                "approval-1",
                "2026-10-09T10:00:00Z",
            )
            actuator._receipt_store = native_store
            actuator._capture_action_state = action_state
            actuator.cfg = SimpleNamespace(
                LARGE_WATER_THRESHOLD=30.0,
                get_constant=lambda _name: 1800,
            )
            actuator._activate_pump = lambda _seconds: {
                "mqtt_on_published_at": "2026-10-09T10:00:00Z",
                "mqtt_off_published_at": "2026-10-09T10:00:08Z",
            }
            actuator.bind_feedback_receipt(controlled_store, action_id)
            with mock.patch.object(
                self.decision_brain,
                "_update_system_state",
                side_effect=lambda mutator: mutator({}) or {},
            ):
                actuator.execute_pump(8.0, 31.0)

            self.assertEqual([], list(native_store.iter_receipts()))
            receipt = controlled_store.read(action_id)
            self.assertEqual("controlled_execution", receipt["action_source"])
            self.assertEqual("command_completed", receipt["command_status"])
