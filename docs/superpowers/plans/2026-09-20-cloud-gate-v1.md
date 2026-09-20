# Cloud Gate V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a non-executing soil3 `gate.v1` that validates state and strategy facts, emits an explainable three-state admission decision, and safely records an explicit exploration-budget reservation.

**Architecture:** `gate_v1.py` owns pure policy and record construction, while `budget.py` owns the explicit JSON ledger and idempotent atomic reservation. `service.py` is a narrow CLI boundary that requires caller-provided paths and writes one `gate.v1` artifact; it never imports Phase3, MQTT, or an actuator module.

**Tech Stack:** Python standard library, existing `services.soil3.cloud_strategy.StrategyValidator`, JSON Schema, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-20-cloud-gate-v1-design.md`

## Global Constraints

- Accept only `state.v1` for `soil3` and `strategy.v1` that the existing validator accepts against that exact state.
- Return only `allow`, `allow_with_warning`, or `deny`; a deny never changes the ledger.
- Use explicit caller-provided paths. No production default path, provider call, Phase3 import, MQTT publish, `manual_water`, or actuator command.
- The example exploration budget is zero; a non-zero budget must be explicit local configuration.
- Use 900 seconds for freshness warnings and 18,000 seconds for freshness denial.
- Every production behavior starts with a focused failing test and is implemented minimally until it passes.

## Task 1: Define Gate data types and policy validation

**Files:**

- Create: `services/soil3/cloud_gate/__init__.py`
- Create: `services/soil3/cloud_gate/gate_v1.py`
- Create: `tests/test_cloud_gate.py`

**Interfaces:**

- Consumes: `StrategyValidator`, `fingerprint`, and `normalize_timestamp` from `services.soil3.cloud_strategy.validator`.
- Produces: `GatePolicy.from_dict(value: dict[str, object]) -> GatePolicy` and `evaluate_gate(state: dict[str, object], strategy: dict[str, object], policy: GatePolicy, exploration_requested: bool, ledger: BudgetLedger | None = None) -> dict[str, object]`.

- [ ] **Step 1: Write the failing test**

```python
def test_policy_requires_ordered_finite_freshness_bounds(self):
    with self.assertRaisesRegex(ValueError, "warning_age_seconds"):
        GatePolicy.from_dict({"warning_age_seconds": 18001, "deny_age_seconds": 18000,
                              "window_seconds": 86400, "max_exploration_water_seconds": 0})

    with self.assertRaisesRegex(ValueError, "max_exploration_water_seconds"):
        GatePolicy.from_dict({"warning_age_seconds": 900, "deny_age_seconds": 18000,
                              "window_seconds": 86400, "max_exploration_water_seconds": -1})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_cloud_gate.CloudGatePolicyTests.test_policy_requires_ordered_finite_freshness_bounds -v`

Expected: FAIL because `services.soil3.cloud_gate` does not yet exist.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(frozen=True)
class GatePolicy:
    warning_age_seconds: float
    deny_age_seconds: float
    window_seconds: float
    max_exploration_water_seconds: float

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "GatePolicy":
        # Require exactly the four public policy fields, convert finite numbers,
        # and reject negative values or warning > deny.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_cloud_gate.CloudGatePolicyTests.test_policy_requires_ordered_finite_freshness_bounds -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add services/soil3/cloud_gate/__init__.py services/soil3/cloud_gate/gate_v1.py tests/test_cloud_gate.py`

Run: `git commit -m "feat: define cloud gate policy"`

## Task 2: Implement fail-closed three-state admission

**Files:**

- Modify: `services/soil3/cloud_gate/gate_v1.py`
- Modify: `tests/test_cloud_gate.py`

**Interfaces:**

- Consumes: `GatePolicy` from Task 1 and an existing valid `strategy.v1` fixture bound to a `state.v1` fixture.
- Produces: `evaluate_gate(...)` records containing `schema_version`, binding fields, decision, reason/warning codes, zero-reservation budget details, and fixed non-executing metadata.

- [ ] **Step 1: Write failing tests**

