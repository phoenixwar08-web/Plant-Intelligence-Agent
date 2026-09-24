from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any

from services.soil3.cloud_gate import GatePolicy, evaluate_gate_v2
from services.soil3.cloud_strategy.service import append_audit, run_chain
from services.soil3.cloud_strategy.validator import PROMPT_VERSION, StrategyValidator, fingerprint, normalize_timestamp
from services.soil3.episode.episode_v1 import EpisodeStore
from services.soil3.phase3_bridge import Phase3Bridge
from services.soil3.runner.runner_v1 import DryRunRunner, RunnerStore
from services.soil3.state.state_v1 import StateBuilder
from services.soil3.telemetry.events import build_health_snapshot
from services.soil3.trace import TraceStore


CONFIG_FIELDS = {
    "device_code",
    "provider_mode",
    "provider",
    "exploration_requested",
    "phase3_state_path",
    "sensor_log_path",
    "irrigation_trials_path",
    "phase3_service_unit",
    "runtime_root",
    "state_output",
    "prompt_path",
    "strategy_validator",
    "gate_policy",
}
GATE_V2_FIELDS = frozenset({
    "schema_version",
    "gate_id",
    "decided_at",
    "device_code",
    "strategy_id",
    "state_observed_at",
    "state_sha256",
    "strategy_sha256",
    "decision",
    "reason_codes",
    "warning_codes",
    "budget",
    "execution",
})
GATE_BUDGET_FIELDS = frozenset({
    "exploration_requested",
    "requested_water_seconds",
    "reserved_water_seconds",
    "remaining_water_seconds",
    "reservation_id",
})

QWEN_PROVIDER_FIELDS = {
    "base_url",
    "model",
    "api_key_env",
    "timeout_seconds",
    "max_retries",
    "temperature",
    "max_tokens",
    "json_response_format",
}


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


