# Soil3 Agent Runtime Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a traceable soil3 proposal-only chain on openEuler that continuously produces real `state.v1`, creates a clearly labelled offline strategy, fail-closes through Gate, runs Runner only after admission, and persists an Episode without any MQTT, pump, or Phase3 call.

**Architecture:** Add a small `services.soil3.agent_runtime` orchestration package without changing any frozen protocol or Phase3 source. A State oneshot service creates an atomic latest snapshot from the existing telemetry health adapter; a Pipeline oneshot service consumes that snapshot, runs the existing strategy/Gate/Runner/Episode modules, and records truthful skip information when Gate denies. Two five-minute systemd timers invoke those units from a versioned release directory, independent of the existing Phase3 and MQTT services.

**Tech Stack:** Python 3.9 standard library, existing `services.soil3` modules, JSON files, systemd oneshot services and timers, `unittest`, SSH/Git archive for the post-merge deployment.

**Spec:** `docs/superpowers/specs/2026-09-20-soil3-agent-runtime-baseline-design.md`

## Global Constraints

- Select and record the exact latest merged `main` revision immediately before deployment; do not deploy an arbitrary feature branch.
- Do not alter `state.v1`, Phase3 logic, ActuatorLayer, MQTT, ESP32, Vision scheduling, `/wyc/plant-agent`, or existing systemd unit files.
- Read facts only through existing soil3 runtime files and `telemetry.events.build_health_snapshot`; absent facts remain absent.
- The only initial provider mode is `offline_fixture`; it has no provider URL, no secret, and records `offline_fixture` in all strategy metadata and pipeline records.
- Set `exploration_requested` to `false`; Gate never grants actuator permission; Runner receives no Phase3/MQTT dependency and is invoked only for an admitted gate decision.
- All new persistent data is below `/root/water/runtime/instances/soil3/agent_chain`; no Phase3 runtime/data/log path is writable by new units.
- New units are `Type=oneshot`, use `PYTHONDONTWRITEBYTECODE=1`, can be disabled independently, and never restart existing services.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `services/soil3/agent_runtime/runtime_v1.py` | Pure orchestration functions: read real health facts, atomically write state/records, build labelled fixture strategies, apply Gate, conditionally dry-run Runner, and create Episode records. |
| `services/soil3/agent_runtime/service.py` | CLI with `state` and `pipeline` subcommands; reads one explicit runtime config file. |
| `services/soil3/agent_runtime/__init__.py` | Declares the isolated runtime package. |
| `config/soil3_agent_runtime.example.json` | Non-secret, explicit source paths, runtime output paths, Gate policy, and `offline_fixture` mode. |
| `services/soil3/agent_runtime/README.md` | Runtime contract, provider-mode truthfulness, state/deny/runner semantics, installation and rollback commands. |
| `deploy/systemd/plant-agent-soil3-state.service` | One atomic state snapshot build. |
| `deploy/systemd/plant-agent-soil3-state.timer` | Five-minute State schedule. |
| `deploy/systemd/plant-agent-soil3-pipeline.service` | One pipeline pass after requiring State. |
| `deploy/systemd/plant-agent-soil3-pipeline.timer` | Five-minute Pipeline schedule. |
| `tests/test_soil3_agent_runtime.py` | Unit and integration-style tests for state fact preservation, fixture labelling, Gate deny/allow branching, Runner isolation, Episode persistence, and unit-file boundaries. |

## Task 1: Add the isolated runtime state producer

**Files:**
- Create: `services/soil3/agent_runtime/__init__.py`
- Create: `services/soil3/agent_runtime/runtime_v1.py`
- Create: `services/soil3/agent_runtime/service.py`
- Create: `tests/test_soil3_agent_runtime.py`

