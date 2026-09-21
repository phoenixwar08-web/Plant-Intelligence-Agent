# Soil3 Qwen Provider and Production State Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox - [ ] syntax for tracking.

**Goal:** Produce a traceable Qwen-backed proposal-only soil3 pipeline and map real openGauss soil humidity/freshness into the unchanged state.v1 contract.

**Architecture:** telemetry.events gains one read-only openGauss query that returns a valid latest soil row or no row. RuntimeConfig gains a strict explicit Qwen mode that supplies only non-secret provider settings to the existing Cloud Strategy client; provider failure remains visible and never falls back to the fixture. The systemd pipeline unit gains an optional root-only environment file, while its runner, Gate, and physical-execution boundary do not change.

**Tech Stack:** Python 3 standard library, existing requests OpenAI-compatible client, openGauss gsql via runuser, systemd, Python unittest.

**Spec:** docs/superpowers/specs/2026-09-21-soil3-qwen-provider-state-telemetry-design.md

## Global Constraints

- state.v1, Strategy validator, and Gate safety rules are frozen; do not modify their protocol or relax their checks.
- Qwen model is qwen3.8-Flash, accessed only through the approved runtime workspace endpoint.
- offline_fixture and qwen_dashscope are explicit modes; Qwen failure never silently selects the fixture.
- The Qwen key exists only in an openEuler root-owned mode-0600 environment file and must never appear in source, test fixtures, runtime JSON, audit, Episode, command lines, or journal output.
- The telemetry adapter is read-only. Empty, error, null, non-finite, or out-of-Phase3-range readings remain missing.
- cloud_protection and pending_soak remain missing when no explicit source fact exists. Never infer false from absence.
- Runner remains dry-run; do not add a Phase3 Bridge, MQTT client/publisher, pump entry point, database write, or stateful actuator path.
- Source changes go through Issue #42 and an Owner-merged PR before openEuler deployment.

---

## File structure

- services/soil3/telemetry/events.py — isolated openGauss read adapter and existing snapshot sensor_readings feed.
- services/soil3/agent_runtime/runtime_v1.py — strict provider mode/configuration, real validated Qwen chain, and provenance.
- config/soil3_agent_runtime.example.json — default offline fixture with a non-secret provider object.
- deploy/systemd/plant-agent-soil3-pipeline.service — optional root-only Qwen environment file.
- services/soil3/agent_runtime/README.md — post-merge secret installation, isolated smoke, shadow activation, and rollback.
- tests/test_soil3_agent_runtime.py — telemetry mapping, provider boundaries, provenance, failure without fallback, and unit boundary tests.
- tests/test_cloud_strategy.py — Qwen request-envelope and validated provenance tests.

### Task 1: Read-only canonical soil telemetry

**Files:**
- Modify: services/soil3/telemetry/events.py, functions adjacent to _opengauss_health and build_health_snapshot.
- Modify: tests/test_soil3_agent_runtime.py, StateProducerTests.

**Interfaces:**
- Consumes: build_health_snapshot(device_name, state_path, ..., now=None), existing runuser/gsql execution pattern.
- Produces: _opengauss_latest_soil_reading(device_name: str, *, timeout: float = 12, runner=subprocess.run) -> dict | None.
- Produces, when valid: exactly timestamp, humidity, temperature, ec_raw; otherwise None.

- [ ] **Step 1: Write the failing tests**

~~~python
def test_opengauss_soil_row_maps_to_state_humidity_and_freshness(self):
    completed = mock.Mock(
        returncode=0,
        stdout="2026-09-21 01:53:00+00|35.0|23.2|610\n",
        stderr="",
    )
    with mock.patch("services.soil3.telemetry.events._service_status",
                    return_value={"status": "active"}):
        with mock.patch("services.soil3.telemetry.events.subprocess.run",
                        return_value=completed):
            snapshot = build_health_snapshot("soil3", self.state_path, now=1789955700)
    state = StateBuilder("soil3").build_from_health_snapshot(snapshot)
    self.assertEqual(35.0, state["soil"]["humidity_percent"])
    self.assertEqual("2026-09-21T01:53:00Z", state["source_timestamps"]["soil"])
    self.assertIsNotNone(state["data_quality"]["soil_age_sec"])