@dataclass(frozen=True)
class RuntimeConfig:
    device_code: str
    provider_mode: str
    provider: dict[str, Any]
    phase3_state_path: Path
    sensor_log_path: Path
    irrigation_trials_path: Path
    phase3_service_unit: str
    runtime_root: Path
    state_output: Path
    prompt_path: Path
    strategy_validator: dict[str, Any]
    gate_policy: GatePolicy

    @classmethod
    def from_dict(cls, value: Any) -> "RuntimeConfig":
        if not isinstance(value, dict) or set(value) != CONFIG_FIELDS:
            raise ValueError("runtime config fields are invalid")
        if value["device_code"] != "soil3":
            raise ValueError("runtime config device_code must be soil3")
        provider_mode = value["provider_mode"]
        provider = value["provider"]
        if provider_mode not in {"offline_fixture", "qwen_dashscope"}:
            raise ValueError("runtime config provider_mode is invalid")
        if provider_mode == "offline_fixture":
            if provider != {}:
                raise ValueError("offline_fixture provider must be empty")
        else:
            if not isinstance(provider, dict) or set(provider) != QWEN_PROVIDER_FIELDS:
                raise ValueError("qwen provider fields are invalid")
            if (
                not isinstance(provider["base_url"], str)
                or not provider["base_url"].startswith("https://")
                or not isinstance(provider["api_key_env"], str)
                or not provider["api_key_env"].isidentifier()
                or provider["model"] != "qwen3.8-Flash"
                or isinstance(provider["timeout_seconds"], bool)
                or not isinstance(provider["timeout_seconds"], (int, float))
                or not isfinite(float(provider["timeout_seconds"]))
                or float(provider["timeout_seconds"]) <= 0
                or isinstance(provider["max_retries"], bool)
                or not isinstance(provider["max_retries"], int)
                or provider["max_retries"] < 0
                or isinstance(provider["temperature"], bool)
                or not isinstance(provider["temperature"], (int, float))
                or not isfinite(float(provider["temperature"]))
                or isinstance(provider["max_tokens"], bool)
                or not isinstance(provider["max_tokens"], int)
                or provider["max_tokens"] < 1
                or not isinstance(provider["json_response_format"], bool)
            ):
                raise ValueError("qwen provider configuration is invalid")
        if value["exploration_requested"] is not False:
            raise ValueError("runtime config exploration_requested must be false")
        if value["phase3_service_unit"] != "phase3_soil3.service":
            raise ValueError("runtime config phase3_service_unit must be phase3_soil3.service")

        runtime_root = Path(value["runtime_root"])
        state_output = Path(value["state_output"])
        if state_output != runtime_root / "state" / "latest.json":
            raise ValueError("state_output must be runtime_root/state/latest.json")
        phase3_state_path = Path(value["phase3_state_path"])
        if phase3_state_path.name != "system_state.json":
            raise ValueError("phase3_state_path must name system_state.json")

        strategy_validator = value["strategy_validator"]
        if not isinstance(strategy_validator, dict):
            raise ValueError("strategy_validator must be an object")
        StrategyValidator(
            max_actions=strategy_validator.get("max_actions"),
            max_pump_seconds=strategy_validator.get("max_pump_seconds"),
            max_wait_seconds=strategy_validator.get("max_wait_seconds"),
            max_total_pump_seconds=strategy_validator.get("max_total_pump_seconds"),
            max_total_seconds=strategy_validator.get("max_total_seconds"),
        )
        return cls(
            device_code="soil3",
            provider_mode=provider_mode,
            provider=dict(provider),
            phase3_state_path=phase3_state_path,
            sensor_log_path=Path(value["sensor_log_path"]),
            irrigation_trials_path=Path(value["irrigation_trials_path"]),
            phase3_service_unit="phase3_soil3.service",
            runtime_root=runtime_root,
            state_output=state_output,
            prompt_path=Path(value["prompt_path"]),
            strategy_validator=dict(strategy_validator),
            gate_policy=GatePolicy.from_dict(value["gate_policy"]),
        )

    def strategy_config(self) -> dict[str, Any]:
        if self.provider_mode == "qwen_dashscope":
            return {
                "enabled": True,
                "provider": "qwen_dashscope",
                **dict(self.provider),
                "validator": dict(self.strategy_validator),
            }
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
    def feedback_dir(self) -> Path:
        return self.runtime_root / "feedback"

    @property
    def trace_dir(self) -> Path:
        return self.runtime_root / "traces"

    @property
    def bridge_dir(self) -> Path:
        return self.runtime_root / "bridge"

    @property
    def audit_dir(self) -> Path:
        return self.runtime_root / "audit"

    @property
    def runs_dir(self) -> Path:
        return self.runtime_root / "runs"


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_prompt_path(prompt_path: Path) -> Path:
    if prompt_path.is_absolute():
        return prompt_path
    return Path(__file__).resolve().parents[3] / prompt_path


def _artifact_ref(
    schema_version: str,
    path: Path,
    *,
    record_id: str | None = None,
) -> dict[str, Any]:
    """Build a trace reference from an artifact that was actually persisted."""

    return {
        "schema_version": schema_version,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "record_id": record_id,
    }


def _model_metrics(strategy_result: dict[str, Any], latency_ms: float) -> dict[str, Any]:
    """Keep only model facts with sources accepted by trace.v1."""

    cloud = strategy_result.get("cloud")
    cloud = cloud if isinstance(cloud, dict) else {}
    usage = cloud.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    total_tokens = usage.get("total_tokens")
    token_usage = None
    if (
        not isinstance(total_tokens, bool)
        and isinstance(total_tokens, (int, float))
        and isfinite(float(total_tokens))
        and float(total_tokens) >= 0
    ):
        token_usage = {
            "source": "provider_response.usage",
            "value": total_tokens,
            "unit": "tokens",
        }
    provider = cloud.get("provider")
    model = cloud.get("model")
    return {
        "provider": provider if isinstance(provider, str) and provider.strip() else None,
        "model": model if isinstance(model, str) and model.strip() else None,
        "token_usage": token_usage,
        "cost": None,
        "latency": {
            "source": "runtime_monotonic_clock",
            "value": max(0.0, float(latency_ms)),
            "unit": "ms",
        },
    }


