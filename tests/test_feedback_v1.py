import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.soil3.cloud_strategy.validator import is_canonical_timestamp
from services.soil3.episode.episode_v1 import EpisodeError, EpisodeStore
from services.soil3.feedback.feedback_v1 import (
    ASSESSMENT_KEYS,
    BOOL_LIKE_ASSESSMENTS,
    CONTRADICTION_BEFORE_REFERENCE,
    CONTRADICTION_NO_SOIL_OBSERVATION,
    CONTRADICTION_NO_VISION_OBSERVATION,
    CONTRADICTION_OUTSIDE_WINDOW,
    CONTRADICTION_SUSTAINED_DRY_AND_WET,
    DATA_QUALITY_VALUES,
    EXPECTED_DEVICE_CODE,
    FEEDBACK_ID_PATTERN,
    OBSERVATION_KEYS,
    RECOVERY_VALUES,
    SCHEMA_VERSION,
    UNKNOWN,
    VISUAL_RECOVERY_VALUES,
    WINDOWS,
    FeedbackError,
    FeedbackStore,
)
from services.soil3.state.state_v1 import StateBuilder


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "services" / "soil3" / "feedback" / "feedback.v1.schema.json"

# A pattern-valid episode id for records that are never attached. Tests that
# attach build a real episode through EpisodeStore instead.
TEST_EPISODE_ID = "ep-0123456789abcdef01234567"
OTHER_EPISODE_ID = "ep-fedcba9876543210fedcba98"

# The watering action the windows are measured from.
REFERENCE = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
WINDOW_OFFSET_MINUTES = {"30min": 30, "2-3h": 150, "6-12h": 480, "24h": 1440}


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def soil_facts(humidity=36.2):
    return {"humidity_percent": humidity, "humidity_before_percent": 31.4, "reading_count": 3}


def vision_facts():
    return {"zone": "z1", "leaf_color": "green", "wilting": "none"}


def full_assessments():
    return {
        "recovery": "good",
        "sustained_dry": False,
        "sustained_wet": False,
        "rewater_needed": False,
        "visual_recovery": "improved",
        "data_quality": "good",
    }


def payload(window="30min", episode_id=TEST_EPISODE_ID, **overrides):
    """A complete, internally consistent window observation."""
    offset = WINDOW_OFFSET_MINUTES.get(window, 30)  # invalid windows still need a time
    base = {
        "episode_id": episode_id,
        "window": window,
        "observed_at": iso(REFERENCE + timedelta(minutes=offset)),
        "reference_action_at": iso(REFERENCE),
        "observations": {"soil": soil_facts(), "vision": vision_facts()},
        "assessments": full_assessments(),
    }
    base.update(overrides)
    return base


def real_state(soil_humidity=31.4):
    """A state.v1 record from the actual producer, as in the episode tests."""
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


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.store_dir = self.dir / "feedback"
        self.store = FeedbackStore(self.store_dir)

    def stored_files(self):
        if not self.store_dir.exists():
            return []
        return sorted(path.name for path in self.store_dir.iterdir())


