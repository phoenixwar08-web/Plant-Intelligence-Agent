# Trigger Phase 1 Conditional Qwen Invocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing deterministic Trigger decision control whether one soil3 Shadow runtime cycle invokes Qwen, while preserving factual no-cloud artifacts and all downstream safety boundaries.

**Architecture:** The existing runtime creates Trace and State first, then obtains a durable internal Trigger admission result. `CALL_QWEN` continues through the unchanged Vision/Experience and Shadow chain; `SKIP_CLOUD` and `LOCAL_SAFETY_ONLY` persist a completed local runtime record and return before any cloud or downstream work. Trigger admission errors persist a fail-closed runtime record when possible, raise a stable error, and never fall through to Qwen.

**Tech Stack:** Python 3.9-compatible standard library, `unittest`, existing `TraceStore`, `TriggerShadowStore`, State/Strategy/Gate/Runner/Bridge/Episode services.

**Spec:** `docs/superpowers/specs/2026-10-20-trigger-phase1-control-design.md`

## Global Constraints

- Do not change Trigger rules or thresholds.
- Do not change `state.v1`, `strategy.v1`, `gate.v2`, or `trace.v1`.
- Do not change Runner, Bridge, Phase3, MQTT, ActuatorLayer, or execution permissions.
- Only `CALL_QWEN` may enter Vision, Experience, Qwen, Validator, Gate, Runner, Bridge, or Episode.
- `SKIP_CLOUD`, `LOCAL_SAFETY_ONLY`, and Trigger admission failures must never invoke Qwen.
- Non-call cycles must not invent provider output, token/cost/latency, Strategy, Gate, Episode, Feedback, or execution facts.
- Keep `phase3_called=false` and `physical_actions_performed=false` for every path.
- Do not deploy, enable `plant-agent-soil3-pipeline.timer`, or run a live Qwen smoke test in this Issue.

## File map

- Modify `services/soil3/agent_runtime/runtime_v1.py`: return a typed internal Trigger admission, branch before cloud work, and persist uniform runtime observability.
- Modify `tests/test_soil3_agent_runtime.py`: replace Phase 0 unconditional-call expectations with Phase 1 admission and fail-closed regressions.
- Modify `services/soil3/agent_runtime/README.md`: document the three runtime branches and no-cloud artifacts.
- Modify `docs/SYSTEM_ARCHITECTURE.md`: replace the obsolete Phase 0 unconditional-Qwen description with Phase 1 behavior.

---

### Task 1: Make successful Trigger decisions control cloud admission

**Files:**
- Modify: `tests/test_soil3_agent_runtime.py:719-782`
- Modify: `services/soil3/agent_runtime/runtime_v1.py:18-22`
- Modify: `services/soil3/agent_runtime/runtime_v1.py:511-542`
- Modify: `services/soil3/agent_runtime/runtime_v1.py:761-1014`

**Interfaces:**
- Consumes: `evaluate_trigger(...) -> TriggerDecision`, `TriggerShadowStore.create(...) -> dict[str, Any]`, `TraceStore.set_decision(...)`.
- Produces: private `TriggerAdmission(store: TriggerShadowStore, path: str, decision: TriggerDecision)` and runtime record fields `trigger_status`, `trigger_decision`, `trigger_reason_codes`, `cloud_invocation_status`.

- [ ] **Step 1: Replace the Phase 0 unconditional-call test with a failing `CALL_QWEN` preservation test**

Add a test that fixes the evaluator output to `CALL_QWEN`, wraps the real
`run_chain`, and asserts one call plus the full existing Gate/Trace behavior:

```python
def test_call_qwen_trigger_runs_existing_shadow_chain_once(self):
    from services.soil3.agent_runtime import runtime_v1
    from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig, run_pipeline
    from services.soil3.cloud_trigger import CALL_QWEN, TriggerDecision

    with tempfile.TemporaryDirectory() as directory:
        config = RuntimeConfig.from_dict(self.runtime_config_value(Path(directory)))
        with mock.patch(
            "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
            return_value=self.health_snapshot(),
        ), mock.patch(
            "services.soil3.agent_runtime.runtime_v1.evaluate_trigger",
            return_value=TriggerDecision(CALL_QWEN, ("test_call",)),
        ), mock.patch(
            "services.soil3.agent_runtime.runtime_v1.run_chain",
            wraps=runtime_v1.run_chain,
        ) as cloud_call:
            record = run_pipeline(config)

        cloud_call.assert_called_once()
        self.assertEqual("recorded", record["trigger_status"])
        self.assertEqual(CALL_QWEN, record["trigger_decision"])
        self.assertEqual(["test_call"], record["trigger_reason_codes"])
        self.assertEqual("called", record["cloud_invocation_status"])
        self.assertIsNotNone(record["gate_path"])
```

- [ ] **Step 2: Run the CALL test and verify RED**

Run:

```powershell
python -m unittest tests.test_soil3_agent_runtime.StateProducerTests.test_call_qwen_trigger_runs_existing_shadow_chain_once -v
```

Expected: FAIL because the current runtime record has none of the four Phase 1
admission fields.

- [ ] **Step 3: Add failing no-cloud tests for both non-call decisions**

Use subtests for `SKIP_CLOUD` and `LOCAL_SAFETY_ONLY`. Patch the public/owned
entry points that must not start and assert both call counts and persisted
facts:

```python
def test_non_call_trigger_decisions_stop_before_cloud_and_downstream(self):
    from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig, run_pipeline
    from services.soil3.cloud_trigger import (
        LOCAL_SAFETY_ONLY,
        SKIP_CLOUD,
        TriggerDecision,
        TriggerShadowStore,
    )
    from services.soil3.trace.trace_v1 import TraceStore

    cases = (
        (SKIP_CLOUD, "skipped_trigger"),
        (LOCAL_SAFETY_ONLY, "blocked_local_safety"),
    )
    for decision_name, cloud_status in cases:
        with self.subTest(decision=decision_name), tempfile.TemporaryDirectory() as directory:
            config = RuntimeConfig.from_dict(self.runtime_config_value(Path(directory)))
            with mock.patch(
                "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                return_value=self.health_snapshot(),
            ), mock.patch(
                "services.soil3.agent_runtime.runtime_v1.evaluate_trigger",
                return_value=TriggerDecision(decision_name, ("test_reason",)),
            ), mock.patch(
                "services.soil3.agent_runtime.runtime_v1._collect_vision"
            ) as vision, mock.patch(
                "services.soil3.agent_runtime.runtime_v1._collect_experience"
            ) as experience, mock.patch(
                "services.soil3.agent_runtime.runtime_v1.run_chain"
            ) as cloud_call, mock.patch(
                "services.soil3.agent_runtime.runtime_v1.evaluate_gate_v2"
            ) as gate, mock.patch(
                "services.soil3.agent_runtime.runtime_v1.DryRunRunner.run"
            ) as runner, mock.patch(
                "services.soil3.agent_runtime.runtime_v1.Phase3Bridge.verify"
            ) as bridge, mock.patch(
                "services.soil3.agent_runtime.runtime_v1.EpisodeStore.create"
            ) as episode:
                record = run_pipeline(config)

            for blocked in (vision, experience, cloud_call, gate, runner, bridge, episode):
                blocked.assert_not_called()
            self.assertEqual(decision_name, record["trigger_decision"])
            self.assertEqual(cloud_status, record["cloud_invocation_status"])
            self.assertIsNone(record["provider"])
            self.assertIsNone(record["model"])
            self.assertIsNone(record["strategy_path"])
            self.assertIsNone(record["gate_path"])
            self.assertIsNone(record["audit_path"])

            shadow = TriggerShadowStore(config.trigger_shadow_dir).read(record["trace_id"])
            self.assertIsNone(shadow["actual_qwen_result"])
            trace = TraceStore(config.trace_dir).read(record["trace_id"])
            self.assertIsNotNone(trace["decision"]["state_ref"])
            self.assertEqual(
                {"availability": "not_requested", "ref": None},
                trace["decision"]["vision"],
            )
            self.assertIsNone(trace["decision"]["strategy_ref"])
```

