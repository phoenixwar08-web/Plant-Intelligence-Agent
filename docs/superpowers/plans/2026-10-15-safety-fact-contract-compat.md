# Safety Fact Contract Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align State, Trigger, and Gate with the approved Phase3 `pending_soak` and reserved `cloud_protection` compatibility semantics while preserving fail-closed safety behavior.

**Architecture:** Keep `state.v1` output unchanged and publish the required/optional partition beside its existing allowlist. Trigger projects optional absence into a non-dangerous status only when the surrounding freshness contract permits it; Gate validates present optional values and continues denying every missing required fact. Gate v2 inherits the same admission behavior through Gate v1.

**Tech Stack:** Python 3.9+, `unittest`, existing soil3 State/Trigger/Gate modules.

**Spec:** `docs/superpowers/specs/2026-10-15-safety-fact-contract-compat-design.md`

## Global Constraints

- Do not change frozen `state.v1` output or synthesize missing safety values.
- Do not modify Phase3, Qwen scheduling, Runner, Bridge, MQTT, ActuatorLayer, or physical execution permissions.
- Missing/null `pending_soak` is inactive only with usable Phase3 freshness; a valid persisted object remains active until Phase3 removes it.
- Missing `cloud_protection` is allowed; present active or malformed values remain blocking.
- Every safety fact outside these two optional names remains required and fail-closed.
- No deployment or live Qwen/Phase3/MQTT action is part of Issue #67.

---

### Task 1: Publish and preserve the shared safety-fact partition

**Files:**
- Modify: `services/soil3/state/state_v1.py`
- Test: `tests/test_plant_state_v1.py`

**Interfaces:**
- Produces: `PHASE3_OPTIONAL_SAFETY_FLAG_KEYS` and `PHASE3_REQUIRED_SAFETY_FLAG_KEYS`, both immutable sets derived from `PHASE3_SAFETY_FLAG_KEYS`.
- Preserves: `StateBuilder.build()` copies only supplied Phase3 facts and never synthesizes optional values.

- [ ] **Step 1: Write the failing State contract test**

Add a test that asserts the required and optional sets form an exact, disjoint partition of the existing allowlist and that absent `pending_soak` / `cloud_protection` remain absent from built State.

- [ ] **Step 2: Run the State test and verify RED**

Run: `python -m unittest tests.test_plant_state_v1 -v`

Expected: FAIL because the required/optional partition is not exported yet.

- [ ] **Step 3: Add the minimal shared classification**

Define:

```python
PHASE3_OPTIONAL_SAFETY_FLAG_KEYS = frozenset({"pending_soak", "cloud_protection"})
PHASE3_REQUIRED_SAFETY_FLAG_KEYS = (
    frozenset(PHASE3_SAFETY_FLAG_KEYS) - PHASE3_OPTIONAL_SAFETY_FLAG_KEYS
)
```

Do not change the State mapping comprehension.

- [ ] **Step 4: Run the State tests and verify GREEN**

Run: `python -m unittest tests.test_plant_state_v1 -v`

Expected: all tests pass.

### Task 2: Apply optional-fact semantics to Trigger

**Files:**
- Modify: `services/soil3/cloud_trigger/context_v1.py`
- Modify: `services/soil3/cloud_trigger/evaluator_v1.py`
- Test: `tests/test_cloud_trigger_shadow.py`

**Interfaces:**
- Consumes: shared required/optional sets from `state_v1.py`.
- Produces: Decision Context statuses `active`, `inactive`, `missing`, or `invalid` using the approved compatibility rules.

- [ ] **Step 1: Write failing Trigger table tests**

Cover literal cases for missing/null `pending_soak` with valid versus unavailable/stale Phase3 freshness; valid persisted objects before, at, and after their nominal deadline; malformed objects/scalars; and missing/inactive/active/malformed `cloud_protection`. Assert final decisions, not private implementation details.

- [ ] **Step 2: Run focused Trigger tests and verify RED**

Run: `python -m unittest tests.test_cloud_trigger_shadow -v`

Expected failures: missing optional facts currently become `LOCAL_SAFETY_ONLY`, and expired persisted pending soak currently becomes inactive.

- [ ] **Step 3: Implement the minimal Trigger projection**

- Treat absent `cloud_protection` as inactive.
- Treat absent/null `pending_soak` as inactive only when `phase3_state_age_sec` is finite and non-negative; otherwise retain missing/unavailable behavior.
- Validate persisted pending soak as a dictionary with finite `pump_end_time_wall` and finite non-negative `soak_duration`, then keep it active regardless of nominal deadline.
- Leave required facts, dangerous facts, freshness checks, and protection priority unchanged.

- [ ] **Step 4: Run focused Trigger tests and verify GREEN**

Run: `python -m unittest tests.test_cloud_trigger_shadow -v`

Expected: all tests pass.

### Task 3: Apply the same admission contract to Gate v1/v2

**Files:**
- Modify: `services/soil3/cloud_gate/gate_v1.py`
- Test: `tests/test_cloud_gate.py`
- Test: `tests/test_cloud_gate_v2.py`

**Interfaces:**
- Consumes: `PHASE3_REQUIRED_SAFETY_FLAG_KEYS` and `PHASE3_OPTIONAL_SAFETY_FLAG_KEYS`.
- Preserves: `evaluate_gate()` and `evaluate_gate_v2()` signatures and all existing reason-code formats.

- [ ] **Step 1: Write failing Gate v1/v2 parity tests**

For both evaluators, assert allow/deny behavior for missing/null/valid/malformed `pending_soak`, missing/inactive/active/malformed `cloud_protection`, stale Phase3 state, and each other required fact missing. A valid persisted pending soak must deny even after its nominal deadline.

- [ ] **Step 2: Run focused Gate tests and verify RED**

Run: `python -m unittest tests.test_cloud_gate tests.test_cloud_gate_v2 -v`

Expected failures: Gate currently requires both optional facts and cannot validate the persisted pending-soak object.

- [ ] **Step 3: Implement minimal Gate validation**

Iterate the shared required set for missing checks. Validate optional facts only when present. Interpret `pending_soak=None` as inactive; validate a present object using the approved finite-field contract and treat it as active; reject scalar or malformed forms. Continue to reject active/malformed `cloud_protection` and every stale or missing required safety fact.

- [ ] **Step 4: Run focused Gate tests and verify GREEN**

Run: `python -m unittest tests.test_cloud_gate tests.test_cloud_gate_v2 -v`

Expected: all tests pass with v1/v2 parity.

### Task 4: Run related regressions and prepare Review

**Files:**
- Modify only test fixtures that incorrectly model optional Phase3 facts as required scalar booleans.
- Do not modify production modules outside State/Trigger/Gate.

**Interfaces:**
- Produces: a review-ready Issue branch with no execution-chain changes.

- [ ] **Step 1: Run focused and related regression suites**

Run State, Trigger, Gate, Runtime, Day5 Fault/Safety, Strategy, Runner, Bridge, and Experience Retrieval tests. Then run full discovery when the environment permits.

- [ ] **Step 2: Inspect scope and formatting**

Run `git diff --check`, inspect `git diff --stat`, and verify Phase3/MQTT/ActuatorLayer files are absent.

- [ ] **Step 3: Commit implementation and tests**

Stage only the approved design, plan, State/Trigger/Gate files, and their tests. Commit with a scoped Issue #67 message.

- [ ] **Step 4: Push and open the PR**

Push `issue-67-safety-fact-contract`, open one PR to `main`, include actual test counts and explicit unchanged boundaries, and leave merging to the Owner.
