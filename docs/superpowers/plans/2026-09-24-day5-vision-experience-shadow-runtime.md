# Day 5 Vision and Experience Shadow Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Vision V1 and Experience Retrieval V1 optional, persisted, trace-linked inputs that Cloud Strategy actually receives before the existing Validator, Gate v2, dry-run Runner, verification-only Bridge, and Episode flow.

**Architecture:** Vision owns a new minimal `vision_run.v1` manifest and returns its public artifact reference; Experience continues through `ExperienceRetriever` and is persisted by the runtime. The runtime converts disabled, failed, and successful calls into explicit context objects, writes Trace associations once, and gives the same contexts to Cloud Strategy's audited model input without changing `strategy.v1` or any execution component.

**Tech Stack:** Python 3 standard library, dataclasses, existing Vision/Experience/Trace stores, `unittest`, JSON artifacts.

**Spec:** `docs/superpowers/specs/2026-09-24-day5-vision-experience-shadow-runtime-design.md`

## Global Constraints

- Do not modify `state.v1`, `vision.v1`, `experience_retrieval.v1`, `trace.v1`, `strategy.v1`, or `gate.v2` semantics.
- Do not modify Runner, Bridge, Phase3, MQTT, ActuatorLayer, `manual_water`, or physical execution paths.
- Disabled modules remain `not_requested`; failures become `unavailable` with null facts and null Trace refs; no fact may be invented.
- Vision paths come only from a public Vision artifact reference; the runtime must not reconstruct Vision's private directory layout.
- Successful Experience retrieval is `available` even when both result classes are empty.
- The checked-in runtime example remains offline, nonsecret, and disables both optional modules.
- No deployment, camera/provider smoke test, production data write, or PR merge is part of this plan.

## File map

- `services/soil3/vision/vision_v1.py`: public immutable Vision artifact-reference fields.
- `services/soil3/vision/vision_service.py`: atomic zone refs and `vision_run.v1` manifest persistence.
- `services/soil3/vision/__init__.py`: export the public artifact type and configuration error.
- `services/soil3/cloud_strategy/service.py`: optional-context validation/projection and audited model input.
- `services/soil3/cloud_strategy/prompts/strategy_v1.txt`: explain Vision/Experience facts and missing-state rules without changing Strategy output.
- `services/soil3/agent_runtime/runtime_v1.py`: strict switches, optional-module orchestration, persistence, Trace refs, and Strategy handoff.
- `config/soil3_agent_runtime.example.json`: disabled-by-default switches.
- `tests/test_vision_v1.py`: public manifest and compatibility tests.
- `tests/test_cloud_strategy.py`: model-input projection and provider-input tests.
- `tests/test_day5_vision_experience_runtime.py`: complete runtime available/unavailable/not-requested integration tests.
- `services/soil3/vision/README.md`, `services/soil3/agent_runtime/README.md`, `docs/SYSTEM_ARCHITECTURE.md`, `docs/PROTOCOLS_AND_BOUNDARIES.md`: implemented behavior and boundaries.

---

### Task 1: Add the public Vision run manifest

**Files:**
- Modify: `services/soil3/vision/vision_v1.py`
- Modify: `services/soil3/vision/vision_service.py`
- Modify: `services/soil3/vision/__init__.py`
- Test: `tests/test_vision_v1.py`

**Interfaces:**
- Consumes: validated `vision.v1` records already produced by `VisionService._observe_zone()`.
- Produces: `VisionArtifactRef(schema_version, record_id, path, sha256)`, `CaptureOutcome.artifact_ref`, and `VisionRunResult.manifest_ref`.

- [ ] **Step 1: Add failing public-result compatibility and manifest tests**

Add tests equivalent to:

```python
def test_run_result_defaults_to_no_manifest_for_existing_callers(self):
    result = VisionRunResult("capture_failed", None, ())
    self.assertIsNone(result.manifest_ref)

def test_successful_run_returns_hash_verified_public_manifest(self):
    result = build_test_vision_service(data_root).capture_and_analyze_once()
    self.assertIsNotNone(result.manifest_ref)
    manifest_path = Path(result.manifest_ref.path)
    self.assertEqual(
        result.manifest_ref.sha256,
        hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    self.assertEqual("vision_run.v1", manifest["schema_version"])
    self.assertEqual("soil3", manifest["device_code"])
    self.assertTrue(manifest["outcomes"])
    self.assertTrue(all("status" in item for item in manifest["outcomes"]))

def test_all_failed_run_has_no_manifest_ref(self):
    result = build_failing_vision_service(data_root).capture_and_analyze_once()
    self.assertIsNone(result.manifest_ref)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
python -m unittest tests.test_vision_v1
```

