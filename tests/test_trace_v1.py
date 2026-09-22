import json
import re
import tempfile
import unittest
from pathlib import Path

from services.soil3.trace.trace_v1 import (
    SCHEMA_VERSION,
    TRACE_ID_PATTERN,
    TraceStore,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "services" / "soil3" / "trace" / "trace.v1.schema.json"


class TestTraceCreation(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = TraceStore(Path(self._temporary.name) / "traces")

    def test_create_writes_explicit_analysis_only_empties(self):
        """Catches creation that omits empty associations or enables execution."""
        record = self.store.create()

        self.assertEqual("trace.v1", record["schema_version"])
        self.assertRegex(record["trace_id"], TRACE_ID_PATTERN)
        self.assertEqual(
            {"phase3_called": False, "physical_actions_performed": False},
            record["execution"],
        )
        self.assertEqual([], record["feedback_refs"])
        self.assertIsNone(record["outcome_ref"])
        self.assertEqual(
            {"availability": "not_requested", "ref": None},
            record["decision"]["vision"],
        )
        self.assertEqual(
            {"availability": "not_requested", "ref": None},
            record["decision"]["experience"],
        )
        self.assertIsNone(record["decision"]["state_ref"])
        self.assertIsNone(record["decision"]["strategy_ref"])
        self.assertIsNone(record["decision"]["gate_ref"])
        self.assertIsNone(record["decision"]["runner_ref"])
        self.assertIsNone(record["decision"]["bridge_ref"])
        self.assertIsNone(record["decision"]["episode_ref"])
        self.assertEqual(record, self.store.read(record["trace_id"]))


class TestTraceSchema(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = TraceStore(Path(self._temporary.name) / "traces")
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_matches_the_created_record_boundary(self):
        """Catches schema drift that would admit or reject a Store-created record."""
        record = self.store.create()

        self.assertEqual(set(self.schema["required"]), set(record))
        self.assertFalse(self.schema["additionalProperties"])
        self.assertEqual(SCHEMA_VERSION, self.schema["properties"]["schema_version"]["const"])
        self.assertEqual(TRACE_ID_PATTERN.pattern, self.schema["properties"]["trace_id"]["pattern"])
        self.assertRegex(record["created_at"], re.compile(self.schema["$defs"]["rfc3339_utc"]["pattern"]))
        self.assertRegex(record["updated_at"], re.compile(self.schema["$defs"]["rfc3339_utc"]["pattern"]))


if __name__ == "__main__":
    unittest.main()
