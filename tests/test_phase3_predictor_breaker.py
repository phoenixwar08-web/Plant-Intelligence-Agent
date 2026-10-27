"""Regression for counting predictor time separately from the irrigation cycle."""
import importlib.util
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class PredictorBreakerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.phase3 = str(Path(__file__).resolve().parents[1] / "services/soil3/phase3")
        cls.previous_fcntl = sys.modules.get("fcntl")
        if os.name == "nt":
            fcntl = types.ModuleType("fcntl")
            fcntl.LOCK_SH, fcntl.LOCK_EX, fcntl.LOCK_UN = 1, 2, 8
            fcntl.flock = lambda *args: None
            sys.modules["fcntl"] = fcntl
        sys.path.insert(0, cls.phase3)
        import config_manager
        import decision_brain
        cls.config = config_manager
        cls.brain = decision_brain
        spec = importlib.util.spec_from_file_location("predictor_breaker_main", Path(cls.phase3) / "main.py")
        cls.main = importlib.util.module_from_spec(spec)
        # Import the real daemon class without installing daemon logging handlers.
        with mock.patch.object(logging, "basicConfig"), mock.patch.object(logging, "FileHandler"):
            spec.loader.exec_module(cls.main)

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.phase3)
        if os.name == "nt":
            if cls.previous_fcntl is None:
                sys.modules.pop("fcntl", None)
            else:
                sys.modules["fcntl"] = cls.previous_fcntl

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "system_state.json"
        self.path_patch = mock.patch.dict(self.config.FILE_PATHS, {"SYSTEM_STATE": self.state_path})
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.values = {"PREDICTOR_CIRCUIT_OPEN": False}
        constants = {
            "SHM_TIMEOUT_SEC": 30,
            "SHM_REQUEST_FILE": str(self.root / "request.json"),
            "SHM_RESPONSE_FILE": str(self.root / "response.json"),
        }
        self.cfg = SimpleNamespace(
            get_constant=constants.get,
            get=lambda name, default=None: self.values.get(name, default),
            update=lambda values, persist=False: self.values.update(values),
        )
        self.predictor = self.brain.Phase2Predictor(self.cfg)
        self.breaker = self.main.PredictorCircuitBreaker(self.cfg, self.predictor)

    def write_probe(self, **changes):
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        probe = {
            "called": True, "success": True, "timestamp": 100.0, "elapsed_seconds": 2.0,
            "probe_id": self.breaker._probe_id or "unowned-legacy-probe",
        }
        probe.update(changes)
        state["predictor_last_probe"] = probe
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def start_half_open(self):
        self.breaker._state = "HALF_OPEN"
        self.breaker._fail_count = 26
        self.breaker.before_cycle()

    def test_fast_probe_closes_half_open_despite_56_second_cycle(self):
        self.start_half_open()
        self.write_probe()
        self.breaker.after_cycle(56.0, 1)
        state = json.loads(self.state_path.read_text())
        self.assertEqual("CLOSED", state["predictor_circuit"]["state"])
        self.assertEqual(0, state["predictor_circuit"]["fail_count"])
        self.assertFalse(self.values["PREDICTOR_CIRCUIT_OPEN"])

    def test_real_prediction_timeout_reopens_half_open(self):
        self.start_half_open()
        self.write_probe(success=False, elapsed_seconds=30.0)
        self.breaker.after_cycle(56.0, 1)
        self.assertEqual("OPEN", self.breaker._state)
        self.assertEqual(27, self.breaker._fail_count)

    def test_three_prediction_timeouts_open_closed_breaker(self):
        for cycle in range(1, 4):
            self.breaker.before_cycle()
            self.write_probe(success=False, elapsed_seconds=30.0, timestamp=float(cycle))
            self.breaker.after_cycle(31.0, cycle)
            self.assertEqual(cycle, self.breaker._fail_count)
            self.assertEqual("OPEN" if cycle == 3 else "CLOSED", self.breaker._state)

    def test_old_success_is_not_reused_when_this_cycle_skips_predictor(self):
        self.write_probe()
        self.start_half_open()
        self.breaker.after_cycle(1.0, 1)
        self.assertEqual("HALF_OPEN", self.breaker._state)
        self.assertEqual(26, self.breaker._fail_count)

    def test_failed_cycle_start_read_cannot_adopt_delayed_old_success(self):
        with mock.patch.object(self.breaker, "_last_predictor_probe", return_value={}):
            self.start_half_open()
        self.write_probe(probe_id="previous-cycle-success")
        self.breaker.after_cycle(1.0, 1)
        self.assertEqual("HALF_OPEN", self.breaker._state)
        self.assertEqual(26, self.breaker._fail_count)

    def test_delayed_previous_cycle_success_cannot_close_next_cycle(self):
        self.start_half_open()
        self.run_prediction("success")
        previous_probe = json.loads(self.state_path.read_text())["predictor_last_probe"]
        self.write_probe(called=False, success=False, elapsed_seconds=None)
        self.breaker.before_cycle()
        state = json.loads(self.state_path.read_text())
        state["predictor_last_probe"] = previous_probe
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.breaker.after_cycle(1.0, 2)
        self.assertEqual("HALF_OPEN", self.breaker._state)
        self.assertEqual(26, self.breaker._fail_count)

    def test_legacy_success_without_probe_id_cannot_close_half_open(self):
        self.start_half_open()
        self.write_probe()
        state = json.loads(self.state_path.read_text())
        del state["predictor_last_probe"]["probe_id"]
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.breaker.after_cycle(1.0, 1)
        self.assertEqual("HALF_OPEN", self.breaker._state)
        self.assertEqual(26, self.breaker._fail_count)

    def test_probe_ids_rotate_between_real_predictions(self):
        self.start_half_open()
        first = self.run_prediction("success")
        self.breaker.after_cycle(56.0, 1)
        self.breaker.before_cycle()
        second = self.run_prediction("success")
        self.assertNotEqual(first["probe_id"], second["probe_id"])
        self.assertEqual(self.breaker._probe_id, second["probe_id"])
        self.breaker.after_cycle(56.0, 2)
        self.assertEqual("CLOSED", self.breaker._state)

    def test_no_probe_keeps_half_open_even_for_long_cycle(self):
        self.start_half_open()
        self.breaker.after_cycle(56.0, 1)
        self.assertEqual("HALF_OPEN", self.breaker._state)

    def test_explicit_skipped_probe_does_not_close_half_open(self):
        self.start_half_open()
        self.write_probe(called=False, success=False, elapsed_seconds=None)
        self.breaker.after_cycle(56.0, 1)
        self.assertEqual("HALF_OPEN", self.breaker._state)

    def test_skipped_cycle_preserves_closed_failure_count_until_real_success(self):
        self.breaker._fail_count = 2
        self.breaker.before_cycle()
        self.breaker.after_cycle(1.0, 1)
        self.assertEqual(2, self.breaker._fail_count)
        self.breaker.before_cycle()
        self.write_probe()
        self.breaker.after_cycle(56.0, 2)
        self.assertEqual(0, self.breaker._fail_count)

    def test_restart_does_not_treat_persisted_success_as_new_probe(self):
        self.start_half_open()
        self.write_probe()
        self.breaker._persist()
        restored = self.main.PredictorCircuitBreaker(self.cfg, self.predictor)
        restored.before_cycle()
        restored.after_cycle(1.0, 1)
        self.assertEqual("HALF_OPEN", restored._state)

    def test_missing_or_invalid_timing_never_closes_half_open(self):
        for duration in (None, True, "2", -1.0, float("nan"), float("inf")):
            with self.subTest(duration=duration):
                self.start_half_open()
                self.write_probe(timestamp=200.0, elapsed_seconds=duration)
                self.breaker.after_cycle(1.0, 1)
                self.assertEqual("OPEN", self.breaker._state)
                self.assertTrue(self.values["PREDICTOR_CIRCUIT_OPEN"])

    def test_legacy_probe_without_timing_is_not_a_successful_probe(self):
        self.start_half_open()
        self.write_probe()
        state = json.loads(self.state_path.read_text())
        del state["predictor_last_probe"]["elapsed_seconds"]
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.breaker.after_cycle(1.0, 1)
        self.assertEqual("OPEN", self.breaker._state)

    def run_prediction(self, outcome):
        clock = [0.0]
        predictor = self.predictor

        def submit(path, payload):
            self.probe_at_submission = json.loads(self.state_path.read_text())["predictor_last_probe"]
            clock[0] += 2.0
            if outcome == "write_error":
                raise OSError("shm unavailable")
            if outcome == "exception":
                raise RuntimeError("unexpected failure")
            if outcome == "success":
                response = {
                    "request_id": payload["request_id"], "device_code": "soil3",
                    "trajectories": {"observe": [31.0] * 12},
                }
                predictor.resp_path.write_text(json.dumps(response), encoding="utf-8")

        def sleep(seconds):
            clock[0] += seconds

        def fallback(*args):
            # Slow fallback work must also stay outside the predictor timer.
            clock[0] += 56.0
            return {"observe": [30.0] * 12}

        with mock.patch.object(self.brain.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(self.brain.time, "sleep", side_effect=sleep), \
             mock.patch.object(self.brain, "_predictor_profile", return_value={"zone": "low"}), \
             mock.patch.object(self.brain, "save_json_locked", side_effect=submit), \
             mock.patch.object(predictor, "_fallback_trajectories", side_effect=fallback):
            plans = [self.brain.ActionPlan("observe", 0.0)]
            reading = self.brain.SensorReading(31.0, 25.0, 1.0)
            if outcome == "exception":
                with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
                    predictor.request_prediction(plans, reading)
            else:
                predictor.request_prediction(plans, reading)
        return json.loads(self.state_path.read_text())["predictor_last_probe"]

    def test_real_predictor_persists_monotonic_duration_for_success(self):
        self.start_half_open()
        expected_id = self.breaker._probe_id
        probe = self.run_prediction("success")
        self.assertEqual(expected_id, self.probe_at_submission["probe_id"])
        self.assertFalse(self.probe_at_submission["success"])
        self.assertEqual(expected_id, probe["probe_id"])
        self.assertTrue(probe["success"])
        self.assertEqual(2.0, probe.get("elapsed_seconds"))
        self.breaker.after_cycle(56.0, 1)
        self.assertEqual("CLOSED", self.breaker._state)

    def test_timeout_duration_excludes_fallback_work(self):
        self.start_half_open()
        probe = self.run_prediction("timeout")
        self.assertFalse(probe["success"])
        self.assertEqual(32.0, probe.get("elapsed_seconds"))
        self.breaker.after_cycle(88.0, 1)
        self.assertEqual("OPEN", self.breaker._state)

    def test_write_failure_records_failed_probe_before_fallback(self):
        self.start_half_open()
        probe = self.run_prediction("write_error")
        self.assertFalse(probe["success"])
        self.assertEqual(2.0, probe.get("elapsed_seconds"))
        self.breaker.after_cycle(58.0, 1)
        self.assertEqual("OPEN", self.breaker._state)

    def test_unexpected_error_cannot_reuse_previous_success(self):
        self.write_probe()
        self.start_half_open()
        probe = self.run_prediction("exception")
        self.assertFalse(probe["success"])
        self.breaker.after_cycle(1.0, 1)
        self.assertEqual("OPEN", self.breaker._state)


if __name__ == "__main__":
    unittest.main()