Expected: failures because `VisionArtifactRef`, `artifact_ref`, and `manifest_ref` do not exist.

- [ ] **Step 3: Add the immutable public artifact type without breaking constructors**

Add to `vision_v1.py`:

```python
@dataclass(frozen=True)
class VisionArtifactRef:
    schema_version: str
    record_id: str
    path: str
    sha256: str

@dataclass(frozen=True)
class CaptureOutcome:
    # existing fields stay in their current order
    artifact_ref: VisionArtifactRef | None = None

@dataclass(frozen=True)
class VisionRunResult:
    # existing fields stay in their current order
    manifest_ref: VisionArtifactRef | None = None
```

Append only defaulted fields so existing positional constructors remain valid.

- [ ] **Step 4: Return zone-record refs and persist one manifest atomically**

In `vision_service.py`:

- make `_persist_record()` return a `VisionArtifactRef` built from the exact file written;
- attach it to each successful `CaptureOutcome`;
- after all zones finish, persist a manifest only when at least one outcome has an artifact ref;
- write the manifest beneath `<vision_data_root>/runs/<UTC-date>/<run_id>.json` using the existing atomic writer;
- hash the bytes after persistence and return its `VisionArtifactRef` in `VisionRunResult`;
- never infer a manifest for capture failure or all-zone analysis failure.

The manifest writer must emit exactly:

```python
{
    "schema_version": "vision_run.v1",
    "run_id": run_id,
    "device_code": self._device_code,
    "created_at": _utc_timestamp(now),
    "status": status,
    "frame_id": frame_id,
    "outcomes": [
        {
            "zone_id": outcome.zone_id,
            "status": outcome.status,
            "artifact_ref": asdict(outcome.artifact_ref) if outcome.artifact_ref else None,
            "error_code": outcome.error_code,
            "http_status": outcome.http_status,
            "provider_error_code": outcome.provider_error_code,
        }
        for outcome in outcomes
    ],
}
```

- [ ] **Step 5: Export and verify the public API**

Export `VisionArtifactRef` and `VisionConfigurationError` from
`services.soil3.vision`. Run:

```bash
python -m unittest tests.test_vision_v1 tests.test_qwen_vision
```

Expected: all tests pass and existing callers still construct three-field
`VisionRunResult` values.

- [ ] **Step 6: Commit the Vision boundary**

```bash
git add services/soil3/vision/vision_v1.py services/soil3/vision/vision_service.py services/soil3/vision/__init__.py tests/test_vision_v1.py
git commit -m "feat: expose vision run manifest"
```

---

### Task 2: Put optional facts into the Cloud Strategy input

**Files:**
- Modify: `services/soil3/cloud_strategy/service.py`
- Modify: `services/soil3/cloud_strategy/prompts/strategy_v1.txt`
- Test: `tests/test_cloud_strategy.py`

**Interfaces:**
- Consumes: context objects shaped as `{"availability": str, "facts": object | null}`.
- Produces: `run_chain(*, state, config, prompt, fixture_content=None, session=None, vision_context=None, experience_context=None)` with audited `model_input.vision` and `model_input.experience`.

- [ ] **Step 1: Add failing model-input tests for all availability states**

Add tests equivalent to:

```python
def test_omitted_optional_contexts_are_not_requested(self):
    result = run_chain(state=state, config=config, prompt="prompt", fixture_content=fixture)
    self.assertEqual({"availability": "not_requested", "facts": None}, result["model_input"]["vision"])
    self.assertEqual({"availability": "not_requested", "facts": None}, result["model_input"]["experience"])

def test_available_contexts_reach_the_exact_provider_input(self):
    session = FakeSession(success_response)
    run_chain(
        state=state,
        config=config,
        prompt="prompt",
        session=session,
        vision_context={"availability": "available", "facts": [validated_vision]},
        experience_context={"availability": "available", "facts": retrieval_result},
    )
    sent = json.loads(session.calls[0][1]["json"]["messages"][1]["content"])
    self.assertEqual(expected_projected_vision, sent["vision"]["facts"])
    self.assertEqual(retrieval_result, sent["experience"]["facts"])

def test_unavailable_contexts_send_no_facts(self):
    result = run_chain(
        state=state,
        config=config,
        prompt="prompt",
        fixture_content=fixture,
        vision_context={"availability": "unavailable", "facts": None},
        experience_context={"availability": "unavailable", "facts": None},
    )
    self.assertIsNone(result["model_input"]["vision"]["facts"])
    self.assertIsNone(result["model_input"]["experience"]["facts"])
```

