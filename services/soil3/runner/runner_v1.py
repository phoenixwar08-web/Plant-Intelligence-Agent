from __future__ import annotations

import copy
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict

from services.soil3.cloud_strategy.validator import (
    ALLOWED_ACTIONS,
    EXPECTED_DEVICE_CODE,
    REQUIRED_FIELDS,
    fingerprint,
    is_canonical_timestamp,
    is_state_hash,
    parse_timestamp,
)
from services.soil3.telemetry.common import atomic_write_json, load_json


RUN_STATUSES = {"running", "waiting", "completed", "stopped"}
STEP_STATUSES = {"pending", "waiting", "completed", "skipped"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("runner clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_positive_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (OverflowError, ValueError):
        return False


def _validate_strategy_shape(strategy: Any) -> None:
    if not isinstance(strategy, dict) or strategy.get("schema_version") != "strategy.v1":
        raise ValueError("runner requires a strategy.v1 object")
    if set(strategy) != REQUIRED_FIELDS:
        raise ValueError("strategy fields do not match strategy.v1")
    try:
        uuid.UUID(str(strategy.get("strategy_id")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("strategy_id must be a UUID") from exc
    if strategy.get("device_code") != EXPECTED_DEVICE_CODE:
        raise ValueError("runner only accepts soil3 strategies")
    if not is_state_hash(strategy.get("state_sha256")):
        raise ValueError("strategy has an invalid state_sha256")
    for field in ("state_observed_at", "state_generated_at", "created_at"):
        if not is_canonical_timestamp(strategy.get(field)):
            raise ValueError(f"strategy has an invalid {field}")
    execution = strategy.get("execution")
    if execution != {"mode": "proposal_only", "actuator_commands_allowed": False}:
        raise ValueError("strategy execution metadata is not proposal-only")
    actions = strategy.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("strategy actions must be a non-empty list")
    seen = set()
    stop_seen = False
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ValueError(f"action[{index}] must be an object")
        action_id = action.get("action_id")
        if not isinstance(action_id, str) or not action_id.strip() or action_id in seen:
            raise ValueError(f"action[{index}] has an invalid or duplicate action_id")
        seen.add(action_id)
        action_type = action.get("type")
        if action_type not in ALLOWED_ACTIONS:
            raise ValueError(f"action[{index}] has an unknown type")
        if stop_seen:
            raise ValueError("actions after stop are not allowed")
        stop_seen = action_type == "stop"
        if action_type == "water" and not _is_positive_number(action.get("pump_seconds")):
            raise ValueError(f"action[{index}] has invalid pump_seconds")
        if action_type == "wait" and not _is_positive_number(action.get("seconds")):
            raise ValueError(f"action[{index}] has invalid wait seconds")
        allowed = {
            "water": {"action_id", "type", "pump_seconds"},
            "wait": {"action_id", "type", "seconds"},
            "observe": {"action_id", "type"},
            "stop": {"action_id", "type"},
        }[action_type]
        if set(action) != allowed:
            raise ValueError(f"action[{index}] fields do not match strategy.v1")


class RunnerStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def path_for(self, strategy_id: str) -> Path:
        try:
            normalized = str(uuid.UUID(str(strategy_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("strategy_id must be a UUID") from exc
        return self.directory / f"{normalized}.json"

    def exists(self, strategy_id: str) -> bool:
        return self.path_for(strategy_id).exists()

    def read(self, strategy_id: str) -> Dict[str, Any]:
        path = self.path_for(strategy_id)
        if not path.exists():
            raise FileNotFoundError(f"runner state not found for {strategy_id}")
        value = load_json(path)
        self._validate_record(value, strategy_id)
        return value

    def write(self, record: Dict[str, Any]) -> None:
        self._validate_record(record, record.get("strategy_id"))
        atomic_write_json(self.path_for(record["strategy_id"]), record)

    @staticmethod
    def _validate_record(value: Any, strategy_id: Any) -> None:
        if not isinstance(value, dict) or value.get("schema_version") != "runner_state.v1":
            raise ValueError("invalid runner state record")
        if value.get("strategy_id") != strategy_id:
            raise ValueError("runner state strategy_id mismatch")
        if value.get("status") not in RUN_STATUSES:
            raise ValueError("invalid runner status")
        if not isinstance(value.get("steps"), list) or any(
            not isinstance(step, dict) or step.get("status") not in STEP_STATUSES
            for step in value["steps"]
        ):
            raise ValueError("invalid runner steps")
        current = value.get("current_step_index")
        if isinstance(current, bool) or not isinstance(current, int) or not 0 <= current <= len(value["steps"]):
            raise ValueError("invalid current_step_index")


class DryRunRunner:
    """Advance strategy steps while producing records and no physical side effects."""

    def __init__(self, store: RunnerStore, *, clock: Callable[[], datetime] = _now) -> None:
        self.store = store
        self.clock = clock

    def start(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        _validate_strategy_shape(strategy)
        strategy_id = strategy["strategy_id"]
        strategy_hash = fingerprint(strategy)
        if self.store.exists(strategy_id):
            existing = self.store.read(strategy_id)
            if existing.get("strategy_sha256") != strategy_hash:
                raise ValueError("strategy_id already exists with different content")
            return existing

        now = _iso(self.clock())
        record = {
            "schema_version": "runner_state.v1",
            "strategy_id": strategy_id,
            "strategy_sha256": strategy_hash,
            "mode": "dry_run",
            "status": "running",
            "current_step_index": 0,
            "created_at": now,
            "updated_at": now,
            "steps": [
                {
                    "index": index,
                    "action": copy.deepcopy(action),
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "wait_until": None,
                    "result": None,
                }
                for index, action in enumerate(strategy["actions"])
            ],
            "execution": {"physical_actions_performed": False, "phase3_called": False},
        }
        self.store.write(record)
        return record

    def run(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        record = self.start(strategy)
        return self.resume(record["strategy_id"])

    def read(self, strategy_id: str) -> Dict[str, Any]:
        return self.store.read(strategy_id)

    def resume(self, strategy_id: str) -> Dict[str, Any]:
        record = self.store.read(strategy_id)
        if record["status"] in {"completed", "stopped"}:
            return record

        now_dt = self.clock()
        now = _iso(now_dt)
        while record["current_step_index"] < len(record["steps"]):
            index = record["current_step_index"]
            step = record["steps"][index]
            action = step["action"]
            action_type = action["type"]

            if action_type == "wait":
                if step["status"] == "pending":
                    step["status"] = "waiting"
                    step["started_at"] = now
                    step["wait_until"] = _iso(now_dt + timedelta(seconds=float(action["seconds"])))
                    step["result"] = {
                        "kind": "dry_run_wait",
                        "requested_seconds": float(action["seconds"]),
                    }
                    record["status"] = "waiting"
                    record["updated_at"] = now
                    self.store.write(record)
                    return record
                wait_until = parse_timestamp(step.get("wait_until"))
                if wait_until is None:
                    raise ValueError("persisted wait step has invalid wait_until")
                if now_dt.astimezone(timezone.utc) < wait_until.astimezone(timezone.utc):
                    return record
                self._complete_step(record, step, now)
                continue

            if step["status"] == "completed":
                record["current_step_index"] += 1
                continue
            if step["status"] != "pending":
                raise ValueError("non-wait step has invalid persisted status")

            step["started_at"] = now
            if action_type == "water":
                step["result"] = {
                    "kind": "dry_run_water",
                    "pump_seconds": float(action["pump_seconds"]),
                    "physical_action_performed": False,
                }
                self._complete_step(record, step, now)
            elif action_type == "observe":
                step["result"] = {
                    "kind": "dry_run_observe",
                    "observation_collected": False,
                }
                self._complete_step(record, step, now)
            elif action_type == "stop":
                step["result"] = {"kind": "dry_run_stop"}
                step["status"] = "completed"
                step["completed_at"] = now
                record["current_step_index"] += 1
                record["status"] = "stopped"
                record["updated_at"] = now
                self.store.write(record)
                return record
            else:  # guarded by start validation and persisted-record checks
                raise ValueError(f"unknown action type: {action_type}")

        record["status"] = "completed"
        record["updated_at"] = now
        self.store.write(record)
        return record

    def _complete_step(self, record: Dict[str, Any], step: Dict[str, Any], now: str) -> None:
        step["status"] = "completed"
        step["completed_at"] = now
        record["current_step_index"] += 1
        record["status"] = "running"
        record["updated_at"] = now
        # Persist after every completed step. A restarted process therefore never
        # creates a second completion record for an already-recorded dry-run water.
        self.store.write(record)
