# soil3 Feedback Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Persist real watering evidence, collect four delayed factual feedback windows, close the corresponding Episode at 24 hours, and make only confirmed causal outcomes available to Experience Retrieval.

**Architecture:** A write-ahead feedback_action_receipt.v1 sidecar is emitted by native Phase3, Controlled Execution, or a non-executing manual-confirmation CLI. A separate timer-driven Collector consumes only durable receipts, creates or reuses Episodes, records existing feedback.v1 observations, then finalizes with the existing FeedbackStore. Experience Retrieval stays read-only and accepts feedback Outcomes only when their parent Episode has exactly one confirmed physical action.

**Tech Stack:** Python 3.9+, existing EpisodeStore, FeedbackStore, StateBuilder, build_health_snapshot, Vision public API, unittest, systemd oneshot service and timer.

**Spec:** docs/superpowers/specs/2026-10-08-soil3-feedback-collector-design.md

## Global Constraints

- Keep state.v1, strategy.v1, gate.v2, Runner, Bridge, and feedback.v1 payload fields unchanged.
- Do not add a direct MQTT, GPIO, ActuatorLayer, or manual_water path outside existing Phase3.
- Native Phase3 MQTT on/off success is command_completed, never physical-device confirmation.
- Persist a receipt before native Phase3 can publish MQTT on; if that write fails, do not publish on.
- The Collector consumes receipt files only; it must not infer actions from pending_soak, logs, timestamps, or Shadow Episodes.
- Native actions create independent Episodes. Controlled actions reuse only their verified Episode binding.
- experience.enabled stays false; no deployment, board execution, or Day-level acceptance is part of this Issue.
- Use canonical telemetry via the existing state/health adapters; never fall back to CSV as a soil source.
- All new runtime writes are atomic. A restart or repeated timer invocation must not create a second action, Feedback record, or close operation.
- Support Python 3.9 syntax and standard library only.

---

## File structure

| File | Responsibility |
| --- | --- |
| services/soil3/feedback_collector/action_receipt_v1.py | Validates, creates, transitions, and references durable action receipts. |
| services/soil3/feedback_collector/collector_v1.py | Scans receipts, persists schedules, captures State/Vision, writes Feedback, and finalizes Episodes. |
| services/soil3/feedback_collector/service.py | Non-executing CLI for collection and manual confirmation. |
| services/soil3/feedback_collector/__init__.py | Public exports. |
| services/soil3/feedback_collector/README.md | Runtime and safety-boundary instructions. |
| services/soil3/phase3/decision_brain.py | Minimal native write-ahead receipt calls around the existing pump operation. |
| services/soil3/controlled_execution/controlled_v1.py | Controlled receipt write-ahead/update around its formal run_cycle handoff. |
| services/soil3/experience_retrieval/retrieval_v1.py | Feedback Outcome classification gated by confirmed parent action evidence. |
| deploy/systemd/plant-agent-soil3-feedback-collector.service | Isolated one-shot collector service. |
| deploy/systemd/plant-agent-soil3-feedback-collector.timer | Independent five-minute persistent schedule. |
| tests/test_feedback_action_receipt_v1.py | Receipt lifecycle and manual-record tests. |
| tests/test_phase3_feedback_receipt.py | Native Phase3 sequencing tests with MQTT mocked. |
| tests/test_feedback_collector_v1.py | Windows, restart, Episode, Vision, and close tests. |
| tests/test_controlled_phase3_execution.py | Controlled receipt regression tests. |
| tests/test_experience_retrieval_v1.py | Confirmed-action feedback retrieval tests. |

## Task 1: Create the durable receipt store

**Files:**
- Create: services/soil3/feedback_collector/__init__.py
- Create: services/soil3/feedback_collector/action_receipt_v1.py
- Test: tests/test_feedback_action_receipt_v1.py

**Interfaces:**
- Produces ActionReceiptStore(root_dir: str | Path).
- Produces prepare_native(action_id, initial_state, pump_seconds, created_at) -> dict.
- Produces prepare_controlled(action_id, initial_state, trace_id, episode_id, approval_id, created_at) -> dict.
- Produces record_manual(action_id, initial_state, confirmed_by, reference_action_at, pump_seconds) -> dict.
- Produces complete_command(action_id, reference_action_at, on_published_at, off_published_at, pump_seconds) -> dict.
- Produces mark_incomplete(action_id, reason) -> dict, read(action_id) -> dict, iter_receipts() -> Iterator[dict], and artifact_ref(action_id) -> dict.

