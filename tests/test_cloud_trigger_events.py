"""Business-event admission tests for the Trigger (Phase-1 follow-up).

Run on the real machine:
    python3 -m unittest tests.test_cloud_trigger_events -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.soil3.cloud_trigger import (
    CALL_QWEN,
    LOCAL_SAFETY_ONLY,
    SKIP_CLOUD,
    TriggerShadowStore,
    build_decision_context,
    evaluate_trigger,
)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def complete_state(observed_at="2026-11-09T06:00:00Z"):
    return {
        "schema_version": "state.v1",
        "device_code": "soil3",
        "observed_at": observed_at,
        "generated_at": observed_at,
        "soil": {"humidity_percent": 34.0},
        "trends": {
            "humidity_1h": -1.0,
            "humidity_3h": -2.0,
            "humidity_6h": -3.0,
        },
        "data_quality": {"soil_age_sec": 5.0, "phase3_state_age_sec": 4.0},
        "safety": {
            "field_capacity": 45.0,
            "target_low": 32.0,
            "hard_safety_low": 28.0,
            "kp": 1.2,
            "flags": {
                "pump_active": False,
                "water_delivery_suspect": False,
                "reservoir_empty_suspect": False,
                "low_wet_recovery_suspect": False,
                "sensor_fault": False,
                "dynamic_cooldown": False,
                "predictor_circuit": {"state": "CLOSED"},
                "watering_trigger_guard": False,
                "recent_response_guard": False,
                "hard_safety_low_guard": False,
            },
        },
        "irrigation": {"pump_active": False},
    }


def series(end: datetime, values, step_minutes=5):
    start = end - timedelta(minutes=step_minutes * (len(values) - 1))
    return [
        {
            "observed_at": iso(start + timedelta(minutes=step_minutes * index)),
            "humidity_percent": value,
        }
        for index, value in enumerate(values)
    ]


def declining(end, start_value=45.0, pp_per_hour=1.0, hours=3.0):
    count = int(hours * 60 / 5) + 1
    values = [start_value - pp_per_hour * (index * 5) / 60.0 for index in range(count)]
    return series(end, values)


class BusinessEventAdmissionTests(unittest.TestCase):
    END = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)

    def context(self, readings):
        state = complete_state(observed_at=iso(self.END))
        return build_decision_context(
            state,
            deny_age_seconds=18000,
            humidity_series=readings,
        )

    def evaluate(self, readings, memory=None, policy="business_event_v1"):
        return evaluate_trigger(
            self.context(readings),
            None,
            deny_age_seconds=18000,
            trigger_policy=policy,
            memory=memory,
        )

    def test_jitter_never_admits_cloud(self):
        pattern = [32.0, 32.1, 32.0, 32.1, 32.0, 32.0, 32.1, 32.0] * 15
        decision = self.evaluate(series(self.END, pattern))
        self.assertEqual(SKIP_CLOUD, decision.decision)
        self.assertEqual(("no_business_event",), decision.reason_codes)
        self.assertIsNone(decision.business_event)

    def test_sustained_decline_admits_once_after_confirmation(self):
        readings = declining(self.END)
        first = self.evaluate(readings)
        self.assertEqual(SKIP_CLOUD, first.decision, "one observation must not confirm")
        self.assertIsNotNone(first.trigger_memory)

        second = self.evaluate(readings, memory=first.trigger_memory)
        self.assertEqual(CALL_QWEN, second.decision)
        self.assertEqual(("business_event:falling_start",), second.reason_codes)
        event = second.business_event
        self.assertEqual("falling_trend_review", event["task_type"])
        self.assertEqual("diagnose_only", event["allowed_scope"])
        self.assertIn("slope_2h", event["evidence"])

        third = self.evaluate(readings, memory=second.trigger_memory)
        self.assertEqual(SKIP_CLOUD, third.decision)
        self.assertEqual(("no_business_event",), third.reason_codes)

    def test_protection_still_skips_before_events(self):
        state = complete_state(observed_at=iso(self.END))
        state["safety"]["flags"]["pending_soak"] = {
            "pump_end_time_wall": self.END.timestamp(),
            "soak_duration": 600.0,
            "water_sec": 5.0,
            "pre_humidity": 40.0,
        }
        context = build_decision_context(
            state,
            deny_age_seconds=18000,
            humidity_series=declining(self.END),
        )
        decision = evaluate_trigger(
            context,
            None,
            deny_age_seconds=18000,
            trigger_policy="business_event_v1",
        )
        self.assertEqual(SKIP_CLOUD, decision.decision)
        self.assertIn("protection_active:pending_soak", decision.reason_codes)

    def test_missing_series_fails_closed_without_calling(self):
        state = complete_state(observed_at=iso(self.END))
        context = build_decision_context(state, deny_age_seconds=18000)
        decision = evaluate_trigger(
            context,
            None,
            deny_age_seconds=18000,
            trigger_policy="business_event_v1",
        )
        self.assertEqual(SKIP_CLOUD, decision.decision)
        self.assertEqual(("event_input_unavailable",), decision.reason_codes)
        self.assertIsNone(decision.business_event)

    def test_unknown_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_trigger(
                self.context(declining(self.END)),
                None,
                deny_age_seconds=18000,
                trigger_policy="trend_v9",
            )

    def test_default_policy_keeps_classification_semantics(self):
        context = self.context(declining(self.END))
        self.assertEqual(CALL_QWEN, evaluate_trigger(context, None, deny_age_seconds=18000).decision)


class SeriesAndStoreTests(unittest.TestCase):
    STATE_REF = {
        "schema_version": "state.v1",
        "path": "/runtime/state/latest.json",
        "sha256": "a" * 64,
        "record_id": None,
    }

    def test_context_normalizes_series_and_drops_invalid_rows(self):
        end = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)
        readings = series(end, [40.0, 39.5, 39.0]) + [
            {"observed_at": "bad", "humidity_percent": 39.0},
            {"observed_at": iso(end), "humidity_percent": 101.0},
        ]
        context = build_decision_context(
            complete_state(observed_at=iso(end)),
            deny_age_seconds=18000,
            humidity_series=readings,
        )
        self.assertEqual(3, len(context["humidity_series_6h"]))
        self.assertEqual(39.0, context["humidity_series_6h"][-1]["humidity_percent"])

    def test_store_persists_optional_event_fields_and_reads_old_records(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TriggerShadowStore(Path(directory))
            end = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)
            readings = declining(end)
            state = complete_state(observed_at=iso(end))
            context = build_decision_context(
                state,
                deny_age_seconds=18000,
                humidity_series=readings,
            )
            first = evaluate_trigger(
                context,
                None,
                deny_age_seconds=18000,
                trigger_policy="business_event_v1",
            )
            second = evaluate_trigger(
                context,
                None,
                deny_age_seconds=18000,
                trigger_policy="business_event_v1",
                memory=first.trigger_memory,
            )
            self.assertEqual(CALL_QWEN, second.decision)

            trace_id = "tr-" + "7" * 24
            created = store.create(
                trace_id,
                self.STATE_REF,
                second,
                context,
                None,
            )
            self.assertEqual("falling_start", created["business_event"]["event_type"])
            self.assertIn("slope_2h", created["trigger_signals"])
            self.assertEqual(created, store.read(trace_id))

            legacy_path = store.path_for("tr-" + "8" * 24)
            legacy = dict(created)
            legacy["trace_id"] = "tr-" + "8" * 24
            legacy.pop("business_event")
            legacy.pop("trigger_signals")
            legacy_path.write_text(
                json.dumps(legacy, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                "falling_start",
                store.read(trace_id)["business_event"]["event_type"],
            )

    def test_recent_series_is_rebuilt_from_stored_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TriggerShadowStore(Path(directory))
            base = datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc)
            for index, humidity in enumerate((40.0, 39.5, 39.0)):
                stamp = base + timedelta(minutes=5 * index)
                state = complete_state(observed_at=iso(stamp))
                state["soil"]["humidity_percent"] = humidity
                context = build_decision_context(state, deny_age_seconds=18000)
                decision = evaluate_trigger(context, None, deny_age_seconds=18000)
                store.create(
                    "tr-%024d" % (index + 1),
                    self.STATE_REF,
                    decision,
                    context,
                    None,
                )

            now = base + timedelta(minutes=10)
            rebuilt = store.recent_humidity_series(
                now=iso(now),
                window_seconds=6 * 3600 + 1800,
            )
        self.assertEqual([40.0, 39.5, 39.0], [p["humidity_percent"] for p in rebuilt])
        self.assertEqual(iso(now), rebuilt[-1]["observed_at"])


class SafetyParityTests(unittest.TestCase):
    def test_local_safety_only_takes_precedence_in_business_mode(self):
        state = complete_state()
        state["data_quality"]["soil_age_sec"] = 18001.0
        context = build_decision_context(state, deny_age_seconds=18000)
        decision = evaluate_trigger(
            context,
            None,
            deny_age_seconds=18000,
            trigger_policy="business_event_v1",
        )
        self.assertEqual(LOCAL_SAFETY_ONLY, decision.decision)


if __name__ == "__main__":
    unittest.main()