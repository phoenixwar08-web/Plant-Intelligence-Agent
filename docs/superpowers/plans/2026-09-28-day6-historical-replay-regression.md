# Day 6 Historical Replay Regression Implementation Plan

> **For agentic workers:** Implement each task in order with tests first. Do not
> widen this plan into the Shadow runtime or execution chain.

**Goal:** Batch-run existing soil3 `replay_sample.v1` artifacts through the
public Cloud Strategy, Strategy Validator, and Gate v2 boundaries and produce
reproducible JSON/Markdown evidence and an objective risky-case list.

**Architecture:** A new `historical_regression` module validates a manifest,
loads immutable replay samples, delegates Strategy and Gate decisions to their
existing public functions, and reduces returned facts into stable per-case and
aggregate results. A thin CLI owns file loading and atomic report writes.
Fixture mode is deterministic; live-provider mode is explicit and marked
non-deterministic.

**Tech stack:** Python 3 standard library, existing soil3 Replay/Cloud Strategy/
Gate modules, `unittest`, JSON and Markdown artifacts.

**Spec:** `docs/superpowers/specs/2026-09-28-day6-historical-replay-regression-design.md`

## Global constraints

- Do not change State, Replay, Strategy, Gate, Runner, Bridge, Episode, Trace,
  Phase3, MQTT, ActuatorLayer, or physical-control semantics.
- Call only public `run_chain()` and `evaluate_gate_v2()` decision interfaces.
- Always use `exploration_requested=False` and no budget ledger.
- Fixture mode is the reproducible acceptance baseline; live mode is opt-in.
- Never mutate historical inputs and never write production/runtime data.
- Do not deploy, run a real pump, merge the PR, or claim Day 6 completion.

## File map

- `services/soil3/historical_regression/__init__.py`: public batch types/functions.
- `services/soil3/historical_regression/batch_v1.py`: manifest validation,
  case orchestration, stable statistics, risky-case classification.
- `services/soil3/historical_regression/service.py`: CLI, config/prompt loading,
  atomic JSON/Markdown output.
- `services/soil3/historical_regression/README.md`: inputs, modes, outputs,
  reproducibility, and safety boundary.
- `tests/test_day6_historical_regression.py`: focused unit/integration coverage.
- `docs/SYSTEM_ARCHITECTURE.md`: record the implemented Day 6 read-only path.

---

### Task 1: Validate immutable manifest and Replay inputs

**Files:**
- Create: `services/soil3/historical_regression/__init__.py`
- Create: `services/soil3/historical_regression/batch_v1.py`
- Create: `tests/test_day6_historical_regression.py`

- [ ] **Step 1: Write failing manifest tests**

Add tests for a valid ordered manifest, relative-path resolution, duplicate or
empty case IDs, missing fixture paths in fixture mode, invalid top-level keys,
wrong `replay_sample.v1` version, missing/non-`state.v1` State, and non-soil3
State.

- [ ] **Step 2: Run the focused tests and confirm RED**

```powershell
python -m unittest tests.test_day6_historical_regression
```

Expected: import failures because the new module does not exist.

- [ ] **Step 3: Implement strict loaders and input digests**

Add immutable dataclasses for manifest cases and loaded case inputs. Resolve
relative paths from the manifest directory, validate exact supported fields,
and compute SHA-256 from source bytes. Do not rewrite any source artifact.

- [ ] **Step 4: Re-run focused tests**

Expected: manifest and Replay tests pass; orchestration tests remain absent.

- [ ] **Step 5: Commit the input boundary**

```powershell
git add services/soil3/historical_regression tests/test_day6_historical_regression.py
git commit -m "feat: validate historical regression inputs"
```

---

### Task 2: Execute fixture cases through public Strategy and Gate v2

**Files:**
- Modify: `services/soil3/historical_regression/batch_v1.py`
- Modify: `services/soil3/historical_regression/__init__.py`
- Modify: `tests/test_day6_historical_regression.py`

- [ ] **Step 1: Add failing orchestration tests**

Construct Replay samples with `StateBuilder` and captured Strategy responses.
Patch the public imported call boundaries to assert:

- fixture content reaches `run_chain()`;
- a formally accepted Strategy reaches `evaluate_gate_v2()`;
- Gate receives `False` and `None` for exploration and ledger;
- Validator rejection never reaches Gate;
- one case-local exception is recorded while later cases still run.

- [ ] **Step 2: Confirm RED**

```powershell
python -m unittest tests.test_day6_historical_regression
```

- [ ] **Step 3: Implement `run_batch()`**

Run cases in manifest order, use the existing prompt/config unchanged, summarize
the Strategy chain result, and call public Gate v2 only when
`validation.accepted` is true and the validated Strategy exists. Catch only at
the case boundary, record a stable system-error code, and continue.