- [ ] **Step 1: Write failing receipt lifecycle tests**

    class ActionReceiptStoreTests(unittest.TestCase):
        def test_native_prepare_and_completion_keep_one_action_id(self):
            first = self.store.prepare_native(ACTION_ID, valid_state(), 8.0, ISO)
            self.assertEqual("intent_only", first["execution_evidence"]["level"])
            last = self.store.complete_command(ACTION_ID, ISO_END, ISO, ISO_END, 8.0)
            self.assertEqual(ACTION_ID, last["action_id"])
            self.assertEqual("command_completed", last["command_status"])
            self.assertFalse(last["execution_evidence"]["physical_action_confirmed"])

        def test_manual_same_id_is_idempotent_only_for_identical_facts(self):
            first = self.store.record_manual(ACTION_ID, valid_state(), "owner", ISO, 5.0)
            self.assertEqual(first, self.store.record_manual(ACTION_ID, valid_state(), "owner", ISO, 5.0))
            with self.assertRaises(ActionReceiptError):
                self.store.record_manual(ACTION_ID, valid_state(), "owner", ISO, 6.0)

- [ ] **Step 2: Run the focused test and verify it fails**

Run: python -m unittest tests.test_feedback_action_receipt_v1 -v

Expected: FAIL because the feedback_collector package does not exist.

- [ ] **Step 3: Implement schema validation and create-if-absent persistence**

    SCHEMA_VERSION = "feedback_action_receipt.v1"
    ACTION_ID_PATTERN = re.compile(r"^act-[0-9a-f]{24}$")

    class ActionReceiptStore:
        def prepare_native(self, action_id, initial_state, pump_seconds, created_at):
            return self._create_once({
                "schema_version": SCHEMA_VERSION,
                "action_id": action_id,
                "device_code": "soil3",
                "action_source": "phase3_native",
                "created_at": created_at,
                "reference_action_at": None,
                "command": {"kind": "water", "pump_seconds": pump_seconds},
                "command_status": "prepared",
                "execution_evidence": {"level": "intent_only", "physical_action_confirmed": False},
                "episode_binding": {"mode": "create_independent", "episode_id": None},
                "initial_state": copy.deepcopy(initial_state),
                "trace_id": None,
                "source_receipt_ref": None,
            })

Validate exact fields, IDs, timestamps, finite positive durations, state.v1, and source-specific binding before every write. The first write is exclusive creation; later transitions atomically replace the same file. A missing receipt is never interpreted as a command outcome.

- [ ] **Step 4: Add invalid-input and restart-state tests**

    def test_prepared_receipt_survives_restart_but_is_not_completed(self):
        self.store.prepare_native(ACTION_ID, valid_state(), 8.0, ISO)
        reopened = ActionReceiptStore(self.root)
        self.assertEqual("prepared", reopened.read(ACTION_ID)["command_status"])

    def test_invalid_state_leaves_no_receipt_file(self):
        with self.assertRaises(ActionReceiptError):
            self.store.prepare_native(ACTION_ID, {"schema_version": "bad"}, 8.0, ISO)
        self.assertEqual([], list(self.store.iter_receipts()))

- [ ] **Step 5: Run focused tests and commit**

Run: python -m unittest tests.test_feedback_action_receipt_v1 -v

Expected: PASS.

    git add services/soil3/feedback_collector/__init__.py services/soil3/feedback_collector/action_receipt_v1.py tests/test_feedback_action_receipt_v1.py
    git commit -m "feat: add durable feedback action receipts"

## Task 2: Add native Phase3 write-ahead receipt emission

**Files:**
- Modify: services/soil3/phase3/decision_brain.py in ActuatorLayer.execute_pump
- Create: tests/test_phase3_feedback_receipt.py
- Test: tests/test_feedback_action_receipt_v1.py

**Interfaces:**
- Consumes Task 1 ActionReceiptStore.
- Produces exactly one receipt before the existing _activate_pump call and updates the same receipt after that call returns or raises.

