import tempfile
import unittest
from pathlib import Path

from services.soil3.cloud_trigger import (
    CALL_QWEN,
    LOCAL_SAFETY_ONLY,
    SKIP_CLOUD,
    TriggerShadowError,
    TriggerShadowStore,
    build_decision_context,
    evaluate_trigger,
    semantic_projection,
)
from services.soil3.feedback.feedback_v1 import FeedbackStore
from services.soil3.feedback_collector.action_receipt_v1 import ActionReceiptStore


EPISODE_ID = "ep-" + "1" * 24
TRACE_ID = "tr-" + "2" * 24
ACTION_ID = "act-" + "3" * 24


def complete_state():
    return {
        "schema_version": "state.v1",
        "device_code": "soil3",
        "observed_at": "2026-10-12T12:00:00Z",
        "generated_at": "2026-10-12T12:00:05Z",
        "soil": {"humidity_percent": 34.0},
        "trends": {
            "humidity_1h": -1.0,
            "humidity_3h": -2.0,
            "humidity_6h": 1.0,
        },
        "data_quality": {"soil_age_sec": 5.0, "phase3_state_age_sec": 4.0},
        "safety": {
            "field_capacity": 45.0,
            "target_low": 32.0,
            "hard_safety_low": 28.0,
            "kp": 1.2,
            "flags": {
                "pump_active": False,
                "pending_soak": False,
                "water_delivery_suspect": False,
                "reservoir_empty_suspect": False,
                "low_wet_recovery_suspect": False,
                "sensor_fault": False,
                "dynamic_cooldown": False,
                "predictor_circuit": {"state": "CLOSED"},
                "watering_trigger_guard": False,
                "recent_response_guard": False,
                "hard_safety_low_guard": False,
                "cloud_protection": False,
            },
        },
        "irrigation": {"pump_active": False},
    }


