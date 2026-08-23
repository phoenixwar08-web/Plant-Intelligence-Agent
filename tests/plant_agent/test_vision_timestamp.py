"""Tests for the timestamp emitted with new visual observations."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from agent.vision_agent import current_observed_at


class VisionTimestampTests(unittest.TestCase):
    def test_current_observed_at_is_timezone_aware_shanghai_iso8601(self):
        observed_at = current_observed_at()
        parsed = datetime.fromisoformat(observed_at)

        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))
        self.assertTrue(observed_at.endswith("+08:00"))


if __name__ == "__main__":
    unittest.main()