- [ ] **Step 1: Write failing ordering tests**

    def test_receipt_is_prepared_before_existing_pump_activation(self):
        events = []
        actuator = configured_actuator(events)
        actuator.execute_pump(8.0, 31.0)
        self.assertEqual(["prepare", "activate", "complete"], events)

    def test_prepare_failure_prevents_mqtt_activation(self):
        actuator = configured_actuator([])
        actuator.receipt_store.prepare_native = Mock(side_effect=OSError("disk full"))
        with self.assertRaises(OSError):
            actuator.execute_pump(8.0, 31.0)
        actuator._activate_pump.assert_not_called()

- [ ] **Step 2: Run the Phase3 focused test and verify it fails**

Run: python -m unittest tests.test_phase3_feedback_receipt -v

Expected: FAIL because ActuatorLayer has no receipt calls.

- [ ] **Step 3: Add only the receipt adapter around the existing call**

Use build_health_snapshot plus StateBuilder("soil3").build_from_health_snapshot to capture the actual pre-command state. Generate one action ID and persist it before calling _activate_pump. Retain the existing duration selection, safety paths, MQTT payloads, and actuator sequencing.

    receipt = self._receipt_store.prepare_native(
        new_action_id(), self._capture_action_state(), water_sec, utc_now()
    )
    try:
        self._activate_pump(water_sec)
    except Exception as error:
        self._receipt_store.mark_incomplete(receipt["action_id"], type(error).__name__)
        raise
    self._receipt_store.complete_command(
        receipt["action_id"], utc_now(), receipt["created_at"], utc_now(), water_sec
    )

A zero-second silent action creates no receipt. Do not retry a failed physical command.

- [ ] **Step 4: Add error-boundary regression tests**

    def test_mqtt_error_marks_one_existing_receipt_incomplete(self):
        actuator._activate_pump = Mock(side_effect=PumpExecutionError("off failed"))
        with self.assertRaises(PumpExecutionError):
            actuator.execute_pump(8.0, 31.0)
        self.assertEqual("command_incomplete", only_receipt(actuator)["command_status"])

    def test_existing_mqtt_payload_sequence_remains_on_then_off(self):
        actuator.execute_pump(8.0, 31.0)
        self.assertEqual(["on", "off"], published_payloads())

- [ ] **Step 5: Run focused tests and commit**

Run: python -m unittest tests.test_phase3_feedback_receipt tests.test_feedback_action_receipt_v1 -v

Expected: PASS.

    git add services/soil3/phase3/decision_brain.py tests/test_phase3_feedback_receipt.py
    git commit -m "feat: journal native phase3 feedback actions"

## Task 3: Add controlled and manual receipt producers

**Files:**
- Modify: services/soil3/controlled_execution/controlled_v1.py in ControlledPhase3Executor.execute
- Create: services/soil3/feedback_collector/service.py
- Modify: services/soil3/feedback_collector/__init__.py
- Modify: tests/test_controlled_phase3_execution.py
- Modify: tests/test_feedback_action_receipt_v1.py

**Interfaces:**
- Consumes Task 1 ActionReceiptStore.
- Produces a controlled receipt deterministically named from approval_id and bound to its verified trace_id and episode_id.
- Produces python -m services.soil3.feedback_collector.service manual-record with required receipt directory, action id, confirmer, action time, duration, and state source configuration.

- [ ] **Step 1: Write failing controlled/manual tests**

    def test_controlled_receipt_reuses_verified_trace_and_episode(self):
        executor.execute(trace_dir, approval, formal_brain)
        receipt = store.read(controlled_action_id(approval["approval_id"]))
        self.assertEqual(approval["trace_id"], receipt["trace_id"])
        self.assertEqual(approval["episode_id"], receipt["episode_binding"]["episode_id"])

    def test_manual_cli_is_fact_only(self):
        result = run_manual_cli(ACTION_ID)
        self.assertEqual(0, result.returncode)
        self.assertNotIn("manual_water", imported_modules())
        self.assertNotIn("paho.mqtt", imported_modules())

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: python -m unittest tests.test_controlled_phase3_execution tests.test_feedback_action_receipt_v1 -v

Expected: FAIL because neither producer writes the receipt.

- [ ] **Step 3: Implement controlled write-ahead and manual record CLI**

