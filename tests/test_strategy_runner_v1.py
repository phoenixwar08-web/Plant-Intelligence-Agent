import copy
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.soil3.cloud_strategy.validator import PROMPT_VERSION, StrategyValidator, fingerprint
from services.soil3.runner.runner_v1 import (
    RUN_STATUSES,
    STEP_STATUSES,
    DryRunRunner,
    RunnerStore,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "services" / "soil3" / "runner" / "runner_state.v1.schema.json"


def state_snapshot():
    return {
        "device_code": "soil3",
        "observed_at": "2026-09-20T08:00:00Z",
    }


def strategy(actions=None, *, state=None):
    state = state or state_snapshot()
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "device_code": "soil3",
        "state_observed_at": "2026-09-20T08:00:00Z",
        "state_generated_at": "2026-09-20T08:01:00Z",
        "state_sha256": fingerprint(state),
        "created_at": "2026-09-20T08:01:30Z",
        "actions": actions or [
            {"action_id": "a1", "type": "water", "pump_seconds": 5},
            {"action_id": "a2", "type": "wait", "seconds": 10},
            {"action_id": "a3", "type": "observe"},
            {"action_id": "a4", "type": "stop"},
        ],
        "reason_summary": ["fixture"],
        "expected_outcome": {"soil_moisture": "increase", "risk_notes": []},
        "confidence": 0.9,
        "model": {"provider": "test", "name": "fixture", "prompt_version": PROMPT_VERSION},
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 9, 20, 8, 2, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class StrategyRunnerV1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunnerStore(Path(self.temp.name) / "runner")
        self.clock = MutableClock()

    def runner(self):
        return DryRunRunner(self.store, clock=self.clock)

    def test_multi_step_strategy_advances_to_wait_and_persists_current_step(self):
        item = strategy()
        record = self.runner().run(item, state_snapshot())
        self.assertEqual("waiting", record["status"])
        self.assertEqual(1, record["current_step_index"])
        self.assertEqual("completed", record["steps"][0]["status"])
        self.assertEqual("waiting", record["steps"][1]["status"])
        self.assertEqual("dry_run_water", record["steps"][0]["result"]["kind"])
        self.assertFalse(record["steps"][0]["result"]["physical_action_performed"])
        self.assertEqual(record, self.store.read(item["strategy_id"]))

    def test_wait_resumes_after_restart_and_stop_terminates(self):
        item = strategy()
        first = self.runner().run(item, state_snapshot())
        self.clock.advance(9)
        before_expiry = self.runner().resume(item["strategy_id"])
        self.assertEqual(first, before_expiry)

        self.clock.advance(1)
        restarted = DryRunRunner(self.store, clock=self.clock)
        finished = restarted.resume(item["strategy_id"])
        self.assertEqual("stopped", finished["status"])
        self.assertEqual(4, finished["current_step_index"])
        self.assertEqual(
            ["completed", "completed", "completed", "completed"],
            [step["status"] for step in finished["steps"]],
        )
        self.assertFalse(finished["steps"][2]["result"]["observation_collected"])

    def test_strategy_without_stop_completes(self):
        item = strategy(
            [
                {"action_id": "a1", "type": "water", "pump_seconds": 2},
                {"action_id": "a2", "type": "observe"},
            ]
        )
        record = self.runner().run(item, state_snapshot())
        self.assertEqual("completed", record["status"])
        self.assertEqual(2, record["current_step_index"])

    def test_repeated_start_and_resume_never_duplicate_water(self):
        item = strategy()
        first = self.runner().run(item, state_snapshot())
        second = DryRunRunner(self.store, clock=self.clock).run(item, state_snapshot())
        third = self.runner().resume(item["strategy_id"])
        for record in (first, second, third):
            water_results = [
                step["result"]
                for step in record["steps"]
                if step["result"] and step["result"]["kind"] == "dry_run_water"
            ]
            self.assertEqual(1, len(water_results))
        persisted_files = list((Path(self.temp.name) / "runner").glob("*.json"))
        self.assertEqual(1, len(persisted_files))

    def test_same_id_with_changed_content_is_rejected(self):
        item = strategy()
        self.runner().start(item, state_snapshot())
        changed = copy.deepcopy(item)
        changed["actions"][0]["pump_seconds"] = 6
        with self.assertRaisesRegex(ValueError, "different content"):
            self.runner().start(changed, state_snapshot())

    def test_rejects_non_strategy_or_unsafe_execution_metadata(self):
        cases = []
        missing = strategy()
        missing.pop("state_sha256")
        cases.append(missing)
        wrong_device = strategy()
        wrong_device["device_code"] = "soil1"
        cases.append(wrong_device)
        permission = strategy()
        permission["execution"]["actuator_commands_allowed"] = True
        cases.append(permission)
        bad_action = strategy()
        bad_action["actions"][0]["pump_seconds"] = 0
        cases.append(bad_action)
        after_stop = strategy(
            [
                {"action_id": "a1", "type": "stop"},
                {"action_id": "a2", "type": "observe"},
            ]
        )
        cases.append(after_stop)

        for item in cases:
            with self.subTest(strategy_id=item.get("strategy_id")):
                with self.assertRaises(ValueError):
                    self.runner().start(item, state_snapshot())

    def test_runner_matches_formal_action_limits_before_persisting(self):
        state = state_snapshot()
        twelve_actions = [
            {"action_id": f"observe-{index}", "type": "observe"}
            for index in range(12)
        ]
        thirteen_actions = twelve_actions + [{"action_id": "observe-12", "type": "observe"}]
        cases = {
            "water 120 seconds": ([{"action_id": "water", "type": "water", "pump_seconds": 120}], True),
            "water 121 seconds": ([{"action_id": "water", "type": "water", "pump_seconds": 121}], False),
            "wait 86400 seconds": ([{"action_id": "wait", "type": "wait", "seconds": 86400}], True),
            "wait 86401 seconds": ([{"action_id": "wait", "type": "wait", "seconds": 86401}], False),
            "twelve actions": (twelve_actions, True),
            "thirteen actions": (thirteen_actions, False),
        }

        for label, (actions, accepted) in cases.items():
            with self.subTest(case=label):
                item = strategy(actions)
                item["state_sha256"] = fingerprint(state)
                formal = StrategyValidator().validate(item, state)
                self.assertEqual(accepted, formal.accepted)

                if accepted:
                    record = self.runner().start(item, state)
                    self.assertEqual(item["strategy_id"], record["strategy_id"])
                else:
                    with self.assertRaises(ValueError):
                        self.runner().start(item, state)
                    self.assertFalse(self.store.exists(item["strategy_id"]))

    def test_runner_refuses_formal_state_binding_mismatch_without_persisting(self):
        item = strategy()
        mismatched_state = state_snapshot()
        mismatched_state["quality"] = {"status": "good"}

        formal = StrategyValidator().validate(item, mismatched_state)
        self.assertFalse(formal.accepted)
        self.assertIn("state_sha256_mismatch", formal.reason_codes)
        with self.assertRaises(ValueError):
            self.runner().start(item, mismatched_state)
        self.assertFalse(self.store.exists(item["strategy_id"]))

    def test_store_rejects_path_traversal_and_corrupt_state(self):
        with self.assertRaises(ValueError):
            self.store.read("../../state")
        item = strategy()
        path = self.store.path_for(item["strategy_id"])
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid runner state"):
            self.store.read(item["strategy_id"])

    def test_schema_statuses_match_code_and_declares_no_execution(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(RUN_STATUSES, set(schema["properties"]["status"]["enum"]))
        step_status = schema["properties"]["steps"]["items"]["properties"]["status"]["enum"]
        self.assertEqual(STEP_STATUSES, set(step_status))
        execution = schema["properties"]["execution"]["properties"]
        self.assertFalse(execution["physical_actions_performed"]["const"])
        self.assertFalse(execution["phase3_called"]["const"])

    def test_cli_start_and_read_round_trip(self):
        item = strategy()
        strategy_path = Path(self.temp.name) / "strategy.json"
        strategy_path.write_text(json.dumps(item), encoding="utf-8")
        state_path = Path(self.temp.name) / "state.json"
        state_path.write_text(json.dumps(state_snapshot()), encoding="utf-8")
        directory = Path(self.temp.name) / "cli-store"
        base = [sys.executable, "-m", "services.soil3.runner.service", "--store-dir", str(directory)]
        started = subprocess.run(
            base + ["start", "--strategy", str(strategy_path), "--state", str(state_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, started.returncode, started.stderr)
        self.assertEqual("waiting", json.loads(started.stdout)["status"])
        read = subprocess.run(
            base + ["read", "--strategy-id", item["strategy_id"]],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, read.returncode, read.stderr)
        self.assertEqual(json.loads(started.stdout), json.loads(read.stdout))

    def test_runner_source_has_no_real_control_or_same_day_dependencies(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "services" / "soil3" / "runner").glob("*.py")
        )
        for forbidden in (
            "services.soil3.phase3",
            "services.soil3.gate",
            "services.soil3.episode",
            "import paho",
            "manual_water",
            ".publish(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
