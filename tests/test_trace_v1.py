import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from services.soil3.trace.trace_v1 import (
    SCHEMA_VERSION,
    TRACE_ID_PATTERN,
    TraceError,
    TraceStore,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "services" / "soil3" / "trace" / "trace.v1.schema.json"
REFERENCE_SHA256 = "a" * 64


def reference(schema_version, path, record_id=None):
    return {
        "schema_version": schema_version,
        "path": path,
        "sha256": REFERENCE_SHA256,
        "record_id": record_id,
    }


def model_metrics(**overrides):
    value = {
        "provider": "qwen_dashscope",
        "model": "qwen3.8-Flash",
        "token_usage": {"source": "provider_response.usage", "value": 123, "unit": "tokens"},
        "cost": {"source": "provider_response.billing", "value": 0.0125, "unit": "CNY"},
        "latency": {"source": "runtime_monotonic_clock", "value": 842, "unit": "ms"},
    }
    value.update(overrides)
    return value


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


class TestDecisionUpdates(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = TraceStore(Path(self._temporary.name) / "traces")

    def test_state_reference_is_set_once_and_identical_retry_is_a_noop(self):
        """Catches replacing a decision-stage State binding after it was recorded."""
        trace_id = self.store.create()["trace_id"]
        state_ref = reference("state.v1", "runtime/state/latest.json")

        first = self.store.set_decision(trace_id, state_ref=state_ref)
        self.assertEqual(state_ref, first["decision"]["state_ref"])
        self.assertEqual(
            first,
            self.store.set_decision(trace_id, state_ref=copy.deepcopy(state_ref)),
        )
        with self.assertRaises(TraceError) as caught:
            self.store.set_decision(
                trace_id,
                state_ref=reference("state.v1", "runtime/state/other.json"),
            )
        self.assertIn("state_ref_already_set", caught.exception.reasons)

    def test_invalid_multi_field_update_keeps_record_byte_identical(self):
        """Catches partial persistence when one requested decision update is invalid."""
        trace_id = self.store.create()["trace_id"]
        path = self.store.trace_path(trace_id)
        before = path.read_bytes()

        with self.assertRaises(TraceError) as caught:
            self.store.set_decision(
                trace_id,
                state_ref=reference("state.v1", "runtime/state/latest.json"),
                gate_decision="approve",
            )
        self.assertIn("invalid_gate_decision", caught.exception.reasons)
        self.assertEqual(before, path.read_bytes())

    def test_vision_availability_requires_a_matching_reference_shape(self):
        """Catches unavailable/available Vision fields that silently invent or lose a record."""
        trace_id = self.store.create()["trace_id"]

        with self.assertRaises(TraceError) as caught:
            self.store.set_decision(
                trace_id,
                vision={"availability": "available", "ref": None},
            )
        self.assertIn("vision_available_requires_ref", caught.exception.reasons)
        stored = self.store.set_decision(
            trace_id,
            vision={
                "availability": "available",
                "ref": reference("vision.v1", "runtime/vision/zone-a.json"),
            },
        )
        self.assertEqual("available", stored["decision"]["vision"]["availability"])

    def test_metrics_accept_only_sourced_actual_values(self):
        """Catches fabricated or calculated-looking metrics without a real reported source."""
        trace_id = self.store.create()["trace_id"]

        stored = self.store.set_decision(trace_id, model_metrics=model_metrics())
        self.assertEqual("provider_response.usage", stored["decision"]["model_metrics"]["token_usage"]["source"])

        another_trace = self.store.create()["trace_id"]
        with self.assertRaises(TraceError) as caught:
            self.store.set_decision(
                another_trace,
                model_metrics=model_metrics(
                    latency={"source": "runtime_monotonic_clock", "value": -1, "unit": "ms"},
                ),
            )
        self.assertIn("latency_metric_invalid", caught.exception.reasons)

        estimated_trace = self.store.create()["trace_id"]
        with self.assertRaises(TraceError) as caught:
            self.store.set_decision(
                estimated_trace,
                model_metrics=model_metrics(
                    token_usage={"source": "estimated_from_prompt", "value": 123, "unit": "tokens"},
                ),
            )
        self.assertIn("token_usage_metric_invalid", caught.exception.reasons)

    def test_unknown_decision_field_is_refused_without_a_write(self):
        """Catches a generic patch path that could change unreviewed Trace state."""
        trace_id = self.store.create()["trace_id"]
        path = self.store.trace_path(trace_id)
        before = path.read_bytes()

        with self.assertRaises(TraceError) as caught:
            self.store.set_decision(trace_id, execution={"phase3_called": True})
        self.assertIn("unknown_decision_field:execution", caught.exception.reasons)
        self.assertEqual(before, path.read_bytes())

    def test_unhashable_gate_or_availability_values_are_structured_refusals(self):
        """Catches malformed JSON types escaping validation as implementation exceptions."""
        gate_trace = self.store.create()["trace_id"]
        with self.assertRaises(TraceError) as caught:
            self.store.set_decision(gate_trace, gate_decision=[])
        self.assertIn("invalid_gate_decision", caught.exception.reasons)

        vision_trace = self.store.create()["trace_id"]
        with self.assertRaises(TraceError) as caught:
            self.store.set_decision(
                vision_trace,
                vision={"availability": [], "ref": None},
            )
        self.assertIn("vision_availability_invalid", caught.exception.reasons)


class TestFeedbackAndOutcome(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = TraceStore(Path(self._temporary.name) / "traces")

    def test_feedback_references_only_append_new_records_in_order(self):
        """Catches replacement, reordering, or duplicate counting of feedback facts."""
        trace_id = self.store.create()["trace_id"]
        first = reference("feedback.v1", "runtime/feedback/fb-1.json", "fb-1")
        second = reference("feedback.v1", "runtime/feedback/fb-2.json", "fb-2")

        self.store.append_feedback_refs(trace_id, [first])
        updated = self.store.append_feedback_refs(trace_id, [second])
        self.assertEqual([first, second], updated["feedback_refs"])

        path = self.store.trace_path(trace_id)
        before = path.read_bytes()
        with self.assertRaises(TraceError) as caught:
            self.store.append_feedback_refs(trace_id, [first])
        self.assertIn("feedback_ref_already_appended", caught.exception.reasons)
        self.assertEqual(before, path.read_bytes())

    def test_feedback_refusal_for_empty_or_invalid_request_does_not_write(self):
        """Catches append endpoints that accept no fact or partially persist a malformed one."""
        trace_id = self.store.create()["trace_id"]
        path = self.store.trace_path(trace_id)
        before = path.read_bytes()

        with self.assertRaises(TraceError) as caught:
            self.store.append_feedback_refs(trace_id, [])
        self.assertIn("feedback_refs_required", caught.exception.reasons)
        self.assertEqual(before, path.read_bytes())

        with self.assertRaises(TraceError) as caught:
            self.store.append_feedback_refs(
                trace_id,
                [reference("feedback.v1", "runtime/feedback/fb-1.json", "fb-1"), {"bad": "ref"}],
            )
        self.assertIn("feedback_ref[1]_fields_invalid", caught.exception.reasons)
        self.assertEqual(before, path.read_bytes())

    def test_outcome_reference_moves_from_null_once_only(self):
        """Catches replacing the one later experiment Outcome tied to a trace."""
        trace_id = self.store.create()["trace_id"]
        outcome = reference("feedback.outcome.v1", "runtime/feedback/outcome.json")

        first = self.store.set_outcome_ref(trace_id, outcome)
        self.assertEqual(outcome, first["outcome_ref"])
        self.assertEqual(first, self.store.set_outcome_ref(trace_id, copy.deepcopy(outcome)))

        path = self.store.trace_path(trace_id)
        before = path.read_bytes()
        with self.assertRaises(TraceError) as caught:
            self.store.set_outcome_ref(
                trace_id,
                reference("feedback.outcome.v1", "runtime/feedback/other-outcome.json"),
            )
        self.assertIn("outcome_ref_already_set", caught.exception.reasons)
        self.assertEqual(before, path.read_bytes())


class TestTraceCli(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.store_dir = self.root / "traces"

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "services.soil3.trace.service", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_cli_creates_updates_and_reads_the_same_trace(self):
        """Catches a CLI that cannot persist the documented analysis-only lifecycle."""
        created = self.run_cli("--store-dir", str(self.store_dir), "create")
        self.assertEqual(0, created.returncode, created.stderr)
        trace_id = json.loads(created.stdout)["trace_id"]

        updates_path = self.write_json(
            "updates.json",
            {"state_ref": reference("state.v1", "runtime/state/latest.json")},
        )
        updated = self.run_cli(
            "--store-dir", str(self.store_dir),
            "set-decision",
            "--trace-id", trace_id,
            "--updates", str(updates_path),
        )
        self.assertEqual(0, updated.returncode, updated.stderr)
        self.assertEqual("state.v1", json.loads(updated.stdout)["decision"]["state_ref"]["schema_version"])

        read = self.run_cli("--store-dir", str(self.store_dir), "read", "--trace-id", trace_id)
        self.assertEqual(0, read.returncode, read.stderr)
        self.assertEqual(trace_id, json.loads(read.stdout)["trace_id"])

    def test_cli_returns_structured_refusal_for_execution_update(self):
        """Catches a command-line bypass that would let Trace become an execution input."""
        created = self.run_cli("--store-dir", str(self.store_dir), "create")
        trace_id = json.loads(created.stdout)["trace_id"]
        updates_path = self.write_json("invalid.json", {"execution": {"phase3_called": True}})

        result = self.run_cli(
            "--store-dir", str(self.store_dir),
            "set-decision",
            "--trace-id", trace_id,
            "--updates", str(updates_path),
        )
        self.assertEqual(2, result.returncode)
        error = json.loads(result.stderr)
        self.assertEqual("validation_failed", error["error"])
        self.assertIn("unknown_decision_field:execution", error["reasons"])


if __name__ == "__main__":
    unittest.main()
