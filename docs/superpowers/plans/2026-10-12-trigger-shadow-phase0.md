# Trigger Shadow Phase 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete State dynamic parameters and humidity trends, then record a deterministic Trigger decision beside every soil3 pipeline cycle without changing whether Qwen is called.

**Architecture:** The State producer reads Phase3 parameter facts through a read-only adapter and requests bounded canonical openGauss history. A new `cloud_trigger` package builds a factual Decision Context, evaluates deterministic rules, and persists one independent shadow artifact per trace. `run_pipeline` invokes this observer before the existing unconditional Qwen call and finalizes it with references to actual cloud results.

**Tech Stack:** Python 3.9+, stdlib dataclasses/json/pathlib/datetime, existing StateBuilder, telemetry openGauss adapter, ActionReceiptStore, FeedbackStore, Strategy fingerprint/artifact refs, unittest.

**Spec:** `docs/superpowers/specs/2026-10-12-trigger-shadow-phase0-design.md`

## Global Constraints

- Do not condition, delay, or suppress `run_chain`; Qwen remains unconditional.
- Do not modify `state.v1`, `strategy.v1`, `gate.v2`, or `trace.v1`.
- Do not modify Runner, Bridge, Phase3, MQTT, ActuatorLayer, or execution permissions.
- Dynamic plant boundaries come only from existing Phase3 facts; no defaults or copied values.
- Canonical soil telemetry remains openGauss; never fall back to CSV.
- Trigger artifacts are observation-only and must report `phase3_called=false` and `physical_actions_performed=false`.
- Use test-first RED -> GREEN for every production change.

---

### Task 1: Read-only Phase3 parameter adapter

**Files:**
- Create: `services/soil3/state/phase3_parameters.py`
- Modify: `services/soil3/agent_runtime/runtime_v1.py`
- Modify: `config/soil3_agent_runtime.example.json`
- Test: `tests/test_soil3_agent_runtime.py`

**Interfaces:**
- Consumes: `parameters_path: Path`, `system_state: Any`.
- Produces: `load_state_parameters(parameters_path, system_state) -> dict[str, float | None]` with exactly `FC`, `TARGET_LOW`, `HARD_SAFETY_LOW`, and `K_P`.
- Produces: `RuntimeConfig.phase3_parameters_path: Path`.

- [ ] **Step 1: Write failing parameter mapping tests**

Add tests that create a real parameter JSON file, supply
`hard_safety_low_guard.effective_low`, call `write_state_snapshot`, and assert
the four State safety fields equal the source facts. Add separate missing and
malformed-source cases asserting `None` rather than defaults.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_soil3_agent_runtime.StateProducerTests -v
```

Expected: FAIL because `RuntimeConfig` has no parameter path and
`write_state_snapshot` does not pass parameters.

- [ ] **Step 3: Implement the minimal adapter and runtime wiring**

Implement:

```python
def load_state_parameters(parameters_path: str | Path, system_state: Any) -> dict[str, float | None]:
    ...
```

Read JSON without instantiating ConfigManager. Accept finite numeric values
only. Read the effective hard line only from the supplied system-state fact.
Add the explicit config path and pass the result to
`StateBuilder.build_from_health_snapshot`.

- [ ] **Step 4: Re-run focused tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit the independently reviewable change**

```powershell
git add services/soil3/state/phase3_parameters.py services/soil3/agent_runtime/runtime_v1.py config/soil3_agent_runtime.example.json tests/test_soil3_agent_runtime.py
git commit -m "fix: supply real phase3 parameters to state"
```

### Task 2: Bounded canonical telemetry history

**Files:**
- Modify: `services/soil3/telemetry/events.py`
- Modify: `services/soil3/agent_runtime/runtime_v1.py`
- Test: `tests/test_soil3_agent_runtime.py`
- Test: `tests/test_plant_state_v1.py`

**Interfaces:**
- Produces: `_opengauss_recent_soil_readings(device_name, *, observed_at, history_seconds, limit, runner=subprocess.run) -> list[dict[str, Any]]`.
- Extends compatibly: `build_health_snapshot(..., soil_history_seconds: float | None = None)`.

- [ ] **Step 1: Write failing history adapter tests**

Test ordered multi-row parsing, upper-bound exclusion of future rows, lower
window bound, malformed-row omission, query failure returning no readings, and
no CSV fallback. Add an integration test with points near 1h/3h/6h asserting
the unchanged StateBuilder returns the expected deltas.

- [ ] **Step 2: Run tests and verify RED**

```powershell
python -m unittest tests.test_soil3_agent_runtime.CanonicalSoilTelemetryTests tests.test_plant_state_v1.StateV1Tests -v
```

Expected: FAIL because only the latest canonical row is queried.

- [ ] **Step 3: Implement the bounded query and opt-in runtime call**

Use a parameterized-by-construction SQL string with escaped device code and UTC
cutoffs. Return valid rows ascending by timestamp. Keep latest-only behavior
when `soil_history_seconds` is `None`; have agent runtime request 23,400
seconds, derived from the six-hour horizon plus existing 30-minute tolerance.

- [ ] **Step 4: Re-run tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add services/soil3/telemetry/events.py services/soil3/agent_runtime/runtime_v1.py tests/test_soil3_agent_runtime.py tests/test_plant_state_v1.py
git commit -m "fix: provide canonical history for state trends"
```