Also assert that the Strategy and Gate output files and Runner, Bridge, Episode,
audit, Vision, and Experience directories were not created in each fresh
temporary runtime root.

- [ ] **Step 4: Run the no-cloud test and verify RED**

Run:

```powershell
python -m unittest tests.test_soil3_agent_runtime.StateProducerTests.test_non_call_trigger_decisions_stop_before_cloud_and_downstream -v
```

Expected: FAIL because the current Phase 0 runtime calls `_collect_vision()`,
`_collect_experience()`, and `run_chain()` for both decisions.

- [ ] **Step 5: Implement the minimal typed admission result**

Import the existing constants/type and add one private frozen dataclass:

```python
from services.soil3.cloud_trigger import (
    CALL_QWEN,
    LOCAL_SAFETY_ONLY,
    SKIP_CLOUD,
    TriggerDecision,
    TriggerShadowStore,
    build_decision_context,
    evaluate_trigger,
)

@dataclass(frozen=True)
class TriggerAdmission:
    store: TriggerShadowStore
    path: str
    decision: TriggerDecision
```

Change `_start_trigger_shadow(...) -> TriggerAdmission` to remove its broad
catch, persist the existing Trigger record, and return the exact evaluator
result. Do not change `build_decision_context`, `evaluate_trigger`, or
`TriggerShadowStore`.

- [ ] **Step 6: Add one private runtime-record writer and the early branches**

Add a private helper that atomically writes the existing internal
`agent_runtime.v1` shape with explicit admission fields. For non-call paths use:

```python
record = {
    "schema_version": "agent_runtime.v1",
    "run_id": str(uuid.uuid4()),
    "provider_mode": config.provider_mode,
    "provider": None,
    "model": None,
    "trace_id": trace_id,
    "trace_path": str(trace_path),
    "state_path": str(config.state_output),
    "state_sha256": fingerprint(state),
    "trigger_status": "recorded",
    "trigger_decision": admission.decision.decision,
    "trigger_reason_codes": list(admission.decision.reason_codes),
    "cloud_invocation_status": cloud_status,
    "trigger_shadow_path": admission.path,
    "strategy_path": None,
    "gate_path": None,
    "gate_decision": None,
    "runner_status": not_started_status,
    "runner_path": None,
    "bridge_status": not_started_status,
    "bridge_path": None,
    "episode_id": None,
    "episode_path": None,
    "audit_path": None,
    "execution": {"physical_actions_performed": False, "phase3_called": False},
}
```

In `run_pipeline`, set `state_ref` on Trace immediately after State is written,
then obtain `TriggerAdmission`. Return the no-cloud record for `SKIP_CLOUD` and
`LOCAL_SAFETY_ONLY`; enter `_collect_vision` only when the decision equals
`CALL_QWEN`. An unrecognized decision must raise rather than defaulting to a
call.

Add the four admission fields to both existing CALL-path records (provider /
validation failure and completed Strategy/Gate path) with
`cloud_invocation_status="called"`.

- [ ] **Step 7: Run the two focused tests and verify GREEN**

Run:

```powershell
python -m unittest \
  tests.test_soil3_agent_runtime.StateProducerTests.test_call_qwen_trigger_runs_existing_shadow_chain_once \
  tests.test_soil3_agent_runtime.StateProducerTests.test_non_call_trigger_decisions_stop_before_cloud_and_downstream -v
```

Expected: 2 tests pass; no blocked entry point is called.

- [ ] **Step 8: Run the runtime module tests**

Run:

```powershell
python -m unittest tests.test_soil3_agent_runtime -v
```

Expected: all tests pass after replacing Phase 0 assertions that conflict with
the approved Phase 1 behavior.

- [ ] **Step 9: Commit Task 1**

```powershell
git add -- services/soil3/agent_runtime/runtime_v1.py tests/test_soil3_agent_runtime.py
git commit -m "feat: gate qwen calls with trigger decisions"
```

---

### Task 2: Fail closed on Trigger admission errors