def test_invalid_or_failed_opengauss_soil_row_stays_missing(self):
    results = (
        mock.Mock(returncode=1, stdout="", stderr="connection refused"),
        mock.Mock(returncode=0, stdout="2026-09-21 01:53:00+00|5|23|610\n", stderr=""),
        mock.Mock(returncode=0, stdout="2026-09-21 01:53:00+00|35|46|610\n", stderr=""),
    )
    for result in results:
        with mock.patch("services.soil3.telemetry.events.subprocess.run", return_value=result):
            snapshot = build_health_snapshot("soil3", self.state_path)
        state = StateBuilder("soil3").build_from_health_snapshot(snapshot)
        self.assertIsNone(state["soil"]["humidity_percent"])
        self.assertIsNone(state["data_quality"]["soil_age_sec"])
~~~

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: python -m unittest tests.test_soil3_agent_runtime.StateProducerTests -v

Expected: FAIL because the snapshot has no canonical database soil row and the adapter is not implemented.

- [ ] **Step 3: Write the minimal adapter**

~~~python
def _opengauss_latest_soil_reading(device_name, *, timeout=12, runner=subprocess.run):
    sql = (
        "SELECT recv_time::text,humidity,temp,ec FROM soil_sensor_readings "
        "WHERE device_code='%s' AND humidity IS NOT NULL AND temp IS NOT NULL "
        "AND ec IS NOT NULL ORDER BY recv_time DESC,id DESC LIMIT 1;"
    ) % str(device_name).replace("'", "''")
    result = runner(
        _opengauss_command(sql), capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if result.returncode != 0:
        return None
    return _parse_canonical_soil_row(result.stdout)

def _parse_canonical_soil_row(stdout):
    parts = stdout.strip().split("|", 3)
    if len(parts) != 4:
        return None
    timestamp, humidity, temperature, ec_raw = parts
    values = tuple(_safe_float(value) for value in (humidity, temperature, ec_raw))
    valid = (
        timestamp.strip() and
        all(value is not None and math.isfinite(value) for value in values) and
        5.0 < values[0] <= 100.0 and
        0.0 <= values[1] <= 45.0 and
        0.0 <= values[2] <= 5000.0
    )
    if not valid:
        return None
    return {"timestamp": timestamp.strip(), "humidity": values[0],
            "temperature": values[1], "ec_raw": values[2]}
~~~

Extract _opengauss_command from the existing _opengauss_health command construction so both queries use the same runuser, library path, gsql path, database, and port. In build_health_snapshot, use the valid canonical row as the sole sensor_readings list; use the existing CSV list only when this adapter returns None. Do not add write SQL, a default reading, or a safety flag.

- [ ] **Step 4: Run state and protocol regressions**

Run: python -m unittest tests.test_soil3_agent_runtime tests.test_plant_state_v1 -v

Expected: PASS. A valid row creates real humidity and soil_age_sec; all failed/invalid cases remain missing and preserve missing pending_soak/cloud_protection.

- [ ] **Step 5: Commit**

~~~bash
git add services/soil3/telemetry/events.py tests/test_soil3_agent_runtime.py
git commit -m "feat: map canonical soil telemetry into agent state"
~~~

### Task 2: Explicit Qwen runtime mode without fallback

**Files:**
- Modify: services/soil3/agent_runtime/runtime_v1.py.
- Modify: tests/test_soil3_agent_runtime.py, PipelineTests and config helper.
- Modify: tests/test_cloud_strategy.py, ClientAndChainTests.

**Interfaces:**
- Consumes: RuntimeConfig.from_dict(value), run_chain(state, config, prompt, fixture_content=None), and the existing Cloud Strategy client/validator.
- Produces: RuntimeConfig.provider_mode exactly offline_fixture or qwen_dashscope.
- Produces: RuntimeConfig.strategy_config() with enabled false for fixture, complete non-secret client settings for Qwen.
- Produces: a persisted failed-Qwen run record with provider provenance and non-execution flags; Gate, Runner, and Episode are not called without an accepted strategy.

- [ ] **Step 1: Write failing mode, provenance, and no-fallback tests**

~~~python
def test_qwen_mode_requires_complete_nonsecret_provider_config(self):
    value = self.runtime_config_value(root)
    value.update(provider_mode="qwen_dashscope", provider={
        "base_url": "https://workspace.example/api/v1",
        "model": "qwen3.8-Flash",
        "api_key_env": "QWEN_DASHSCOPE_API_KEY",
        "timeout_seconds": 30,
        "max_retries": 1,
        "temperature": 0,
        "max_tokens": 1200,
        "json_response_format": True,
    })
    config = RuntimeConfig.from_dict(value)
    self.assertTrue(config.strategy_config()["enabled"])
    self.assertNotIn("api_key", json.dumps(config.strategy_config()).lower())

def test_qwen_failure_is_recorded_without_fixture_fallback(self):
    failed_chain = {
        "validation": {"accepted": False, "reason_codes": ["model_auth_failed"], "strategy": None},
        "cloud": {"provider": "qwen_dashscope", "model": "qwen3.8-Flash"},
    }
    with mock.patch("services.soil3.agent_runtime.runtime_v1.run_chain",
                    return_value=failed_chain):
        with self.assertRaisesRegex(RuntimeError, "qwen_dashscope strategy was rejected"):
            run_pipeline(qwen_config)
    self.assertFalse((qwen_config.episode_dir).exists())
~~~

Add a FakeSession client test asserting the post URL is base_url plus /chat/completions, the in-memory header uses Bearer secret, payload model is qwen3.8-Flash, response_format is JSON-object, and accepted strategy provenance is qwen_dashscope.

- [ ] **Step 2: Run focused tests and verify failure**

Run: python -m unittest tests.test_soil3_agent_runtime.PipelineTests tests.test_cloud_strategy.ClientAndChainTests -v

Expected: FAIL because RuntimeConfig currently permits only offline_fixture and run_pipeline always passes fixture_content.

- [ ] **Step 3: Implement strict selection**

~~~python
PROVIDER_FIELDS = {
    "base_url", "model", "api_key_env", "timeout_seconds", "max_retries",
    "temperature", "max_tokens", "json_response_format",
}

def strategy_config(self):
    if self.provider_mode == "offline_fixture":
        return {
            "enabled": False, "provider": "offline_fixture",
            "model": "offline_fixture", "validator": dict(self.strategy_validator),
        }
    return {
        "enabled": True, "provider": "qwen_dashscope",
        **dict(self.provider), "validator": dict(self.strategy_validator),
    }
~~~

Add provider to the strict top-level runtime config field set. Require provider == {} for offline_fixture. For qwen_dashscope require exactly PROVIDER_FIELDS, a non-empty HTTPS base URL, model exactly qwen3.8-Flash, identifier-only api_key_env, finite positive timeout/max_tokens, non-negative retries, finite temperature, and boolean json_response_format. Reject any supplied secret-value field; api_key_env is a name, not a key.

Pass fixture_content only for offline_fixture. For Qwen rejection, append the existing audit, write a run record with selected provider mode, cloud provider/model, runner_status equal to not_started_provider_or_validation_failure, and execution flags false; then raise a stable RuntimeError. Do not evaluate Gate, create an Episode, or call Runner without accepted strategy.v1. On success, keep the validator and all execution fields unchanged, then evaluate Gate normally.

- [ ] **Step 4: Run provider and chain regressions**

Run: python -m unittest tests.test_cloud_strategy tests.test_cloud_gate tests.test_strategy_runner_v1 tests.test_episode_v1 tests.test_soil3_agent_runtime -v

Expected: PASS. Fixture behavior is unchanged; Qwen provenance is explicit; Qwen failure has no fallback; Gate deny skips Runner; persisted physical_actions_performed and phase3_called remain false.

- [ ] **Step 5: Commit**

~~~bash
git add services/soil3/agent_runtime/runtime_v1.py tests/test_soil3_agent_runtime.py tests/test_cloud_strategy.py
git commit -m "feat: add explicit qwen shadow provider mode"
~~~

### Task 3: Non-secret deployment assets and operational contract

**Files:**
- Modify: config/soil3_agent_runtime.example.json.
- Modify: deploy/systemd/plant-agent-soil3-pipeline.service.
- Modify: services/soil3/agent_runtime/README.md.
- Modify: tests/test_soil3_agent_runtime.py, DeploymentAssetTests.

**Interfaces:**
- Consumes: runtime root /root/water/runtime/instances/soil3/agent_chain and existing pipeline service.
- Produces: default offline fixture config with provider {} and no secret material.
- Produces: optional EnvironmentFile=-/root/water/runtime/instances/soil3/agent_chain/secrets/qwen.env.
- Produces: a documented isolated smoke procedure that stops only the pipeline timer.

- [ ] **Step 1: Write failing asset-boundary tests**

~~~python
def test_pipeline_unit_reads_optional_root_only_qwen_secret_without_control_access(self):
    source = PIPELINE_UNIT.read_text(encoding="utf-8")
    self.assertIn(
        "EnvironmentFile=-/root/water/runtime/instances/soil3/agent_chain/secrets/qwen.env",
        source,
    )
    self.assertNotIn("mqtt", source.lower())
    self.assertNotIn("phase3/main.py", source)
    self.assertNotIn("manual_water", source)

def test_example_runtime_config_is_fixture_and_has_no_secret_material(self):
    value = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    self.assertEqual("offline_fixture", value["provider_mode"])
    self.assertEqual({}, value["provider"])
    self.assertNotRegex(json.dumps(value).lower(),
                        r"api_key(?!_env)|secret|token|password")
~~~

- [ ] **Step 2: Run asset tests and verify failure**

Run: python -m unittest tests.test_soil3_agent_runtime.DeploymentAssetTests -v

Expected: FAIL because the runtime example lacks provider {} and the pipeline service does not read an optional Qwen environment file.

- [ ] **Step 3: Implement non-secret assets and operating procedure**

Add provider {} to the example fixture config. Add exactly this optional EnvironmentFile line to the pipeline unit; retain Type=oneshot, existing ReadOnlyPaths/ReadWritePaths, and no MQTT/Phase3 execution dependency:

~~~ini
EnvironmentFile=-/root/water/runtime/instances/soil3/agent_chain/secrets/qwen.env
~~~

In the runtime README, show the Qwen runtime JSON field shape with provider_mode qwen_dashscope, approved workspace base_url, model qwen3.8-Flash, and an api_key_env name only. Specify these smoke safety commands:

~~~bash
install -d -m 700 /root/water/runtime/instances/soil3/agent_chain/secrets
install -m 600 /dev/null /root/water/runtime/instances/soil3/agent_chain/secrets/qwen.env
systemctl stop plant-agent-soil3-pipeline.timer
systemctl start plant-agent-soil3-pipeline.service
~~~

Document that the Owner inserts the secret locally into the 0600 file without shell history or source control, inspects one Qwen run, and only then enables the timer. Document explicit fixture rollback and timer disable. Do not add a systemd capability, path, or command for MQTT, Phase3, or pumps.

- [ ] **Step 4: Run focused full checks**

Run: python -m unittest tests.test_soil3_agent_runtime tests.test_plant_state_v1 tests.test_cloud_strategy tests.test_cloud_gate tests.test_strategy_runner_v1 tests.test_episode_v1 -v

Expected: PASS. Assets expose no secret, Qwen is explicit, and the proposal-only service boundary remains intact.

- [ ] **Step 5: Commit**

~~~bash
git add config/soil3_agent_runtime.example.json deploy/systemd/plant-agent-soil3-pipeline.service services/soil3/agent_runtime/README.md tests/test_soil3_agent_runtime.py
git commit -m "docs: define qwen shadow deployment boundary"
~~~

### Task 4: Review, PR, and post-merge shadow verification

**Files:**
- Modify: design/plan documents only if review discovers a factual discrepancy or execution checkboxes need marking.

**Interfaces:**
- Consumes: passing source tests and Owner-selected merged main revision.
- Produces: a PR for Issue #42 and, after manual Owner merge only, board evidence from a non-physical Qwen shadow run.

- [ ] **Step 1: Run source boundary checks**

Run: rg -n -i "mqtt|manual_water|phase3.*bridge|publish\(" services/soil3/agent_runtime services/soil3/telemetry deploy/systemd config/soil3_agent_runtime.example.json

Expected: no new publisher, control, Bridge, or secret value. Review expected source labels and read-only SELECT SQL manually.

- [ ] **Step 2: Run all available tests**

Run: python -m unittest discover -s tests -v

Expected: PASS. If an environment-specific unrelated test cannot run, record its exact failure without editing unrelated code.

- [ ] **Step 3: Inspect scope and create PR**

Run: git diff origin/main...HEAD --check

Run: git diff --stat origin/main...HEAD

Expected: only Tasks 1-3 files plus design/plan docs; no protocol, Gate, Phase3, MQTT, schema, historical-data, or credential change. Push the Issue branch and create a PR to main. Leave it in Review; do not approve, merge, or close Issue #42.

- [ ] **Step 4: After Owner merge, perform the isolated openEuler smoke**

Install the merged main release, stop only plant-agent-soil3-pipeline.timer, verify Phase3 and MQTT services stay active, install the secret outside the repository, set explicit qwen_dashscope runtime config, run one pipeline, and inspect state, strategy, Gate, run, audit, and Episode records.

Expected: strategy/audit/run identify Qwen and qwen3.8-Flash; real humidity and soil_age_sec exist only if database returned a valid row; Gate records its actual decision; Gate deny skips Runner; every execution record says physical actions and Phase3 calls are false.

- [ ] **Step 5: Enable a scheduled shadow cycle or explicitly roll back**

Run: systemctl enable --now plant-agent-soil3-pipeline.timer

Expected: enable only after smoke passes, then verify one scheduled five-minute Qwen run with the same non-execution evidence. On provider/validator failure, leave the timer disabled or explicitly restore offline_fixture; never use silent fallback.