```python
def test_gate_allows_fresh_safe_valid_strategy(self):
    record = evaluate_gate(self.fresh_state(), self.valid_strategy(), self.policy(), False)
    self.assertEqual("allow", record["decision"])
    self.assertEqual([], record["reason_codes"])
    self.assertEqual([], record["warning_codes"])
    self.assertFalse(record["execution"]["actuator_commands_allowed"])

def test_gate_warns_for_freshness_at_warning_boundary(self):
    state = self.fresh_state(soil_age_sec=900)
    record = evaluate_gate(state, self.valid_strategy(state), self.policy(), False)
    self.assertEqual("allow_with_warning", record["decision"])
    self.assertIn("soil_data_age_warning", record["warning_codes"])

def test_gate_denies_active_phase3_protection_without_budget_change(self):
    state = self.fresh_state(flags={"sensor_fault": {"active": True}})
    record = evaluate_gate(state, self.valid_strategy(state), self.policy(), True, self.ledger())
    self.assertEqual("deny", record["decision"])
    self.assertIn("safety_flag_active:sensor_fault", record["reason_codes"])
    self.assertEqual(0, record["budget"]["reserved_water_seconds"])
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest tests.test_cloud_gate.CloudGateAdmissionTests -v`

Expected: FAIL because `evaluate_gate` is not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def evaluate_gate(state, strategy, policy, exploration_requested, ledger=None):
    validation = StrategyValidator().validate(strategy, state)
    # Collect invalid state/binding facts, active Phase3 protections, pump state,
    # soil and Phase3 age facts, and predictor-circuit facts into reason codes.
    # Only after no reason is present, collect warning-band age/HALF_OPEN codes.
    # Construct the complete non-executing gate.v1 record deterministically.
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `python -m unittest tests.test_cloud_gate.CloudGateAdmissionTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add services/soil3/cloud_gate/gate_v1.py tests/test_cloud_gate.py`

Run: `git commit -m "feat: add cloud gate admission rules"`

## Task 3: Add durable idempotent exploration budget reservations

**Files:**

- Create: `services/soil3/cloud_gate/budget.py`
- Modify: `services/soil3/cloud_gate/gate_v1.py`
- Modify: `tests/test_cloud_gate.py`

**Interfaces:**

- Consumes: allowed Gate admission from Task 2 and an explicit `Path`.
- Produces: `BudgetLedger(path: Path).reserve(device_code: str, reservation_id: str, requested_water_seconds: float, policy: GatePolicy, decided_at: str) -> BudgetReservation`.

- [ ] **Step 1: Write failing tests**

```python
def test_budget_reservation_is_idempotent_after_ledger_reopen(self):
    with tempfile.TemporaryDirectory() as directory:
        ledger_path = Path(directory) / "budget.json"
        first = BudgetLedger(ledger_path).reserve("soil3", "same", 6, self.policy(limit=10), self.now)
        second = BudgetLedger(ledger_path).reserve("soil3", "same", 6, self.policy(limit=10), self.now)
    self.assertEqual(6, first.reserved_water_seconds)
    self.assertEqual(first, second)

def test_budget_exhaustion_denies_without_creating_a_second_charge(self):
    with tempfile.TemporaryDirectory() as directory:
        ledger = BudgetLedger(Path(directory) / "budget.json")
        policy = self.policy(limit=10)
        first = ledger.reserve("soil3", "first", 6, policy, self.now)
        exhausted = ledger.reserve("soil3", "second", 6, policy, self.now)
    self.assertTrue(first.available)
    self.assertFalse(exhausted.available)
    self.assertEqual(0, exhausted.reserved_water_seconds)
    self.assertEqual(4, exhausted.remaining_water_seconds)
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest tests.test_cloud_gate.CloudGateBudgetTests -v`

Expected: FAIL because `BudgetLedger` is not defined.

- [ ] **Step 3: Write minimal implementation**

```python
class BudgetLedger:
    def reserve(self, device_code, reservation_id, requested_water_seconds, policy, decided_at):
        # Acquire a sidecar O_EXCL lock, load valid JSON or fail closed, discard
        # expired-window entries, return an equal existing reservation, otherwise
        # append one fitting reservation with fsync + os.replace, then release lock.
```

- [ ] **Step 4: Connect ledger after admission and verify GREEN**

Run: `python -m unittest tests.test_cloud_gate.CloudGateBudgetTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add services/soil3/cloud_gate/budget.py services/soil3/cloud_gate/gate_v1.py tests/test_cloud_gate.py`