After _load_verified_chain succeeds but before _invoke_formal_phase3_cycle, create a receipt with action id deterministically derived from approval_id. Update that same receipt after Phase3 returns. The positive returned duration is still command_completed, not physical confirmation.

    action_id = controlled_action_id(approval["approval_id"])
    receipt_store.prepare_controlled(
        action_id, state, approval["trace_id"], approval["episode_id"],
        approval["approval_id"], iso_utc(now)
    )
    result = _invoke_formal_phase3_cycle(decision_brain)
    receipt_store.complete_command(action_id, finished_at, started_at, finished_at, action_sec)

The CLI builds one State snapshot, calls record_manual only, prints the action ID, and refuses conflicting reuse of an ID. It must not import services.soil3.phase3.

- [ ] **Step 4: Add retry and conflicting-confirmation tests**

    def test_controlled_failure_keeps_one_incomplete_receipt_and_cannot_replay(self):
        with self.assertRaises(ControlledExecutionError):
            executor.execute(trace_dir, approval, failing_formal_brain)
        self.assertEqual("command_incomplete", store.read(controlled_action_id(approval["approval_id"]))["command_status"])
        with self.assertRaises(ControlledExecutionError):
            executor.execute(trace_dir, approval, formal_brain)

    def test_manual_conflicting_id_reuse_is_refused(self):
        self.assertNotEqual(0, run_manual_cli(ACTION_ID, confirmed_by="other").returncode)

- [ ] **Step 5: Run focused tests and commit**

Run: python -m unittest tests.test_controlled_phase3_execution tests.test_feedback_action_receipt_v1 -v

Expected: PASS.

    git add services/soil3/controlled_execution/controlled_v1.py services/soil3/feedback_collector/service.py services/soil3/feedback_collector/__init__.py tests/test_controlled_phase3_execution.py tests/test_feedback_action_receipt_v1.py
    git commit -m "feat: record controlled and manual feedback actions"

## Task 4: Implement receipt-only Collector scheduling and binding

**Files:**
- Create: services/soil3/feedback_collector/collector_v1.py
- Create: tests/test_feedback_collector_v1.py
- Modify: services/soil3/feedback_collector/__init__.py

**Interfaces:**
- Produces CollectorConfig.from_dict(value) -> CollectorConfig.
- Produces FeedbackCollector(config, clock=utc_now).run_once() -> dict.
- Produces capture_current_state(config) -> dict and due_window(reference_action_at, now) -> str | None.

- [ ] **Step 1: Write failing receipt-only and deduplication tests**

    def test_shadow_episode_without_receipt_never_creates_tracking(self):
        EpisodeStore(self.episode_dir).create(valid_state())
        self.assertEqual(0, self.collector.run_once()["tracked_actions"])
        self.assertEqual([], list(Path(self.feedback_dir).glob("fb-*.json")))

    def test_native_receipt_creates_one_independent_episode(self):
        self.receipts.complete_command(ACTION_ID, AT, ON, OFF, 8.0)
        self.collector.run_once()
        self.collector.run_once()
        episodes = list(Path(self.episode_dir).glob("ep-*.json"))
        self.assertEqual(1, len(episodes))
        self.assertEqual("phase3_native", read_episode(episodes[0])["executed_actions"][0]["action_source"])

- [ ] **Step 2: Run collector tests and verify they fail**

Run: python -m unittest tests.test_feedback_collector_v1 -v

Expected: FAIL because FeedbackCollector does not exist.

- [ ] **Step 3: Implement receipt filtering, tracking state, and Episode binding**

    COLLECTABLE_LEVELS = {"command_completed", "manual_confirmed", "device_confirmed"}

    def run_once(self):
        for receipt in self.receipts.iter_receipts():
            if receipt["execution_evidence"]["level"] not in COLLECTABLE_LEVELS:
                continue
            tracking = self._load_or_create_tracking(receipt)
            self._ensure_episode(receipt, tracking)
            self._collect_due_windows(receipt, tracking)

Native/manual receipts create an Episode from receipt initial_state. Controlled receipts load only their explicit Episode id. Append one action fact containing action_id, source, executed_at, duration, evidence, and a hash-validated receipt reference. Store one collector state file per action id with the Episode id and per-window states pending, recorded, or missed.

