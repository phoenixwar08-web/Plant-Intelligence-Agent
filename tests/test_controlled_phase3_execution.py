import ast
import copy
import hashlib
import json
import sys
import tempfile
import types
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.soil3.cloud_gate.gate_v2 import GatePolicy, evaluate_gate_v2
from services.soil3.cloud_strategy.validator import PROMPT_VERSION, fingerprint
from services.soil3.controlled_execution import controlled_v1
from services.soil3.controlled_execution import ControlledExecutionError, ControlledPhase3Executor
from services.soil3.episode.episode_v1 import EpisodeStore
from services.soil3.phase3_bridge import Phase3Bridge
from services.soil3.runner.runner_v1 import DryRunRunner, RunnerStore
from services.soil3.state.state_v1 import PHASE3_SAFETY_FLAG_KEYS
from services.soil3.telemetry.common import atomic_write_json
from services.soil3.trace import TraceStore


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def artifact_ref(schema_version, path, record_id=None):
    return {
        "schema_version": schema_version,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "record_id": record_id,
    }


def state_snapshot():
    flags = {key: False for key in PHASE3_SAFETY_FLAG_KEYS}
    flags["predictor_circuit"] = {"state": "CLOSED", "active": False}
    return {
        "schema_version": "state.v1",
        "device_code": "soil3",
        "observed_at": "2026-09-21T08:00:00Z",
        "generated_at": "2026-09-21T08:00:01Z",
        "soil": {"humidity_percent": 30.0},
        "data_quality": {"soil_age_sec": 1.0, "phase3_state_age_sec": 1.0},
        "irrigation": {"pump_active": False},
        "safety": {"flags": flags},
    }


def strategy_for(state):
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "device_code": "soil3",
        "state_observed_at": state["observed_at"],
        "state_generated_at": state["generated_at"],
        "state_sha256": fingerprint(state),
        "created_at": "2026-09-21T08:00:02Z",
        "actions": [
            {"action_id": "a1", "type": "water", "pump_seconds": 5},
            {"action_id": "a2", "type": "observe"},
        ],
        "reason_summary": ["controlled fixture"],
        "expected_outcome": {"soil_moisture": "increase", "risk_notes": []},
        "confidence": 0.8,
        "model": {"provider": "test", "name": "fixture", "prompt_version": PROMPT_VERSION},
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }


class FixedClock:
    def __call__(self):
        return datetime(2026, 9, 21, 8, 0, 3, tzinfo=timezone.utc)


def phase3_result(action_sec=0.0):
    return SimpleNamespace(
        zone=SimpleNamespace(name="SAFE_SLEEP"),
        chosen_plan=SimpleNamespace(label="phase3_safe_sleep") if action_sec else None,
        action_sec=action_sec,
        notes="Phase3 final decision",
        reading=SimpleNamespace(humidity=42.0, temperature=24.0, ec_raw=510.0),
    )


class PersistedChain:
    def __init__(self, root):
        self.root = root
        self.state = state_snapshot()
        self.strategy = strategy_for(self.state)
        self.gate = evaluate_gate_v2(
            self.state,
            self.strategy,
            GatePolicy(60, 120, 86400, 20),
            exploration_requested=False,
        )
        self.runner = DryRunRunner(RunnerStore(root / "runner"), clock=FixedClock()).run(
            self.strategy, self.state
        )
        self.bridge = Phase3Bridge().verify(self.state, self.strategy, self.gate, self.runner)
        self.paths = {
            "state": root / "state.json",
            "strategy": root / "strategy.json",
            "gate": root / "gate.json",
            "runner": root / "runner.json",
            "bridge": root / "bridge.json",
        }
        for name, value in (
            ("state", self.state),
            ("strategy", self.strategy),
            ("gate", self.gate),
            ("runner", self.runner),
            ("bridge", self.bridge),
        ):
            atomic_write_json(self.paths[name], value)

        episodes = EpisodeStore(root / "episodes")
        episode = episodes.create(self.state)
        self.episode_id = episode["episode_id"]
        episodes.update(self.episode_id, strategy=self.strategy, gate_result=self.gate)
        self.episode_path = episodes.episode_path(self.episode_id)

        traces = TraceStore(root / "traces")
        trace = traces.create()
        self.trace_id = trace["trace_id"]
        traces.set_decision(
            self.trace_id,
            state_ref=artifact_ref("state.v1", self.paths["state"]),
            strategy_ref=artifact_ref(
                "strategy.v1", self.paths["strategy"], self.strategy["strategy_id"]
            ),
            gate_ref=artifact_ref("gate.v2", self.paths["gate"], self.gate["gate_id"]),
            gate_decision=self.gate["decision"],
            gate_reason_codes=self.gate["reason_codes"],
            runner_ref=artifact_ref(
                "runner_state.v1", self.paths["runner"], self.strategy["strategy_id"]
            ),
            bridge_ref=artifact_ref(
                "phase3_bridge_response.v1",
                self.paths["bridge"],
                self.bridge["handoff"]["request_id"],
            ),
            episode_ref=artifact_ref("episode.v1", self.episode_path, self.episode_id),
        )
        self.trace_dir = root / "traces"

    def approval(self, **updates):
        handoff = self.bridge["handoff"]
        value = {
            "schema_version": "controlled_scenario_approval.v1",
            "approval_id": str(uuid.uuid4()),
            "scenario_id": "soil3-conservative-observation-001",
            "device_code": "soil3",
            "approved_by": "owner",
            "approved_at": "2026-09-24T07:55:00Z",
            "expires_at": "2026-09-24T08:05:00Z",
            "scope": "phase3_run_cycle_once",
            "max_runs": 1,
            "bridge_request_id": handoff["request_id"],
            "bridge_bindings": dict(handoff["bindings"]),
            "trace_id": self.trace_id,
            "episode_id": self.episode_id,
        }
        value.update(updates)
        return value

    def rewrite_trace(self, mutate):
        path = self.trace_dir / f"{self.trace_id}.json"
        trace = json.loads(path.read_text(encoding="utf-8"))
        mutate(trace)
        atomic_write_json(path, trace)