Also assert projected Vision facts exclude `image_path`, `image_sha256`,
`source_frame_path`, provider diagnostics, and all unlisted fields.

- [ ] **Step 2: Run the focused tests and confirm RED**

```bash
python -m unittest tests.test_cloud_strategy
```

Expected: failures because `run_chain()` does not accept or emit contexts.

- [ ] **Step 3: Add explicit context projection**

Add `MODEL_INPUT_VISION_FIELDS` containing only:

```python
(
    "plant_zone", "captured_at", "image_quality", "target_detected",
    "leaf_droop", "yellowing", "visible_damage", "browning",
    "leaf_curl", "spots_or_lesions", "leaf_loss", "stem_posture",
    "occlusion", "target_ambiguity", "leaf_spread", "wilting",
    "overall_visual_state", "change_vs_previous", "confidence",
)
```

Implement:

```python
def project_optional_context(value, *, kind):
    if value is None:
        return {"availability": "not_requested", "facts": None}
    availability = value["availability"]
    facts = value["facts"]
    if availability != "available":
        return {"availability": availability, "facts": None}
    if kind == "vision":
        facts = [
            {key: record[key] for key in MODEL_INPUT_VISION_FIELDS if key in record}
            for record in facts
        ]
    return {"availability": "available", "facts": copy.deepcopy(facts)}
```

Validate the exact two keys, availability enum, and available/null consistency;
raise `ValueError("invalid_<kind>_context")` before a provider call for malformed
trusted-local integration input.

- [ ] **Step 4: Extend `run_chain()` without changing Strategy output**

Add keyword parameters defaulting to `None`, then append projected `vision` and
`experience` blocks to the existing top-level State projection before calling
the provider. Keep State binding, response parsing, Validator, and
`strategy.v1` unchanged.

- [ ] **Step 5: Update prompt facts and missing-value rules**

Add concise instructions that:

- `vision` and `experience` are read-only supporting facts;
- `available` permits use of their `facts`;
- `unavailable` and `not_requested` mean no facts exist and must not be inferred;
- visual/experience facts never override stale State or active safety facts;
- output remains exactly `strategy.v1` with `prompt_version="strategy-prompt.v2"`.

- [ ] **Step 6: Run focused tests and commit**

```bash
python -m unittest tests.test_cloud_strategy
git add services/soil3/cloud_strategy/service.py services/soil3/cloud_strategy/prompts/strategy_v1.txt tests/test_cloud_strategy.py
git commit -m "feat: add vision experience strategy context"
```

---

### Task 3: Orchestrate optional modules in the Shadow runtime

**Files:**
- Modify: `services/soil3/agent_runtime/runtime_v1.py`
- Modify: `config/soil3_agent_runtime.example.json`
- Create: `tests/test_day5_vision_experience_runtime.py`
- Modify: `tests/test_soil3_agent_runtime.py`

**Interfaces:**
- Consumes: `VisionRunResult.manifest_ref`, successful Vision observations,
  `ExperienceRetriever.retrieve()`, and Task 2's `run_chain()` contexts.
- Produces: exact RuntimeConfig switches, persisted Experience artifacts,
  Trace associations, and audited Strategy contexts.

- [ ] **Step 1: Add failing strict-config tests**

Test exact valid values and rejection of missing fields, non-boolean switches,
and invalid limits:

```python
value["vision"] = {"enabled": False}
value["experience"] = {"enabled": False, "limit_per_class": 3}
config = RuntimeConfig.from_dict(value)
self.assertFalse(config.vision_enabled)
self.assertFalse(config.experience_enabled)

for invalid in (True, 0, 21):
    value["experience"]["limit_per_class"] = invalid
    with self.assertRaises(ValueError):
        RuntimeConfig.from_dict(value)
```