- [ ] **Step 4: Verify fixture cases and side-effect arguments**

Re-run the focused suite and inspect assertions for call order, Gate arguments,
and absence of any budget-ledger path.

- [ ] **Step 5: Commit orchestration**

```powershell
git add services/soil3/historical_regression tests/test_day6_historical_regression.py
git commit -m "feat: run historical strategy gate regression"
```

---

### Task 3: Add stable statistics and risky-case evidence

**Files:**
- Modify: `services/soil3/historical_regression/batch_v1.py`
- Modify: `tests/test_day6_historical_regression.py`

- [ ] **Step 1: Add failing distribution tests**

Cover accepted/rejected Strategy counts, Validator reasons, action types and
counts, accepted pump-duration totals, all three Gate decisions, Gate reasons
and warnings, model failure codes, and system-error counts.

- [ ] **Step 2: Add failing risky-case tests**

Cover water plus Validator reject, water plus Gate deny/warning, safety/data/
freshness/binding Gate denies, and model/system errors. Assert plain safe allow
does not appear and every retained record includes stable case/source evidence.

- [ ] **Step 3: Implement stable aggregation and digest**

Aggregate only returned facts. Sort counter keys, retain manifest case order,
exclude volatile UUID/timestamp fields from the reproducible projection, and
hash that projection for `summary_sha256`.

- [ ] **Step 4: Prove repeatability**

Run the same fixture batch twice and assert equal stable statistics,
risky-case list, and summary digest even if upstream run/gate IDs differ.

- [ ] **Step 5: Commit reporting logic**

```powershell
git add services/soil3/historical_regression/batch_v1.py tests/test_day6_historical_regression.py
git commit -m "feat: summarize historical regression risks"
```

---

### Task 4: Add CLI, atomic reports, and opt-in live mode

**Files:**
- Create: `services/soil3/historical_regression/service.py`
- Modify: `services/soil3/historical_regression/batch_v1.py`
- Modify: `tests/test_day6_historical_regression.py`

- [ ] **Step 1: Add failing CLI tests**

Test default fixture mode, explicit `--mode live-provider`, JSON and Markdown
outputs, batch-level invalid input exit, and a live-mode call that passes no
fixture content. Assert the report marks live mode non-deterministic.

- [ ] **Step 2: Implement the thin CLI**

Accept manifest, Strategy config, prompt, Gate policy, JSON output, Markdown
output, and mode. Load batch-level inputs once, call `run_batch()`, render
Markdown only from the JSON report, and atomically replace explicitly named
outputs.

- [ ] **Step 3: Verify input immutability**

Capture bytes for manifest, replay samples, fixture responses, prompt, and
configs before CLI execution and assert all remain byte-identical afterward.

- [ ] **Step 4: Run focused tests**

```powershell
python -m unittest tests.test_day6_historical_regression
```

- [ ] **Step 5: Commit the service boundary**

```powershell
git add services/soil3/historical_regression tests/test_day6_historical_regression.py
git commit -m "feat: expose historical regression reports"
```

---

### Task 5: Document and verify the Issue boundary

**Files:**
- Create: `services/soil3/historical_regression/README.md`
- Modify: `docs/SYSTEM_ARCHITECTURE.md`
- Modify: `tests/test_day6_historical_regression.py`

- [ ] **Step 1: Add a boundary test**

Scan the new module and assert it does not import Runner, Bridge, Episode,
Trace, Phase3, MQTT, ActuatorLayer, or manual-water modules. Assert report Gate
execution metadata never claims actuator permission.

- [ ] **Step 2: Document exact usage and limitations**

Document manifest shape, fixture/live modes, outputs, risky-case rules,
read-only behavior, and that live-provider results are non-deterministic. Add a
small architecture entry without changing protocol status.

- [ ] **Step 3: Run focused and related regression suites**

```powershell
python -m unittest tests.test_day6_historical_regression
python -m unittest tests.test_soil3_replay_v1 tests.test_cloud_strategy tests.test_cloud_gate tests.test_cloud_gate_v2
python -m unittest discover -s tests
```

Record exact counts and any environment-limited suite honestly.

- [ ] **Step 4: Inspect final scope**

```powershell
git diff --check origin/main...HEAD
git diff --name-only origin/main...HEAD
git status --short
```

Expected diff contains only the new historical-regression module, its focused
test, the approved spec/plan, and scoped documentation.

- [ ] **Step 5: Commit documentation and test evidence**

```powershell
git add services/soil3/historical_regression/README.md docs/SYSTEM_ARCHITECTURE.md tests/test_day6_historical_regression.py
git commit -m "docs: describe Day 6 historical regression"
```

After self-review, push the Issue branch and open a PR only when explicitly
requested or when continuing the already-authorized Issue workflow. Do not
merge it and do not deploy Issue #25 independently.