**Interfaces:**
- Consumes: `telemetry.events.build_health_snapshot(device_name, state_path, sensor_log_path, local_sensor_log_path, irrigation_trials_path, service_unit, mqtt_connected)` and `StateBuilder(device_code).build_from_health_snapshot(snapshot, parameters)`.
- Produces: `RuntimeConfig.from_dict(value) -> RuntimeConfig`, `write_state_snapshot(config: RuntimeConfig) -> dict`, and `atomic_write_json(path: Path, value: dict) -> None`.
- State output: the exact `state.v1` returned by `StateBuilder`; no added safety keys or protocol fields.

- [ ] **Step 1: Write failing state-producer tests**

```python
class StateProducerTests(unittest.TestCase):
    def test_write_state_snapshot_preserves_missing_safety_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_config(Path(directory))
            snapshot = health_snapshot(system_state={"pump_active": False})
            with mock.patch(
                "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                return_value=snapshot,
            ):
                state = write_state_snapshot(config)
            stored = load_json(config.state_output)
            self.assertEqual(state, stored)
            self.assertEqual("state.v1", stored["schema_version"])
            self.assertEqual({"pump_active": False}, stored["safety"]["flags"])
            self.assertNotIn("pending_soak", stored["safety"]["flags"])
            self.assertNotIn("cloud_protection", stored["safety"]["flags"])

    def test_config_refuses_non_soil3_or_non_fixture_output_paths(self):
        with self.assertRaisesRegex(ValueError, "device_code"):
            RuntimeConfig.from_dict({"device_code": "soil2"})
```

- [ ] **Step 2: Run the state-producer tests and verify failure**

Run: `python -m unittest tests.test_soil3_agent_runtime.StateProducerTests -v`

Expected: `ModuleNotFoundError: No module named 'services.soil3.agent_runtime'`.

- [ ] **Step 3: Implement the minimum state runtime API**

```python
@dataclass(frozen=True)
class RuntimeConfig:
    device_code: str
    phase3_state_path: Path
    sensor_log_path: Path
    irrigation_trials_path: Path
    state_output: Path
    runtime_root: Path
    phase3_service_unit: str
    provider_mode: str
    prompt_path: Path
    strategy_validator: dict[str, Any]
    gate_policy: GatePolicy

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeConfig":
        required = {
            "device_code", "provider_mode", "exploration_requested",
            "phase3_state_path", "sensor_log_path", "irrigation_trials_path",
            "phase3_service_unit", "runtime_root", "state_output",
            "prompt_path", "strategy_validator", "gate_policy",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("runtime config fields are invalid")
        if value["device_code"] != "soil3":
            raise ValueError("runtime config device_code must be soil3")
        if value["provider_mode"] != "offline_fixture":
            raise ValueError("runtime config provider_mode must be offline_fixture")
        if value["exploration_requested"] is not False:
            raise ValueError("runtime config exploration_requested must be false")
        root = Path(value["runtime_root"])
        state_output = Path(value["state_output"])
        if root != state_output.parent.parent or state_output.name != "latest.json":
            raise ValueError("state_output must be runtime_root/state/latest.json")
        if Path(value["phase3_state_path"]).name != "system_state.json":
            raise ValueError("phase3_state_path must name system_state.json")
        if str(value["phase3_service_unit"]) != "phase3_soil3.service":
            raise ValueError("phase3_service_unit must be phase3_soil3.service")
        return cls(
            device_code="soil3", phase3_state_path=Path(value["phase3_state_path"]),
            sensor_log_path=Path(value["sensor_log_path"]),
            irrigation_trials_path=Path(value["irrigation_trials_path"]),
            state_output=state_output, runtime_root=root,
            phase3_service_unit="phase3_soil3.service",
            provider_mode="offline_fixture",
            prompt_path=Path(value["prompt_path"]),
            strategy_validator=dict(value["strategy_validator"]),
            gate_policy=GatePolicy.from_dict(value["gate_policy"]),
        )

    def strategy_config(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "provider": "offline_fixture",
            "model": "offline_fixture",
            "validator": dict(self.strategy_validator),
        }

    @property
    def strategy_output(self) -> Path:
        return self.runtime_root / "strategy" / "latest.json"

    @property
    def gate_output(self) -> Path:
        return self.runtime_root / "gate" / "latest.json"

    @property
    def runner_dir(self) -> Path:
        return self.runtime_root / "runner"

    @property
    def episode_dir(self) -> Path:
        return self.runtime_root / "episodes"

    @property
    def audit_dir(self) -> Path:
        return self.runtime_root / "audit"

def write_state_snapshot(config: RuntimeConfig) -> dict[str, Any]:
    snapshot = build_health_snapshot(
        config.device_code,
        str(config.phase3_state_path),
        sensor_log_path=str(config.sensor_log_path),
        irrigation_trials_path=str(config.irrigation_trials_path),
        service_unit=config.phase3_service_unit,
    )
    state = StateBuilder(config.device_code).build_from_health_snapshot(snapshot)
    atomic_write_json(config.state_output, state)
    return state
```

