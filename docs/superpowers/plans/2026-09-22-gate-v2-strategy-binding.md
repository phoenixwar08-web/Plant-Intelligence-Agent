# Gate V2 Strategy Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use task-by-task execution with test-first changes. Steps use checkbox syntax for tracking.

**Goal:** Add independently reviewable gate.v2 records that bind every formally valid Strategy to its canonical content hash without changing gate.v1.

**Architecture:** evaluate_gate_v2 delegates admission, budget handling, and the no-actuator boundary to the existing v1 evaluator. It separately runs the formal StrategyValidator against the same normalized inputs, then adds strategy_sha256 only to the new v2 record. Phase3 Bridge is outside this plan.

**Tech Stack:** Python 3, unittest, JSON Schema, existing canonical JSON and SHA-256 fingerprint.

**Spec:** docs/superpowers/specs/2026-09-22-gate-v2-strategy-binding-design.md

## Global Constraints

- gate.v1 must keep its existing schema version, fields, and decision semantics.
- A Validator-accepted Strategy has a canonical hash for allow, warning-allow, and deny; a Validator-rejected Strategy has null.
- Reuse only services.soil3.cloud_strategy.validator.fingerprint.
- Do not change Phase3, MQTT, ActuatorLayer, Runner, state.v1, or services/soil3/phase3_bridge.
- Remain local, dry-run, and non-executing.

---

### Task 1: Define the Gate V2 contract with failing tests

**Files:**
- Create: tests/test_cloud_gate_v2.py
- Read: tests/test_cloud_gate.py and services/soil3/cloud_gate/gate_v1.py

**Interfaces:**
- Consumes: GatePolicy and evaluate_gate_v2(state, strategy, policy, exploration_requested, ledger=None).
- Produces: executable tests for the V2 binding contract.

- [ ] **Step 1: Write a failing valid-Strategy binding test**

~~~
def test_valid_strategy_hash_is_retained_for_each_decision(self):
    cases = [
        (fresh_state(), "allow"),
        (fresh_state(soil_age_sec=900), "allow_with_warning"),
        (fresh_state(flags={"sensor_fault": {"active": True}}), "deny"),
    ]
    for state, expected_decision in cases:
        strategy = valid_strategy(state)
        record = evaluate_gate_v2(state, strategy, policy(), False)
        self.assertEqual("gate.v2", record["schema_version"])
        self.assertEqual(expected_decision, record["decision"])
        self.assertEqual(fingerprint(strategy), record["strategy_sha256"])
~~~

- [ ] **Step 2: Run it and observe red**

Run: python -m unittest tests.test_cloud_gate_v2.CloudGateV2Tests.test_valid_strategy_hash_is_retained_for_each_decision

Expected: FAIL because evaluate_gate_v2 does not exist.

- [ ] **Step 3: Add invalid-Strategy, V1 compatibility, and schema tests**

~~~
def test_invalid_strategy_has_null_content_binding(self):
    state = fresh_state()
    invalid = valid_strategy(state, [{"action_id": "water", "type": "water", "pump_seconds": 121}])
    record = evaluate_gate_v2(state, invalid, policy(), False)
    self.assertEqual("deny", record["decision"])
    self.assertIsNone(record["strategy_sha256"])

def test_gate_v1_shape_remains_unchanged(self):
    state = fresh_state()
    record = evaluate_gate(state, valid_strategy(state), policy(), False)
    self.assertEqual("gate.v1", record["schema_version"])
    self.assertNotIn("strategy_sha256", record)
~~~

Load the new schema with json.loads and assert strategy_sha256 is required, has type ["string", "null"], and pattern ^[0-9a-f]{64}$.

- [ ] **Step 4: Run the full new module and observe red**

Run: python -m unittest tests.test_cloud_gate_v2

Expected: FAIL because the V2 module and schema do not exist.

### Task 2: Implement the V2 evaluator and closed schema

**Files:**
- Create: services/soil3/cloud_gate/gate_v2.py
- Create: services/soil3/cloud_gate/gate.v2.schema.json
- Modify: services/soil3/cloud_gate/__init__.py
- Test: tests/test_cloud_gate_v2.py