class TestRecordNormal(StoreTestCase):
    def test_all_four_windows_are_representable(self):
        records = [self.store.record(payload(window=window)) for window in WINDOWS]
        self.assertEqual([record["window"] for record in records], list(WINDOWS))
        listed = self.store.list_for_episode(TEST_EPISODE_ID)
        self.assertEqual([record["window"] for record in listed], list(WINDOWS))
        outcome = self.store.outcome(TEST_EPISODE_ID)
        self.assertEqual(outcome["windows_included"], list(WINDOWS))
        self.assertEqual(outcome["windows_missing"], [])
        self.assertEqual(outcome["record_count"], 4)

    def test_record_stores_supplied_facts_unchanged(self):
        supplied = payload()
        record = self.store.record(supplied)
        self.assertEqual(record["schema_version"], SCHEMA_VERSION)
        self.assertEqual(record["device_code"], EXPECTED_DEVICE_CODE)
        self.assertRegex(record["feedback_id"], FEEDBACK_ID_PATTERN)
        self.assertEqual(record["episode_id"], TEST_EPISODE_ID)
        self.assertEqual(record["window"], "30min")
        self.assertEqual(record["observations"]["soil"], soil_facts())
        self.assertEqual(record["observations"]["vision"], vision_facts())
        self.assertEqual(record["assessments"], full_assessments())
        self.assertEqual(record["contradictions"], [])
        self.assertEqual(record["missing_observations"], [])
        for field in ("observed_at", "reference_action_at", "created_at"):
            self.assertTrue(is_canonical_timestamp(record[field]), record[field])
        # The supplied payload is not mutated by recording.
        self.assertEqual(supplied, payload())

    def test_timestamp_notation_is_normalized(self):
        record = self.store.record(payload(observed_at="2026-09-16T18:30:00+08:00"))
        self.assertEqual(record["observed_at"], "2026-09-16T10:30:00Z")
        self.assertEqual(record["contradictions"], [])

    def test_reference_action_at_is_optional(self):
        supplied = payload()
        del supplied["reference_action_at"]
        record = self.store.record(supplied)
        self.assertIsNone(record["reference_action_at"])
        self.assertEqual(record["contradictions"], [])

    def test_unparseable_reference_is_kept_verbatim_and_skips_timing_check(self):
        record = self.store.record(payload(reference_action_at="sometime yesterday"))
        self.assertEqual(record["reference_action_at"], "sometime yesterday")
        self.assertNotIn(CONTRADICTION_OUTSIDE_WINDOW, record["contradictions"])
        self.assertNotIn(CONTRADICTION_BEFORE_REFERENCE, record["contradictions"])

    def test_read_returns_deep_copy(self):
        record = self.store.record(payload())
        loaded = self.store.read(record["feedback_id"])
        self.assertEqual(loaded, record)
        loaded["assessments"]["recovery"] = "poor"
        loaded["observations"]["soil"]["humidity_percent"] = 0.0
        self.assertEqual(self.store.read(record["feedback_id"]), record)

    def test_multiple_records_per_window_are_all_kept(self):
        first = self.store.record(payload(observations={"soil": soil_facts(33.0), "vision": None}))
        second = self.store.record(
            payload(
                observed_at=iso(REFERENCE + timedelta(minutes=45)),
                observations={"soil": soil_facts(35.0), "vision": None},
            )
        )
        listed = self.store.list_for_episode(TEST_EPISODE_ID)
        self.assertEqual([record["feedback_id"] for record in listed],
                         [first["feedback_id"], second["feedback_id"]])
        outcome = self.store.outcome(TEST_EPISODE_ID)
        self.assertEqual(outcome["record_count"], 2)
        self.assertEqual(outcome["feedback_ids"], [second["feedback_id"]])