Extend the shown validation with a constructor argument used only by tests to
set the permitted runtime root. Production CLI invocation passes exactly
`/root/water/runtime/instances/soil3/agent_chain`; test helpers pass their
temporary directory. `atomic_write_json` must use a same-directory temporary
file, `fsync`, and `os.replace`.

Implement `service.py` so `python -m services.soil3.agent_runtime.service state --config PATH` loads JSON, calls `write_state_snapshot`, and prints only the output path, schema version, `observed_at`, and available safety-flag names.

- [ ] **Step 4: Run the state-producer tests and the existing state tests**

Run: `python -m unittest tests.test_soil3_agent_runtime.StateProducerTests tests.test_plant_state_v1 -v`

Expected: all pass; the existing producer contract remains unchanged.

- [ ] **Step 5: Commit the state producer**

```bash
git add services/soil3/agent_runtime/__init__.py services/soil3/agent_runtime/runtime_v1.py \
  services/soil3/agent_runtime/service.py tests/test_soil3_agent_runtime.py
git commit -m "feat: add soil3 runtime state producer"
```

## Task 2: Add the proposal-only pipeline and Episode trace

**Files:**
- Modify: `services/soil3/agent_runtime/runtime_v1.py`
- Modify: `services/soil3/agent_runtime/service.py`
- Modify: `tests/test_soil3_agent_runtime.py`

**Interfaces:**
- Consumes: `write_state_snapshot(config)`, `run_chain(state, config, prompt, fixture_content)`, `evaluate_gate(state, strategy, policy, exploration_requested=False)`, `DryRunRunner(RunnerStore(path))`, and `EpisodeStore(path)`.
- Produces: `build_offline_fixture(state: dict) -> dict`, `run_pipeline(config: RuntimeConfig) -> dict`, and one UUID-named pipeline JSON record directly below `runtime_root/runs`.
- Pipeline record fields: `schema_version: "agent_runtime.v1"`, `state_path`, `state_sha256`, `strategy_path`, `gate_path`, `runner_status`, `episode_id`, `episode_path`, `provider_mode`, and `execution` with both booleans false.

- [ ] **Step 1: Write failing Gate-deny and Gate-allow tests**

```python
class PipelineTests(unittest.TestCase):
    def test_gate_deny_skips_runner_and_closes_truthful_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_config(Path(directory))
            with mock.patch("services.soil3.agent_runtime.runtime_v1.write_state_snapshot",
                            return_value=denied_real_state()):
                record = run_pipeline(config)
            self.assertEqual("offline_fixture", record["provider_mode"])
            self.assertEqual("deny", record["gate_decision"])
            self.assertEqual("skipped_due_to_gate_deny", record["runner_status"])
            self.assertFalse(record["execution"]["physical_actions_performed"])
            episode = EpisodeStore(config.episode_dir).read(record["episode_id"])
            self.assertIn("executed_actions", episode["missing_facts"])
            self.assertEqual("closed", episode["status"])

    def test_gate_allow_runs_only_dry_run_and_records_nonphysical_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_config(Path(directory))
            with mock.patch("services.soil3.agent_runtime.runtime_v1.evaluate_gate",
                            return_value=allowing_gate()):
                record = run_pipeline(config)
            runner = load_json(Path(record["runner_path"]))
            self.assertEqual("dry_run", runner["mode"])
            self.assertFalse(runner["execution"]["phase3_called"])
            self.assertFalse(runner["execution"]["physical_actions_performed"])
```

