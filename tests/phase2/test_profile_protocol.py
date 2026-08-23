import copy
import os
import sys
import tempfile
import time
import types
from types import SimpleNamespace
from unittest import mock
import unittest

from phase2_predictor.config import DEFAULT_CONFIG
from phase2_predictor.phase_detection import DynamicPhaseTracker
from phase2_predictor.service import (
    PredictorService,
    _score_candidate,
    _shadow_profile_peak,
    validate_request,
)


class ProfileProtocolTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(DEFAULT_CONFIG)

    def request(self, **extra):
        value = {
            "timestamp": 100.0,
            "candidates": [{"label": "water", "water_sec": 3.0}],
            "horizon_steps": 12,
        }
        value.update(extra)
        return value

    def test_legacy_request_remains_valid(self):
        timestamp, candidates, horizon, metadata = validate_request(self.request(), self.config)
        self.assertEqual(timestamp, 100.0)
        self.assertEqual(candidates[0]["water_sec"], 3.0)
        self.assertEqual(horizon, 12)
        self.assertEqual(metadata, {"device_code": None, "request_id": None, "profile": None})

    def test_identified_profile_is_normalized(self):
        profile = {
            "fc": 42.7, "target_low": 33.0, "kp": 1.7, "zone": "low",
            "zone_stats": {"stable_success": 4},
            "water_delivery_suspect": {"active": False},
        }
        *_, metadata = validate_request(self.request(
            device_code="soil3", request_id="soil3-abc", profile=profile
        ), self.config)
        self.assertEqual(metadata["device_code"], "soil3")
        self.assertEqual(metadata["profile"]["fc"], 42.7)

    def test_invalid_profile_and_identifier_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_request(self.request(device_code="soil 3"), self.config)
        with self.assertRaises(ValueError):
            validate_request(self.request(profile={"fc": True, "zone": "low"}), self.config)
        with self.assertRaises(ValueError):
            validate_request(self.request(profile={"zone": "unknown"}), self.config)
        with self.assertRaises(ValueError):
            validate_request(self.request(profile={"x": {"y": {"z": {"too": 1}}}}), self.config)

    def test_shadow_correction_is_bounded_and_does_not_mutate_live_metric(self):
        metric = {"predicted_peak": 52.0, "predicted_humidity_12h": 39.0, "trajectory": [40.0] * 12}
        original = copy.deepcopy(metric)
        profile = {
            "fc": 42.7, "kp_low": 1.5, "zone": "low",
            "zone_stats": {"stable_success": 10},
            "history": {"peak_error_samples": 5, "peak_error_median": -9.0},
        }
        shadow = _shadow_profile_peak(metric, 3.0, 34.0, profile, self.config)
        self.assertEqual(metric, original)
        self.assertLessEqual(
            abs(shadow["profile_bias_used"]),
            self.config["profile_correction"]["max_peak_bias"],
        )
        self.assertEqual(shadow["profile_correction_status"], "shadow_history_and_kp")

    def test_insufficient_samples_is_noop(self):
        shadow = _shadow_profile_peak(
            {"predicted_peak": 40.0}, 3.0, 34.0,
            {"fc": 42.7, "kp_low": 1.5, "zone": "low", "zone_stats": {"stable_success": 1}},
            self.config,
        )
        self.assertEqual(shadow["shadow_corrected_peak"], 40.0)
        self.assertEqual(shadow["profile_bias_used"], 0.0)
        self.assertEqual(shadow["profile_correction_status"], "insufficient_samples")

    def test_response_echoes_ids_and_request_profile_disables_legacy_calibration(self):
        service = PredictorService.__new__(PredictorService)
        service.config = self.config
        service._load_rows = lambda: [(100.0, [0.0] * 9, 34.0, {"time": "2026/01/01 00:00"})] * 12
        calls = []

        def predict(_rows, _candidate, _horizon, use_legacy_calibration=True):
            calls.append(use_legacy_calibration)
            return {
                "trajectory": [35.0] * 12,
                "predicted_peak": 36.0,
                "raw_predicted_peak": 36.0,
                "predicted_humidity_12h": 35.0,
            }, False

        service._predict_candidate = predict
        service._predict_shadow_candidate = lambda *_args, **_kwargs: ({
            "trajectory": [34.5] * 12,
            "predicted_peak": 35.5,
            "raw_predicted_peak": 35.5,
            "predicted_humidity_12h": 34.5,
            "predicted_minutes_to_peak": 20.0,
            "backend": "cpu_pytorch",
        }, False)
        response = service._build_response(self.request(
            device_code="soil3", request_id="soil3-1",
            profile={"fc": 42.7, "zone": "low", "zone_stats": {"stable_success": 0}},
        ))
        self.assertEqual(response["request_id"], "soil3-1")
        self.assertEqual(response["device_code"], "soil3")
        self.assertEqual(calls, [False])
        self.assertEqual(response["candidate_metrics"]["water"]["predicted_peak"], 36.0)
        self.assertEqual(response["trajectories"]["water"], [35.0] * 12)
        self.assertEqual(
            response["candidate_metrics"]["water"]["shadow_model_peak"], 35.5
        )
        self.assertEqual(
            response["candidate_metrics"]["water"]["shadow_model_trajectory"],
            [34.5] * 12,
        )

    def test_control_horizon_downweights_far_future_peak(self):
        metric = {
            "trajectory": [36.0, 37.0, 38.0, 39.0, 40.0, 41.0, 46.0, 46.0, 46.0, 46.0, 46.0, 46.0],
            "predicted_peak": 46.0,
            "predicted_humidity_12h": 46.0,
        }
        legacy = copy.deepcopy(self.config)
        legacy["decision"]["control_horizon"]["enabled"] = False
        old_score, _ = _score_candidate(metric, 3.0, {}, legacy)
        new_score, policy = _score_candidate(metric, 3.0, {}, self.config)
        self.assertLess(new_score, old_score)
        self.assertTrue(policy["control_horizon"]["enabled"])


