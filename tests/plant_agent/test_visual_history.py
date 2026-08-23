"""Regression tests for append-only soil2 visual observation history."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from agent import vision_agent


class VisualHistoryTests(unittest.TestCase):
    def test_append_creates_parseable_soil2_history_record(self):
        observed_at = "2026-07-16T18:00:00+08:00"
        visual = {
            "plant_health": "normal",
            "disease_suspected": False,
            "yellow_leaf_ratio": 0.02,
            "raw": "must not be persisted",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            history_dir = Path(temp_dir) / "history"
            with patch.object(vision_agent, "VISION_HISTORY_DIR", history_dir):
                written = vision_agent.append_visual_history(
                    "soil2",
                    observed_at,
                    visual,
                )

            history_file = history_dir / "soil2.jsonl"
            rows = [
                json.loads(line)
                for line in history_file.read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(written)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_code"], "soil2")
        self.assertEqual(rows[0]["observed_at"], observed_at)
        self.assertEqual(rows[0]["model"], vision_agent.MODEL)
        self.assertEqual(rows[0]["visual"]["yellow_leaf_ratio"], 0.02)
        self.assertNotIn("raw", rows[0]["visual"])

    def test_append_keeps_independent_records_for_successive_observations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_dir = Path(temp_dir) / "history"
            with patch.object(vision_agent, "VISION_HISTORY_DIR", history_dir):
                vision_agent.append_visual_history(
                    "soil2",
                    "2026-07-16T18:00:00+08:00",
                    {"plant_health": "normal"},
                )
                vision_agent.append_visual_history(
                    "soil2",
                    "2026-07-16T19:00:00+08:00",
                    {"plant_health": "warning"},
                )

            rows = [
                json.loads(line)
                for line in (history_dir / "soil2.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual([row["observed_at"] for row in rows], [
            "2026-07-16T18:00:00+08:00",
            "2026-07-16T19:00:00+08:00",
        ])

    def test_non_soil2_observation_is_not_archived(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_dir = Path(temp_dir) / "history"
            with patch.object(vision_agent, "VISION_HISTORY_DIR", history_dir):
                written = vision_agent.append_visual_history(
                    "soil3",
                    "2026-07-16T18:00:00+08:00",
                    {"plant_health": "normal"},
                )

            self.assertFalse(written)
            self.assertFalse(history_dir.exists())

    def test_history_failure_does_not_prevent_current_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            image_dir = temp_root / "images"
            output_dir = temp_root / "output"
            image_dir.mkdir()
            (image_dir / "soil2.jpg").write_bytes(b"jpeg")
            stderr = io.StringIO()

            with patch.object(vision_agent, "IMAGE_DIR", image_dir), \
                 patch.object(vision_agent, "OUTPUT_DIR", output_dir), \
                 patch.object(vision_agent, "analyze_image", return_value="ok"), \
                 patch.object(
                     vision_agent,
                     "parse_visual",
                     return_value={"plant_health": "normal"},
                 ), \
                 patch.object(vision_agent, "analyze_leaf", return_value={}), \
                 patch.object(
                     vision_agent,
                     "current_observed_at",
                     return_value="2026-07-16T18:00:00+08:00",
                 ), \
                 patch.object(
                     vision_agent,
                     "append_visual_history",
                     side_effect=OSError("history unavailable"),
                 ), \
                 patch.object(vision_agent.sys, "argv", ["vision_agent.py", "soil2"]), \
                 redirect_stderr(stderr):
                vision_agent.main()

            snapshot = json.loads(
                (output_dir / "soil2_vision.json").read_text(encoding="utf-8")
            )

        self.assertEqual(snapshot["observed_at"], "2026-07-16T18:00:00+08:00")
        self.assertIn("视觉历史写入失败", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