class ControlledPhase3ExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fcntl = types.ModuleType("fcntl")
        fcntl.LOCK_SH = 1
        fcntl.LOCK_EX = 2
        fcntl.LOCK_UN = 8
        fcntl.flock = lambda *args: None
        cls.previous_fcntl = sys.modules.get("fcntl")
        sys.modules["fcntl"] = fcntl
        cls.phase3_dir = str((ROOT / "services" / "soil3" / "phase3").resolve())
        sys.path.insert(0, cls.phase3_dir)
        import decision_brain
        cls.DecisionBrain = decision_brain.DecisionBrain

    @classmethod
    def tearDownClass(cls):
        if cls.phase3_dir in sys.path:
            sys.path.remove(cls.phase3_dir)
        if cls.previous_fcntl is None:
            sys.modules.pop("fcntl", None)
        else:
            sys.modules["fcntl"] = cls.previous_fcntl

    def executor(self, root):
        return ControlledPhase3Executor(Path(root) / "receipts", clock=lambda: NOW)

    def brain(self):
        return object.__new__(self.DecisionBrain)

    def test_code_identity_comparison_uses_lnotab_without_linetable(self):
        fields = {
            "co_argcount": 1,
            "co_posonlyargcount": 0,
            "co_kwonlyargcount": 0,
            "co_nlocals": 1,
            "co_stacksize": 1,
            "co_flags": 3,
            "co_code": b"official",
            "co_consts": (None,),
            "co_names": (),
            "co_varnames": ("self",),
            "co_freevars": (),
            "co_cellvars": (),
            "co_firstlineno": 10,
            "co_lnotab": b"\x00\x01",
        }
        actual = SimpleNamespace(**fields)
        expected = SimpleNamespace(**fields)
        compare = getattr(controlled_v1, "_code_objects_match", lambda *_: False)

        self.assertTrue(compare(actual, expected))

    def test_persisted_chain_is_reverified_before_formal_phase3_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = PersistedChain(Path(directory))
            brain = self.brain()
            with mock.patch(
                "services.soil3.controlled_execution.controlled_v1."
                "_invoke_formal_phase3_cycle",
                return_value=phase3_result(),
            ) as run_cycle:
                receipt = self.executor(directory).execute(
                    chain.trace_dir, chain.approval(), brain
                )

            run_cycle.assert_called_once_with(brain)
            self.assertEqual("completed", receipt["status"])
            self.assertTrue(receipt["phase3_called"])
            self.assertFalse(receipt["physical_actions_performed"])
            self.assertEqual(
                chain.bridge["handoff"]["request_id"], receipt["bridge_request_id"]
            )

    def test_forged_bridge_is_rejected_even_when_trace_hash_is_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = PersistedChain(Path(directory))
            forged = copy.deepcopy(chain.bridge)
            forged["handoff"]["bindings"]["state_sha256"] = "0" * 64
            atomic_write_json(chain.paths["bridge"], forged)
            chain.rewrite_trace(
                lambda trace: trace["decision"]["bridge_ref"].update(
                    sha256=hashlib.sha256(chain.paths["bridge"].read_bytes()).hexdigest()
                )
            )
            brain = self.brain()
            with mock.patch(
                "services.soil3.controlled_execution.controlled_v1."
                "_invoke_formal_phase3_cycle"
            ) as run_cycle:
                with self.assertRaisesRegex(ControlledExecutionError, "preflight_rejected"):
                    self.executor(directory).execute(chain.trace_dir, chain.approval(), brain)
            run_cycle.assert_not_called()

    def test_forged_trace_or_episode_is_rejected(self):
        cases = {
            "trace_execution": lambda chain: chain.rewrite_trace(
                lambda trace: trace["execution"].update(phase3_called=True)
            ),
            "trace_decision_field": lambda chain: chain.rewrite_trace(
                lambda trace: trace["decision"].update(forged_field="accepted")
            ),
            "episode_strategy": lambda chain: (
                lambda episode: atomic_write_json(chain.episode_path, episode)
            )(
                {
                    **json.loads(chain.episode_path.read_text(encoding="utf-8")),
                    "strategy": {**chain.strategy, "confidence": 0.1},
                }
            ),
        }
        for label, corrupt in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                chain = PersistedChain(Path(directory))
                corrupt(chain)
                brain = self.brain()
                with mock.patch(
                    "services.soil3.controlled_execution.controlled_v1."
                    "_invoke_formal_phase3_cycle"
                ) as run_cycle:
                    with self.assertRaisesRegex(ControlledExecutionError, "preflight_rejected"):
                        self.executor(directory).execute(chain.trace_dir, chain.approval(), brain)
                run_cycle.assert_not_called()

    def test_nonformal_callable_or_object_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = PersistedChain(Path(directory))
            spoofed_type = type(
                "DecisionBrain",
                (),
                {"__module__": "services.soil3.phase3.decision_brain", "run_cycle": lambda self: phase3_result()},
            )
            candidates = (
                lambda: phase3_result(),
                mock.Mock(),
                SimpleNamespace(run_cycle=lambda: phase3_result()),
                spoofed_type(),
            )
            for candidate in candidates:
                with self.subTest(candidate=type(candidate).__name__):
                    with self.assertRaisesRegex(
                        ControlledExecutionError, "formal_phase3_instance_required"
                    ):
                        self.executor(directory).execute(
                            chain.trace_dir, chain.approval(), candidate
                        )
            forged_module = types.ModuleType("decision_brain")
            forged_module.__file__ = str(
                ROOT / "services" / "soil3" / "phase3" / "decision_brain.py"
            )
            forged_type = type(
                "DecisionBrain",
                (),
                {"__module__": "decision_brain", "run_cycle": lambda self: phase3_result()},
            )
            forged_module.DecisionBrain = forged_type
            with mock.patch.dict(sys.modules, {"decision_brain": forged_module}):
                with self.assertRaisesRegex(
                    ControlledExecutionError, "formal_phase3_instance_required"
                ):
                    self.executor(directory).execute(
                        chain.trace_dir, chain.approval(), forged_type()
                    )
            grafted_module = types.ModuleType("decision_brain")
            grafted_module.__file__ = forged_module.__file__
            grafted_type = type(
                "DecisionBrain",
                (),
                {
                    "__module__": "decision_brain",
                    "run_cycle": self.DecisionBrain.run_cycle,
                },
            )
            grafted_module.DecisionBrain = grafted_type
            with mock.patch.dict(sys.modules, {"decision_brain": grafted_module}):
                with self.assertRaisesRegex(
                    ControlledExecutionError, "formal_phase3_instance_required"
                ):
                    self.executor(directory).execute(
                        chain.trace_dir, chain.approval(), grafted_type()
                    )

    def test_invalid_owner_approval_never_calls_phase3(self):
        cases = {
            "wrong_owner": {"approved_by": "someone-else"},
            "expired": {"expires_at": "2026-09-24T07:59:59Z"},
            "wrong_trace": {"trace_id": "tr-" + "a" * 24},
            "multiple_runs": {"max_runs": 2},
        }
        for label, updates in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                chain = PersistedChain(Path(directory))
                brain = self.brain()
                with mock.patch(
                    "services.soil3.controlled_execution.controlled_v1."
                    "_invoke_formal_phase3_cycle"
                ) as run_cycle:
                    with self.assertRaises(ControlledExecutionError):
                        self.executor(directory).execute(
                            chain.trace_dir, chain.approval(**updates), brain
                        )
                run_cycle.assert_not_called()
                self.assertFalse((Path(directory) / "receipts").exists())

    def test_approval_is_at_most_once_and_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = PersistedChain(Path(directory))
            permit = chain.approval()
            brain = self.brain()
            with mock.patch(
                "services.soil3.controlled_execution.controlled_v1."
                "_invoke_formal_phase3_cycle",
                side_effect=RuntimeError("hardware unavailable"),
            ) as run_cycle:
                with self.assertRaisesRegex(ControlledExecutionError, "phase3_cycle_failed"):
                    self.executor(directory).execute(chain.trace_dir, permit, brain)
                with self.assertRaisesRegex(
                    ControlledExecutionError, "approval_already_consumed"
                ):
                    self.executor(directory).execute(chain.trace_dir, permit, brain)
            run_cycle.assert_called_once_with(brain)
            receipt = self.executor(directory).read(permit["approval_id"])
            self.assertEqual("phase3_error", receipt["status"])
            self.assertIsNone(receipt["physical_actions_performed"])

    def test_adapter_has_no_direct_actuator_mqtt_or_manual_water_dependency(self):
        source_path = (
            ROOT / "services" / "soil3" / "controlled_execution" / "controlled_v1.py"
        )
        source = source_path.read_text(encoding="utf-8")
        imports = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        forbidden = (
            "services.soil3.phase3.decision_brain",
            "services.soil3.phase1",
            "paho.mqtt",
        )
        self.assertFalse([
            name
            for name in imports
            if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
        ])
        symbols = {
            node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)
        } | {
            node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute)
        }
        self.assertTrue({"manual_water", "publish", "ActuatorLayer"}.isdisjoint(symbols))


if __name__ == "__main__":
    unittest.main()