class PhaseMergeLimitTests(unittest.TestCase):
    def _tracker(self, folder):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["phase_state"] = os.path.join(folder, "phase_state.json")
        config["paths"]["response_events_log"] = os.path.join(folder, "events.csv")
        config["phase_detection"]["maximum_watering_merge_gap_seconds"] = 1800
        config["phase_detection"]["maximum_watering_event_seconds"] = 3600
        config["phase_detection"]["minimum_interrupted_response_seconds"] = 1800
        tracker = DynamicPhaseTracker(config)
        tracker.previous = {
            "baseline": {"delta_limit": 0.01, "range_limit": 0.01, "slope_limit": 0.01},
            "group_baselines": {}, "baseline_calculated_at": time.time(),
            "last_processed_timestamp": 0.0,
        }
        return tracker, config

    def test_repeated_pulses_cannot_merge_across_event_age_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            tracker, _ = self._tracker(folder)
            rows = []
            for index in range(5):
                timestamp = 1000.0 + index * 1800.0
                metadata = {
                    "time": f"2026/01/01 0{index}:00", "humidity": 30.0 + index,
                    "temperature": 25.0, "light": 100.0, "watered": 1,
                    "water_seconds": 3.0, "quality_gap_before": False,
                }
                rows.append((timestamp, [0.0] * 9, metadata["humidity"], metadata))
            state = tracker.label_rows(rows)
            segment_ids = {row[3]["segment_id"] for row in rows}
            self.assertGreaterEqual(len(segment_ids), 2)
            self.assertLessEqual(state["active_event"]["water_seconds"], 6.0)

    def test_next_watering_closes_peak_after_sufficient_observation(self):
        from phase2_predictor.watering_samples import build_dose_response_samples

        with tempfile.TemporaryDirectory() as folder:
            tracker, config = self._tracker(folder)
            rows = []
            for index in range(14):
                timestamp = 1000.0 + index * 300.0
                watered = index in (0, 13)
                metadata = {
                    "time": f"2026/01/01 00:{index:02d}",
                    "humidity": 30.0 + min(index, 4) * 0.5,
                    "temperature": 25.0, "light": 100.0,
                    "watered": int(watered),
                    "water_seconds": 3.0 if watered else 0.0,
                    "quality_gap_before": False,
                }
                rows.append((timestamp, [0.0] * 9, metadata["humidity"], metadata))

            tracker.label_rows(rows)
            self.assertTrue(rows[12][3]["segment_peak_complete"])
            config["model"]["sequence_length"] = 1
            samples = build_dose_response_samples(rows, config)
            self.assertEqual(len(samples), 1)
            self.assertTrue(samples[0][2]["segment_closed_by_next_watering"])
            self.assertEqual(samples[0][2]["water_seconds"], 3.0)

    def test_short_interrupted_response_stays_peak_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            tracker, _ = self._tracker(folder)
            rows = []
            for index in range(4):
                timestamp = 1000.0 + index * 300.0
                watered = index in (0, 3)
                metadata = {
                    "time": f"2026/01/01 00:{index:02d}",
                    "humidity": 30.0 + index * 0.2,
                    "temperature": 25.0, "light": 100.0,
                    "watered": int(watered),
                    "water_seconds": 3.0 if watered else 0.0,
                    "quality_gap_before": False,
                }
                rows.append((timestamp, [0.0] * 9, metadata["humidity"], metadata))

            tracker.label_rows(rows)
            self.assertFalse(rows[2][3].get("segment_peak_complete", False))


