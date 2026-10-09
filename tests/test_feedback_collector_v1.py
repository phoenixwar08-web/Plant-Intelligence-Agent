import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from services.soil3.episode.episode_v1 import EpisodeStore
from services.soil3.feedback_collector.action_receipt_v1 import ActionReceiptStore
from services.soil3.feedback_collector.collector_v1 import CollectorConfig, FeedbackCollector
from services.soil3.state.state_v1 import StateBuilder


ACTION_ID = "act-0123456789abcdef01234567"
ACTION_AT = "2026-10-09T10:00:08Z"


def state_snapshot():
    return StateBuilder("soil3").build(
        {
            "observed_at": "2026-10-09T10:00:00Z",
            "generated_at": "2026-10-09T10:00:01Z",
            "sensor_readings": [{"timestamp": "2026-10-09T10:00:00Z", "humidity": 31.0}],
            "parameters": {"FC": 40.0, "TARGET_LOW": 35.0, "HARD_SAFETY_LOW": 25.0},
        }
    )


class FeedbackCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.receipt_dir = root / "receipts"
        self.episode_dir = root / "episodes"
        self.config = CollectorConfig(
            receipt_dir=self.receipt_dir,
            tracking_dir=root / "tracking",
            episode_dir=self.episode_dir,
            feedback_dir=root / "feedback",
        )
        self.store = ActionReceiptStore(self.receipt_dir)
        self.collector = FeedbackCollector(
            self.config,
            clock=lambda: datetime(2026, 10, 9, 10, 1, tzinfo=timezone.utc),
        )

    def test_native_receipt_creates_one_independent_episode(self):
        """Deleting receipt-only binding would let native watering attach to an unrelated Shadow Episode."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )

        first = self.collector.run_once()
        second = self.collector.run_once()

        episodes = sorted(self.episode_dir.glob("ep-*.json"))
        self.assertEqual(1, len(episodes))
        episode = EpisodeStore(self.episode_dir).read(episodes[0].stem)
        self.assertEqual("phase3_native", episode["executed_actions"][0]["action_source"])
        self.assertEqual(ACTION_ID, episode["executed_actions"][0]["action_id"])
        self.assertEqual(1, first["tracked_actions"])
        self.assertEqual(1, second["tracked_actions"])

    def test_open_shadow_episode_without_receipt_is_ignored(self):
        """Removing receipt filtering would fabricate feedback tracking from proposal-only Episodes."""
        EpisodeStore(self.episode_dir).create(state_snapshot())

        summary = self.collector.run_once()

        self.assertEqual(0, summary["tracked_actions"])
        self.assertEqual([], list(self.config.tracking_dir.glob("*.json")))