Update every existing runtime test fixture to declare both disabled objects;
do not add implicit defaults.

- [ ] **Step 2: Add failing complete-pipeline tests**

Create `tests/test_day5_vision_experience_runtime.py` with cases that inspect
the persisted Trace and Cloud Strategy audit:

```python
class Day5VisionExperienceRuntimeTests(unittest.TestCase):
    def test_disabled_modules_are_not_called_and_strategy_gets_not_requested(self):
        config = runtime_config(self.root, vision=False, experience=False)
        record = run_pipeline(config)
        trace = TraceStore(config.trace_dir).read(record["trace_id"])
        audit = read_only_audit_record(record["audit_path"])
        self.assertEqual({"availability": "not_requested", "ref": None}, trace["decision"]["vision"])
        self.assertEqual({"availability": "not_requested", "facts": None}, audit["model_input"]["vision"])

    def test_available_modules_persist_refs_and_reach_strategy_input(self):
        config = runtime_config(self.root, vision=True, experience=True)
        record = run_pipeline(config)
        assert_available_ref_matches_file(self, TraceStore(config.trace_dir).read(record["trace_id"])["decision"]["vision"])
        audit = read_only_audit_record(record["audit_path"])
        self.assertEqual("available", audit["model_input"]["vision"]["availability"])
        self.assertEqual("available", audit["model_input"]["experience"]["availability"])

    def test_vision_failure_is_unavailable_without_facts_or_ref(self):
        self.assert_unavailable_context(module="vision")

    def test_experience_failure_is_unavailable_without_facts_or_ref(self):
        self.assert_unavailable_context(module="experience")

    def test_empty_experience_result_is_available_and_persisted(self):
        self.assert_available_empty_experience_result()
```

Define `runtime_config`, `read_only_audit_record`,
`assert_available_ref_matches_file`, `assert_unavailable_context`, and
`assert_available_empty_experience_result` in that test module. Each helper
must create real temporary artifacts and make the assertions named by the
helper; none may return a precomputed pass/fail flag.

For the available Vision case, return a public `VisionRunResult` whose
`manifest_ref` points to a real temporary manifest with a correct SHA-256. Do
not construct or infer a private Vision zone-record path in runtime code.

For every case assert:

- Trace association availability and ref;
- ref path exists and ref SHA-256 matches bytes when available;
- the daily Cloud Strategy audit's `model_input` contains the same context;
- unavailable/not-requested facts are null;
- `phase3_called` and `physical_actions_performed` remain false.

- [ ] **Step 3: Run focused tests and confirm RED**

```bash
python -m unittest tests.test_day5_vision_experience_runtime tests.test_soil3_agent_runtime
```

Expected: config and orchestration tests fail because the switches and calls do
not exist.

- [ ] **Step 4: Implement exact RuntimeConfig fields and paths**

Add `vision` and `experience` to `CONFIG_FIELDS`; expose immutable values:

```python
vision_enabled: bool
experience_enabled: bool
experience_limit_per_class: int

@property
def experience_dir(self) -> Path:
    return self.runtime_root / "experience"
```

Keep `config/soil3_agent_runtime.example.json` disabled by default.

- [ ] **Step 5: Implement optional context collectors**

Implement helpers with this return contract:

```python
association = {"availability": value, "ref": trace_ref_or_none}
strategy_context = {"availability": value, "facts": facts_or_none}
```

Vision helper rules:

- disabled: return both `not_requested` forms without importing camera code;
- enabled: lazily call the public `services.soil3.vision.capture_and_analyze_once`;
- available only when a public manifest ref and at least one validated
  observation exist;
- convert public configuration errors, missing optional Vision dependencies,
  and normal no-observation results to unavailable;
- map the public manifest reference directly into Trace shape and verify its
  referenced file/hash before accepting it.

Experience helper rules:

- disabled: return both `not_requested` forms;
- enabled: call `ExperienceRetriever(config.episode_dir).retrieve(state,
  limit_per_class=config.experience_limit_per_class)`;
- atomically persist successful output to
  `config.experience_dir / f"{trace_id}.json"`;
- create the Trace ref through `_artifact_ref()`;
- convert `RetrievalError` to unavailable without a partial artifact.

- [ ] **Step 6: Insert collectors before the first Trace decision update**

After State persistence and before Strategy:

