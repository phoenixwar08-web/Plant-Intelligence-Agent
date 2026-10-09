import tempfile
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from services.soil3.episode.episode_v1 import EpisodeStore
from services.soil3.feedback_collector.action_receipt_v1 import ActionReceiptStore
from services.soil3.feedback_collector.collector_v1 import CollectorConfig, FeedbackCollector
from services.soil3.feedback.feedback_v1 import FeedbackStore
from services.soil3.state.state_v1 import StateBuilder
from services.soil3.telemetry.common import load_json


ACTION_ID = "act-0123456789abcdef01234567"
ACTION_AT = "2026-10-09T10:00:08Z"
ROOT = Path(__file__).resolve().parents[1]


def state_snapshot(observed_at="2026-10-09T10:00:00Z", humidity=31.0):
    return StateBuilder("soil3").build(
        {
            "observed_at": observed_at,
            "generated_at": observed_at,
            "sensor_readings": [{"timestamp": observed_at, "humidity": humidity}],
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

    def test_due_30min_window_records_canonical_state_and_null_vision(self):
        """Replacing the state supplier with a Shadow artifact would record an invented plant response."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )
        collector = FeedbackCollector(
            self.config,
            clock=lambda: datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc),
            state_supplier=lambda: state_snapshot("2026-10-09T10:30:00Z", humidity=37.0),
        )

        collector.run_once()

        episode_id = next(self.episode_dir.glob("ep-*.json")).stem
        records = FeedbackStore(self.config.feedback_dir).list_for_episode(episode_id)
        self.assertEqual(1, len(records))
        self.assertEqual("30min", records[0]["window"])
        self.assertEqual("state.v1", records[0]["observations"]["soil"]["schema_version"])
        self.assertEqual(37.0, records[0]["observations"]["soil"]["soil"]["humidity_percent"])
        self.assertIsNone(records[0]["observations"]["vision"])

    def test_24h_window_finalizes_once_and_keeps_missed_windows_explicit(self):
        """Dropping finalization would leave a completed action unavailable to the closed-Episode lifecycle."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )
        collector = FeedbackCollector(
            self.config,
            clock=lambda: datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc),
            state_supplier=lambda: state_snapshot("2026-10-10T10:00:00Z", humidity=37.0),
        )

        collector.run_once()
        episode_path = next(self.episode_dir.glob("ep-*.json"))
        first = EpisodeStore(self.episode_dir).read(episode_path.stem)
        self.assertTrue(load_json(collector.tracking_path(ACTION_ID))["finalized"])
        collector.run_once()
        second = EpisodeStore(self.episode_dir).read(episode_path.stem)

        self.assertEqual("closed", first["status"])
        self.assertEqual(["30min", "2-3h", "6-12h"], first["outcome"]["windows_missing"])
        self.assertEqual(first, second)

    def test_capture_current_state_uses_health_snapshot_without_csv_fallback(self):
        """Replacing the health adapter with a CSV source would make post-action evidence non-canonical."""
        from services.soil3.feedback_collector.collector_v1 import capture_current_state

        config = CollectorConfig(
            receipt_dir=self.receipt_dir,
            tracking_dir=self.config.tracking_dir,
            episode_dir=self.episode_dir,
            feedback_dir=self.config.feedback_dir,
            phase3_state_path="/runtime/phase3-state.json",
            sensor_log_path="/runtime/sensor-log.csv",
            irrigation_trials_path="/runtime/irrigation-trials.json",
            parameters={"FC": 40.0, "TARGET_LOW": 35.0, "HARD_SAFETY_LOW": 25.0},
        )
        snapshot = {
            "observed_at": "2026-10-09T10:30:00Z",
            "sensor_readings": [
                {"timestamp": "2026-10-09T10:30:00Z", "humidity": 37.0}
            ],
            "system_state": {},
            "watering_history": [],
            "environment": {},
            "state_file": {"age_seconds": 1.0},
        }
        with patch(
            "services.soil3.feedback_collector.collector_v1.build_health_snapshot",
            return_value=snapshot,
        ) as health:
            state = capture_current_state(config)

        self.assertEqual("state.v1", state["schema_version"])
        self.assertEqual(37.0, state["soil"]["humidity_percent"])
        self.assertEqual(str(config.phase3_state_path), health.call_args.args[1])
        self.assertNotIn("local_sensor_log_path", health.call_args.kwargs)

    def test_restart_preserves_recorded_window_and_records_the_next_once(self):
        """Dropping persisted scheduling would duplicate a feedback fact after a collector restart."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )
        now = [datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc)]
        first = FeedbackCollector(
            self.config,
            clock=lambda: now[0],
            state_supplier=lambda: state_snapshot(now[0].isoformat().replace("+00:00", "Z")),
        )
        first.run_once()
        now[0] = datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc)
        restarted = FeedbackCollector(
            self.config,
            clock=lambda: now[0],
            state_supplier=lambda: state_snapshot(now[0].isoformat().replace("+00:00", "Z")),
        )
        restarted.run_once()
        restarted.run_once()

        episode_id = next(self.episode_dir.glob("ep-*.json")).stem
        windows = [
            record["window"]
            for record in FeedbackStore(self.config.feedback_dir).list_for_episode(episode_id)
        ]
        self.assertEqual(["30min", "2-3h"], windows)

    def test_missed_window_is_marked_and_never_backfilled(self):
        """Backfilling a missed window would invent a delayed observation at the wrong time."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )
        collector = FeedbackCollector(
            self.config,
            clock=lambda: datetime(2026, 10, 9, 11, 11, tzinfo=timezone.utc),
            state_supplier=lambda: state_snapshot("2026-10-09T11:11:00Z"),
        )

        collector.run_once()

        tracking = load_json(collector.tracking_path(ACTION_ID))
        self.assertEqual("missed", tracking["windows"]["30min"])
        self.assertEqual([], list(self.config.feedback_dir.glob("fb-*.json")))

    def test_all_expired_windows_close_without_inventing_feedback(self):
        """Leaving an action open forever after a long outage would lose its explicit no-observation outcome."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )
        collector = FeedbackCollector(
            self.config,
            clock=lambda: datetime(2026, 10, 11, 10, 1, tzinfo=timezone.utc),
            state_supplier=lambda: self.fail("expired windows must not capture a late state"),
        )

        collector.run_once()

        episode_id = next(self.episode_dir.glob("ep-*.json")).stem
        episode = EpisodeStore(self.episode_dir).read(episode_id)
        self.assertEqual("closed", episode["status"])
        self.assertEqual(0, episode["outcome"]["record_count"])
        self.assertEqual(["30min", "2-3h", "6-12h", "24h"], episode["outcome"]["windows_missing"])
        self.assertEqual([], list(self.config.feedback_dir.glob("fb-*.json")))

    def test_available_vision_facts_are_saved_with_the_same_feedback_window(self):
        """Writing only a Vision ref would leave feedback without the verified observations it represents."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )
        config = CollectorConfig(
            receipt_dir=self.config.receipt_dir,
            tracking_dir=self.config.tracking_dir,
            episode_dir=self.config.episode_dir,
            feedback_dir=self.config.feedback_dir,
            vision_enabled=True,
        )
        vision = {
            "manifest_ref": {"schema_version": "vision_run.v1", "record_id": "run", "path": "/ref", "sha256": "a"},
            "facts": [{"schema_version": "vision.v1", "image_id": "observation"}],
        }
        collector = FeedbackCollector(
            config,
            clock=lambda: datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc),
            state_supplier=lambda: state_snapshot("2026-10-09T10:30:00Z", humidity=37.0),
        )
        with patch(
            "services.soil3.feedback_collector.collector_v1.capture_current_vision",
            return_value=vision,
        ):
            collector.run_once()

        episode_id = next(self.episode_dir.glob("ep-*.json")).stem
        record = FeedbackStore(self.config.feedback_dir).list_for_episode(episode_id)[0]
        self.assertEqual(vision, record["observations"]["vision"])

    def test_vision_configuration_failure_is_recorded_as_missing_not_pipeline_failure(self):
        """Letting an optional Vision configuration error abort soil feedback would discard real telemetry facts."""
        from services.soil3.feedback_collector.collector_v1 import capture_current_vision

        config = CollectorConfig(
            receipt_dir=self.config.receipt_dir,
            tracking_dir=self.config.tracking_dir,
            episode_dir=self.config.episode_dir,
            feedback_dir=self.config.feedback_dir,
            vision_enabled=True,
        )
        self.assertIsNone(capture_current_vision(config))

    def test_restart_after_feedback_write_recovers_the_same_window_without_duplicate(self):
        """A crash after feedback persistence must recover the factual record, not write a second 30-minute observation."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )
        collector = FeedbackCollector(
            self.config,
            clock=lambda: datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc),
            state_supplier=lambda: state_snapshot("2026-10-09T10:30:00Z", humidity=37.0),
        )
        original_write = __import__(
            "services.soil3.feedback_collector.collector_v1",
            fromlist=["atomic_write_json"],
        ).atomic_write_json
        writes = [0]

        def crash_after_feedback(path, value):
            writes[0] += 1
            if Path(path) == collector.tracking_path(ACTION_ID) and writes[0] == 2:
                raise RuntimeError("simulated_crash_after_feedback")
            return original_write(path, value)

        with patch(
            "services.soil3.feedback_collector.collector_v1.atomic_write_json",
            side_effect=crash_after_feedback,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated_crash_after_feedback"):
                collector.run_once()

        restarted = FeedbackCollector(
            self.config,
            clock=lambda: datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc),
            state_supplier=lambda: state_snapshot("2026-10-09T10:30:00Z", humidity=37.0),
        )
        restarted.run_once()

        episode_id = next(self.episode_dir.glob("ep-*.json")).stem
        records = FeedbackStore(self.config.feedback_dir).list_for_episode(episode_id)
        self.assertEqual(["30min"], [record["window"] for record in records])

    def test_active_collector_lock_skips_a_concurrent_scan_and_stale_lock_recovers(self):
        """Allowing two live scans to create tracking independently would duplicate an action Episode."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )
        collector = FeedbackCollector(
            self.config,
            state_supplier=lambda: state_snapshot("2026-10-09T10:30:00Z", humidity=37.0),
        )
        lock_path = self.config.tracking_dir / ".feedback_collector.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.mkdir()

        skipped = collector.run_once()
        os.utime(lock_path, (time.time() - 1900, time.time() - 1900))
        recovered = collector.run_once()

        self.assertTrue(skipped["skipped_due_to_lock"])
        self.assertFalse(recovered["skipped_due_to_lock"])
        self.assertEqual(1, len(list(self.episode_dir.glob("ep-*.json"))))

    def test_tracking_create_failure_never_orphans_a_second_episode_after_restart(self):
        """Persisting an action-to-Episode binding after Episode creation would orphan the first Episode on a crash."""
        self.store.prepare_native(ACTION_ID, state_snapshot(), 8.0, "2026-10-09T10:00:00Z")
        self.store.complete_command(
            ACTION_ID,
            reference_action_at=ACTION_AT,
            on_published_at="2026-10-09T10:00:00Z",
            off_published_at=ACTION_AT,
            pump_seconds=8.0,
        )
        collector = FeedbackCollector(
            self.config,
            clock=lambda: datetime(2026, 10, 9, 10, 1, tzinfo=timezone.utc),
            state_supplier=lambda: state_snapshot("2026-10-09T10:01:00Z"),
        )
        with patch(
            "services.soil3.feedback_collector.collector_v1.atomic_write_json",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                collector.run_once()
        self.assertEqual([], list(self.episode_dir.glob("ep-*.json")))

        FeedbackCollector(
            self.config,
            clock=lambda: datetime(2026, 10, 9, 10, 1, tzinfo=timezone.utc),
            state_supplier=lambda: state_snapshot("2026-10-09T10:01:00Z"),
        ).run_once()
        self.assertEqual(1, len(list(self.episode_dir.glob("ep-*.json"))))

    def test_stale_lock_with_a_live_owner_is_never_taken_over(self):
        """mtime alone cannot prove a slow Vision or telemetry call has stopped running."""
        lock_path = self.config.tracking_dir / ".feedback_collector.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.mkdir()
        (lock_path / "owner.json").write_text(
            '{"pid": %d, "token": "live-owner"}' % os.getpid(),
            encoding="utf-8",
        )
        os.utime(lock_path, (time.time() - 1900, time.time() - 1900))

        summary = FeedbackCollector(self.config).run_once()

        self.assertTrue(summary["skipped_due_to_lock"])
        self.assertTrue(lock_path.exists())

    def test_acquired_lock_records_live_owner_before_any_long_operation(self):
        """A lock created by the Collector itself must remain non-stealable while its owner PID is alive."""
        from services.soil3.feedback_collector.collector_v1 import _CollectorRunLock

        path = self.config.tracking_dir / ".feedback_collector.lock"
        first = _CollectorRunLock(path, stale_seconds=1.0)
        self.assertTrue(first.acquire())
        os.utime(path, (time.time() - 10, time.time() - 10))
        second = _CollectorRunLock(path, stale_seconds=1.0)
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()

    def test_runtime_assets_use_canonical_state_and_opengauss_socket(self):
        """A private /tmp without the database socket would turn real feedback soil facts into missing values."""
        unit = (
            ROOT / "deploy" / "systemd" / "plant-agent-soil3-feedback-collector.service"
        ).read_text(encoding="utf-8")
        readme = (
            ROOT / "services" / "soil3" / "feedback_collector" / "README.md"
        ).read_text(encoding="utf-8")

        self.assertIn("BindReadOnlyPaths=-/tmp/.s.PGSQL.7654", unit)
        self.assertIn("agent_chain/state/latest.json", readme)
        self.assertNotIn("phase3/system_state.json", readme)