**Files:**
- Modify: `tests/test_soil3_agent_runtime.py:784-806`
- Modify: `services/soil3/agent_runtime/runtime_v1.py:761-805`

**Interfaces:**
- Consumes: the Task 1 `TriggerAdmission` result and internal runtime-record writer.
- Produces: stable failure fields `trigger_status="failed_closed"`, `trigger_decision=None`, `trigger_reason_codes=["trigger_admission_failed"]`, and `cloud_invocation_status="blocked_trigger_failure"`.

- [ ] **Step 1: Replace the Phase 0 observer-failure test with a failing fail-closed test**

```python
def test_trigger_admission_failure_is_persisted_and_never_calls_cloud(self):
    from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig, run_pipeline
    from services.soil3.trace.trace_v1 import TraceStore

    with tempfile.TemporaryDirectory() as directory:
        config = RuntimeConfig.from_dict(self.runtime_config_value(Path(directory)))
        with mock.patch(
            "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
            return_value=self.health_snapshot(),
        ), mock.patch(
            "services.soil3.agent_runtime.runtime_v1.build_decision_context",
            side_effect=RuntimeError("secret-bearing observer detail"),
        ), mock.patch(
            "services.soil3.agent_runtime.runtime_v1.run_chain"
        ) as cloud_call, mock.patch(
            "services.soil3.agent_runtime.runtime_v1._collect_vision"
        ) as vision, mock.patch(
            "services.soil3.agent_runtime.runtime_v1._collect_experience"
        ) as experience:
            with self.assertRaisesRegex(RuntimeError, "trigger admission failed"):
                run_pipeline(config)

        cloud_call.assert_not_called()
        vision.assert_not_called()
        experience.assert_not_called()
        record_path, = config.runs_dir.glob("*.json")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual("failed_closed", record["trigger_status"])
        self.assertIsNone(record["trigger_decision"])
        self.assertEqual(["trigger_admission_failed"], record["trigger_reason_codes"])
        self.assertEqual("blocked_trigger_failure", record["cloud_invocation_status"])
        self.assertNotIn("secret-bearing", json.dumps(record))
        trace = TraceStore(config.trace_dir).read(record["trace_id"])
        self.assertIsNotNone(trace["decision"]["state_ref"])
        self.assertIsNone(trace["decision"]["strategy_ref"])
```

- [ ] **Step 2: Run the failure test and verify RED**

Run:

```powershell
python -m unittest tests.test_soil3_agent_runtime.StateProducerTests.test_trigger_admission_failure_is_persisted_and_never_calls_cloud -v
```

Expected: FAIL because the current implementation either ignores the Trigger
failure or has not yet persisted a stable fail-closed record.

- [ ] **Step 3: Implement the minimal failure branch**

Wrap only `_start_trigger_shadow(...)` in `run_pipeline`. On any exception:

1. write the internal failure record using the already-persisted State and Trace;
2. set all provider/model/downstream paths and decisions to `None`;
3. use `not_started_trigger_failure` for Runner and Bridge status;
4. raise `RuntimeError("trigger admission failed") from error`.

Do not put the later CALL chain inside this broad catch, and do not serialize
the exception message or traceback into artifacts.

- [ ] **Step 4: Add store-write and invalid-decision fail-closed regressions**

Patch `TriggerShadowStore.create` to raise `OSError("disk full")`, then assert
the same no-cloud failure behavior and stable persisted code. Patch
`evaluate_trigger` to return a malformed object and assert that admission is
rejected before `_collect_vision` and `run_chain`. These tests prove that the
durable admission record is mandatory and an unknown value never defaults to
`CALL_QWEN`.

- [ ] **Step 5: Run the failure-focused tests and verify GREEN**

Run:

```powershell
python -m unittest \
  tests.test_soil3_agent_runtime.StateProducerTests.test_trigger_admission_failure_is_persisted_and_never_calls_cloud \
  tests.test_soil3_agent_runtime.StateProducerTests.test_trigger_store_failure_never_calls_cloud \
  tests.test_soil3_agent_runtime.StateProducerTests.test_invalid_trigger_decision_never_calls_cloud -v
```