```python
vision_association, vision_context = collect_vision(config)
experience_association, experience_context = collect_experience(config, state, trace_id)
trace_store.set_decision(
    trace_id,
    state_ref=_artifact_ref("state.v1", config.state_output),
    vision=vision_association,
    experience=experience_association,
)
strategy_result = run_chain(
    state=state,
    config=config.strategy_config(),
    prompt=prompt,
    fixture_content=(
        json.dumps(build_offline_fixture(state))
        if config.provider_mode == "offline_fixture"
        else None
    ),
    vision_context=vision_context,
    experience_context=experience_context,
)
```

Do not update either association a second time because Trace decision fields are
set-once.

- [ ] **Step 7: Run runtime, Trace, and Day 5 focused tests**

```bash
python -m unittest \
  tests.test_day5_vision_experience_runtime \
  tests.test_soil3_agent_runtime \
  tests.test_day5_fault_safety \
  tests.test_trace_v1
```

Expected: all pass; no test calls Phase3, MQTT, or an actuator.

- [ ] **Step 8: Commit runtime integration**

```bash
git add services/soil3/agent_runtime/runtime_v1.py config/soil3_agent_runtime.example.json tests/test_day5_vision_experience_runtime.py tests/test_soil3_agent_runtime.py
git commit -m "feat: integrate vision experience shadow context"
```

---

### Task 4: Synchronize boundaries and complete regression evidence

**Files:**
- Modify: `services/soil3/vision/README.md`
- Modify: `services/soil3/agent_runtime/README.md`
- Modify: `docs/SYSTEM_ARCHITECTURE.md`
- Modify: `docs/PROTOCOLS_AND_BOUNDARIES.md`
- Test: `tests/test_day5_vision_experience_runtime.py`

**Interfaces:**
- Consumes: the implemented public manifest, context projection, and runtime switches.
- Produces: accurate lifecycle documentation and final Review evidence.

- [ ] **Step 1: Add documentation-contract assertions**

Add assertions that documentation states:

- Vision/Experience runtime integration is implemented;
- both are optional and explicit when unavailable or not requested;
- Vision uses a public `vision_run.v1` manifest;
- Strategy receives validated facts;
- Phase3 remains uncalled and physical actions remain false.

- [ ] **Step 2: Update the four routed documents**

Describe only implemented behavior. Remove the runtime README statement that
Vision and Experience are always `not_requested`. Do not claim a production
camera/provider run or openEuler deployment.

- [ ] **Step 3: Run focused and related regression suites**

```bash
python -m unittest \
  tests.test_day5_vision_experience_runtime \
  tests.test_vision_v1 \
  tests.test_qwen_vision \
  tests.test_experience_retrieval_v1 \
  tests.test_cloud_strategy \
  tests.test_soil3_agent_runtime \
  tests.test_day5_fault_safety \
  tests.test_trace_v1 \
  tests.test_cloud_gate_v2 \
  tests.test_strategy_runner_v1 \
  tests.test_phase3_bridge_v1 \
  tests.test_episode_v1
```

Also run the full suite when the environment supplies `requests`, OpenCV, and
timezone data. If it does not, report the exact environment failures rather
than claiming full regression success.

- [ ] **Step 4: Inspect scope and boundary imports**

```bash
git diff origin/main...HEAD --name-only
git diff origin/main...HEAD --check
rg -n "phase3|paho\.mqtt|manual_water|ActuatorLayer" services/soil3/agent_runtime services/soil3/cloud_strategy
```

Expected: no imports or calls into Phase3, MQTT, manual water, or ActuatorLayer;
any documentation mentions are boundary statements only.

- [ ] **Step 5: Commit documentation and test contracts**

```bash
git add services/soil3/vision/README.md services/soil3/agent_runtime/README.md docs/SYSTEM_ARCHITECTURE.md docs/PROTOCOLS_AND_BOUNDARIES.md tests/test_day5_vision_experience_runtime.py
git commit -m "docs: describe shadow context integration"
```

- [ ] **Step 6: Request independent Review and open the PR**

Review the complete `origin/main...HEAD` range. Fix all Critical and Important
findings within scope, rerun affected tests, then push
`codex/day5-vision-experience-runtime` and create one PR to `main`. The PR body
must list changed modules, explicit non-changes, exact test counts, the absence
of deployment/hardware validation, and remaining risks. Do not merge it.
