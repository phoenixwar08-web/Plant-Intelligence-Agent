from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict

from services.soil3.cloud_strategy.validator import (
    StrategyValidator,
    fingerprint,
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


def _validate_strategy(strategy: Any, state: Any) -> None:
    if not isinstance(state, dict):
        raise ValueError("runner requires the bound state.v1 snapshot")
    validation = StrategyValidator().validate(strategy, state)
    if not validation.accepted:
        raise ValueError("runner rejected strategy.v1: " + ", ".join(validation.reason_codes))


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

    def start(self, strategy: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        _validate_strategy(strategy, state)
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

    def run(self, strategy: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        record = self.start(strategy, state)
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