- [ ] **Step 2: Run pipeline tests and verify failure**

Run: `python -m unittest tests.test_soil3_agent_runtime.PipelineTests -v`

Expected: `ImportError: cannot import name 'run_pipeline'`.

- [ ] **Step 3: Implement fixture creation and pipeline branching**

```python
def build_offline_fixture(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "device_code": state["device_code"],
        "state_observed_at": normalize_timestamp(state["observed_at"]),
        "state_generated_at": normalize_timestamp(state["generated_at"]),
        "state_sha256": fingerprint(state),
        "created_at": utc_now(),
        "actions": [{"action_id": "fixture-stop", "type": "stop"}],
        "reason_summary": ["offline_fixture_for_nonexecuting_chain_validation"],
        "expected_outcome": {"soil_moisture": "not_evaluated", "risk_notes": ["offline_fixture"]},
        "confidence": 0.0,
        "model": {"provider": "offline_fixture", "name": "offline_fixture", "prompt_version": PROMPT_VERSION},
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }

def run_pipeline(config: RuntimeConfig) -> dict[str, Any]:
    state = write_state_snapshot(config)
    strategy_result = run_chain(state=state, config=config.strategy_config(),
                                prompt=config.prompt_path.read_text(encoding="utf-8"),
                                fixture_content=json.dumps(build_offline_fixture(state)))
    strategy = strategy_result["validation"]["strategy"]
    gate = evaluate_gate(state, strategy, config.gate_policy, False)
    episode = EpisodeStore(config.episode_dir).create(state)
    episode = EpisodeStore(config.episode_dir).update(episode["episode_id"], strategy=strategy, gate_result=gate)
    if gate["decision"] == "deny":
        return close_denied_episode_and_write_pipeline_record(config, episode, state, strategy, gate)
    return run_dry_runner_and_write_pipeline_record(config, episode, state, strategy, gate)
```

Define the two branch helpers in `runtime_v1.py` with these exact signatures:

```python
def close_denied_episode_and_write_pipeline_record(
    config: RuntimeConfig, episode: dict[str, Any], state: dict[str, Any],
    strategy: dict[str, Any], gate: dict[str, Any],
) -> dict[str, Any]:
    closed = EpisodeStore(config.episode_dir).close(episode["episode_id"])
    return write_pipeline_record(config, state, strategy, gate, None, closed,
                                 "skipped_due_to_gate_deny")

def run_dry_runner_and_write_pipeline_record(
    config: RuntimeConfig, episode: dict[str, Any], state: dict[str, Any],
    strategy: dict[str, Any], gate: dict[str, Any],
) -> dict[str, Any]:
    runner = DryRunRunner(RunnerStore(config.runner_dir)).run(strategy, state)
    facts = [step["result"] for step in runner["steps"]
             if step.get("status") == "completed" and isinstance(step.get("result"), dict)]
    updated = EpisodeStore(config.episode_dir).update(
        episode["episode_id"], executed_actions=facts,
    )
    closed = EpisodeStore(config.episode_dir).close(updated["episode_id"])
    return write_pipeline_record(config, state, strategy, gate, runner, closed,
                                 "dry_run_completed")
```

`write_pipeline_record` must create a UUID-named JSON file atomically under
`config.runtime_root / "runs"`, include the fields stated in this task's
interface, and set `execution` to exactly
`{"physical_actions_performed": false, "phase3_called": false}`. The fixture
must be generated only after reading the real state and must call the existing
`run_chain`, so the existing prompt, binding, validator, audit and proposal-only
boundary are exercised. The code must reject a fixture result that the Validator
did not accept. Gate output is written atomically before any Runner branch. The
Gate-deny branch must not construct `DryRunRunner`, `RunnerStore`, a Phase3
client, MQTT client, or an executed-action entry. The allow branch may attach
only completed Runner step results; each persisted runner fact must include
`physical_action_performed: false` or be the exact `dry_run_stop` result.