capture_current_state calls build_health_snapshot and StateBuilder("soil3").build_from_health_snapshot in memory. It does not read pending_soak, a Shadow state artifact, a log, or a CSV soil fallback.

- [ ] **Step 4: Add restart and missed-window tests**

    def test_restart_preserves_recorded_window_and_collects_next_window_once(self):
        self.clock.set(plus_minutes(REFERENCE, 30))
        self.collector.run_once()
        restarted = FeedbackCollector(self.config, clock=self.clock.now)
        self.clock.set(plus_minutes(REFERENCE, 150))
        restarted.run_once()
        self.assertEqual(["30min", "2-3h"], feedback_windows(self.feedback_dir))

    def test_missed_window_is_not_backfilled(self):
        self.clock.set(plus_minutes(REFERENCE, 70))
        self.collector.run_once()
        self.assertNotIn("30min", feedback_windows(self.feedback_dir))
        self.assertEqual("missed", tracking(ACTION_ID)["windows"]["30min"])

- [ ] **Step 5: Run focused tests and commit**

Run: python -m unittest tests.test_feedback_collector_v1 -v

Expected: PASS.

    git add services/soil3/feedback_collector/collector_v1.py services/soil3/feedback_collector/__init__.py tests/test_feedback_collector_v1.py
    git commit -m "feat: schedule receipt-bound feedback collection"

## Task 5: Capture feedback, optional Vision, and Outcome closure

**Files:**
- Modify: services/soil3/feedback_collector/collector_v1.py
- Modify: tests/test_feedback_collector_v1.py
- Test: tests/test_feedback_v1.py

**Interfaces:**
- Consumes Task 4 Collector, existing FeedbackStore, and public Vision API.
- Produces one feedback.v1 record per action/window and one closed Episode after the final 24-hour window or its expiry.

- [ ] **Step 1: Write failing factual-capture and closure tests**

    def test_due_window_records_state_and_null_vision_when_unavailable(self):
        self.clock.set(plus_minutes(REFERENCE, 30))
        self.collector.run_once()
        record = only_feedback(self.feedback_dir)
        self.assertEqual(REFERENCE, record["reference_action_at"])
        self.assertEqual("state.v1", record["observations"]["soil"]["schema_version"])
        self.assertIsNone(record["observations"]["vision"])

    def test_24h_finalizes_once_and_keeps_missed_windows(self):
        self.clock.set(plus_minutes(REFERENCE, 1440))
        self.collector.run_once()
        episode = tracked_episode(ACTION_ID)
        self.assertEqual("closed", episode["status"])
        self.assertIn("2-3h", episode["outcome"]["windows_missing"])
        self.collector.run_once()
        self.assertEqual("closed", EpisodeStore(self.episode_dir).read(episode["episode_id"])["status"])

- [ ] **Step 2: Run focused tests and verify they fail**

Run: python -m unittest tests.test_feedback_collector_v1 -v

Expected: FAIL because the Collector does not create feedback.v1 or close an Episode.

- [ ] **Step 3: Build factual payloads and attach through FeedbackStore**

    def _feedback_payload(receipt, episode_id, window, state, vision):
        humidity = state["soil"]["humidity_percent"]
        target_low = state["safety"]["target_low"]
        field_capacity = state["safety"]["field_capacity"]
        dry = humidity <= target_low if all_numbers(humidity, target_low) else "unknown"
        wet = humidity >= field_capacity if all_numbers(humidity, field_capacity) else "unknown"
        recovery = "poor" if True in (dry, wet) else ("good" if dry is False and wet is False else "unknown")
        return {
            "device_code": "soil3", "episode_id": episode_id, "window": window,
            "observed_at": state["observed_at"], "reference_action_at": receipt["reference_action_at"],
            "observations": {"soil": state, "vision": vision},
            "assessments": {"recovery": recovery, "sustained_dry": dry, "sustained_wet": wet,
                            "rewater_needed": dry, "visual_recovery": "unknown", "data_quality": "unknown"},
        }

Use capture_and_analyze_once and public manifest/observation validation before storing Vision facts. Any capture, configuration, import, file, hash, or validation error returns None and continues soil collection. Attach every accepted record with finalize=False. When the last window is recorded or marked missed, call attach_to_episode with finalize=True exactly once.

