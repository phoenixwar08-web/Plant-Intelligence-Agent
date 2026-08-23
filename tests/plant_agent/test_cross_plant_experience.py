import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cross_plant_experience import (  # noqa: E402
    CandidateError,
    CandidateStateError,
    build_candidate,
    classify_settlement,
    confirmed_library_status,
    group_id_from_conversation_key,
    parse_confirmation,
    render_candidate_text,
    safe_error_text,
    sensor_timestamp_iso,
    select_unique_pending,
    transition_candidate,
    validation_block_reasons,
)


def snapshot(device_code, *, fresh=True, humidity=42.0, trend="wetting"):
    return {
        "device_code": device_code,
        "captured_at": "2026-07-25T18:30:00+08:00",
        "fresh": fresh,
        "sensor": {
            "humidity": humidity,
            "temperature": 27.0,
            "air_humidity": 45.0,
            "lux": 1000.0,
        },
        "trend": {"label": trend, "delta": 2.5},
        "recent_irrigation": {"observed": True, "settled": True, "humidity_delta": 3.1},
        "safety": {"pending_soak": False, "water_delivery_suspect": False},
        "visual": {"available": False, "reason": "no_fresh_per_pot_visual"},
    }


class CrossPlantExperienceTests(unittest.TestCase):
    def test_candidate_is_derived_from_snapshots_and_never_exports_control_parameters(self):
        candidate = build_candidate(
            snapshot("soil2", humidity=43.5),
            snapshot("soil3", humidity=42.1),
            candidate_id="XP-20260725-ABC123",
        )

        self.assertEqual(candidate["source_device"], "soil2")
        self.assertEqual(candidate["target_device"], "soil3")
        self.assertEqual(candidate["status"], "awaiting_user_choice")
        self.assertEqual(candidate["recommendation"], "eligible_for_one_local_validation_only")
        self.assertEqual(candidate["source_action"], "local_legal_irrigation_observed")
        self.assertNotIn("water_sec", str(candidate))
        self.assertNotIn("Kp", str(candidate))
        self.assertNotIn("threshold", str(candidate))

    def test_candidate_can_use_historical_snapshots_as_reference_only(self):
        candidate = build_candidate(
            snapshot("soil2", fresh=False),
            snapshot("soil3", fresh=False),
            candidate_id="XP-20260725-ABC123",
        )

        self.assertEqual(candidate["evidence_mode"], "historical_reference_only")
        self.assertFalse(candidate["evidence_freshness"]["source_current"])
        self.assertFalse(candidate["evidence_freshness"]["target_current"])
        self.assertIn("历史快照", render_candidate_text(candidate))
        self.assertIn("历史快照", render_candidate_text(candidate))

    def test_candidate_uses_source_pre_watering_conditions_when_available(self):
        source = snapshot("soil2", humidity=43.5)
        source["recent_irrigation"]["pre_sensor"] = {
            "humidity": 35.0,
            "temperature": 25.1,
            "air_humidity": 60.0,
            "lux": 120.0,
        }

        candidate = build_candidate(source, snapshot("soil3"), candidate_id="XP-20260725-ABC123")

        self.assertEqual(candidate["source_conditions"]["humidity"], 35.0)

    def test_dialogue_summary_hides_the_internal_candidate_id_and_never_exports_controls(self):
        candidate = build_candidate(
            snapshot("soil2", humidity=36.5),
            snapshot("soil3", humidity=40.3, trend="drying"),
            candidate_id="XP-20260725-HIDDEN01",
        )

        text = render_candidate_text(candidate)

        self.assertNotIn("XP-", text)
        self.assertNotIn("water_sec", text)
        self.assertNotIn("Kp", text)
        self.assertIn("采纳本次经验", text)
        self.assertIn("仅保存本次经验", text)
        self.assertIn("不采纳本次经验", text)

    def test_group_conversation_key_is_required_for_plain_confirmation_binding(self):
        self.assertEqual(
            group_id_from_conversation_key("agent:qqbot4:qqbot:group:group-123"),
            "group-123",
        )
        self.assertEqual(
            group_id_from_conversation_key("agent:qqbot4:qqbot:group:b0a2122480841340aad793955a1cea03"),
            "B0A2122480841340AAD793955A1CEA03",
        )
        with self.assertRaisesRegex(CandidateError, "invalid_conversation_key"):
            group_id_from_conversation_key("agent:qqbot4:qqbot:direct:user-7")

    def test_historical_reference_keeps_the_actual_sensor_sample_time(self):
        self.assertEqual(
            sensor_timestamp_iso("2026-07-25 19:19:02"),
            "2026-07-25T19:19:02+08:00",
        )

    def test_plain_confirmation_fails_closed_when_more_than_one_candidate_is_bound(self):
        row = {"candidate_id": "XP-20260725-ONE"}
        self.assertEqual(select_unique_pending([row]), row)
        with self.assertRaisesRegex(CandidateStateError, "candidate_not_found"):
            select_unique_pending([])
        with self.assertRaisesRegex(CandidateStateError, "candidate_ambiguous"):
            select_unique_pending([row, {"candidate_id": "XP-20260725-TWO"}])

    def test_candidate_refuses_a_source_event_without_an_observed_recovery(self):
        source = snapshot("soil2")
        source["recent_irrigation"]["humidity_delta"] = 0.0
        with self.assertRaisesRegex(CandidateError, "source_result"):
            build_candidate(source, snapshot("soil3"), candidate_id="XP-20260725-ABC123")

    def test_user_safe_errors_never_contain_a_control_instruction(self):
        text = safe_error_text(CandidateError("stale_snapshot"))

        self.assertIn("未进入验证", text)
        self.assertNotIn("秒", text)
        self.assertNotIn("浇水", text)

    def test_confirmation_is_namespaced_and_rejects_legacy_bare_choices(self):
        self.assertEqual(parse_confirmation("经验采纳 XP-20260725-ABC123"), ("adopt", "XP-20260725-ABC123"))
        self.assertEqual(parse_confirmation("经验仅保存 XP-20260725-ABC123"), ("save_only", "XP-20260725-ABC123"))
        self.assertEqual(parse_confirmation("经验拒绝 XP-20260725-ABC123"), ("reject", "XP-20260725-ABC123"))
        self.assertIsNone(parse_confirmation("A"))
        self.assertIsNone(parse_confirmation("确认浇水 3 秒"))

    def test_settlement_requires_clean_local_evidence(self):
        self.assertEqual(
            classify_settlement(40.0, 43.0, 1.0, manual_event=False, safety_anomaly=False, sensor_fresh=True),
            "supported_once",
        )
        self.assertEqual(
            classify_settlement(40.0, 43.0, 1.0, manual_event=True, safety_anomaly=False, sensor_fresh=True),
            "inconclusive",
        )
        self.assertEqual(
            classify_settlement(40.0, 39.8, 1.0, manual_event=False, safety_anomaly=False, sensor_fresh=True),
            "not_supported",
        )

    def test_library_requires_three_supported_local_trials(self):
        self.assertIsNone(confirmed_library_status(2))
        self.assertEqual(confirmed_library_status(3), "confirmed_local_experience")

    def test_adopt_creates_only_a_waiting_validation_request(self):
        candidate = build_candidate(
            snapshot("soil2"), snapshot("soil3"), candidate_id="XP-20260725-ABC123"
        )

        updated, request = transition_candidate(candidate, "adopt", "2026-07-26T18:30:00+08:00")

        self.assertEqual(updated["status"], "approved_waiting_for_safe_window")
        self.assertEqual(request["target_device"], "soil3")
        self.assertEqual(request["status"], "approved_waiting_for_safe_window")
        self.assertNotIn("water_sec", str(request))
        self.assertNotIn("threshold", str(request))

    def test_candidate_cannot_be_confirmed_twice(self):
        candidate = build_candidate(
            snapshot("soil2"), snapshot("soil3"), candidate_id="XP-20260725-ABC123"
        )
        updated, _ = transition_candidate(candidate, "save_only", "2026-07-26T18:30:00+08:00")

        with self.assertRaises(CandidateStateError):
            transition_candidate(updated, "adopt", "2026-07-26T18:30:00+08:00")

    def test_validation_can_only_label_an_already_legal_local_action(self):
        self.assertEqual(
            validation_block_reasons(
                action_sec=3.0,
                sensor_fresh=True,
                pending_soak=False,
                safety_flags={"water_delivery_suspect": False, "reservoir_empty_suspect": False},
                recent_manual_event=False,
                recent_probe_event=False,
            ),
            [],
        )
        self.assertEqual(
            validation_block_reasons(
                action_sec=0.0,
                sensor_fresh=True,
                pending_soak=False,
                safety_flags={},
                recent_manual_event=False,
                recent_probe_event=False,
            ),
            ["no_local_phase3_action"],
        )
        self.assertIn(
            "recent_manual_event",
            validation_block_reasons(
                action_sec=3.0,
                sensor_fresh=True,
                pending_soak=False,
                safety_flags={},
                recent_manual_event=True,
                recent_probe_event=False,
            ),
        )