Run: `git commit -m "feat: persist cloud gate exploration budget"`

## Task 4: Add schema, explicit CLI, example policy, and protocol status

**Files:**

- Create: `services/soil3/cloud_gate/gate.v1.schema.json`
- Create: `services/soil3/cloud_gate/service.py`
- Create: `config/cloud_gate.example.json`
- Modify: `services/soil3/cloud_gate/__init__.py`
- Modify: `docs/PROTOCOLS_AND_BOUNDARIES.md`
- Modify: `tests/test_cloud_gate.py`

**Interfaces:**

- Consumes: `evaluate_gate`, `GatePolicy`, and `BudgetLedger` from Tasks 1–3.
- Produces: `python -m services.soil3.cloud_gate.service --state STATE --strategy STRATEGY --config CONFIG --budget-ledger LEDGER --output OUTPUT [--exploration-requested]`.

- [ ] **Step 1: Write failing CLI and schema tests**

```python
def test_cli_requires_every_io_path(self):
    with self.assertRaises(SystemExit):
        service.main([])

def test_cli_writes_one_atomic_nonexecuting_gate_record(self):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state_path, strategy_path = root / "state.json", root / "strategy.json"
        config_path, ledger_path, output_path = root / "gate.json", root / "budget.json", root / "output.json"
        state = self.fresh_state()
        state_path.write_text(json.dumps(state), encoding="utf-8")
        strategy_path.write_text(json.dumps(self.valid_strategy(state)), encoding="utf-8")
        config_path.write_text(json.dumps(self.policy_dict(limit=10)), encoding="utf-8")
        service.main(["--state", str(state_path), "--strategy", str(strategy_path),
                      "--config", str(config_path), "--budget-ledger", str(ledger_path),
                      "--output", str(output_path), "--exploration-requested"])
        record = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual("gate.v1", record["schema_version"])
        self.assertFalse(record["execution"]["actuator_commands_allowed"])
        self.assertFalse(output_path.with_name(output_path.name + ".tmp").exists())
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest tests.test_cloud_gate.CloudGateCliTests -v`

Expected: FAIL because `service` and the schema do not exist.

- [ ] **Step 3: Write minimal implementation**

```python
def main(argv=None):
    parser = argparse.ArgumentParser(description="soil3 non-executing Cloud Gate V1")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--strategy", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--budget-ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--exploration-requested", action="store_true")
```

Use a temporary sibling output plus `os.replace`; the example policy must carry
`max_exploration_water_seconds: 0`. Mark `gate.v1` implemented with an explicit
non-executing boundary in the protocol document.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `python -m unittest tests.test_cloud_gate.CloudGateCliTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add services/soil3/cloud_gate config/cloud_gate.example.json docs/PROTOCOLS_AND_BOUNDARIES.md tests/test_cloud_gate.py`

Run: `git commit -m "feat: expose cloud gate v1 cli"`

## Task 5: Run regression checks and review the Issue boundary

**Files:**

- Modify: only files identified by an observed test failure.

**Interfaces:**

- Consumes: all completed Gate files.
- Produces: evidence for the Review handoff.

- [ ] **Step 1: Run the focused Gate suite**

Run: `python -m unittest tests.test_cloud_gate -v`

Expected: PASS with all Gate policy, admission, budget, and CLI tests.

- [ ] **Step 2: Run the repository test suite**

Run: `python -m pytest -q`

Expected: PASS; record the actual count and do not substitute an earlier count.

- [ ] **Step 3: Check formatting and forbidden boundaries**

Run: `git diff --check origin/main...HEAD`

Run: `rg -n 'mqtt|manual_water|ActuatorLayer|phase3' services/soil3/cloud_gate tests/test_cloud_gate.py`

Expected: no implementation import/call into MQTT, `manual_water`, actuator, or Phase3; documentation text may state prohibited boundaries.

- [ ] **Step 4: Inspect complete diff**

Run: `git diff --stat origin/main...HEAD`

Run: `git diff origin/main...HEAD`

Expected: only #15 Gate module, example configuration, protocol status, tests, and this Issue's design/plan documents change.

- [ ] **Step 5: Prepare Review handoff**

State the changed modules, untouched Phase3/MQTT/production paths, actual test results, and remaining limitations. Do not merge or close Issue #15.
