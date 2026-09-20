import copy
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services.soil3.cloud_strategy.validator import (
    fingerprint,
    is_canonical_timestamp,
    normalize_timestamp,
)
from services.soil3.episode.episode_v1 import (
    APPEND_SECTIONS,
    EXPECTED_DEVICE_CODE,
    EPISODE_ID_PATTERN,
    FORBIDDEN_PAYLOAD_KEYS,
    GATE_DECISIONS,
    SCHEMA_VERSION,
    SET_ONCE_SECTIONS,
    STATUS_CLOSED,
    STATUS_OPEN,
    TRACKED_SECTIONS,
    EpisodeError,
    EpisodeStore,
    new_episode_id,
    utc_now,
)
from services.soil3.state.state_v1 import StateBuilder


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "services" / "soil3" / "episode" / "episode.v1.schema.json"


def real_state(soil_humidity=31.4):
    """A state.v1 record from the actual producer, not a hand-made look-alike.

    episode.v1 embeds the snapshot and hashes it with the strategy.v1 binding
    hash, so every fixture goes through StateBuilder: if the producer changes
    shape, these tests notice. The humidity argument builds a second genuine
    record for the same device and the same observation moment, which is the
    case a content hash exists to separate.
    """
    return StateBuilder("soil3").build(
        {
            "observed_at": "2026-09-16T10:00:00Z",
            "generated_at": "2026-09-16T10:00:01Z",
            "sensor_readings": [
                {"timestamp": "2026-09-16T09:55:00Z", "humidity": soil_humidity, "temperature": 24.1, "ec_raw": 680, "lux": 1200},
                {"timestamp": "2026-09-16T07:00:00Z", "humidity": 33.0, "temperature": 22.0},
            ],
            "system_state": {
                "pump_active": False,
                "pump_total_cycles": 42,
                "total_water_sec_dispensed": 3100,
                "sensor_fault": 0,
                "hard_safety_low_guard": 1,
            },
            "watering_history": [{"timestamp": "2026-09-15T08:00:00Z", "water_sec": 60}],
            "environment": {
                "air": {
                    "humidity_percent": 58.0,
                    "temperature_c": 26.4,
                    "observed_at": "2026-09-16T09:50:00Z",
                    "source": "environment.air",
                }
            },
            "parameters": {"FC": 38.0, "TARGET_LOW": 40.0, "HARD_SAFETY_LOW": 25.0, "K_P": 1.2},
        }
    )


def valid_strategy(state):
    """A strategy.v1-shaped record genuinely bound to ``state``."""
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "device_code": state["device_code"],
        "state_observed_at": state["observed_at"],
        "state_generated_at": state["generated_at"],
        "state_sha256": fingerprint(state),
        "created_at": "2026-09-16T10:00:02Z",
        "actions": [{"action_id": "a1", "type": "water", "pump_seconds": 8}],
        "reason_summary": ["soil_moisture_declining", "previous_small_dose_insufficient"],
        "expected_outcome": {"soil_moisture": "increase", "risk_notes": ["overwatering_risk_low"]},
        "confidence": 0.78,
        "model": {"provider": "fixture", "name": "fixture-model", "prompt_version": "strategy-prompt.v2"},
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_dir = Path(self._tmp.name)
        self.store = EpisodeStore(self.store_dir)
        self.state = real_state()