Extend the CLI with `pipeline --config PATH`; print `run_id`, state path,
strategy acceptance, Gate decision, runner status, Episode path, and the two
false execution booleans. Return exit code zero for an expected Gate deny and
nonzero only for malformed/configuration/IO failures.

- [ ] **Step 4: Run pipeline tests and all Day1--Day3 focused tests**

Run: `python -m unittest tests.test_soil3_agent_runtime tests.test_plant_state_v1 tests.test_cloud_strategy tests.test_cloud_gate tests.test_strategy_runner_v1 tests.test_episode_v1 -v`

Expected: all pass; Gate-deny test proves no Runner record was created and Gate-allow test proves every Runner execution boolean is false.

- [ ] **Step 5: Commit the pipeline**

```bash
git add services/soil3/agent_runtime/runtime_v1.py services/soil3/agent_runtime/service.py \
  tests/test_soil3_agent_runtime.py
git commit -m "feat: add soil3 proposal-only runtime pipeline"
```

## Task 3: Add explicit runtime configuration and independently reversible systemd assets

**Files:**
- Create: `config/soil3_agent_runtime.example.json`
- Create: `services/soil3/agent_runtime/README.md`
- Create: `deploy/systemd/plant-agent-soil3-state.service`
- Create: `deploy/systemd/plant-agent-soil3-state.timer`
- Create: `deploy/systemd/plant-agent-soil3-pipeline.service`
- Create: `deploy/systemd/plant-agent-soil3-pipeline.timer`
- Modify: `tests/test_soil3_agent_runtime.py`

**Interfaces:**
- Consumes: `python3 -m services.soil3.agent_runtime.service state|pipeline --config /root/water/runtime/instances/soil3/agent_chain/config/runtime.json`.
- Produces: two timer/service pairs with names exactly matching this task and one copied production configuration whose provider mode is `offline_fixture`.

- [ ] **Step 1: Write failing configuration and unit-boundary tests**

```python
class DeploymentAssetTests(unittest.TestCase):
    def test_example_config_is_nonsecret_offline_fixture(self):
        value = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual("offline_fixture", value["provider_mode"])
        self.assertNotIn("api_key", json.dumps(value).lower())
        self.assertEqual(False, value["exploration_requested"])

    def test_systemd_units_do_not_name_phase3_or_mqtt_execution(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in UNIT_FILES)
        self.assertIn("Type=oneshot", source)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", source)
        self.assertNotIn("mqtt", source.lower())
        self.assertNotIn("manual_water", source.lower())
        self.assertNotIn("phase3/main.py", source)
```

- [ ] **Step 2: Run deployment-asset tests and verify failure**

Run: `python -m unittest tests.test_soil3_agent_runtime.DeploymentAssetTests -v`

Expected: `FileNotFoundError` for `config/soil3_agent_runtime.example.json`.

- [ ] **Step 3: Implement config, unit files, and operations README**

Use this exact example configuration shape; production paths are copied to the
new runtime directory during deployment, never committed with secrets:

```json
{
  "device_code": "soil3",
  "provider_mode": "offline_fixture",
  "exploration_requested": false,
  "phase3_state_path": "/root/water/runtime/instances/soil3/phase3/system_state.json",
  "sensor_log_path": "/root/water/runtime/instances/soil3/phase3/sensor_log.csv",
  "irrigation_trials_path": "/root/water/runtime/instances/soil3/phase3/irrigation_trials.json",
  "phase3_service_unit": "phase3_soil3.service",
  "runtime_root": "/root/water/runtime/instances/soil3/agent_chain",
  "state_output": "/root/water/runtime/instances/soil3/agent_chain/state/latest.json",
  "prompt_path": "services/soil3/cloud_strategy/prompts/strategy_v1.txt",
  "strategy_validator": {
    "max_actions": 12,
    "max_pump_seconds": 120,
    "max_wait_seconds": 86400,
    "max_total_pump_seconds": 240,
    "max_total_seconds": 86400
  },
  "gate_policy": {
    "warning_age_seconds": 900,
    "deny_age_seconds": 18000,
    "window_seconds": 86400,
    "max_exploration_water_seconds": 0
  }
}
```