class TestRecordRefusals(StoreTestCase):
    def refuse_cases(self):
        return [
            ("not a dict", "payload_not_object"),
            ({}, "observed_at_required"),
            (payload(window="1h"), "unknown_window"),
            (payload(observed_at=None), "observed_at_required"),
            (payload(observed_at="yesterday"), "observed_at_unparseable"),
            (payload(episode_id=None), "episode_id_required"),
            (payload(episode_id="ep-not-hex"), "invalid_episode_id"),
            (payload(device_code="soil4"), "invalid_device_code"),
            (payload(reference_action_at={"bad": 1}), "reference_action_at_unparseable"),
            (payload(reward=0.5), "unknown_field:reward"),
            (payload(contradictions=[]), "unknown_field:contradictions"),
            (payload(missing_observations=[]), "unknown_field:missing_observations"),
            (payload(feedback_id="fb-" + "a" * 24), "unknown_field:feedback_id"),
            (payload(observations=[]), "observations_not_object"),
            (payload(observations={"audio": {}}), "unknown_observation:audio"),
            (payload(observations={"soil": 42}), "observation_not_object:soil"),
            (payload(assessments=[]), "assessments_not_object"),
            (payload(assessments={"recovery": "excellent"}), "invalid_assessment:recovery"),
            (payload(assessments={"sustained_dry": "yes"}), "invalid_assessment:sustained_dry"),
            (payload(assessments={"sustained_dry": 1}), "invalid_assessment:sustained_dry"),
            (payload(assessments={"visual_recovery": True}), "invalid_assessment:visual_recovery"),
            (payload(assessments={"reward": 0.9}), "unknown_assessment:reward"),
            (payload(thinking="maybe I should water more"), "forbidden_key:thinking"),
            (payload(observations={"soil": {"chain_of_thought": "..."}}),
             "forbidden_key:observations.soil.chain_of_thought"),
            (payload(assessments={"recovery": "good", "hidden_reasoning": "..."}),
             "forbidden_key:assessments.hidden_reasoning"),
        ]

    def test_refused_payloads_fail_loudly_and_write_nothing(self):
        for case, expected_reason in self.refuse_cases():
            with self.subTest(case=expected_reason):
                with self.assertRaises(FeedbackError) as caught:
                    self.store.record(case)
                self.assertEqual(caught.exception.code, "validation_failed")
                self.assertIn(expected_reason, caught.exception.reasons)
                self.assertEqual(self.stored_files(), [])


class TestMissingFacts(StoreTestCase):
    def test_missing_assessments_stay_unknown(self):
        record = self.store.record(payload(assessments={"recovery": "partial"}))
        self.assertEqual(record["assessments"]["recovery"], "partial")
        for key in ASSESSMENT_KEYS:
            if key != "recovery":
                self.assertEqual(record["assessments"][key], UNKNOWN, key)

    def test_missing_observations_are_listed_not_invented(self):
        assessments = full_assessments()
        assessments["visual_recovery"] = UNKNOWN  # nothing visual was observed
        record = self.store.record(
            payload(observations={"soil": soil_facts()}, assessments=assessments)
        )
        self.assertIsNone(record["observations"]["vision"])
        self.assertEqual(record["missing_observations"], ["vision"])
        self.assertEqual(record["contradictions"], [])

    def test_bare_minimum_payload_is_still_recorded(self):
        minimal = {
            "episode_id": TEST_EPISODE_ID,
            "window": "6-12h",
            "observed_at": iso(REFERENCE + timedelta(minutes=480)),
        }
        record = self.store.record(minimal)
        self.assertEqual(record["missing_observations"], list(OBSERVATION_KEYS))
        self.assertEqual(
            record["assessments"], {key: UNKNOWN for key in ASSESSMENT_KEYS}
        )
        self.assertEqual(record["contradictions"], [])
        self.assertIsNone(record["reference_action_at"])

    def test_explicit_unknown_is_preserved(self):
        record = self.store.record(payload(assessments={"recovery": UNKNOWN, "data_quality": UNKNOWN}))
        self.assertEqual(record["assessments"]["recovery"], UNKNOWN)
        self.assertEqual(record["assessments"]["data_quality"], UNKNOWN)
        self.assertEqual(record["contradictions"], [])