- [ ] **Step 4: Add Vision and duplicate-window tests**

    def test_valid_vision_manifest_is_saved_as_fact(self):
        self.vision.return_value = valid_vision_run()
        self.clock.set(plus_minutes(REFERENCE, 30))
        self.collector.run_once()
        self.assertEqual("vision.v1", only_feedback(self.feedback_dir)["observations"]["vision"][0]["schema_version"])

    def test_repeated_due_tick_does_not_write_a_second_feedback_file(self):
        self.clock.set(plus_minutes(REFERENCE, 30))
        self.collector.run_once()
        self.collector.run_once()
        self.assertEqual(1, len(list(Path(self.feedback_dir).glob("fb-*.json"))))

- [ ] **Step 5: Run feedback regressions and commit**

Run: python -m unittest tests.test_feedback_collector_v1 tests.test_feedback_v1 -v

Expected: PASS.

    git add services/soil3/feedback_collector/collector_v1.py tests/test_feedback_collector_v1.py
    git commit -m "feat: collect and finalize soil3 action feedback"

## Task 6: Make only confirmed feedback Outcomes retrievable

**Files:**
- Modify: services/soil3/experience_retrieval/retrieval_v1.py
- Modify: tests/test_experience_retrieval_v1.py

**Interfaces:**
- Consumes closed Episodes with feedback.v1 Outcomes and executed_actions.
- Produces good -> success and poor/none -> failure only when exactly one parent action has manual_confirmed or device_confirmed evidence.

- [ ] **Step 1: Write failing causal-evidence tests**

    def test_confirmed_feedback_outcome_is_ranked(self):
        write_closed_feedback_episode(self.dir, recovery="good", evidence="manual_confirmed")
        result = self.retriever.retrieve(valid_state())
        self.assertEqual(1, len(result["matches"]["success"]))

    def test_command_completed_feedback_is_not_causal_experience(self):
        write_closed_feedback_episode(self.dir, recovery="good", evidence="command_completed")
        result = self.retriever.retrieve(valid_state())
        self.assertEqual([], result["matches"]["success"])
        self.assertEqual(1, result["skipped"]["outcome_unclassified"])

- [ ] **Step 2: Run retrieval tests and verify they fail**

Run: python -m unittest tests.test_experience_retrieval_v1 -v

Expected: FAIL because feedback Outcomes are not classified or evidence-gated.

- [ ] **Step 3: Implement conservative feedback classification**

    def _confirmed_feedback_action(episode):
        actions = [
            entry for entry in episode.get("executed_actions", [])
            if isinstance(entry, dict)
            and entry.get("execution_evidence", {}).get("level") in {"manual_confirmed", "device_confirmed"}
        ]
        return actions[0] if len(actions) == 1 else None

    def _classify_feedback_outcome(outcome):
        if outcome.get("source_schema") != "feedback.v1" or outcome.get("contradictions"):
            return None
        return {"good": "success", "poor": "failure", "none": "failure"}.get(
            outcome.get("assessments", {}).get("recovery")
        )

Preserve existing scalar Outcome classification for all non-feedback Episodes unchanged.

- [ ] **Step 4: Add conflict and multiple-confirmed-action test**

    def test_conflicted_feedback_or_two_confirmed_actions_are_skipped(self):
        write_closed_feedback_episode(
            self.dir, recovery="good", evidence="manual_confirmed",
            contradictions=["sustained_dry_and_wet"], extra_confirmed_action=True
        )
        self.assertEqual([], self.retriever.retrieve(valid_state())["matches"]["success"])

- [ ] **Step 5: Run regression tests and commit**

Run: python -m unittest tests.test_experience_retrieval_v1 -v

Expected: PASS.

    git add services/soil3/experience_retrieval/retrieval_v1.py tests/test_experience_retrieval_v1.py
    git commit -m "feat: retrieve confirmed feedback outcomes"

## Task 7: Add service, timer, documentation, and final acceptance

**Files:**
- Create: deploy/systemd/plant-agent-soil3-feedback-collector.service
- Create: deploy/systemd/plant-agent-soil3-feedback-collector.timer
- Create: services/soil3/feedback_collector/README.md
- Modify: tests/test_feedback_collector_v1.py