Expected: 3 tests pass and all cloud/downstream mocks remain uncalled.

- [ ] **Step 6: Run Trigger and runtime focused regression**

Run:

```powershell
python -m unittest tests.test_cloud_trigger_shadow tests.test_soil3_agent_runtime -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add -- services/soil3/agent_runtime/runtime_v1.py tests/test_soil3_agent_runtime.py
git commit -m "test: enforce fail closed trigger admission"
```

---

### Task 3: Synchronize architecture documentation and verify the Issue boundary

**Files:**
- Modify: `services/soil3/agent_runtime/README.md`
- Modify: `docs/SYSTEM_ARCHITECTURE.md`

**Interfaces:**
- Consumes: Task 1 runtime record fields and Task 2 failure semantics.
- Produces: one non-conflicting operational description of Phase 1; no code interface.

- [ ] **Step 1: Update the runtime README**

Document this exact branch table:

| Trigger result | Vision / Experience | Qwen and later Shadow stages | Runtime result |
| --- | --- | --- | --- |
| `CALL_QWEN` | Existing configured behavior | Runs once through existing chain | `cloud_invocation_status=called` |
| `SKIP_CLOUD` | Not requested | Not started | `cloud_invocation_status=skipped_trigger` |
| `LOCAL_SAFETY_ONLY` | Not requested | Not started | `cloud_invocation_status=blocked_local_safety` |
| Trigger admission failure | Not requested | Not started; service reports failure | `cloud_invocation_status=blocked_trigger_failure` |

State that non-call Trace fields remain factual null/not-requested values,
`trigger_shadow.v1.actual_qwen_result` remains null, and no path gains execution
authority. Retain the explicit note that deployment and timer enablement are
separate operations.

- [ ] **Step 2: Replace the obsolete Phase 0 architecture paragraph**

In `docs/SYSTEM_ARCHITECTURE.md`, change the Trigger module responsibility from
an observer that never suppresses Qwen to Phase 1 conditional cloud admission.
Describe that the evaluator remains deterministic, the durable Trigger record
precedes admission, and only `CALL_QWEN` reaches the existing Shadow chain.
Do not change protocol status or the formal Phase3 control chain.

- [ ] **Step 3: Run all related module regressions**

Run:

```powershell
python -m unittest \
  tests.test_cloud_trigger_shadow \
  tests.test_soil3_agent_runtime \
  tests.test_day5_fault_safety \
  tests.test_day5_vision_experience_runtime \
  tests.test_trace_v1 \
  tests.test_cloud_strategy \
  tests.test_cloud_gate \
  tests.test_cloud_gate_v2 \
  tests.test_strategy_runner_v1 \
  tests.test_phase3_bridge_v1 \
  tests.test_episode_v1 -v
```

Expected: all related tests pass.

- [ ] **Step 4: Run the full repository regression**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests pass, or environment-only unavailable dependencies are
reported separately with the focused suite still green.

- [ ] **Step 5: Inspect the final diff**

Run:

```powershell
git diff --check
git diff origin/main...HEAD --name-only
git status --short
```

Expected changed implementation scope after commits:

```text
docs/SYSTEM_ARCHITECTURE.md
docs/superpowers/plans/2026-10-20-trigger-phase1-control.md
docs/superpowers/specs/2026-10-20-trigger-phase1-control-design.md
services/soil3/agent_runtime/README.md
services/soil3/agent_runtime/runtime_v1.py
tests/test_soil3_agent_runtime.py
```

- [ ] **Step 6: Commit documentation**

```powershell
git add -- docs/SYSTEM_ARCHITECTURE.md services/soil3/agent_runtime/README.md
git commit -m "docs: describe conditional cloud admission"
```

- [ ] **Step 7: Prepare Review handoff without deployment or merge**

Report the exact focused/full test counts, diff scope, unchanged safety modules,
and the remaining operational step: Owner review and merge must precede any
openEuler deployment or pipeline timer enablement. Do not merge the PR.
