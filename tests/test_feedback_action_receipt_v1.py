import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from services.soil3.feedback_collector.action_receipt_v1 import (
    ActionReceiptError,
    ActionReceiptStore,
)
from services.soil3.state.state_v1 import StateBuilder


ACTION_ID = "act-0123456789abcdef01234567"
PREPARED_AT = "2026-10-09T10:00:00Z"
COMMAND_DONE_AT = "2026-10-09T10:00:08Z"


def valid_state():
    return StateBuilder("soil3").build(
        {
            "observed_at": "2026-10-09T09:59:59Z",
            "generated_at": PREPARED_AT,
            "sensor_readings": [
                {"timestamp": "2026-10-09T09:59:59Z", "humidity": 31.0},
            ],
            "parameters": {"FC": 40.0, "TARGET_LOW": 35.0, "HARD_SAFETY_LOW": 25.0},
        }
    )


class ActionReceiptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ActionReceiptStore(Path(self.temp.name) / "receipts")

    def test_native_command_completion_updates_the_same_durable_action(self):
        """A completion update must not create another action or claim physical proof."""
        prepared = self.store.prepare_native(ACTION_ID, valid_state(), 8.0, PREPARED_AT)

        completed = self.store.complete_command(
            ACTION_ID,
            reference_action_at=COMMAND_DONE_AT,
            on_published_at=PREPARED_AT,
            off_published_at=COMMAND_DONE_AT,
            pump_seconds=8.0,
        )

        self.assertEqual(ACTION_ID, prepared["action_id"])
        self.assertEqual(ACTION_ID, completed["action_id"])
        self.assertEqual("command_completed", completed["command_status"])
        self.assertEqual("command_completed", completed["execution_evidence"]["level"])
        self.assertFalse(completed["execution_evidence"]["physical_action_confirmed"])
        self.assertEqual([ACTION_ID], [receipt["action_id"] for receipt in self.store.iter_receipts()])

    def test_manual_action_id_is_idempotent_only_for_identical_facts(self):
        """Changing a fact under a reused action id must not create a second action."""
        first = self.store.record_manual(
            ACTION_ID,
            valid_state(),
            confirmed_by="owner",
            reference_action_at=PREPARED_AT,
            pump_seconds=5.0,
        )

        repeated = self.store.record_manual(
            ACTION_ID,
            valid_state(),
            confirmed_by="owner",
            reference_action_at=PREPARED_AT,
            pump_seconds=5.0,
        )

        with self.assertRaises(ActionReceiptError):
            self.store.record_manual(
                ACTION_ID,
                valid_state(),
                confirmed_by="owner",
                reference_action_at=PREPARED_AT,
                pump_seconds=6.0,
            )

        self.assertEqual(first, repeated)
        self.assertEqual([ACTION_ID], [receipt["action_id"] for receipt in self.store.iter_receipts()])

    def test_prepared_receipt_survives_restart_without_being_promoted(self):
        """A restart must preserve uncertainty instead of inferring a command outcome."""
        self.store.prepare_native(ACTION_ID, valid_state(), 8.0, PREPARED_AT)

        reopened = ActionReceiptStore(self.store.root)

        self.assertEqual("prepared", reopened.read(ACTION_ID)["command_status"])
        self.assertEqual("intent_only", reopened.read(ACTION_ID)["execution_evidence"]["level"])

    def test_manual_record_cli_persists_confirmation_without_execution_modules(self):
        """A manual confirmation must be a fact write, not an alternate watering path."""
        from services.soil3.feedback_collector import service

        state_path = Path(self.temp.name) / "state.json"
        state_path.write_text(json.dumps(valid_state()), encoding="utf-8")
        args = [
            "manual-record",
            "--receipt-dir", str(self.store.root),
            "--state-file", str(state_path),
            "--action-id", ACTION_ID,
            "--confirmed-by", "owner",
            "--reference-action-at", PREPARED_AT,
            "--pump-seconds", "5",
        ]

        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, service.main(args))
        receipt = self.store.read(ACTION_ID)
        self.assertEqual("manual_confirmed", receipt["command_status"])
        self.assertTrue(receipt["execution_evidence"]["physical_action_confirmed"])

    def test_collect_cli_loads_only_explicit_collector_paths(self):
        """Making collection infer Phase3 paths would couple the sidecar to a mutable runtime layout."""
        from services.soil3.feedback_collector import service

        config_path = Path(self.temp.name) / "collector.json"
        config_path.write_text(json.dumps({
            "receipt_dir": str(self.store.root),
            "tracking_dir": str(Path(self.temp.name) / "tracking"),
            "episode_dir": str(Path(self.temp.name) / "episodes"),
            "feedback_dir": str(Path(self.temp.name) / "feedback"),
            "phase3_state_path": str(Path(self.temp.name) / "phase3-state.json"),
            "parameters": {"FC": 40.0, "TARGET_LOW": 35.0, "HARD_SAFETY_LOW": 25.0},
            "vision_enabled": False,
        }), encoding="utf-8")
        collector = Mock()
        collector.run_once.return_value = {"tracked_actions": 0}

        with patch("services.soil3.feedback_collector.service.FeedbackCollector", return_value=collector) as factory:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, service.main(["collect", "--config", str(config_path)]))

        config = factory.call_args.args[0]
        self.assertEqual(self.store.root, config.receipt_dir)
        self.assertFalse(config.vision_enabled)
        collector.run_once.assert_called_once_with()