**Interfaces:**
- Consumes python -m services.soil3.feedback_collector.service collect --config /root/water/runtime/instances/soil3/agent_chain/config/feedback-collector.json.
- Produces an isolated five-minute persistent Collector service without execution capability.

- [ ] **Step 1: Write failing unit-file boundary tests**

    def test_collector_unit_has_no_pipeline_or_execution_dependency(self):
        unit = (ROOT / "deploy/systemd/plant-agent-soil3-feedback-collector.service").read_text()
        self.assertIn("Type=oneshot", unit)
        self.assertIn("ReadWritePaths=/root/water/runtime/instances/soil3/agent_chain", unit)
        self.assertNotIn("plant-agent-soil3-pipeline.service", unit)
        self.assertNotIn("mqtt", unit.lower())

    def test_collector_timer_is_persistent_and_five_minute(self):
        timer = (ROOT / "deploy/systemd/plant-agent-soil3-feedback-collector.timer").read_text()
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertIn("Persistent=true", timer)

- [ ] **Step 2: Run static tests and verify they fail**

Run: python -m unittest tests.test_feedback_collector_v1 -v

Expected: FAIL because the unit and timer files do not exist.

- [ ] **Step 3: Create isolated runtime artifacts**

    [Service]
    Type=oneshot
    User=root
    WorkingDirectory=/root/water/releases/plant-intelligence-release
    Environment=PYTHONDONTWRITEBYTECODE=1
    Environment=PYTHONPATH=/root/water/releases/plant-intelligence-release
    ExecStart=/usr/bin/python3 -m services.soil3.feedback_collector.service collect --config /root/water/runtime/instances/soil3/agent_chain/config/feedback-collector.json
    NoNewPrivileges=true
    PrivateTmp=true
    ProtectSystem=full
    ReadOnlyPaths=/root/water
    ReadWritePaths=/root/water/runtime/instances/soil3/agent_chain

The README states receipt evidence levels, the exact manual-record invocation, required Collector config paths, and that this Issue does not deploy, actuate, or enable Experience.

- [ ] **Step 4: Run focused and related acceptance suites**

Run: python -m unittest tests.test_feedback_action_receipt_v1 tests.test_phase3_feedback_receipt tests.test_feedback_collector_v1 -v

Expected: PASS.

Run: python -m unittest tests.test_feedback_v1 tests.test_episode_v1 tests.test_experience_retrieval_v1 tests.test_controlled_phase3_execution tests.test_soil3_agent_runtime -v

Expected: PASS, including Shadow runtime remaining non-executing.

Run: python -m unittest discover -s tests -v

Expected: PASS when supported locally; otherwise report the exact unavailable dependency.

Run: git diff --check origin/main...HEAD

Expected: no output.

- [ ] **Step 5: Review scope and commit**

    git diff --name-only origin/main...HEAD
    git status --short
    git add deploy/systemd/plant-agent-soil3-feedback-collector.service deploy/systemd/plant-agent-soil3-feedback-collector.timer services/soil3/feedback_collector/README.md tests/test_feedback_collector_v1.py docs/superpowers/specs/2026-10-08-soil3-feedback-collector-design.md docs/superpowers/plans/2026-10-09-soil3-feedback-collector.md
    git commit -m "docs: add feedback collector runtime guide"

Do not push, merge, deploy, enable experience.enabled, or conduct a board run. Hand the finished branch to independent PR Review with focused and full test results plus the explicit non-execution boundary.

## Self-review

### Spec coverage

- Action provenance, command/physical evidence, stable IDs, and crash recovery: Tasks 1-3.
- No Shadow inference, independent native Episodes, and controlled binding reuse: Task 4.
- Canonical State, optional Vision, all windows, duplicate protection, missing windows, Outcome, and close: Tasks 4-5.
- Feedback/Outcome compatibility and confirmed Experience eligibility: Task 6.
- Separate service/timer, documentation, and all required regressions: Task 7.

### Placeholder scan

Each task lists concrete files, public interfaces, failing test bodies, commands, implementation logic, expected test state, and a commit boundary. No action is deferred outside this plan.

### Type consistency

Every producer and the Collector use ActionReceiptStore, feedback_action_receipt.v1, action_id, and the evidence levels created in Task 1. Task 4 writes the action facts consumed by Task 6. Task 5 uses only the existing FeedbackStore and EpisodeStore attachment interfaces.