class TestContradictions(StoreTestCase):
    def test_sustained_dry_and_wet_at_once_is_flagged_and_preserved(self):
        assessments = full_assessments()
        assessments["sustained_dry"] = True
        assessments["sustained_wet"] = True
        record = self.store.record(payload(assessments=assessments))
        self.assertIn(CONTRADICTION_SUSTAINED_DRY_AND_WET, record["contradictions"])
        self.assertIs(record["assessments"]["sustained_dry"], True)
        self.assertIs(record["assessments"]["sustained_wet"], True)

    def test_soil_assessment_without_soil_observation_is_flagged(self):
        record = self.store.record(payload(observations={"vision": vision_facts()}))
        self.assertIn(CONTRADICTION_NO_SOIL_OBSERVATION, record["contradictions"])
        # The assessment is a supplied fact and is preserved unchanged.
        self.assertEqual(record["assessments"]["recovery"], "good")

    def test_vision_assessment_without_vision_observation_is_flagged(self):
        record = self.store.record(payload(observations={"soil": soil_facts()}))
        self.assertIn(CONTRADICTION_NO_VISION_OBSERVATION, record["contradictions"])
        self.assertEqual(record["assessments"]["visual_recovery"], "improved")

    def test_unknown_assessments_without_observations_are_not_flagged(self):
        record = self.store.record(payload(observations=None, assessments=None))
        self.assertEqual(record["missing_observations"], list(OBSERVATION_KEYS))
        self.assertEqual(record["contradictions"], [])

    def test_observation_outside_its_window_is_flagged_but_kept(self):
        record = self.store.record(
            payload(window="30min", observed_at=iso(REFERENCE + timedelta(hours=10)))
        )
        self.assertIn(CONTRADICTION_OUTSIDE_WINDOW, record["contradictions"])
        self.assertEqual(record["window"], "30min")
        self.assertEqual(record["observed_at"], iso(REFERENCE + timedelta(hours=10)))

    def test_observation_before_reference_is_flagged(self):
        record = self.store.record(
            payload(window="24h", observed_at=iso(REFERENCE - timedelta(hours=1)))
        )
        self.assertIn(CONTRADICTION_BEFORE_REFERENCE, record["contradictions"])

    def test_window_jitter_inside_the_plausibility_range_is_not_flagged(self):
        record = self.store.record(
            payload(window="2-3h", observed_at=iso(REFERENCE + timedelta(minutes=100)))
        )
        self.assertEqual(record["contradictions"], [])


class TestOutcome(StoreTestCase):
    def test_outcome_without_records_states_unknown_everywhere(self):
        outcome = self.store.outcome(TEST_EPISODE_ID)
        self.assertEqual(outcome["record_count"], 0)
        self.assertEqual(outcome["windows_included"], [])
        self.assertEqual(outcome["windows_missing"], list(WINDOWS))
        self.assertEqual(outcome["feedback_ids"], [])
        self.assertEqual(outcome["contradictions"], [])
        for key in ASSESSMENT_KEYS:
            self.assertEqual(outcome["assessments"][key], UNKNOWN, key)
            self.assertIsNone(outcome["assessment_sources"][key], key)

    def test_latest_known_value_wins_and_unknown_never_overrides_it(self):
        early = dict(full_assessments(), recovery="poor", rewater_needed=True)
        self.store.record(payload(window="30min", assessments=early))
        mid = dict(full_assessments(), recovery="good")
        del mid["rewater_needed"]  # 2-3h did not assess rewater need
        self.store.record(payload(window="2-3h", assessments=mid))
        late = dict(full_assessments(), data_quality="degraded")
        del late["recovery"]  # 24h did not re-assess recovery
        del late["rewater_needed"]
        self.store.record(payload(window="24h", assessments=late))

        outcome = self.store.outcome(TEST_EPISODE_ID)
        self.assertEqual(outcome["assessments"]["recovery"], "good")
        self.assertEqual(outcome["assessment_sources"]["recovery"], "2-3h")
        self.assertIs(outcome["assessments"]["rewater_needed"], True)
        self.assertEqual(outcome["assessment_sources"]["rewater_needed"], "30min")
        self.assertEqual(outcome["assessments"]["data_quality"], "degraded")
        self.assertEqual(outcome["assessment_sources"]["data_quality"], "24h")
        self.assertEqual(outcome["windows_missing"], ["6-12h"])

    def test_later_window_supersedes_earlier_known_value(self):
        self.store.record(payload(window="30min",
                                  assessments=dict(full_assessments(), rewater_needed=True)))
        self.store.record(payload(window="24h",
                                  assessments=dict(full_assessments(), rewater_needed=False)))
        outcome = self.store.outcome(TEST_EPISODE_ID)
        self.assertIs(outcome["assessments"]["rewater_needed"], False)
        self.assertEqual(outcome["assessment_sources"]["rewater_needed"], "24h")

    def test_contradictions_are_unioned_including_superseded_records(self):
        conflicting = dict(full_assessments(), sustained_dry=True, sustained_wet=True)
        self.store.record(payload(window="30min", assessments=conflicting))
        clean = self.store.record(payload(window="30min",
                                          observed_at=iso(REFERENCE + timedelta(minutes=40))))
        last = self.store.record(payload(window="24h", observations={"soil": soil_facts()}))
        outcome = self.store.outcome(TEST_EPISODE_ID)
        self.assertIn(CONTRADICTION_SUSTAINED_DRY_AND_WET, outcome["contradictions"])
        self.assertIn(CONTRADICTION_NO_VISION_OBSERVATION, outcome["contradictions"])
        self.assertEqual(outcome["record_count"], 3)
        # The superseded first 30min record does not contribute values: the
        # later clean 30min record does, and the known 24h assessments win.
        self.assertIs(outcome["assessments"]["sustained_dry"], False)
        self.assertEqual(outcome["assessment_sources"]["sustained_dry"], "24h")
        self.assertEqual(outcome["feedback_ids"], [clean["feedback_id"], last["feedback_id"]])

    def test_outcome_computes_no_unified_reward(self):
        self.store.record(payload())
        outcome = self.store.outcome(TEST_EPISODE_ID)
        self.assertEqual(set(outcome["assessments"]), set(ASSESSMENT_KEYS))
        for forbidden in ("reward", "score", "total", "weighted"):
            self.assertNotIn(forbidden, outcome)
            self.assertNotIn(forbidden, outcome["assessments"])

    def test_outcome_refuses_malformed_episode_id(self):
        with self.assertRaises(FeedbackError) as caught:
            self.store.outcome("../../escape")
        self.assertEqual(caught.exception.code, "invalid_episode_id")