**Interfaces:**
- Consumes: evaluate_gate, GatePolicy, StrategyValidator, fingerprint.
- Produces: evaluate_gate_v2(state, strategy, policy, exploration_requested, ledger=None) -> dict.

- [ ] **Step 1: Implement the minimal evaluator**

~~~
from typing import Any

from services.soil3.cloud_strategy.validator import StrategyValidator, fingerprint
from .gate_v1 import GatePolicy, evaluate_gate

def evaluate_gate_v2(state, strategy, policy: GatePolicy, exploration_requested, ledger: Any = None):
    normalized_state = state if isinstance(state, dict) else {}
    normalized_strategy = strategy if isinstance(strategy, dict) else {}
    validation = StrategyValidator().validate(normalized_strategy, normalized_state)
    record = evaluate_gate(
        normalized_state, normalized_strategy, policy, exploration_requested, ledger
    )
    record["schema_version"] = "gate.v2"
    record["strategy_sha256"] = (
        fingerprint(normalized_strategy) if validation.accepted else None
    )
    return record
~~~

Do not edit evaluate_gate; V1 must not gain a field or semantic change.

- [ ] **Step 2: Add gate.v2.schema.json**

Copy the V1 object schema. Set schema_version to gate.v2, add strategy_sha256 to required, and define it exactly as:

~~~
{"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
~~~

Keep additionalProperties false and add no action, actuator, or execution field.

- [ ] **Step 3: Export only the new evaluator alongside GatePolicy**

~~~
from .gate_v1 import GatePolicy
from .gate_v2 import evaluate_gate_v2

__all__ = ["GatePolicy", "evaluate_gate_v2"]
~~~

- [ ] **Step 4: Run the new contract tests and observe green**

Run: python -m unittest tests.test_cloud_gate_v2

Expected: PASS.

- [ ] **Step 5: Commit the tested feature**

~~~
git add services/soil3/cloud_gate/gate_v2.py services/soil3/cloud_gate/gate.v2.schema.json services/soil3/cloud_gate/__init__.py tests/test_cloud_gate_v2.py
git commit -m "feat: add gate v2 strategy binding"
~~~

### Task 3: Document compatibility and verify boundaries

**Files:**
- Modify: docs/PROTOCOLS_AND_BOUNDARIES.md
- Test: tests/test_cloud_gate.py and tests/test_cloud_gate_v2.py

**Interfaces:**
- Consumes: completed gate.v2 evaluator and schema.
- Produces: an explicit V1/V2 boundary for the later Bridge PR.

- [ ] **Step 1: Add a separate gate.v2 protocol row**

Keep the gate.v1 row unchanged. State that V2 records the canonical complete Strategy fingerprint after formal validation, uses null only when validation fails, stays admission-only, and that a later Bridge PR rejects V1 rather than inferring content from strategy_id.

- [ ] **Step 2: Run Gate suites**

Run: python -m unittest tests.test_cloud_gate tests.test_cloud_gate_v2

Expected: PASS; this protects V1 safety and budget semantics alongside V2 binding.

- [ ] **Step 3: Run canonical-hash consumer regressions**

Run: python -m unittest tests.test_cloud_strategy tests.test_strategy_runner_v1

Expected: PASS; this confirms Strategy fingerprint and dry-run Runner records remain stable.

- [ ] **Step 4: Run full discovery when the environment permits**

Run: python -m unittest discover -s tests

Expected: all available tests pass. Record missing optional dependencies separately; do not edit unrelated modules to compensate.

- [ ] **Step 5: Inspect and commit**

~~~
git diff --check
git diff -- services/soil3/phase3 services/soil3/phase3_bridge
git add docs/PROTOCOLS_AND_BOUNDARIES.md
git commit -m "docs: define gate v2 compatibility boundary"
~~~

The Phase3 and Bridge diff commands must have no output.

- [ ] **Step 6: Push and open only the independent Gate PR**

~~~
git push -u origin codex/gate-v2-strategy-binding
gh pr create --base main --head codex/gate-v2-strategy-binding --title "feat: add gate v2 strategy binding"
~~~

Report changed files, non-changes, focused and full results, and that #47 remains untouched pending Owner merge. Do not merge the PR.