def _gate_contract_valid(
    gate: Any,
    state: dict[str, Any],
    strategy: dict[str, Any],
) -> bool:
    """Check the public gate.v2 contract before dry-run progress."""

    if (
        not isinstance(gate, dict)
        or not isinstance(state, dict)
        or not isinstance(strategy, dict)
        or set(gate) != GATE_V2_FIELDS
    ):
        return False

    try:
        uuid.UUID(gate.get("gate_id"))
    except (ValueError, TypeError, AttributeError):
        return False

    decision = gate.get("decision")
    state_observed_at = normalize_timestamp(state.get("observed_at"))
    if (
        gate.get("schema_version") != "gate.v2"
        or normalize_timestamp(gate.get("decided_at")) is None
        or gate.get("device_code") != state.get("device_code")
        or gate.get("strategy_id") != strategy.get("strategy_id")
        or gate.get("strategy_sha256") != fingerprint(strategy)
        or state_observed_at is None
        or normalize_timestamp(gate.get("state_observed_at")) != state_observed_at
        or gate.get("state_sha256") != fingerprint(state)
        or decision not in {"allow", "allow_with_warning", "deny"}
        or gate.get("execution")
        != {"mode": "admission_only", "actuator_commands_allowed": False}
    ):
        return False
    reason_codes = gate.get("reason_codes")
    warning_codes = gate.get("warning_codes")
    if (
        not isinstance(reason_codes, list)
        or any(not isinstance(code, str) for code in reason_codes)
        or not isinstance(warning_codes, list)
        or any(not isinstance(code, str) for code in warning_codes)
    ):
        return False

    if decision == "deny":
        if not reason_codes or warning_codes:
            return False
    elif reason_codes:
        return False
    elif decision == "allow" and warning_codes:
        return False
    elif decision == "allow_with_warning" and not warning_codes:
        return False

    budget = gate.get("budget")
    if not isinstance(budget, dict) or set(budget) != GATE_BUDGET_FIELDS:
        return False

    def finite_nonnegative(value: Any) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and isfinite(float(value))
            and float(value) >= 0.0
        )

    exploration_requested = budget.get("exploration_requested")
    requested = budget.get("requested_water_seconds")
    reserved = budget.get("reserved_water_seconds")
    remaining = budget.get("remaining_water_seconds")
    reservation_id = budget.get("reservation_id")
    if (
        not isinstance(exploration_requested, bool)
        or not finite_nonnegative(requested)
        or not finite_nonnegative(reserved)
        or (remaining is not None and not finite_nonnegative(remaining))
    ):
        return False
    if not exploration_requested:
        return (
            float(requested) == 0.0
            and float(reserved) == 0.0
            and remaining is None
            and reservation_id is None
        )
    if decision == "deny":
        return reservation_id is None and float(reserved) == 0.0
    return (
        float(requested) > 0.0
        and float(reserved) == float(requested)
        and remaining is not None
        and isinstance(reservation_id, str)
        and bool(reservation_id)
    )


def build_offline_fixture(state: dict[str, Any]) -> dict[str, Any]:
    """Build a visibly offline proposal for integration-only validation."""

    observed_at = normalize_timestamp(state["observed_at"])
    generated_at = _utc_now()
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "device_code": state["device_code"],
        "state_observed_at": observed_at,
        "state_generated_at": normalize_timestamp(state["generated_at"]),
        "state_sha256": fingerprint(state),
        "created_at": generated_at,
        "actions": [{"action_id": "offline-fixture-stop", "type": "stop"}],
        "reason_summary": ["offline_fixture", "non_executing_runtime_validation"],
        "expected_outcome": {
            "soil_moisture": "not_evaluated",
            "risk_notes": ["offline_fixture"],
        },
        "confidence": 0.0,
        "model": {
            "provider": "offline_fixture",
            "name": "offline_fixture",
            "prompt_version": PROMPT_VERSION,
        },
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }


def run_pipeline(config: RuntimeConfig) -> dict[str, Any]:
    """Run one traceable end-to-end Shadow cycle without actuator access."""

    trace_store = TraceStore(config.trace_dir)
    trace = trace_store.create()
    trace_id = trace["trace_id"]
    trace_path = trace_store.trace_path(trace_id)
    state = write_state_snapshot(config)
    trace_store.set_decision(
        trace_id,
        state_ref=_artifact_ref("state.v1", config.state_output),
        vision={"availability": "not_requested", "ref": None},
        experience={"availability": "not_requested", "ref": None},
    )
    prompt = _resolve_prompt_path(config.prompt_path).read_text(encoding="utf-8")
    strategy_started = time.monotonic()
    strategy_result = run_chain(
        state=state,
        config=config.strategy_config(),
        prompt=prompt,
        fixture_content=(
            json.dumps(build_offline_fixture(state))
            if config.provider_mode == "offline_fixture"
            else None
        ),
    )
    strategy_latency_ms = (time.monotonic() - strategy_started) * 1000.0
    trace_store.set_decision(
        trace_id,
        model_metrics=_model_metrics(strategy_result, strategy_latency_ms),
    )
    validation = strategy_result.get("validation")
    if (
        config.provider_mode == "qwen_dashscope"
        and isinstance(validation, dict)
        and validation.get("accepted")
        and isinstance(validation.get("strategy"), dict)
    ):
        strategy_model = dict(validation["strategy"]["model"])
        strategy_model.update(
            {
                "provider": "qwen_dashscope",
                "name": config.provider["model"],
            }
        )
        validation["strategy"]["model"] = strategy_model
    audit_path = append_audit(config.audit_dir, strategy_result)
    if not isinstance(validation, dict) or not validation.get("accepted") or not isinstance(validation.get("strategy"), dict):
        cloud = strategy_result.get("cloud")
        cloud = cloud if isinstance(cloud, dict) else {}
        strategy_config = config.strategy_config()
        run_id = str(uuid.uuid4())
        record = {
            "schema_version": "agent_runtime.v1",
            "run_id": run_id,
            "provider_mode": config.provider_mode,
            "provider": str(cloud.get("provider") or config.provider_mode),
            "model": str(cloud.get("model") or strategy_config["model"]),
            "trace_id": trace_id,
            "trace_path": str(trace_path),
            "state_path": str(config.state_output),
            "state_sha256": fingerprint(state),
            "strategy_path": None,
            "gate_path": None,
            "gate_decision": None,
            "runner_status": "not_started_provider_or_validation_failure",
            "runner_path": None,
            "bridge_status": "not_started_provider_or_validation_failure",
            "bridge_path": None,
            "episode_id": None,
            "episode_path": None,
            "audit_path": str(audit_path),
            "execution": {"physical_actions_performed": False, "phase3_called": False},
        }
        atomic_write_json(config.runs_dir / f"{run_id}.json", record)
        raise RuntimeError(f"{config.provider_mode} strategy was rejected")

    strategy = validation["strategy"]
    atomic_write_json(config.strategy_output, strategy)
    trace_store.set_decision(
        trace_id,
        strategy_ref=_artifact_ref(
            "strategy.v1",
            config.strategy_output,
            record_id=strategy["strategy_id"],
        ),
    )
    gate = evaluate_gate_v2(
        state=state,
        strategy=strategy,
        policy=config.gate_policy,
        exploration_requested=False,
    )
    atomic_write_json(config.gate_output, gate)
    trace_store.set_decision(
        trace_id,
        gate_ref=_artifact_ref(
            str(gate.get("schema_version") or "unknown"),
            config.gate_output,
            record_id=(gate.get("gate_id") if isinstance(gate.get("gate_id"), str) else None),
        ),
        gate_decision=gate["decision"],
        gate_reason_codes=gate["reason_codes"],
    )

    episode_store = EpisodeStore(config.episode_dir)
    episode = episode_store.create(state)
    episode_id = episode["episode_id"]
    episode_store.update(episode_id, strategy=strategy, gate_result=gate)

    runner_path: str | None = None
    bridge_path: str | None = None
    bridge_status = "not_started"
    gate_decision = gate.get("decision") if isinstance(gate, dict) else None
    gate_binding_valid = _gate_contract_valid(gate, state, strategy)
    if not gate_binding_valid:
        runner_status = "skipped_due_to_invalid_gate_binding"
        bridge_status = "not_started_invalid_gate_binding"
    elif gate["decision"] == "deny":
        runner_status = "skipped_due_to_gate_deny"
        bridge_status = "not_started_gate_deny"
    elif gate["decision"] in {"allow", "allow_with_warning"}:
        runner_store = RunnerStore(config.runner_dir)
        runner_result = DryRunRunner(runner_store).run(strategy, state)
        runner_artifact = runner_store.path_for(strategy["strategy_id"])
        runner_path = str(runner_artifact)
        trace_store.set_decision(
            trace_id,
            runner_ref=_artifact_ref(
                "runner_state.v1",
                runner_artifact,
                record_id=strategy["strategy_id"],
            ),
        )
        executed_actions = [
            {
                **step["result"],
                "action_id": step["action"]["action_id"],
                "executed_at": step["completed_at"],
            }
            for step in runner_result["steps"]
            if step["status"] == "completed" and isinstance(step.get("result"), dict)
        ]
        episode_store.update(episode_id, executed_actions=executed_actions)
        runner_status = "dry_run_completed"
        bridge = Phase3Bridge().verify(state, strategy, gate, runner_result)
        bridge_artifact = config.bridge_dir / f"{trace_id}.json"
        atomic_write_json(bridge_artifact, bridge)
        bridge_path = str(bridge_artifact)
        handoff = bridge.get("handoff")
        bridge_record_id = (
            handoff.get("request_id")
            if isinstance(handoff, dict) and isinstance(handoff.get("request_id"), str)
            else None
        )
        trace_store.set_decision(
            trace_id,
            bridge_ref=_artifact_ref(
                "phase3_bridge_response.v1",
                bridge_artifact,
                record_id=bridge_record_id,
            ),
        )
        expected_bridge_execution = {
            "mode": "verification_only",
            "phase3_called": False,
            "physical_actions_performed": False,
        }
        bridge_status = (
            "verified"
            if bridge.get("accepted") is True
            and bridge.get("execution") == expected_bridge_execution
            and isinstance(handoff, dict)
            and handoff.get("execution") == expected_bridge_execution
            else "rejected"
        )
    else:
        raise RuntimeError(f"unexpected gate decision: {gate['decision']}")

    episode_artifact = episode_store.episode_path(episode_id)
    trace_store.set_decision(
        trace_id,
        episode_ref=_artifact_ref(
            "episode.v1",
            episode_artifact,
            record_id=episode_id,
        ),
    )

    run_id = str(uuid.uuid4())
    record = {
        "schema_version": "agent_runtime.v1",
        "run_id": run_id,
        "provider_mode": config.provider_mode,
        "provider": strategy["model"]["provider"],
        "model": strategy["model"]["name"],
        "trace_id": trace_id,
        "trace_path": str(trace_path),
        "state_path": str(config.state_output),
        "state_sha256": fingerprint(state),
        "strategy_path": str(config.strategy_output),
        "gate_path": str(config.gate_output),
        "gate_decision": gate["decision"],
        "runner_status": runner_status,
        "runner_path": runner_path,
        "bridge_status": bridge_status,
        "bridge_path": bridge_path,
        "episode_id": episode_id,
        "episode_path": str(episode_store.episode_path(episode_id)),
        "episode_status": "open",
        "feedback_status": "pending",
        "audit_path": str(audit_path),
        "execution": {"physical_actions_performed": False, "phase3_called": False},
    }
    atomic_write_json(config.runs_dir / f"{run_id}.json", record)
    if bridge_status == "rejected":
        raise RuntimeError("phase3 bridge verification was rejected")
    return record