class TestAttach(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.episode_dir = self.dir / "episodes"
        self.episodes = EpisodeStore(self.episode_dir)
        self.episode = self.episodes.create(real_state())
        self.episode_id = self.episode["episode_id"]

    def test_finalize_attaches_feedback_sets_outcome_and_closes_episode(self):
        first = self.store.record(payload(window="30min", episode_id=self.episode_id))
        second = self.store.record(payload(window="24h", episode_id=self.episode_id))
        summary = self.store.attach_to_episode(
            self.episode_id, self.episode_dir, finalize=True
        )
        self.assertEqual(summary["attached_feedback_ids"],
                         [first["feedback_id"], second["feedback_id"]])
        self.assertEqual(summary["already_present_feedback_ids"], [])
        self.assertTrue(summary["outcome_set"])
        self.assertFalse(summary["outcome_already_present"])
        self.assertTrue(summary["finalized"])

        episode = self.episodes.read(self.episode_id)
        self.assertEqual(episode["status"], "closed")
        self.assertEqual([entry["feedback_id"] for entry in episode["feedback"]],
                         [first["feedback_id"], second["feedback_id"]])
        self.assertEqual(episode["feedback"][0], self.store.read(first["feedback_id"]))
        self.assertEqual(episode["outcome"]["source_schema"], SCHEMA_VERSION)
        self.assertEqual(episode["outcome"]["windows_included"], ["30min", "24h"])
        self.assertEqual(episode["outcome"]["windows_missing"], ["2-3h", "6-12h"])
        self.assertEqual(episode["outcome"]["assessments"]["recovery"], "good")
        with self.assertRaises(FeedbackError) as caught:
            self.store.attach_to_episode(self.episode_id, self.episode_dir)
        self.assertEqual(caught.exception.code, "episode_not_open")

    def test_attach_is_idempotent(self):
        self.store.record(payload(window="30min", episode_id=self.episode_id))
        self.store.attach_to_episode(self.episode_id, self.episode_dir)
        episode_before = self.episodes.read(self.episode_id)
        summary = self.store.attach_to_episode(self.episode_id, self.episode_dir)
        self.assertEqual(summary["attached_feedback_ids"], [])
        self.assertFalse(summary["outcome_set"])
        self.assertFalse(summary["outcome_already_present"])
        self.assertFalse(summary["finalized"])
        episode_after = self.episodes.read(self.episode_id)
        self.assertEqual(episode_after["feedback"], episode_before["feedback"])
        self.assertIsNone(episode_after["outcome"])
        self.assertEqual(episode_after["updated_at"], episode_before["updated_at"])

    def test_attach_never_rewrites_a_caller_set_outcome(self):
        caller_outcome = {"recovery": "caller_supplied"}
        self.episodes.update(self.episode_id, outcome=caller_outcome)
        self.store.record(payload(window="30min", episode_id=self.episode_id))
        summary = self.store.attach_to_episode(self.episode_id, self.episodes)
        self.assertTrue(summary["outcome_already_present"])
        self.assertFalse(summary["outcome_set"])
        episode = self.episodes.read(self.episode_id)
        self.assertEqual(episode["outcome"], caller_outcome)
        self.assertEqual(len(episode["feedback"]), 1)

    def test_later_windows_stay_open_until_final_outcome_then_close(self):
        self.store.record(payload(window="30min", episode_id=self.episode_id))
        self.store.attach_to_episode(self.episode_id, self.episode_dir)
        pending = self.episodes.read(self.episode_id)
        self.assertEqual(pending["status"], "open")
        self.assertIsNone(pending["outcome"])
        later = self.store.record(payload(window="24h", episode_id=self.episode_id,
                                          assessments=dict(full_assessments(), recovery="poor")))
        summary = self.store.attach_to_episode(
            self.episode_id, self.episode_dir, finalize=True
        )
        self.assertEqual(summary["attached_feedback_ids"], [later["feedback_id"]])
        episode = self.episodes.read(self.episode_id)
        self.assertEqual(episode["status"], "closed")
        self.assertEqual(len(episode["feedback"]), 2)
        self.assertEqual(episode["outcome"]["assessments"]["recovery"], "poor")

    def test_attach_rejects_non_boolean_finalize(self):
        self.store.record(payload(window="30min", episode_id=self.episode_id))
        with self.assertRaises(FeedbackError) as caught:
            self.store.attach_to_episode(
                self.episode_id, self.episode_dir, finalize="yes"
            )
        self.assertEqual(caught.exception.code, "invalid_finalize")

    def test_attach_refuses_closed_episode(self):
        self.store.record(payload(window="30min", episode_id=self.episode_id))
        self.episodes.close(self.episode_id)
        with self.assertRaises(FeedbackError) as caught:
            self.store.attach_to_episode(self.episode_id, self.episode_dir)
        self.assertEqual(caught.exception.code, "episode_not_open")

    def test_attach_refuses_unknown_episode(self):
        self.store.record(payload(window="30min", episode_id=OTHER_EPISODE_ID))
        with self.assertRaises(EpisodeError) as caught:
            self.store.attach_to_episode(OTHER_EPISODE_ID, self.episode_dir)
        self.assertEqual(caught.exception.code, "episode_not_found")

    def test_attach_refuses_when_the_episode_has_no_records(self):
        with self.assertRaises(FeedbackError) as caught:
            self.store.attach_to_episode(self.episode_id, self.episode_dir)
        self.assertEqual(caught.exception.code, "no_feedback_for_episode")

    def test_attach_ignores_records_of_other_episodes(self):
        self.store.record(payload(window="30min", episode_id=self.episode_id))
        self.store.record(payload(window="30min", episode_id=OTHER_EPISODE_ID))
        self.store.attach_to_episode(self.episode_id, self.episode_dir)
        episode = self.episodes.read(self.episode_id)
        self.assertEqual(len(episode["feedback"]), 1)
        self.assertEqual(episode["feedback"][0]["episode_id"], self.episode_id)

    def test_attach_refuses_malformed_episode_id(self):
        with self.assertRaises(FeedbackError) as caught:
            self.store.attach_to_episode("ep-nope", self.episode_dir)
        self.assertEqual(caught.exception.code, "invalid_episode_id")


class TestStoreGuards(StoreTestCase):
    def test_crafted_ids_cannot_escape_the_store_directory(self):
        for bad_id in ("../../etc/passwd", "fb-XYZ", "", None):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(FeedbackError) as caught:
                    self.store.read(bad_id)
                self.assertEqual(caught.exception.code, "invalid_feedback_id")
        with self.assertRaises(FeedbackError) as caught:
            self.store.list_for_episode("../episodes")
        self.assertEqual(caught.exception.code, "invalid_episode_id")

    def test_unknown_record_is_not_found(self):
        with self.assertRaises(FeedbackError) as caught:
            self.store.read("fb-" + "a" * 24)
        self.assertEqual(caught.exception.code, "feedback_not_found")

    def test_corrupt_record_fails_loudly_on_read_and_list(self):
        self.store_dir.mkdir(parents=True)
        corrupt_id = "fb-" + "b" * 24
        (self.store_dir / f"{corrupt_id}.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(FeedbackError) as caught:
            self.store.read(corrupt_id)
        self.assertEqual(caught.exception.code, "feedback_file_corrupt")
        with self.assertRaises(FeedbackError) as caught:
            self.store.list_for_episode(TEST_EPISODE_ID)
        self.assertEqual(caught.exception.code, "feedback_file_corrupt")

    def test_foreign_files_are_not_treated_as_records(self):
        self.store_dir.mkdir(parents=True)
        (self.store_dir / "fb-ZZ.json").write_text("{}", encoding="utf-8")
        (self.store_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
        self.assertEqual(self.store.list_for_episode(TEST_EPISODE_ID), [])

    def test_missing_store_directory_lists_empty(self):
        store = FeedbackStore(self.dir / "never-created")
        self.assertEqual(store.list_for_episode(TEST_EPISODE_ID), [])
        self.assertEqual(store.outcome(TEST_EPISODE_ID)["record_count"], 0)


class TestSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_required_fields_match_the_record_keys_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = FeedbackStore(tmp).record(payload())
        self.assertEqual(set(self.schema["required"]), set(record))
        self.assertFalse(self.schema["additionalProperties"])
        self.assertEqual(self.schema["properties"]["schema_version"]["const"], SCHEMA_VERSION)
        self.assertEqual(self.schema["properties"]["device_code"]["const"], EXPECTED_DEVICE_CODE)

    def test_schema_enums_match_the_code_constants(self):
        properties = self.schema["properties"]
        self.assertEqual(properties["window"]["enum"], list(WINDOWS))
        assessments = properties["assessments"]["properties"]
        self.assertEqual(assessments["recovery"]["enum"], list(RECOVERY_VALUES))
        self.assertEqual(assessments["visual_recovery"]["enum"], list(VISUAL_RECOVERY_VALUES))
        self.assertEqual(assessments["data_quality"]["enum"], list(DATA_QUALITY_VALUES))
        for key in BOOL_LIKE_ASSESSMENTS:
            self.assertEqual(assessments[key]["oneOf"],
                             [{"type": "boolean"}, {"const": UNKNOWN}])
        self.assertEqual(set(assessments), set(ASSESSMENT_KEYS))
        self.assertFalse(properties["assessments"]["additionalProperties"])
        self.assertEqual(
            set(properties["contradictions"]["items"]["enum"]),
            {
                CONTRADICTION_SUSTAINED_DRY_AND_WET,
                CONTRADICTION_NO_SOIL_OBSERVATION,
                CONTRADICTION_NO_VISION_OBSERVATION,
                CONTRADICTION_BEFORE_REFERENCE,
                CONTRADICTION_OUTSIDE_WINDOW,
            },
        )
        self.assertEqual(properties["missing_observations"]["items"]["enum"], list(OBSERVATION_KEYS))
        self.assertEqual(properties["observations"]["required"], list(OBSERVATION_KEYS))


class TestCli(StoreTestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "services.soil3.feedback.service",
             "--store-dir", str(self.store_dir), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )

    def write_payload(self, name, data):
        path = self.dir / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_cli_record_read_list_outcome_round_trip(self):
        first = self.write_payload("first.json", payload(window="30min"))
        recorded = self.run_cli("record", "--input", str(first))
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        summary = json.loads(recorded.stdout)
        self.assertRegex(summary["feedback_id"], FEEDBACK_ID_PATTERN)
        self.assertEqual(summary["window"], "30min")
        self.assertEqual(summary["contradictions"], [])
        self.assertTrue(Path(summary["path"]).exists())

        self.write_payload("second.json", payload(window="24h"))
        self.assertEqual(self.run_cli("record", "--input", str(self.dir / "second.json")).returncode, 0)

        read = self.run_cli("read", "--feedback-id", summary["feedback_id"])
        self.assertEqual(read.returncode, 0, read.stderr)
        record = json.loads(read.stdout)
        self.assertEqual(record["schema_version"], SCHEMA_VERSION)
        self.assertEqual(record["feedback_id"], summary["feedback_id"])

        listed = self.run_cli("list", "--episode-id", TEST_EPISODE_ID)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        listing = json.loads(listed.stdout)
        self.assertEqual(listing["count"], 2)
        self.assertEqual([entry["window"] for entry in listing["records"]], ["30min", "24h"])

        outcome = self.run_cli("outcome", "--episode-id", TEST_EPISODE_ID)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        aggregated = json.loads(outcome.stdout)
        self.assertEqual(aggregated["windows_included"], ["30min", "24h"])
        self.assertEqual(aggregated["windows_missing"], ["2-3h", "6-12h"])

    def test_cli_attach_updates_the_episode(self):
        episode_dir = self.dir / "episodes"
        episode = EpisodeStore(episode_dir).create(real_state())
        self.write_payload("obs.json", payload(window="30min", episode_id=episode["episode_id"]))
        self.assertEqual(self.run_cli("record", "--input", str(self.dir / "obs.json")).returncode, 0)
        attached = self.run_cli("attach", "--episode-id", episode["episode_id"],
                                "--episode-store-dir", str(episode_dir), "--finalize")
        self.assertEqual(attached.returncode, 0, attached.stderr)
        summary = json.loads(attached.stdout)
        self.assertEqual(len(summary["attached_feedback_ids"]), 1)
        self.assertTrue(summary["outcome_set"])
        self.assertTrue(summary["finalized"])
        stored = EpisodeStore(episode_dir).read(episode["episode_id"])
        self.assertEqual(stored["status"], "closed")
        self.assertEqual(len(stored["feedback"]), 1)
        self.assertEqual(stored["outcome"]["source_schema"], SCHEMA_VERSION)

    def test_cli_refusals_print_machine_readable_errors(self):
        bad = self.write_payload("bad.json", payload(window="1h"))
        result = self.run_cli("record", "--input", str(bad))
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stderr)
        self.assertEqual(error["error"], "validation_failed")
        self.assertIn("unknown_window", error["reasons"])

        missing = self.run_cli("read", "--feedback-id", "fb-" + "c" * 24)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(json.loads(missing.stderr)["error"], "feedback_not_found")

        unknown_episode = self.run_cli("attach", "--episode-id", TEST_EPISODE_ID,
                                       "--episode-store-dir", str(self.dir / "episodes"))
        self.assertEqual(unknown_episode.returncode, 2)
        self.assertEqual(json.loads(unknown_episode.stderr)["error"], "no_feedback_for_episode")


if __name__ == "__main__":
    unittest.main()