The State service must set `WorkingDirectory` and `PYTHONPATH` to
`/root/water/releases/plant-intelligence-release`, use the copied runtime config,
and expose only `/root/water/runtime/instances/soil3/agent_chain` as a writable
location. The Pipeline service has `Requires=plant-agent-soil3-state.service`
and `After=plant-agent-soil3-state.service`; both units have
`NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=full`, and no
`Restart=` directive. Each timer uses `OnBootSec=2min`, `OnUnitActiveSec=5min`,
`Persistent=true`, and `Unit=` naming its matching service.

The README must give exact validation, enable, disable, and rollback commands:

```bash
systemctl daemon-reload
systemctl enable --now plant-agent-soil3-state.timer plant-agent-soil3-pipeline.timer
systemctl disable --now plant-agent-soil3-pipeline.timer plant-agent-soil3-state.timer
rm -f /etc/systemd/system/plant-agent-soil3-state.service /etc/systemd/system/plant-agent-soil3-state.timer \
  /etc/systemd/system/plant-agent-soil3-pipeline.service /etc/systemd/system/plant-agent-soil3-pipeline.timer
systemctl daemon-reload
```

The README must state that deleting the release/runtime directory is permitted
only after disabling the timers and confirming it is the agent-chain directory,
not a Phase3 directory.

- [ ] **Step 4: Run deployment-asset tests and full focused suite**

Run: `python -m unittest tests.test_soil3_agent_runtime tests.test_plant_state_v1 tests.test_cloud_strategy tests.test_cloud_gate tests.test_strategy_runner_v1 tests.test_episode_v1 -v`

Expected: all pass, with unit tests proving that no unit file names MQTT, manual water, or Phase3 execution code.

- [ ] **Step 5: Commit deployment assets**

```bash
git add config/soil3_agent_runtime.example.json services/soil3/agent_runtime/README.md \
  deploy/systemd/plant-agent-soil3-state.service deploy/systemd/plant-agent-soil3-state.timer \
  deploy/systemd/plant-agent-soil3-pipeline.service deploy/systemd/plant-agent-soil3-pipeline.timer \
  tests/test_soil3_agent_runtime.py
git commit -m "ops: add soil3 proposal-only runtime units"
```

## Task 4: Review, merge handoff, and controlled openEuler deployment

**Files:**
- Verify: every file changed in Tasks 1--3
- Verify after merge: `/root/water/releases/plant-intelligence-release/REVISION`
- Verify after merge: `/root/water/runtime/instances/soil3/agent_chain/`
- Verify after merge: `/etc/systemd/system/plant-agent-soil3-*.service` and `.timer`

**Interfaces:**
- Consumes: an Owner-merged `main` that contains Tasks 1--3 and a clean openEuler SSH session.
- Produces: a release stamped with its `main` commit, two enabled timers, and one factual pipeline record.

- [ ] **Step 1: Run review checks before creating the PR handoff**

Run:

```bash
git diff origin/main..HEAD --check
python -m unittest tests.test_soil3_agent_runtime tests.test_plant_state_v1 \
  tests.test_cloud_strategy tests.test_cloud_gate tests.test_strategy_runner_v1 tests.test_episode_v1 -v
rg -n "mqtt|manual_water|ActuatorLayer|phase3/main.py" services/soil3/agent_runtime deploy/systemd
```

Expected: no whitespace errors; all tests pass; the final search has no code-path matches other than explanatory boundary text in README.

- [ ] **Step 2: Commit any review-only corrections and submit the PR for Owner review**

```bash
git status --short
git log --oneline origin/main..HEAD
```

