# Soil3 Production Repository Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the imported multi-device repository with a reviewed, source-controlled, soil3-only representation of the openEuler production code.

**Architecture:** Treat the openEuler host as the source baseline, but copy only source and sanitized deployment templates. Preserve the active Phase3 safety boundary; Phase2 remains a candidate-trajectory predictor and never publishes MQTT. The resulting repository contains no production state, logs, credentials, OpenClaw execution path, or soil1/soil2/soil_test business code.

**Tech Stack:** Python 3, systemd templates, cron documentation, MQTT, openGauss, JSON/CSV runtime state.

**Spec:** `docs/植物智能体云端大模型接入与边云协同计划书_V3.md`

## Global Constraints

- Cloud components may generate `StrategyRequest` only; they have no MQTT publish permission.
- Execution remains `StrategyRequest -> Validator -> Cloud Gate -> Phase3 -> ActionPlan`.
- Phase3 retains the final hard-safety and pump authority.
- Do not commit secrets, RTSP URLs, runtime JSON/CSV/SQLite state, logs, backups, or Python bytecode.
- Keep automated tests, but remove soil1, soil2, and soil_test device code and tests.
- OpenClaw is not a dependency of the new visual or cloud-model paths and its watering tools are not retained.

---

### Task 1: Stage and classify the openEuler source baseline

**Files:**
- Create: temporary local staging directory outside the Git working tree
- Source: `/root/water/phase1_test/water-test-soil3.py`
- Source: `/root/water/wyc/phase2_predictor/*.py`
- Source: `/root/water/phase3/soil3/{main,decision_brain,config_manager,runtime_io,auditor,adaptive_evidence,experience_validation}.py`
- Source: `/root/water/wyc/IOT/{iot_agent/*.py,cloud_agent.py,functiongraph/index.py,requirements.txt,README.md}`

- [ ] **Step 1: Copy only named source files through read-only SSH/SCP**

Run: copy the listed files to a temporary staging directory, excluding `*.bak*`, `*.pyc`, `*.log`, `*.json`, `*.csv`, `*.db`, lock files, and environment files.

Expected: staging contains only reviewable Python, documentation, and dependency files.

- [ ] **Step 2: Scan staged content for credential-bearing keys and private endpoints**

Run: `rg -n -i '(password|secret|token|api[_-]?key|rtsp://|mqtts?://)' <staging>`

Expected: no runtime secret is imported; non-secret variable names may remain with example placeholders only.

- [ ] **Step 3: Record the source mapping in repository documentation**

Create `docs/PRODUCTION_BASELINE.md` with source path, role, activity state, and import time for every retained component.

### Task 2: Replace the old multi-device source tree

**Files:**
- Create: `services/soil3/phase1/water_test_soil3.py`
- Create: `services/soil3/phase2_predictor/*.py`
- Create: `services/soil3/phase3/*.py`
- Create: `services/soil3/iot_agent/**/*.py`
- Delete: old `agents/`, `experiments/`, legacy `services/control/`, soil1/soil2/soil_test tests, and other imported multi-device business code

- [ ] **Step 1: Preserve the untracked V3 plan before deleting tracked content**

Run: `git status --short`

Expected: `docs/植物智能体云端大模型接入与边云协同计划书_V3.md` remains present and is added to the rebuilt repository.

- [ ] **Step 2: Remove tracked legacy business files using an explicit Git path list**

Run: `git rm -r agents config deployment experiments firmware scripts services tests web README.md` only after the replacement files are staged outside those paths.

Expected: no soil1, soil2, soil_test, OpenClaw, runtime backup, or legacy multi-device source remains tracked.

- [ ] **Step 3: Add the classified soil3-only source tree**

Place source files according to the paths above. Rename only filenames needed to make the repository layout clear; do not alter Phase3 decision logic during this import.

Expected: `services/soil3/phase1`, `services/soil3/phase2_predictor`, and `services/soil3/phase3` each have explicit, non-overlapping responsibilities.

### Task 3: Add safe deployment and runtime contracts

**Files:**
- Create: `ops/systemd/phase3_soil3.service.example`
- Create: `ops/systemd/wyc-phase2.service.example`
- Create: `ops/cron/soil3-crontab.example`
- Create: `config/soil3.example.json`
- Create: `.gitignore`

- [ ] **Step 1: Create systemd examples without secrets**

Include the real Phase3 entry point and dependency ordering, but omit SMTP and all `EnvironmentFile` values. Mark Phase2 as optional because it was inactive during baseline inspection.

- [ ] **Step 2: Create a soil3-only configuration example**

Use placeholders for hosts, topics, database credentials, and camera endpoints. Include `device_code: soil3`; do not include soil1, soil2, or soil_test configuration.

- [ ] **Step 3: Enforce runtime-file exclusion**

Add patterns for `.env`, `*.log`, `*.db`, `*.db-wal`, `*.db-shm`, `*.lock`, `*.pyc`, `__pycache__/`, `*.bak*`, and runtime state files to `.gitignore`.

### Task 4: Retain only soil3 tests and establish import verification

**Files:**
- Create: `tests/phase3/test_experience_validation.py`
- Create: `tests/phase3/test_experience_wiring.py`
- Create: `tests/phase2/`
- Delete: soil1, soil2, and soil_test tests

- [ ] **Step 1: Copy the two production soil3 Phase3 tests**

Run: `pytest tests/phase3 -q`

Expected: copied tests collect without importing removed soil1/soil2/soil_test modules.

- [ ] **Step 2: Add an import-boundary test**

```python
def test_repository_has_no_legacy_device_packages():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert not any(root.rglob("*soil1*"))
    assert not any(root.rglob("*soil2*"))
    assert not any(root.rglob("*soil_test*"))
```

- [ ] **Step 3: Run static import compilation**

Run: `python -m compileall services tests`

Expected: all retained Python modules compile.

### Task 5: Document the production baseline and publish the replacement

**Files:**
- Create: `README.md`
- Create: `docs/PRODUCTION_BASELINE.md`
- Modify: GitHub issue links in relevant documentation

- [ ] **Step 1: Write a source-of-truth README**

Document the active Phase3 service, inactive Phase1/Phase2 services at inspection time, the shared-memory Phase2 protocol, canonical data direction, and the non-OpenClaw cloud/vision boundary.

- [ ] **Step 2: Verify the final Git index**

Run: `git diff --check`, `git status --short`, `rg -n -i 'soil1|soil2|soil_test|openclaw_soil[23]_water' --glob '!docs/PRODUCTION_BASELINE.md'`

Expected: no legacy device source is staged; documentation may refer to retired systems only in an explicit migration note.

- [ ] **Step 3: Commit and push the replacement**

Run: `git add -A && git commit -m "chore: rebuild repository from soil3 production baseline" && git push origin main`

Expected: GitHub `main` is a soil3-only repository with full prior history preserved by Git.
