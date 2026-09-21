import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from services.soil3.experience_retrieval.retrieval_v1 import (
    ExperienceRetriever,
    RetrievalError,
    classify_outcome,
    compare_states,
)


ROOT = Path(__file__).resolve().parents[1]


def state(
    humidity=31.0,
    *,
    observed_at="2026-09-21T08:00:00Z",
    trends=None,
    air_humidity=58.0,
    air_temperature=24.0,
    last_water_at="2026-09-20T20:00:00Z",
    last_water_sec=8.0,
    flags=None,
):
    return {
        "schema_version": "state.v1",
        "device_code": "soil3",
        "observed_at": observed_at,
        "soil": {"humidity_percent": humidity},
        "trends": trends or {"humidity_1h": -1.0, "humidity_3h": -2.5, "humidity_6h": -4.0},
        "air": {"humidity_percent": air_humidity, "temperature_c": air_temperature},
        "irrigation": {"last_water_at": last_water_at, "last_water_sec": last_water_sec},
        "safety": {
            "target_low": 30.0,
            "hard_safety_low": 22.0,
            "flags": {} if flags is None else flags,
        },
    }


def episode(identifier, initial_state, outcome, *, status="closed", actions=None):
    return {
        "schema_version": "episode.v1",
        "episode_id": identifier,
        "device_code": "soil3",
        "status": status,
        "initial_state": initial_state,
        "executed_actions": actions or [],
        "outcome": outcome,
    }


class ExperienceRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.current = state()

    def tearDown(self):
        self.tmp.cleanup()

    def write_episode(self, value):
        path = self.root / f"{value['episode_id']}.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_ranks_success_and_failure_separately_with_explanations(self):
        closest = episode(
            "ep-000000000000000000000001",
            state(humidity=32.0),
            {"recovery": "good", "soil_delta": 5.0},
            actions=[{"kind": "water", "pump_seconds": 8.0, "physical_action_performed": True}],
        )
        farther = episode(
            "ep-000000000000000000000002",
            state(humidity=44.0, air_temperature=31.0),
            {"result": "success"},
        )
        failed = episode(
            "ep-000000000000000000000003",
            state(humidity=29.0, flags={"water_delivery_suspect": True}),
            {"status": "failed", "reason": "delivery did not respond"},
        )
        for value in (farther, failed, closest):
            self.write_episode(value)

        result = ExperienceRetriever(self.root).retrieve(self.current, limit_per_class=2)

        self.assertEqual(result["schema_version"], "experience_retrieval.v1")
        self.assertEqual(result["history_scanned"], 3)
        self.assertEqual(result["usable_history"], {"success": 2, "failure": 1})
        self.assertEqual(
            [item["episode_id"] for item in result["successful_cases"]],
            [closest["episode_id"], farther["episode_id"]],
        )
        self.assertEqual(result["failed_cases"][0]["episode_id"], failed["episode_id"])
        self.assertTrue(result["availability"]["both_classes"])
        match = result["successful_cases"][0]
        features = {item["feature"] for item in match["matched_on"]}
        self.assertIn("soil_humidity_percent", features)
        self.assertIn("hours_since_last_water", features)
        self.assertIn("safety_flags", features)
        self.assertGreater(match["similarity_score"], result["successful_cases"][1]["similarity_score"])
        self.assertEqual(match["summary"]["outcome"], closest["outcome"])
        self.assertEqual(match["summary"]["executed_actions"][0]["pump_seconds"], 8.0)

    def test_no_history_is_explicitly_empty(self):
        result = ExperienceRetriever(self.root).retrieve(self.current)
        self.assertEqual(result["successful_cases"], [])
        self.assertEqual(result["failed_cases"], [])
        self.assertEqual(
            result["empty_reasons"],
            ["no_usable_success_history", "no_usable_failure_history"],
        )
        self.assertEqual(result["history_scanned"], 0)

    def test_open_unknown_conflicting_and_malformed_records_are_not_guessed(self):
        self.write_episode(episode("ep-000000000000000000000010", state(), {"result": "success"}, status="open"))
        self.write_episode(episode("ep-000000000000000000000011", state(), {"result": "unclear"}))
        self.write_episode(episode(
            "ep-000000000000000000000012", state(), {"result": "success", "recovery": "poor"}
        ))
        (self.root / "ep-000000000000000000000013.json").write_text("{bad", encoding="utf-8")

        result = ExperienceRetriever(self.root).retrieve(self.current)

        self.assertEqual(result["usable_history"], {"success": 0, "failure": 0})
        self.assertEqual(result["skipped_history"]["not_closed"], 1)
        self.assertEqual(result["skipped_history"]["outcome_unclassified"], 2)
        self.assertEqual(result["skipped_history"]["malformed_or_unreadable"], 1)

    def test_insufficient_state_evidence_is_unavailable(self):
        sparse = {
            "schema_version": "state.v1",
            "device_code": "soil3",
            "observed_at": "2026-09-20T08:00:00Z",
            "soil": {"humidity_percent": 31.0},
        }
        self.write_episode(episode("ep-000000000000000000000020", sparse, {"result": "success"}))

        result = ExperienceRetriever(self.root).retrieve(self.current)

        self.assertEqual(result["successful_cases"], [])
        self.assertEqual(result["skipped_history"]["insufficient_state_evidence"], 1)

    def test_missing_dimensions_reduce_coverage_without_inventing_values(self):
        historical = state()
        historical["trends"]["humidity_6h"] = None
        historical["air"]["temperature_c"] = None
        comparison = compare_states(self.current, historical)
        self.assertNotIn("humidity_trend_6h", {item["feature"] for item in comparison["matched_on"]})
        self.assertIn("humidity_trend_6h", comparison["missing_components"])
        self.assertIn("air_temperature_c", comparison["missing_components"])
        self.assertAlmostEqual(comparison["evidence_coverage"], 0.88)

    def test_retrieval_does_not_modify_episode_files_or_inputs(self):
        value = episode("ep-000000000000000000000030", state(), {"classification": "effective"})
        path = self.write_episode(value)
        before_bytes = path.read_bytes()
        before_state = copy.deepcopy(self.current)

        ExperienceRetriever(self.root).retrieve(self.current)

        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(self.current, before_state)

    def test_explicit_outcome_vocabulary(self):
        self.assertEqual(classify_outcome({"recovery": "good"}), "success")
        self.assertEqual(classify_outcome({"recovery": "poor"}), "failure")
        self.assertIsNone(classify_outcome({"soil_delta": 8.0}))
        self.assertIsNone(classify_outcome({"result": "success", "status": "failed"}))

    def test_invalid_request_is_rejected(self):
        with self.assertRaisesRegex(RetrievalError, "state.v1"):
            ExperienceRetriever(self.root).retrieve({})
        wrong = state()
        wrong["device_code"] = "soil2"
        with self.assertRaisesRegex(RetrievalError, "soil3"):
            ExperienceRetriever(self.root).retrieve(wrong)
        with self.assertRaisesRegex(RetrievalError, "positive integer"):
            ExperienceRetriever(self.root).retrieve(self.current, limit_per_class=0)

    def test_cli_prints_structured_empty_result(self):
        state_path = self.root / "state.json"
        state_path.write_text(json.dumps(self.current), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "services.soil3.experience_retrieval.service",
                "--episode-dir",
                str(self.root / "missing"),
                "--state",
                str(state_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["history_scanned"], 0)


if __name__ == "__main__":
    unittest.main()