### Task 3: Decision Context and deterministic evaluator

**Files:**
- Create: `services/soil3/cloud_trigger/__init__.py`
- Create: `services/soil3/cloud_trigger/context_v1.py`
- Create: `services/soil3/cloud_trigger/evaluator_v1.py`
- Test: `tests/test_cloud_trigger_shadow.py`

**Interfaces:**
- Produces: `build_decision_context(state, *, last_model_decision=None, receipt_dir=None, feedback_dir=None, phase3_system_state=None) -> dict[str, Any]`.
- Produces: `semantic_projection(context) -> dict[str, Any]`.
- Produces: `TriggerDecision(decision: str, reason_codes: tuple[str, ...])`.
- Produces: `evaluate_trigger(context, baseline_context, *, deny_age_seconds) -> TriggerDecision`.

- [ ] **Step 1: Write failing Decision Context tests**

Cover dynamic distances, trend direction projection, unknown preservation,
strict flag normalization, latest durable receipt selection, command-completed
non-causal status, confirmed-action causal status, and latest factual Feedback
association.

- [ ] **Step 2: Verify Decision Context tests fail**

```powershell
python -m unittest tests.test_cloud_trigger_shadow.DecisionContextTests -v
```

Expected: import failure because `cloud_trigger` does not exist.

- [ ] **Step 3: Implement the minimal factual context builder**

Use existing ActionReceiptStore/FeedbackStore readers. Do not infer action
ownership from time proximity. Keep unavailable directories and facts explicit.

- [ ] **Step 4: Write failing evaluator tests**

Cover:

- missing core facts -> `LOCAL_SAFETY_ONLY`;
- stale soil/Phase3 fact -> `LOCAL_SAFETY_ONLY`;
- hard-low crossing, dangerous flag, predictor OPEN -> `LOCAL_SAFETY_ONLY`;
- pending soak/cooldown/recent response/watering guard -> `SKIP_CLOUD`;
- no baseline -> `CALL_QWEN`;
- changed dynamic-boundary relation/trend/safety projection -> `CALL_QWEN`;
- unchanged projection -> `SKIP_CLOUD`.

- [ ] **Step 5: Verify evaluator tests fail, then implement and verify GREEN**

```powershell
python -m unittest tests.test_cloud_trigger_shadow -v
```

Expected before implementation: FAIL. Expected after minimal implementation:
PASS.

- [ ] **Step 6: Commit**

```powershell
git add services/soil3/cloud_trigger tests/test_cloud_trigger_shadow.py
git commit -m "feat: add deterministic trigger shadow evaluator"
```

### Task 4: Trigger Shadow artifact store and simulated baseline

**Files:**
- Create: `services/soil3/cloud_trigger/shadow_store.py`
- Create: `services/soil3/cloud_trigger/trigger_shadow.v1.schema.json`
- Modify: `services/soil3/cloud_trigger/__init__.py`
- Test: `tests/test_cloud_trigger_shadow.py`

**Interfaces:**
- Produces: `TriggerShadowStore(root)`.
- Produces: `create(trace_id, state_ref, decision, context, baseline_ref) -> dict`.
- Produces: `finalize(trace_id, actual_qwen_result) -> dict` with set-once semantics.
- Produces: `latest_valid_call_baseline() -> dict[str, Any] | None`.

- [ ] **Step 1: Write failing store tests**