class DecisionContextTests(unittest.TestCase):
    def test_projects_dynamic_distances_trends_and_unknown_feedback(self):
        context = build_decision_context(complete_state())
        self.assertEqual(34.0, context["humidity"])
        self.assertEqual(
            {
                "humidity_minus_target_low": 2.0,
                "humidity_minus_hard_safety_low": 6.0,
                "field_capacity_minus_humidity": 11.0,
            },
            context["distances"],
        )
        self.assertEqual("falling", semantic_projection(context)["trend_directions"]["humidity_1h"])
        self.assertEqual("rising", semantic_projection(context)["trend_directions"]["humidity_6h"])
        self.assertEqual({"status": "unavailable", "record": None}, context["feedback_status"])

    def test_keeps_latest_receipt_evidence_and_matching_feedback_factual(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipts = ActionReceiptStore(root / "receipts")
            feedback = FeedbackStore(root / "feedback")
            receipts.prepare_controlled(
                ACTION_ID,
                complete_state(),
                TRACE_ID,
                EPISODE_ID,
                "approval-1",
                "2026-10-12T10:00:00Z",
            )
            receipts.complete_command(
                ACTION_ID,
                reference_action_at="2026-10-12T10:01:00Z",
                on_published_at="2026-10-12T10:00:55Z",
                off_published_at="2026-10-12T10:01:00Z",
                pump_seconds=5,
            )
            stored_feedback = feedback.record(
                {
                    "episode_id": EPISODE_ID,
                    "window": "30min",
                    "observed_at": "2026-10-12T10:31:00Z",
                    "reference_action_at": "2026-10-12T10:01:00Z",
                    "observations": {"soil": {"humidity_percent": 36.0}},
                }
            )

            context = build_decision_context(
                complete_state(),
                receipt_dir=root / "receipts",
                feedback_dir=root / "feedback",
            )

        self.assertEqual(ACTION_ID, context["last_real_action"]["action_id"])
        self.assertEqual("command_completed", context["last_real_action"]["evidence_level"])
        self.assertFalse(context["last_real_action"]["causal_confirmed"])
        self.assertEqual(stored_feedback["feedback_id"], context["feedback_status"]["record"]["feedback_id"])
        projection = semantic_projection(context)
        self.assertIsNone(projection["confirmed_action_id"])
        self.assertEqual(stored_feedback["feedback_id"], projection["feedback_id"])

    def test_unknown_and_malformed_safety_facts_are_not_normalized_to_false(self):
        state = complete_state()
        state["safety"]["flags"]["sensor_fault"] = {"active": "unknown"}
        del state["safety"]["flags"]["cloud_protection"]
        context = build_decision_context(state)
        self.assertEqual("invalid", context["safety_flags"]["sensor_fault"])
        self.assertEqual("missing", context["safety_flags"]["cloud_protection"])

    def test_prepared_or_incomplete_receipts_are_not_real_action_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipts = ActionReceiptStore(root / "receipts")
            receipts.prepare_native(
                ACTION_ID,
                complete_state(),
                5,
                "2026-10-12T10:00:00Z",
            )

            context = build_decision_context(
                complete_state(),
                receipt_dir=root / "receipts",
            )

        self.assertIsNone(context["last_real_action"])


class TriggerEvaluatorTests(unittest.TestCase):
    def context(self):
        return build_decision_context(complete_state())

    def test_missing_core_fact_stale_state_and_hard_low_fail_locally(self):
        missing = self.context()
        missing["boundaries"]["target_low"] = None
        self.assertEqual(LOCAL_SAFETY_ONLY, evaluate_trigger(missing, None, deny_age_seconds=18000).decision)

        stale = self.context()
        stale["data_quality"]["soil_age_sec"] = 18001.0
        self.assertEqual(LOCAL_SAFETY_ONLY, evaluate_trigger(stale, None, deny_age_seconds=18000).decision)

        hard_low = self.context()
        hard_low["humidity"] = hard_low["boundaries"]["hard_safety_low"]
        self.assertEqual(LOCAL_SAFETY_ONLY, evaluate_trigger(hard_low, None, deny_age_seconds=18000).decision)

    def test_dangerous_or_malformed_safety_and_open_predictor_fail_locally(self):
        danger = self.context()
        danger["safety_flags"]["sensor_fault"] = "active"
        self.assertEqual(LOCAL_SAFETY_ONLY, evaluate_trigger(danger, None, deny_age_seconds=18000).decision)

        malformed = self.context()
        malformed["safety_flags"]["cloud_protection"] = "invalid"
        self.assertEqual(LOCAL_SAFETY_ONLY, evaluate_trigger(malformed, None, deny_age_seconds=18000).decision)

        predictor = self.context()
        predictor["predictor_circuit"] = "OPEN"
        self.assertEqual(LOCAL_SAFETY_ONLY, evaluate_trigger(predictor, None, deny_age_seconds=18000).decision)

    def test_normal_phase3_protections_skip_cloud(self):
        for name in (
            "pending_soak",
            "dynamic_cooldown",
            "recent_response_guard",
            "watering_trigger_guard",
        ):
            with self.subTest(name=name):
                context = self.context()
                context["safety_flags"][name] = "active"
                result = evaluate_trigger(context, None, deny_age_seconds=18000)
                self.assertEqual(SKIP_CLOUD, result.decision)
                self.assertIn(f"protection_active:{name}", result.reason_codes)

    def test_no_baseline_calls_changed_semantics_call_and_unchanged_skips(self):
        context = self.context()
        self.assertEqual(CALL_QWEN, evaluate_trigger(context, None, deny_age_seconds=18000).decision)
        baseline = semantic_projection(context)
        self.assertEqual(SKIP_CLOUD, evaluate_trigger(context, baseline, deny_age_seconds=18000).decision)

        changed = self.context()
        changed["trends"]["humidity_1h"] = 1.0
        result = evaluate_trigger(changed, baseline, deny_age_seconds=18000)
        self.assertEqual(CALL_QWEN, result.decision)
        self.assertIn("semantic_context_changed", result.reason_codes)


class TriggerShadowStoreTests(unittest.TestCase):
    def state_ref(self):
        return {
            "schema_version": "state.v1",
            "path": "/runtime/state/latest.json",
            "sha256": "a" * 64,
            "record_id": None,
        }

    def actual_result(self, *, accepted=True):
        return {
            "provider": "qwen_dashscope",
            "model": "qwen3.8-flash",
            "validator_accepted": accepted,
            "validator_reason_codes": [] if accepted else ["actions_invalid"],
            "strategy_ref": (
                {
                    "schema_version": "strategy.v1",
                    "path": "/runtime/strategy/latest.json",
                    "sha256": "b" * 64,
                    "record_id": "strat-1",
                }
                if accepted
                else None
            ),
            "water_proposed": accepted,
            "proposed_water_seconds": 5.0 if accepted else 0.0,
            "gate_ref": None,
            "gate_decision": None,
            "gate_reason_codes": None,
            "token_usage": None,
            "cost": None,
            "latency": {"source": "runtime_monotonic_clock", "value": 12.0, "unit": "ms"},
        }

    def test_create_finalize_are_atomic_set_once_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TriggerShadowStore(Path(directory))
            context = build_decision_context(complete_state())
            created = store.create(
                TRACE_ID,
                self.state_ref(),
                evaluate_trigger(context, None, deny_age_seconds=18000),
                context,
                None,
            )
            self.assertEqual("trigger_shadow.v1", created["schema_version"])
            self.assertEqual(CALL_QWEN, created["trigger_decision"])
            self.assertIsNone(created["actual_qwen_result"])
            self.assertEqual(
                {"phase3_called": False, "physical_actions_performed": False},
                created["execution"],
            )
            self.assertEqual(created, store.read(TRACE_ID))
            self.assertEqual(created, store.create(
                TRACE_ID,
                self.state_ref(),
                evaluate_trigger(context, None, deny_age_seconds=18000),
                context,
                None,
            ))

            finalized = store.finalize(TRACE_ID, self.actual_result())
            self.assertEqual(self.actual_result(), finalized["actual_qwen_result"])
            self.assertEqual(finalized, store.finalize(TRACE_ID, self.actual_result()))
            with self.assertRaises(TriggerShadowError):
                store.finalize(TRACE_ID, self.actual_result(accepted=False))

    def test_refuses_conflicting_create_and_corrupt_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TriggerShadowStore(Path(directory))
            context = build_decision_context(complete_state())
            decision = evaluate_trigger(context, None, deny_age_seconds=18000)
            store.create(TRACE_ID, self.state_ref(), decision, context, None)
            changed = build_decision_context(complete_state())
            changed["humidity"] = 40.0
            with self.assertRaises(TriggerShadowError):
                store.create(TRACE_ID, self.state_ref(), decision, changed, None)
            store.path_for(TRACE_ID).write_text("{}", encoding="utf-8")
            with self.assertRaises(TriggerShadowError):
                store.read(TRACE_ID)

    def test_baseline_advances_only_for_call_with_validator_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TriggerShadowStore(Path(directory))
            context = build_decision_context(complete_state())
            first_id = "tr-" + "4" * 24
            skip_id = "tr-" + "5" * 24
            rejected_id = "tr-" + "6" * 24
            store.create(
                first_id,
                self.state_ref(),
                evaluate_trigger(context, None, deny_age_seconds=18000),
                context,
                None,
            )
            store.finalize(first_id, self.actual_result())

            skip = evaluate_trigger(context, semantic_projection(context), deny_age_seconds=18000)
            store.create(skip_id, self.state_ref(), skip, context, store.artifact_ref(first_id))
            store.finalize(skip_id, self.actual_result())

            changed = build_decision_context(complete_state())
            changed["trends"]["humidity_1h"] = 1.0
            call = evaluate_trigger(changed, semantic_projection(context), deny_age_seconds=18000)
            store.create(rejected_id, self.state_ref(), call, changed, store.artifact_ref(first_id))
            store.finalize(rejected_id, self.actual_result(accepted=False))

            baseline = store.latest_valid_call_baseline()
            self.assertEqual(first_id, baseline["ref"]["record_id"])
            self.assertEqual(semantic_projection(context), baseline["semantic_projection"])


if __name__ == "__main__":
    unittest.main()
