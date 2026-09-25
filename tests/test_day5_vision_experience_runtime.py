import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig, run_pipeline
from services.soil3.cloud_gate.gate_v2 import evaluate_gate_v2
from services.soil3.experience_retrieval import RetrievalError
from services.soil3.trace import TraceStore
from services.soil3.vision import CaptureOutcome, VisionArtifactRef, VisionRunResult


ROOT = Path(__file__).resolve().parents[1]
NO_EXECUTION = {"phase3_called": False, "physical_actions_performed": False}


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_config(root, *, vision, experience):
    return RuntimeConfig.from_dict({
        "device_code": "soil3",
        "provider_mode": "offline_fixture",
        "provider": {},
        "exploration_requested": False,
        "vision": {"enabled": vision},
        "experience": {"enabled": experience, "limit_per_class": 3},
        "phase3_state_path": str(root / "phase3" / "system_state.json"),
        "sensor_log_path": str(root / "phase3" / "sensor_log.csv"),
        "irrigation_trials_path": str(root / "phase3" / "irrigation_trials.json"),
        "phase3_service_unit": "phase3_soil3.service",
        "runtime_root": str(root / "agent_chain"),
        "state_output": str(root / "agent_chain" / "state" / "latest.json"),
        "prompt_path": "services/soil3/cloud_strategy/prompts/strategy_v1.txt",
        "strategy_validator": {
            "max_actions": 12,
            "max_pump_seconds": 120,
            "max_wait_seconds": 86400,
            "max_total_pump_seconds": 240,
            "max_total_seconds": 86400,
        },
        "gate_policy": {
            "warning_age_seconds": 900,
            "deny_age_seconds": 18000,
            "window_seconds": 86400,
            "max_exploration_water_seconds": 0,
        },
    })


def health_snapshot():
    now = utc_now()
    flags = {
        "pump_active": False,
        "pending_soak": False,
        "water_delivery_suspect": False,
        "reservoir_empty_suspect": False,
        "low_wet_recovery_suspect": False,
        "sensor_fault": False,
        "dynamic_cooldown": False,
        "predictor_circuit": {"state": "CLOSED", "active": False},
        "watering_trigger_guard": False,
        "recent_response_guard": False,
        "hard_safety_low_guard": False,
        "cloud_protection": False,
    }
    return {
        "observed_at": now,
        "state_file": {"age_seconds": 1.0},
        "sensor_readings": [{
            "timestamp": now,
            "humidity": 31.5,
            "temperature": 23.0,
            "ec_raw": 500.0,
        }],
        "watering_history": [],
        "system_state": flags,
        "environment": {"air": {"age_seconds": 9.0}},
    }


def admitted_gate(state, strategy, policy, exploration_requested):
    gate = evaluate_gate_v2(state, strategy, policy, exploration_requested)
    gate["decision"] = "allow"
    gate["reason_codes"] = []
    gate["warning_codes"] = []
    return gate


def vision_record():
    image_id = str(uuid.uuid4())
    frame_id = str(uuid.uuid4())
    now = utc_now()
    return {
        "schema_version": "vision.v1",
        "device_code": "soil3",
        "plant_zone": {"id": "plant_a", "rect": [0.0, 0.0, 1.0, 1.0], "label": "A"},
        "image_id": image_id,
        "image_path": f"images/{image_id}.jpg",
        "image_sha256": "a" * 64,
        "source_frame_id": frame_id,
        "source_frame_path": f"frames/{frame_id}.jpg",
        "source_frame_sha256": "b" * 64,
        "previous_image_id": None,
        "captured_at": now,
        "analyzed_at": now,
        "model": {"provider": "qwen", "name": "qwen3-vl-flash", "prompt_version": "vision.v1"},
        "image_quality": "good",
        "target_detected": True,
        "leaf_droop": "mild",
        "yellowing": "none",
        "visible_damage": "none",
        "browning": "none",
        "leaf_curl": "none",
        "spots_or_lesions": "none",
        "leaf_loss": "none",
        "stem_posture": "none",
        "occlusion": "none",
        "target_ambiguity": "none",
        "leaf_spread": "normal",
        "wilting": False,
        "overall_visual_state": "mild_abnormality",
        "change_vs_previous": "unknown",
        "confidence": 0.82,
    }