Report the new runtime package and unit assets, explicitly state that Phase3,
MQTT, Vision scheduling, and provider secrets were not changed, and wait for
the Owner to merge. Do not push or merge `main` without Owner direction.

- [ ] **Step 3: After Owner merge, resolve the deployment source from fresh main**

Run:

```bash
git fetch origin --prune
git rev-parse origin/main
git archive --format=tar origin/main | ssh openEuler-szut \
  'install -d -m 755 /root/water/releases/plant-intelligence-release && tar -x -C /root/water/releases/plant-intelligence-release'
```

Immediately write the resolved commit to
`/root/water/releases/plant-intelligence-release/REVISION`. Before installing
new units, read and record `systemctl is-active phase3_soil3.service`,
`mqtt_direct_gauss.service`, and `mqtt_logger3.service`; do not restart them.

- [ ] **Step 4: Install only the new agent-chain config and units**

Run these exact checks before enabling:

```bash
install -d -m 755 /root/water/runtime/instances/soil3/agent_chain/config
install -m 600 config/soil3_agent_runtime.example.json \
  /root/water/runtime/instances/soil3/agent_chain/config/runtime.json
install -m 644 deploy/systemd/plant-agent-soil3-state.service \
  deploy/systemd/plant-agent-soil3-state.timer \
  deploy/systemd/plant-agent-soil3-pipeline.service \
  deploy/systemd/plant-agent-soil3-pipeline.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/plant-agent-soil3-state.service \
  /etc/systemd/system/plant-agent-soil3-state.timer \
  /etc/systemd/system/plant-agent-soil3-pipeline.service \
  /etc/systemd/system/plant-agent-soil3-pipeline.timer
systemctl daemon-reload
```

The deployment executor must replace the example relative `prompt_path` in the
copied config with the absolute prompt path inside the selected release. It
must not add a provider URL, API key, MQTT variable, or Phase3 Bridge setting.

- [ ] **Step 5: Start one manual pass, inspect artifacts, then enable timers**

Run:

```bash
systemctl start plant-agent-soil3-pipeline.service
systemctl status --no-pager plant-agent-soil3-state.service plant-agent-soil3-pipeline.service
find /root/water/runtime/instances/soil3/agent_chain -maxdepth 3 -type f -print
systemctl enable --now plant-agent-soil3-state.timer plant-agent-soil3-pipeline.timer
```

Acceptance evidence must include the state snapshot schema/device/timestamps,
the strategy record marked `offline_fixture`, Gate decision and reasons, runner
status, closed Episode ID/path, and `missing_facts` when Gate denied. Also
recheck the three existing service states and show the runner/pipeline records'
`physical_actions_performed`, `phase3_called`, and Gate actuator permission are
all false. A Gate denial caused by current missing or protective facts is a
successful safety result.

- [ ] **Step 6: Verify independent rollback**

Run:

```bash
systemctl disable --now plant-agent-soil3-pipeline.timer plant-agent-soil3-state.timer
systemctl is-active phase3_soil3.service mqtt_direct_gauss.service mqtt_logger3.service
```

Expected: the two new timers are inactive; existing Phase3 and MQTT units stay
active. Re-enable the new timers only after this check passes. Do not remove
the release or runtime artifacts during acceptance; preserve them for the
Owner's audit.

## Plan Self-Review

- Spec coverage: Tasks 1--3 implement the versioned-baseline runtime, real fact
  preservation, offline provider, Gate deny semantics, Runner isolation,
  Episode trace, systemd separation, and rollback. Task 4 covers merge-gated
  deployment and physical-boundary evidence.
- Placeholder scan: every creation path, interface, configuration key, command,
  test command, and expected result is specified above. No deferred task is
  required to make another task's interface defined.
- Type consistency: `RuntimeConfig`, `write_state_snapshot`,
  `build_offline_fixture`, and `run_pipeline` are defined in Task 1/2 before
  later tasks consume them. All runtime paths derive from `RuntimeConfig`.