@unittest.skipIf(sys.version_info < (3, 10), "Phase3 runs under its separate Python 3.10 environment")
class Phase3ResponseMatchingTests(unittest.TestCase):
    def phase3_module(self):
        phase3_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "phase3_soil3"))
        if phase3_dir not in sys.path:
            sys.path.insert(0, phase3_dir)
        sys.modules.setdefault("fcntl", types.SimpleNamespace(
            LOCK_EX=1, LOCK_SH=2, LOCK_UN=8, flock=lambda *_args: None,
        ))
        import decision_brain
        return decision_brain

    def test_stale_response_is_rejected(self):
        _response_matches_request = self.phase3_module()._response_matches_request

        self.assertTrue(_response_matches_request(
            {"request_id": "soil3-a", "device_code": "soil3"}, "soil3-a", "soil3"
        ))
        self.assertFalse(_response_matches_request(
            {"request_id": "soil3-old", "device_code": "soil3"}, "soil3-a", "soil3"
        ))
        self.assertFalse(_response_matches_request(
            {"request_id": "soil3-a", "device_code": "soil2"}, "soil3-a", "soil3"
        ))

    def test_expected_delta_uses_peak_only_inside_settle_window(self):
        module = self.phase3_module()
        plan = module.ActionPlan("water", 3.0)
        plan.predicted_trajectory = [99.0] * 12
        plan.predicted_peak = 40.0
        plan.predicted_minutes_to_peak = 45.0
        self.assertIsNone(module._settle_window_expected_delta(plan, 34.0, 30.0))
        plan.predicted_minutes_to_peak = 20.0
        self.assertEqual(module._settle_window_expected_delta(plan, 34.0, 30.0), 6.0)

    def test_h12_backfill_uses_timely_real_reading(self):
        module = self.phase3_module()
        records = [{
            "request_id": "soil3-a", "h12_status": "pending", "h12_due_at": 1000.0,
            "predicted_h12": 33.0, "shadow_predicted_h12": 34.0,
            "actual_h12": None,
        }]

        def update_json(_path, _default, mutator):
            mutator(records)
            return records

        brain = module.DecisionBrain.__new__(module.DecisionBrain)
        reading = SimpleNamespace(humidity=35.0, sensor_stale_hard=False)
        with mock.patch.object(module, "_load_irrigation_trials", return_value=records), \
             mock.patch.object(module, "_trial_log_path", return_value="trials.json"), \
             mock.patch.object(module, "update_json_locked", side_effect=update_json), \
             mock.patch.object(module.time, "time", return_value=1100.0):
            brain._backfill_pending_h12(reading)
        self.assertEqual(records[0]["actual_h12"], 35.0)
        self.assertEqual(records[0]["h12_error"], 2.0)
        self.assertEqual(records[0]["shadow_h12_error"], 1.0)
        self.assertEqual(records[0]["h12_status"], "complete")

    def test_high_zone_kp_bootstraps_from_accepted_trials(self):
        module = self.phase3_module()

        class Config:
            def __init__(self):
                self.values = {}

            def get(self, key, default=None):
                return self.values.get(key, default)

            def update(self, values):
                self.values.update(values)

        config = Config()
        trials = [
            {"status": "rejected", "zone": "high", "reason": "post_h_above_fc_tolerance"},
            {"status": "accepted", "zone": "high", "water_sec": 3.0, "delta_m": 0.9},
            {"status": "accepted", "zone": "high", "water_sec": 3.0, "delta_m": 0.4},
            {"status": "accepted", "zone": "high", "water_sec": 3.0, "delta_m": 0.8},
        ]
        profile = {"zones": {name: module._empty_zone_profile() for name in ("low", "mid", "high")}}
        with mock.patch.object(module, "_load_irrigation_trials", return_value=trials), \
             mock.patch.object(module, "_load_irrigation_profile", return_value=profile), \
             mock.patch.object(module, "_save_irrigation_profile"):
            value = module._bootstrap_high_zone_kp(config)
        self.assertAlmostEqual(value, 0.26667, places=5)
        self.assertAlmostEqual(config.values["K_P_HIGH"], 0.26667, places=5)
        self.assertEqual(profile["zones"]["high"]["stable_success"], 3)
        self.assertEqual(profile["zones"]["high"]["failure_count"], 0)

    def test_accepted_profile_count_is_independent_from_kp_gate(self):
        module = self.phase3_module()
        actuator = module.ActuatorLayer.__new__(module.ActuatorLayer)
        actuator.cfg = SimpleNamespace(
            FC=42.764, TARGET_LOW=33.0, M_SAFE_SLEEP=42.564, K_P=1.78,
            get=lambda key, default=None: 0.2 if key == "K_P_EMA_ALPHA" else default,
            update=mock.Mock(),
        )
        actuator._record_irrigation_trial = mock.Mock()
        actuator._update_irrigation_profile = mock.Mock()
        actuator._update_zone_kp_profile = mock.Mock()
        with mock.patch.object(module, "_load_pattern_memory", return_value=[]), \
             mock.patch.object(module, "_save_pattern_memory"), \
             mock.patch.object(module, "_zone_kp", return_value=1.78):
            accepted = actuator._update_pattern_memory(
                3.0, 40.3, 40.7, 0.4, plan_label="aggressive"
            )
            actuator._evolve_kp(3.0, 0.4, 40.3, "aggressive")
        self.assertTrue(accepted)
        actuator._update_irrigation_profile.assert_called_once()
        actuator._update_zone_kp_profile.assert_not_called()

    def test_net_kp_uses_natural_loss_compensation(self):
        module = self.phase3_module()
        actuator = module.ActuatorLayer.__new__(module.ActuatorLayer)
        updates = []
        actuator.cfg = SimpleNamespace(
            FC=45.0,
            TARGET_LOW=33.0,
            M_SAFE_SLEEP=42.5,
            K_P=1.0,
            get=lambda key, default=None: {
                "K_P_EMA_ALPHA": 0.5,
                "DRYDOWN_EMA_ALPHA": 0.5,
            }.get(key, default),
            update=lambda data: updates.append(data),
        )
        with mock.patch.object(module, "_zone_kp", return_value=1.0), \
             mock.patch.object(actuator, "_update_zone_kp_profile"):
            actuator._evolve_kp(
                4.0, 3.0, 36.0, "aggressive",
                raw_delta_m=2.0, expected_natural_loss=1.0,
            )
        self.assertAlmostEqual(updates[-1]["K_P"], 0.875, places=5)

        profile = {"zones": {"low": {}, "mid": {}, "high": {}}}
        with mock.patch.object(module, "_load_irrigation_profile", return_value=profile), \
             mock.patch.object(module, "_save_irrigation_profile") as save_profile:
            actuator._update_zone_drydown_profile("mid", 0.25)
        self.assertEqual(profile["zones"]["mid"]["drydown_rate_ema"], 0.25)
        save_profile.assert_called_once()

    def test_observe_phase2_response_queue_is_downsampled(self):
        module = self.phase3_module()
        records = []

        def fake_update(_path, _default, updater):
            records[:] = updater(records)
            return records

        plan = module.ActionPlan("observe", 0.0)
        plan.request_id = "soil3-r1"
        plan.device_code = "soil3"
        plan.prediction_zone = "high"
        plan.predicted_trajectory = [40.0] * 12
        plan.predicted_h12 = 39.0
        plan.shadow_predicted_trajectory = [40.0] * 12
        plan.shadow_predicted_h12 = 39.0
        plan.prediction_timestamp = 1000.0
        reading = module.SensorReading(humidity=40.0, temperature=25.0, ec_raw=400.0)
        with mock.patch.object(module, "update_json_locked", side_effect=fake_update), \
             mock.patch.object(module, "time") as fake_time:
            fake_time.time.return_value = 1000.0
            module._record_phase2_selected_prediction(plan, reading)
            plan.request_id = "soil3-r2"
            plan.prediction_timestamp = 1100.0
            reading.humidity = 40.1
            fake_time.time.return_value = 1100.0
            module._record_phase2_selected_prediction(plan, reading)
            self.assertEqual(len(records), 1)
            plan.request_id = "soil3-r3"
            plan.prediction_timestamp = 2901.0
            fake_time.time.return_value = 2901.0
            module._record_phase2_selected_prediction(plan, reading)
            self.assertEqual(len(records), 2)

    def test_phase3_cost_downweights_long_horizon(self):
        module = self.phase3_module()
        cfg = SimpleNamespace(
            ALPHA=1.0,
            BETA=1.0,
            GAMMA=0.0,
            M_WAKE_UP=33.0,
            RESPIRATION_LIMIT=42.0,
            TRAJ_TOLERANCE=0.0,
            FC=45.0,
            TARGET_LOW=33.0,
            get=lambda _key, default=None: default,
            get_constant=lambda key: {
                "COST_SHORT_HORIZON_STEPS": 3,
                "COST_MEDIUM_HORIZON_STEPS": 6,
                "COST_SHORT_HORIZON_WEIGHT": 1.0,
                "COST_MEDIUM_HORIZON_WEIGHT": 0.4,
                "COST_LONG_HORIZON_WEIGHT": 0.1,
                "WATER_SEC_MAX_HARD": 30.0,
            }.get(key),
        )
        court = module.CostCourt(cfg)
        plan = module.ActionPlan("water", 3.0)
        trajectory = [40.0] * 6 + [45.0] * 6
        self.assertAlmostEqual(court._calc_cost_J(trajectory, plan), 5.4, places=4)

    def test_settle_uses_observed_peak_for_kp_delta(self):
        module = self.phase3_module()
        actuator = module.ActuatorLayer.__new__(module.ActuatorLayer)
        actuator.cfg = SimpleNamespace(
            FC=45.0,
            TARGET_LOW=33.0,
            M_SAFE_SLEEP=42.0,
            K_P=1.0,
            get=lambda key, default=None: {
                "KP_NET_GAIN_ENABLED": True,
                "DRYDOWN_STEP_SEC": 300.0,
                "KP_NET_LOSS_SCALE": 1.0,
                "KP_NET_MAX_LOSS": 2.0,
            }.get(key, default),
        )
        actuator.sensor = SimpleNamespace(
            read=lambda require_fresh=False: module.SensorReading(
                humidity=40.8, temperature=25.0, ec_raw=400.0
            )
        )
        soak = module.PendingSoak(
            water_sec=3.0,
            pre_humidity=40.0,
            pump_end_time=0.0,
            soak_duration=1200.0,
            plan_label="aggressive",
            pre_recent_slope=-0.1,
            observed_peak=41.2,
            observed_peak_elapsed_sec=300.0,
        )
        actuator._update_zone_drydown_profile = mock.Mock()
        actuator._update_pattern_memory = mock.Mock(return_value=True)
        actuator._evolve_kp = mock.Mock()
        with mock.patch.object(module.time, "monotonic", return_value=1200.0):
            actuator.settle(soak)
        args, kwargs = actuator._update_pattern_memory.call_args
        self.assertAlmostEqual(args[3], 1.2, places=4)
        meta = kwargs["prediction_meta"]
        self.assertEqual(meta["endpoint_delta_m"], 0.8)
        self.assertEqual(meta["peak_delta_m"], 1.2)
        evolve_args, evolve_kwargs = actuator._evolve_kp.call_args
        self.assertAlmostEqual(evolve_args[1], 1.3, places=4)
        self.assertAlmostEqual(evolve_kwargs["raw_delta_m"], 1.2, places=4)


if __name__ == "__main__":
    unittest.main()