Test atomic creation, exact schema fields, immutable identity, set-once actual
result, idempotent retry, conflicting retry refusal, corrupt record refusal,
and baseline advancement only for `CALL_QWEN` plus Validator-accepted results.

- [ ] **Step 2: Run and verify RED**

```powershell
python -m unittest tests.test_cloud_trigger_shadow.TriggerShadowStoreTests -v
```

- [ ] **Step 3: Implement minimal atomic store and schema validation**

Reuse the project's atomic JSON writer. Store artifact references and summaries,
not copied business records or raw provider output.

- [ ] **Step 4: Re-run and verify GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add services/soil3/cloud_trigger tests/test_cloud_trigger_shadow.py
git commit -m "feat: persist trigger shadow observations"
```

### Task 5: Non-blocking runtime integration

**Files:**
- Modify: `services/soil3/agent_runtime/runtime_v1.py`
- Modify: `config/soil3_agent_runtime.example.json`
- Test: `tests/test_soil3_agent_runtime.py`
- Test: `tests/test_day5_fault_safety.py`

**Interfaces:**
- Adds derived `RuntimeConfig.trigger_shadow_dir` and
  `RuntimeConfig.feedback_action_receipt_dir` properties under runtime root.
- Adds `trigger_shadow_path` to both successful and failed
  `agent_runtime.v1` run records without changing Trace.

- [ ] **Step 1: Write failing runtime integration tests**

Assert that Trigger runs after State, the artifact is finalized on accepted,
Validator-rejected, and Gate-denied paths, and `run_chain` is called exactly
once for `CALL_QWEN`, `SKIP_CLOUD`, `LOCAL_SAFETY_ONLY`, and Trigger failure.
Assert Gate/Runner/Bridge call counts match existing behavior.

- [ ] **Step 2: Run and verify RED**

```powershell
python -m unittest tests.test_soil3_agent_runtime.PipelineTests tests.test_day5_fault_safety -v
```

- [ ] **Step 3: Implement best-effort observer integration**

Create the Trigger record before `run_chain`. Finalize it before the existing
provider/Validator failure raise or after Gate is persisted. Catch only Trigger
observer failures; preserve every existing cloud/runtime exception and status.

- [ ] **Step 4: Re-run and verify GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add services/soil3/agent_runtime/runtime_v1.py config/soil3_agent_runtime.example.json tests/test_soil3_agent_runtime.py tests/test_day5_fault_safety.py
git commit -m "feat: record trigger shadow beside qwen runtime"
```

### Task 6: Documentation and regression

**Files:**
- Create: `services/soil3/cloud_trigger/README.md`
- Modify: `services/soil3/agent_runtime/README.md`
- Modify: `docs/SYSTEM_ARCHITECTURE.md`
- Test: affected test suites below.

**Interfaces:**
- Documents Trigger as observation-only and explicitly non-authoritative.

- [ ] **Step 1: Document data sources, reasons, persistence, and boundaries**

State that Qwen remains unconditional, no execution module consumes Trigger,
missing facts are never filled, and Issue #63 owns replay/tuning.

- [ ] **Step 2: Run focused tests**

```powershell
python -m unittest tests.test_cloud_trigger_shadow tests.test_plant_state_v1 tests.test_soil3_agent_runtime tests.test_day5_fault_safety -v
```

- [ ] **Step 3: Run related regression**

```powershell
python -m unittest tests.test_cloud_strategy tests.test_cloud_gate tests.test_runner_v1 tests.test_phase3_bridge_v1 tests.test_episode_v1 tests.test_trace_v1 tests.test_feedback_action_receipt_v1 tests.test_feedback_v1 -v
```

- [ ] **Step 4: Run full regression and diff checks**

```powershell
python -m unittest discover -s tests -v
git diff --check
git diff origin/main...HEAD --name-only
```

Expected: all tests pass; no changes appear under Gate, Runner, Bridge, Phase3,
MQTT, ActuatorLayer, strategy protocol, gate protocol, or Trace protocol.

- [ ] **Step 5: Commit documentation and final test adjustments**

```powershell
git add services/soil3/cloud_trigger/README.md services/soil3/agent_runtime/README.md docs/SYSTEM_ARCHITECTURE.md docs/superpowers/specs/2026-10-12-trigger-shadow-phase0-design.md docs/superpowers/plans/2026-10-12-trigger-shadow-phase0.md
git commit -m "docs: define trigger shadow phase zero boundary"
```
