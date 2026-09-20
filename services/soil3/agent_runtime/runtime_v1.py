from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.soil3.cloud_gate.gate_v1 import GatePolicy
from services.soil3.cloud_strategy.validator import StrategyValidator
from services.soil3.state.state_v1 import StateBuilder
from services.soil3.telemetry.events import build_health_snapshot


CONFIG_FIELDS = {
    "device_code",
    "provider_mode",
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
        if value["provider_mode"] != "offline_fixture":
            raise ValueError("runtime config provider_mode must be offline_fixture")
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
            provider_mode="offline_fixture",
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
        return {
            "enabled": False,
            "provider": self.provider_mode,
            "model": self.provider_mode,
            "validator": dict(self.strategy_validator),
        }


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