def available_vision_result(root):
    manifest = root / "public" / "vision-run.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"schema_version": "vision_run.v1"}), encoding="utf-8")
    reference = VisionArtifactRef(
        schema_version="vision_run.v1",
        record_id=str(uuid.uuid4()),
        path=str(manifest),
        sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    record = vision_record()
    return VisionRunResult(
        "success",
        record["source_frame_id"],
        (CaptureOutcome("success", "plant_a", record["image_id"], record, None),),
        reference,
    )


def read_audit(record):
    lines = Path(record["audit_path"]).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


class Day5VisionExperienceRuntimeTests(unittest.TestCase):
    def test_documentation_states_the_optional_shadow_boundaries(self):
        runtime = (ROOT / "services" / "soil3" / "agent_runtime" / "README.md").read_text(
            encoding="utf-8"
        )
        vision = (ROOT / "services" / "soil3" / "vision" / "README.md").read_text(
            encoding="utf-8"
        )
        architecture = (ROOT / "docs" / "SYSTEM_ARCHITECTURE.md").read_text(
            encoding="utf-8"
        )
        protocols = (ROOT / "docs" / "PROTOCOLS_AND_BOUNDARIES.md").read_text(
            encoding="utf-8"
        )

        combined = "\n".join((runtime, vision, architecture, protocols))
        self.assertIn("vision_run.v1", combined)
        self.assertIn("not_requested", runtime)
        self.assertIn("unavailable", runtime)
        self.assertIn("validated Vision", architecture)
        self.assertIn("Experience", architecture)
        self.assertIn("phase3_called=false", combined)
        self.assertIn("physical_actions_performed=false", combined)

    def run_case(self, config, **patches):
        stack = [
            mock.patch(
                "services.soil3.agent_runtime.runtime_v1.build_health_snapshot",
                return_value=health_snapshot(),
            ),
            mock.patch(
                "services.soil3.agent_runtime.runtime_v1.evaluate_gate_v2",
                side_effect=admitted_gate,
            ),
        ]
        stack.extend(patches.values())
        entered = []
        try:
            for item in stack:
                entered.append(item)
                item.start()
            return run_pipeline(config)
        finally:
            for item in reversed(entered):
                item.stop()

    def assert_reference_matches_file(self, association):
        self.assertEqual("available", association["availability"])
        reference = association["ref"]
        path = Path(reference["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), reference["sha256"])

    def test_disabled_modules_are_not_called_and_strategy_gets_not_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_config(Path(directory), vision=False, experience=False)
            with mock.patch(
                "services.soil3.vision.capture_and_analyze_once"
            ) as vision_call, mock.patch(
                "services.soil3.experience_retrieval.ExperienceRetriever.retrieve"
            ) as experience_call:
                record = self.run_case(config)
            trace = TraceStore(config.trace_dir).read(record["trace_id"])
            audit = read_audit(record)

        vision_call.assert_not_called()
        experience_call.assert_not_called()
        self.assertEqual({"availability": "not_requested", "ref": None}, trace["decision"]["vision"])
        self.assertEqual({"availability": "not_requested", "facts": None}, audit["model_input"]["vision"])
        self.assertEqual({"availability": "not_requested", "ref": None}, trace["decision"]["experience"])
        self.assertEqual({"availability": "not_requested", "facts": None}, audit["model_input"]["experience"])
        self.assertEqual(NO_EXECUTION, trace["execution"])

    def test_available_modules_persist_refs_and_reach_strategy_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = runtime_config(root, vision=True, experience=True)
            vision_result = available_vision_result(root)
            with mock.patch(
                "services.soil3.vision.capture_and_analyze_once",
                return_value=vision_result,
            ):
                record = self.run_case(config)
            trace = TraceStore(config.trace_dir).read(record["trace_id"])
            audit = read_audit(record)
            self.assert_reference_matches_file(trace["decision"]["vision"])
            self.assert_reference_matches_file(trace["decision"]["experience"])

        self.assertEqual("available", audit["model_input"]["vision"]["availability"])
        self.assertEqual("mild", audit["model_input"]["vision"]["facts"][0]["leaf_droop"])
        self.assertEqual("available", audit["model_input"]["experience"]["availability"])
        self.assertEqual([], audit["model_input"]["experience"]["facts"]["successful_cases"])
        self.assertEqual(NO_EXECUTION, trace["execution"])

    def test_vision_failure_is_unavailable_without_facts_or_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_config(Path(directory), vision=True, experience=False)
            failed = VisionRunResult(
                "analysis_failed",
                str(uuid.uuid4()),
                (CaptureOutcome("analysis_failed", "plant_a", None, None, "MODEL_TIMEOUT"),),
            )
            with mock.patch(
                "services.soil3.vision.capture_and_analyze_once",
                return_value=failed,
            ):
                record = self.run_case(config)
            trace = TraceStore(config.trace_dir).read(record["trace_id"])
            audit = read_audit(record)

        self.assertEqual({"availability": "unavailable", "ref": None}, trace["decision"]["vision"])
        self.assertEqual({"availability": "unavailable", "facts": None}, audit["model_input"]["vision"])

    def test_experience_failure_is_unavailable_without_partial_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_config(Path(directory), vision=False, experience=True)
            with mock.patch(
                "services.soil3.experience_retrieval.ExperienceRetriever.retrieve",
                side_effect=RetrievalError("bad state"),
            ):
                record = self.run_case(config)
            trace = TraceStore(config.trace_dir).read(record["trace_id"])
            audit = read_audit(record)
            artifacts = list(config.experience_dir.glob("*.json"))

        self.assertEqual([], artifacts)
        self.assertEqual({"availability": "unavailable", "ref": None}, trace["decision"]["experience"])
        self.assertEqual({"availability": "unavailable", "facts": None}, audit["model_input"]["experience"])


if __name__ == "__main__":
    unittest.main()