class TestCreate(StoreTestCase):
    def test_create_opens_an_episode_bound_to_the_real_snapshot(self):
        record = self.store.create(self.state)
        self.assertEqual(record["schema_version"], SCHEMA_VERSION)
        self.assertEqual(record["device_code"], EXPECTED_DEVICE_CODE)
        self.assertRegex(record["episode_id"], EPISODE_ID_PATTERN)
        self.assertEqual(record["status"], STATUS_OPEN)
        self.assertIsNone(record["closed_at"])
        self.assertEqual(record["initial_state"], self.state)
        binding = record["state_binding"]
        self.assertEqual(binding["state_sha256"], fingerprint(self.state))
        self.assertEqual(binding["state_observed_at"], "2026-09-16T10:00:00Z")
        self.assertEqual(binding["state_generated_at"], "2026-09-16T10:00:01Z")
        for field in ("created_at", "updated_at"):
            self.assertTrue(is_canonical_timestamp(record[field]), record[field])

    def test_create_leaves_unsupplied_sections_empty_and_does_not_mutate_input(self):
        state_before = copy.deepcopy(self.state)
        record = self.store.create(self.state)
        self.assertEqual(self.state, state_before, "create must not mutate the caller's snapshot")
        self.assertIsNone(record["strategy"])
        self.assertIsNone(record["gate_result"])
        self.assertIsNone(record["outcome"])
        self.assertEqual(record["executed_actions"], [])
        self.assertEqual(record["feedback"], [])
        self.assertEqual(record["missing_facts"], [])
        record["strategy"] = {"injected": True}
        self.assertIsNone(self.store.read(record["episode_id"])["strategy"])

    def test_create_persists_one_json_file_per_episode(self):
        record = self.store.create(self.state)
        path = self.store.episode_path(record["episode_id"])
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)
        self.assertEqual(len(list(self.store_dir.glob("ep-*.json"))), 1)

    def test_create_separates_two_snapshots_of_the_same_moment_by_content(self):
        first = self.store.create(real_state(31.4))
        second = self.store.create(real_state(29.8))
        self.assertNotEqual(
            first["state_binding"]["state_sha256"], second["state_binding"]["state_sha256"]
        )
        self.assertEqual(
            first["state_binding"]["state_observed_at"], second["state_binding"]["state_observed_at"]
        )

    def test_create_refuses_input_that_is_not_a_soil3_state_v1(self):
        cases = [
            (None, ["initial_state_not_object"]),
            ({}, ["initial_state_not_state_v1", "initial_state_invalid_device_code"]),
            ({"schema_version": "state.v2", "device_code": "soil3"}, ["initial_state_not_state_v1"]),
            ({"schema_version": "state.v1", "device_code": "soil9"}, ["initial_state_invalid_device_code"]),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(EpisodeError) as caught:
                    self.store.create(payload)
                self.assertEqual(caught.exception.code, "validation_failed")
                self.assertEqual(sorted(caught.exception.reasons), sorted(expected))
        self.assertEqual(list(self.store_dir.glob("*.json")), [], "a refused create writes nothing")

    def test_create_refuses_reasoning_shaped_keys_in_the_snapshot(self):
        state = real_state()
        state["chain_of_thought"] = "the model mused about the soil"
        with self.assertRaises(EpisodeError) as caught:
            self.store.create(state)
        self.assertIn("initial_state_forbidden_key:chain_of_thought", caught.exception.reasons)

    def test_create_refuses_deeply_nested_reasoning_keys_without_writing(self):
        invalid = copy.deepcopy(self.state)
        invalid["extensions"] = {"quality_context": {"thinking": "not stored"}}

        with self.assertRaises(EpisodeError) as caught:
            self.store.create(invalid)
        self.assertIn("initial_state_forbidden_key:thinking", caught.exception.reasons)
        self.assertEqual([], list(self.store_dir.glob("*.json")))


class TestRead(StoreTestCase):
    def test_read_returns_the_stored_record_with_lifecycle_status(self):
        created = self.store.create(self.state)
        self.assertEqual(self.store.read(created["episode_id"]), created)
        self.assertEqual(self.store.read(created["episode_id"])["status"], STATUS_OPEN)

    def test_read_refuses_unknown_and_malformed_ids_before_touching_the_disk(self):
        with self.assertRaises(EpisodeError) as missing:
            self.store.read(new_episode_id())
        self.assertEqual(missing.exception.code, "episode_not_found")
        for bad_id in ("../secret", "ep-XYZ", "", None, "ep-" + "0" * 25):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(EpisodeError) as caught:
                    self.store.read(bad_id)
                self.assertEqual(caught.exception.code, "invalid_episode_id")

    def test_read_reports_a_corrupt_file_instead_of_crashing(self):
        record = self.store.create(self.state)
        path = self.store.episode_path(record["episode_id"])
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(EpisodeError) as caught:
            self.store.read(record["episode_id"])
        self.assertEqual(caught.exception.code, "episode_file_corrupt")
        path.write_text(json.dumps({"schema_version": "other.v1"}), encoding="utf-8")
        with self.assertRaises(EpisodeError):
            self.store.read(record["episode_id"])


class TestUpdate(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.record = self.store.create(self.state)
        self.episode_id = self.record["episode_id"]

    def test_update_attaches_strategy_and_preserves_reason_and_confidence(self):
        strategy = valid_strategy(self.state)
        updated = self.store.update(self.episode_id, strategy=strategy)
        self.assertEqual(updated["strategy"], strategy)
        self.assertEqual(updated["strategy"]["reason_summary"], strategy["reason_summary"])
        self.assertEqual(updated["strategy"]["confidence"], 0.78)
        self.assertTrue(is_canonical_timestamp(updated["updated_at"]))

    def test_update_appends_fact_sections_in_order(self):
        first = self.store.update(
            self.episode_id,
            executed_actions=[{"action_id": "a1", "type": "water", "pump_seconds": 8, "executed_at": "2026-09-16T10:05:00Z"}],
        )
        second = self.store.update(
            self.episode_id,
            executed_actions=[{"action_id": "a1", "type": "completed", "result": "ok"}],
            feedback=[{"observed_at": "2026-09-16T10:35:00Z", "humidity_percent": 36.2}],
        )
        self.assertEqual(len(second["executed_actions"]), 2)
        self.assertEqual(second["executed_actions"][0], first["executed_actions"][0])
        self.assertEqual(len(second["feedback"]), 1)

    def test_update_normalizes_parseable_fact_timestamps_and_keeps_unparseable_ones(self):
        epoch = 1789800000  # an instant in 2026, written as a numeric epoch
        updated = self.store.update(
            self.episode_id,
            executed_actions=[{"executed_at": epoch}],
            feedback=[{"observed_at": "telemetry-clock-broken"}],
        )
        self.assertEqual(updated["executed_actions"][0]["executed_at"], normalize_timestamp(epoch))
        self.assertTrue(is_canonical_timestamp(updated["executed_actions"][0]["executed_at"]))
        self.assertEqual(
            updated["feedback"][0]["observed_at"], "telemetry-clock-broken",
            "an unparseable producer timestamp must be preserved, never invented",
        )

    def test_update_refuses_a_strategy_bound_to_a_different_snapshot(self):
        other_strategy = valid_strategy(real_state(29.8))
        with self.assertRaises(EpisodeError) as by_hash:
            self.store.update(self.episode_id, strategy=other_strategy)
        self.assertIn("strategy_state_sha256_mismatch", by_hash.exception.reasons)

        same_hash_other_moment = valid_strategy(self.state)
        same_hash_other_moment["state_observed_at"] = "2026-09-16T09:00:00Z"
        with self.assertRaises(EpisodeError) as by_moment:
            self.store.update(self.episode_id, strategy=same_hash_other_moment)
        self.assertIn("strategy_state_observed_at_mismatch", by_moment.exception.reasons)
        self.assertIsNone(self.store.read(self.episode_id)["strategy"])

    def test_update_refuses_malformed_caller_sections(self):
        cases = {
            "strategy": [
                ([], "strategy_not_object"),
                ({"schema_version": "strategy.v2"}, "strategy_invalid_schema_version"),
                ({"device_code": "soil9"}, "strategy_invalid_device_code"),
                ({"confidence": 1.7}, "strategy_invalid_confidence"),
                ({"confidence": True}, "strategy_invalid_confidence"),
                ({"reason_summary": "one string"}, "strategy_invalid_reason_summary"),
                ({"reasoning_trace": "hidden steps"}, "strategy_forbidden_key:reasoning_trace"),
            ],
            "gate_result": [
                ("allow", "gate_result_not_object"),
                ({"decision": "maybe"}, "gate_result_unknown_decision"),
                ({"reason_codes": "deny:low"}, "gate_result_invalid_reason_codes"),
                ({"thinking": "..." }, "gate_result_forbidden_key:thinking"),
            ],
            "outcome": [
                (42, "outcome_not_object"),
                ({"hidden_reasoning": "x"}, "outcome_forbidden_key:hidden_reasoning"),
            ],
        }
        for section, payloads in cases.items():
            for payload, expected_reason in payloads:
                with self.subTest(section=section, payload=payload):
                    with self.assertRaises(EpisodeError) as caught:
                        self.store.update(self.episode_id, **{section: payload})
                    self.assertEqual(caught.exception.code, "validation_failed")
                    self.assertIn(expected_reason, caught.exception.reasons)

    def test_update_refuses_non_list_fact_sections_and_non_object_entries(self):
        with self.assertRaises(EpisodeError) as caught:
            self.store.update(self.episode_id, executed_actions={"action_id": "a1"})
        self.assertIn("executed_actions_not_list", caught.exception.reasons)
        with self.assertRaises(EpisodeError) as caught:
            self.store.update(self.episode_id, feedback=["humidity rose", {"chain_of_thought": "x"}])
        self.assertIn("feedback[0]:not_object", caught.exception.reasons)
        self.assertIn("feedback[1]:forbidden_key:chain_of_thought", caught.exception.reasons)
        self.assertEqual(self.store.read(self.episode_id)["feedback"], [])

    def test_update_refuses_nested_feedback_evidence_without_changing_file(self):
        path = self.store.episode_path(self.episode_id)
        before = path.read_bytes()
        feedback = [
            {
                "evidence": {
                    "measurements": [
                        {"samples": [{"chain_of_thought": "not stored"}]},
                    ],
                },
            },
        ]

        with self.assertRaises(EpisodeError) as caught:
            self.store.update(self.episode_id, feedback=feedback)
        self.assertIn("feedback[0]:forbidden_key:chain_of_thought", caught.exception.reasons)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([], self.store.read(self.episode_id)["feedback"])

    def test_forbidden_key_check_is_case_insensitive(self):
        for key in sorted(FORBIDDEN_PAYLOAD_KEYS):
            with self.subTest(key=key):
                with self.assertRaises(EpisodeError) as caught:
                    self.store.update(self.episode_id, outcome={key.title(): "leak"})
                self.assertIn(f"outcome_forbidden_key:{key.title()}", caught.exception.reasons)

    def test_set_once_sections_reject_a_different_value_but_accept_the_same_one(self):
        for section in SET_ONCE_SECTIONS:
            with self.subTest(section=section):
                if section == "strategy":
                    value, other = valid_strategy(self.state), valid_strategy(self.state)
                elif section == "gate_result":
                    value, other = {"decision": "allow"}, {"decision": "deny"}
                else:
                    value, other = {"recovery": "good"}, {"recovery": "poor"}
                self.store.update(self.episode_id, **{section: value})
                self.store.update(self.episode_id, **{section: copy.deepcopy(value)})
                with self.assertRaises(EpisodeError) as caught:
                    self.store.update(self.episode_id, **{section: other})
                self.assertIn(f"{section}_already_set", caught.exception.reasons)
                self.assertEqual(self.store.read(self.episode_id)[section], value)

    def test_update_is_all_or_nothing_on_disk(self):
        before = self.store.read(self.episode_id)
        with self.assertRaises(EpisodeError):
            self.store.update(
                self.episode_id,
                gate_result={"decision": "allow"},
                feedback=["not an object"],
            )
        self.assertEqual(self.store.read(self.episode_id), before)

    def test_update_requires_something_to_update(self):
        with self.assertRaises(EpisodeError) as caught:
            self.store.update(self.episode_id)
        self.assertEqual(caught.exception.code, "nothing_to_update")


class TestClose(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.episode_id = self.store.create(self.state)["episode_id"]

    def test_close_finalizes_status_timestamp_and_missing_facts(self):
        closed = self.store.close(self.episode_id)
        self.assertEqual(closed["status"], STATUS_CLOSED)
        self.assertTrue(is_canonical_timestamp(closed["closed_at"]))
        self.assertEqual(closed["missing_facts"], list(TRACKED_SECTIONS))
        self.assertEqual(self.store.read(self.episode_id), closed)

    def test_close_keeps_supplied_facts_and_states_only_real_absences(self):
        self.store.update(
            self.episode_id,
            strategy=valid_strategy(self.state),
            gate_result={"decision": "allow", "reason_codes": ["exploration_budget_ok"]},
            executed_actions=[{"action_id": "a1", "type": "water", "pump_seconds": 8}],
        )
        outcome = {
            "recovery": "good",
            "persistence": "good",
            "underwatering": False,
            "overwatering": False,
            "rewater_needed": False,
            "visual_recovery": "good",
            "data_quality": "good",
        }
        closed = self.store.close(self.episode_id, outcome=outcome)
        self.assertEqual(closed["outcome"], outcome)
        self.assertEqual(closed["gate_result"]["decision"], "allow")
        self.assertEqual(closed["missing_facts"], ["feedback"])

    def test_close_accepts_an_outcome_already_set_while_open(self):
        outcome = {"recovery": "poor", "data_quality": "partial"}
        self.store.update(self.episode_id, outcome=outcome)
        closed = self.store.close(self.episode_id)
        self.assertEqual(closed["outcome"], outcome)
        self.assertNotIn("outcome", closed["missing_facts"])
        with self.assertRaises(EpisodeError):
            self.store.close(self.episode_id)

    def test_close_refuses_a_conflicting_outcome_and_stays_recoverable(self):
        self.store.update(self.episode_id, outcome={"recovery": "good"})
        with self.assertRaises(EpisodeError) as caught:
            self.store.close(self.episode_id, outcome={"recovery": "poor"})
        self.assertIn("outcome_already_set", caught.exception.reasons)
        self.assertEqual(self.store.read(self.episode_id)["status"], STATUS_OPEN)
        closed = self.store.close(self.episode_id, outcome={"recovery": "good"})
        self.assertEqual(closed["status"], STATUS_CLOSED)

    def test_closed_episode_is_immutable(self):
        self.store.close(self.episode_id)
        with self.assertRaises(EpisodeError) as caught:
            self.store.update(self.episode_id, feedback=[{"observed_at": "2026-09-17T10:00:00Z"}])
        self.assertEqual(caught.exception.code, "episode_not_open")
        with self.assertRaises(EpisodeError) as again:
            self.store.close(self.episode_id)
        self.assertEqual(again.exception.code, "episode_already_closed")

    def test_denied_and_failed_episodes_close_and_are_kept(self):
        self.store.update(self.episode_id, gate_result={"decision": "deny", "reason_codes": ["sensor_fault"]})
        closed = self.store.close(self.episode_id, outcome={"data_quality": "bad"})
        self.assertEqual(closed["status"], STATUS_CLOSED)
        self.assertIn("executed_actions", closed["missing_facts"])
        self.assertTrue(self.store.episode_path(self.episode_id).exists())


class TestPersistence(StoreTestCase):
    def test_records_survive_a_new_store_instance(self):
        episode_id = self.store.create(self.state)["episode_id"]
        self.store.update(episode_id, gate_result={"decision": "allow_with_warning"})
        reopened = EpisodeStore(self.store_dir)
        record = reopened.read(episode_id)
        self.assertEqual(record["gate_result"], {"decision": "allow_with_warning"})
        self.assertEqual(reopened.close(episode_id)["status"], STATUS_CLOSED)

    def test_store_writes_nothing_outside_its_directory(self):
        self.store.create(self.state)
        nested = list(self.store_dir.rglob("*"))
        self.assertTrue(all(path.suffix == ".json" for path in nested if path.is_file()))
        self.assertTrue(all(path.parent == self.store_dir for path in nested))


class TestSchemaConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_required_fields_match_the_record_keys_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = EpisodeStore(tmp).create(real_state())
        self.assertEqual(set(self.schema["required"]), set(record))
        self.assertFalse(self.schema["additionalProperties"])
        self.assertEqual(self.schema["properties"]["schema_version"]["const"], SCHEMA_VERSION)
        self.assertEqual(self.schema["properties"]["device_code"]["const"], EXPECTED_DEVICE_CODE)

    def test_schema_enums_match_the_code_constants(self):
        self.assertEqual(self.schema["properties"]["status"]["enum"], [STATUS_OPEN, STATUS_CLOSED])
        self.assertEqual(
            self.schema["properties"]["missing_facts"]["items"]["enum"], list(TRACKED_SECTIONS)
        )
        self.assertEqual(
            self.schema["properties"]["episode_id"]["pattern"], EPISODE_ID_PATTERN.pattern
        )
        for decision in GATE_DECISIONS:
            self.assertIn(decision, self.schema["properties"]["gate_result"]["description"])

    def test_generated_timestamps_and_ids_match_the_schema_patterns(self):
        import re

        rfc3339 = re.compile(self.schema["$defs"]["rfc3339_utc"]["pattern"])
        self.assertRegex(utc_now(), rfc3339)
        with tempfile.TemporaryDirectory() as tmp:
            record = EpisodeStore(tmp).create(real_state())
        self.assertRegex(record["episode_id"], re.compile(self.schema["properties"]["episode_id"]["pattern"]))
        for field in ("created_at", "updated_at"):
            self.assertRegex(record[field], rfc3339)
        self.assertRegex(record["state_binding"]["state_sha256"],
                         re.compile(self.schema["properties"]["state_binding"]["properties"]["state_sha256"]["pattern"]))

    def test_record_serializes_to_json_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EpisodeStore(tmp)
            episode_id = store.create(real_state())["episode_id"]
            store.update(episode_id, feedback=[{"observed_at": datetime.now(timezone.utc).isoformat()}])
            record = store.close(episode_id)
        self.assertEqual(json.loads(json.dumps(record)), record)


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.store_dir = self.dir / "episodes"
        self.state_path = self.dir / "state.json"
        self.state_path.write_text(json.dumps(real_state()), encoding="utf-8")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "services.soil3.episode.service",
             "--store-dir", str(self.store_dir), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )

    def write_payload(self, name, payload):
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_cli_full_lifecycle_round_trip(self):
        created = self.run_cli("create", "--state", str(self.state_path))
        self.assertEqual(created.returncode, 0, created.stderr)
        summary = json.loads(created.stdout)
        episode_id = summary["episode_id"]
        self.assertEqual(summary["status"], STATUS_OPEN)

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        strategy_path = self.write_payload("strategy.json", valid_strategy(state))
        gate_path = self.write_payload("gate.json", {"decision": "allow", "reason_codes": ["ok"]})
        actions_path = self.write_payload("actions.json", [{"action_id": "a1", "type": "water", "pump_seconds": 8}])
        outcome_path = self.write_payload("outcome.json", {"recovery": "good", "data_quality": "good"})

        updated = self.run_cli(
            "update", "--episode-id", episode_id,
            "--strategy", str(strategy_path),
            "--gate-result", str(gate_path),
            "--executed-actions", str(actions_path),
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)

        read = self.run_cli("read", "--episode-id", episode_id)
        self.assertEqual(read.returncode, 0, read.stderr)
        record = json.loads(read.stdout)
        self.assertEqual(record["strategy"]["confidence"], 0.78)

        closed = self.run_cli("close", "--episode-id", episode_id, "--outcome", str(outcome_path))
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(json.loads(closed.stdout)["status"], STATUS_CLOSED)
        self.assertEqual(json.loads(closed.stdout)["missing_facts"], ["feedback"])

    def test_cli_reports_refusals_as_structured_errors(self):
        bad_state = self.write_payload("bad_state.json", {"schema_version": "state.v9"})
        result = self.run_cli("create", "--state", str(bad_state))
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stderr)
        self.assertEqual(error["error"], "validation_failed")
        self.assertIn("initial_state_not_state_v1", error["reasons"])

        missing = self.run_cli("read", "--episode-id", new_episode_id())
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(json.loads(missing.stderr)["error"], "episode_not_found")


if __name__ == "__main__":
    unittest.main()
