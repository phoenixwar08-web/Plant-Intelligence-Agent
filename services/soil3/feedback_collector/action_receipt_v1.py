"""Durable evidence records for real soil3 watering actions.

Receipts are deliberately separate from feedback.v1.  They record what the
producer can prove about one action attempt; a completed MQTT command is not a
claim that a pump physically ran or water reached the plant.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from services.soil3.cloud_strategy.validator import normalize_timestamp
from services.soil3.telemetry.common import load_json


SCHEMA_VERSION = "feedback_action_receipt.v1"
EXPECTED_DEVICE_CODE = "soil3"
ACTION_ID_PATTERN = re.compile(r"^act-[0-9a-f]{24}$")
TRACE_ID_PATTERN = re.compile(r"^tr-[0-9a-f]{24}$")
EPISODE_ID_PATTERN = re.compile(r"^ep-[0-9a-f]{24}$")
RECEIPT_FIELDS = {
    "schema_version",
    "action_id",
    "device_code",
    "action_source",
    "created_at",
    "reference_action_at",
    "command",
    "command_status",
    "execution_evidence",
    "episode_binding",
    "initial_state",
    "trace_id",
    "source_receipt_ref",
}
SOURCES = {"phase3_native", "controlled_execution", "manual_confirmed"}
COMMAND_STATUS = {"prepared", "command_completed", "command_incomplete", "manual_confirmed"}
DEFAULT_RECEIPT_DIR = "/root/water/runtime/instances/soil3/agent_chain/feedback_actions"


class ActionReceiptError(ValueError):
    """A receipt is malformed, unavailable, or conflicts with an existing ID."""

    def __init__(self, code: str, reasons: Optional[List[str]] = None):
        self.code = code
        self.reasons = list(reasons or [])
        detail = f": {', '.join(self.reasons)}" if self.reasons else ""
        super().__init__(f"{code}{detail}")


def new_action_id() -> str:
    """Return one opaque 96-bit action identifier suitable for a receipt path."""
    return "act-" + uuid.uuid4().hex[:24]


def default_feedback_action_receipt_dir() -> Path:
    """One runtime root shared by native, controlled, manual, and collector paths."""
    return Path(os.environ.get("SOIL3_FEEDBACK_ACTION_RECEIPT_DIR", DEFAULT_RECEIPT_DIR))


def _canonical_timestamp(value: Any, field: str) -> str:
    normalized = normalize_timestamp(value)
    if normalized is None:
        raise ActionReceiptError("validation_failed", [f"{field}_invalid"])
    return normalized


def _positive_seconds(value: Any, field: str = "pump_seconds") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionReceiptError("validation_failed", [f"{field}_invalid"])
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ActionReceiptError("validation_failed", [f"{field}_invalid"])
    return seconds


def _valid_state(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == "state.v1"
        and value.get("device_code") == EXPECTED_DEVICE_CODE
        and normalize_timestamp(value.get("observed_at")) is not None
        and normalize_timestamp(value.get("generated_at")) is not None
    )


def _canonical_json(value: Dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ActionReceiptStore:
    """Create and transition one immutable-id receipt per real action attempt."""

    def __init__(self, root_dir: Union[str, Path]):
        self.root = Path(root_dir)

    def receipt_path(self, action_id: str) -> Path:
        if not isinstance(action_id, str) or ACTION_ID_PATTERN.fullmatch(action_id) is None:
            raise ActionReceiptError("invalid_action_id")
        return self.root / f"{action_id}.json"

    def prepare_native(
        self,
        action_id: str,
        initial_state: Dict[str, Any],
        pump_seconds: float,
        created_at: Any,
    ) -> Dict[str, Any]:
        return self._create_once(
            self._base_record(
                action_id=action_id,
                action_source="phase3_native",
                initial_state=initial_state,
                pump_seconds=pump_seconds,
                created_at=created_at,
                trace_id=None,
                episode_id=None,
            )
        )

    def prepare_controlled(
        self,
        action_id: str,
        initial_state: Dict[str, Any],
        trace_id: str,
        episode_id: str,
        approval_id: str,
        created_at: Any,
    ) -> Dict[str, Any]:
        if not isinstance(trace_id, str) or TRACE_ID_PATTERN.fullmatch(trace_id) is None:
            raise ActionReceiptError("validation_failed", ["trace_id_invalid"])
        if not isinstance(episode_id, str) or EPISODE_ID_PATTERN.fullmatch(episode_id) is None:
            raise ActionReceiptError("validation_failed", ["episode_id_invalid"])
        if not isinstance(approval_id, str) or not approval_id:
            raise ActionReceiptError("validation_failed", ["approval_id_invalid"])
        record = self._base_record(
            action_id=action_id,
            action_source="controlled_execution",
            initial_state=initial_state,
            pump_seconds=1.0,
            created_at=created_at,
            trace_id=trace_id,
            episode_id=episode_id,
        )
        record["command"] = {"kind": "water", "pump_seconds": None, "approval_id": approval_id}
        return self._create_once(record)

    def record_manual(
        self,
        action_id: str,
        initial_state: Dict[str, Any],
        *,
        confirmed_by: str,
        reference_action_at: Any,
        pump_seconds: float,
    ) -> Dict[str, Any]:
        if not isinstance(confirmed_by, str) or not confirmed_by.strip():
            raise ActionReceiptError("validation_failed", ["confirmed_by_invalid"])
        reference = _canonical_timestamp(reference_action_at, "reference_action_at")
        record = self._base_record(
            action_id=action_id,
            action_source="manual_confirmed",
            initial_state=initial_state,
            pump_seconds=pump_seconds,
            created_at=reference,
            trace_id=None,
            episode_id=None,
        )
        record["reference_action_at"] = reference
        record["command_status"] = "manual_confirmed"
        record["execution_evidence"] = {
            "level": "manual_confirmed",
            "physical_action_confirmed": True,
            "confirmed_by": confirmed_by.strip(),
        }
        return self._create_once(record)

    def complete_command(
        self,
        action_id: str,
        *,
        reference_action_at: Any,
        on_published_at: Any,
        off_published_at: Any,
        pump_seconds: float,
    ) -> Dict[str, Any]:
        record = self.read(action_id)
        if record["action_source"] not in {"phase3_native", "controlled_execution"}:
            raise ActionReceiptError("invalid_transition")
        reference = _canonical_timestamp(reference_action_at, "reference_action_at")
        on_at = _canonical_timestamp(on_published_at, "on_published_at")
        off_at = _canonical_timestamp(off_published_at, "off_published_at")
        seconds = _positive_seconds(pump_seconds)
        if record["command_status"] == "command_completed":
            expected = copy.deepcopy(record)
            expected["reference_action_at"] = reference
            expected["command"] = {**expected["command"], "pump_seconds": seconds}
            expected["execution_evidence"] = {
                "level": "command_completed",
                "physical_action_confirmed": False,
                "mqtt_on_published_at": on_at,
                "mqtt_off_published_at": off_at,
            }
            if expected != record:
                raise ActionReceiptError("action_id_conflict")
            return copy.deepcopy(record)
        if record["command_status"] != "prepared":
            raise ActionReceiptError("invalid_transition")
        record["reference_action_at"] = reference
        record["command"] = {**record["command"], "pump_seconds": seconds}
        record["command_status"] = "command_completed"
        record["execution_evidence"] = {
            "level": "command_completed",
            "physical_action_confirmed": False,
            "mqtt_on_published_at": on_at,
            "mqtt_off_published_at": off_at,
        }
        return self._replace(record)

    def mark_incomplete(self, action_id: str, reason: str) -> Dict[str, Any]:
        if not isinstance(reason, str) or not reason:
            raise ActionReceiptError("validation_failed", ["reason_invalid"])
        record = self.read(action_id)
        if record["command_status"] != "prepared":
            raise ActionReceiptError("invalid_transition")
        record["command_status"] = "command_incomplete"
        record["execution_evidence"] = {
            "level": "command_incomplete",
            "physical_action_confirmed": False,
            "reason": reason,
        }
        return self._replace(record)

    def read(self, action_id: str) -> Dict[str, Any]:
        path = self.receipt_path(action_id)
        if not path.exists():
            raise ActionReceiptError("receipt_not_found")
        try:
            value = load_json(path)
        except (OSError, ValueError) as error:
            raise ActionReceiptError("receipt_invalid", [str(error)]) from error
        self._validate_stored(value)
        return copy.deepcopy(value)

    def iter_receipts(self) -> Iterator[Dict[str, Any]]:
        if not self.root.is_dir():
            return iter(())
        return iter([self.read(path.stem) for path in sorted(self.root.glob("act-*.json"))])

    def artifact_ref(self, action_id: str) -> Dict[str, Any]:
        path = self.receipt_path(action_id)
        record = self.read(action_id)
        payload = path.read_bytes()
        return {
            "schema_version": SCHEMA_VERSION,
            "path": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "record_id": record["action_id"],
        }

    def _base_record(
        self,
        *,
        action_id: str,
        action_source: str,
        initial_state: Dict[str, Any],
        pump_seconds: float,
        created_at: Any,
        trace_id: Optional[str],
        episode_id: Optional[str],
    ) -> Dict[str, Any]:
        self.receipt_path(action_id)
        if action_source not in SOURCES:
            raise ActionReceiptError("validation_failed", ["action_source_invalid"])
        if not _valid_state(initial_state):
            raise ActionReceiptError("validation_failed", ["initial_state_invalid"])
        seconds = _positive_seconds(pump_seconds)
        return {
            "schema_version": SCHEMA_VERSION,
            "action_id": action_id,
            "device_code": EXPECTED_DEVICE_CODE,
            "action_source": action_source,
            "created_at": _canonical_timestamp(created_at, "created_at"),
            "reference_action_at": None,
            "command": {"kind": "water", "pump_seconds": seconds},
            "command_status": "prepared",
            "execution_evidence": {"level": "intent_only", "physical_action_confirmed": False},
            "episode_binding": {
                "mode": "existing" if episode_id is not None else "create_independent",
                "episode_id": episode_id,
            },
            "initial_state": copy.deepcopy(initial_state),
            "trace_id": trace_id,
            "source_receipt_ref": None,
        }

    def _create_once(self, record: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_stored(record)
        path = self.receipt_path(record["action_id"])
        self.root.mkdir(parents=True, exist_ok=True)
        data = _canonical_json(record) + b"\n"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self.read(record["action_id"])
            if existing != record:
                raise ActionReceiptError("action_id_conflict")
            return copy.deepcopy(existing)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return copy.deepcopy(record)

    def _replace(self, record: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_stored(record)
        target = self.receipt_path(record["action_id"])
        if not target.exists():
            raise ActionReceiptError("receipt_not_found")
        descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=str(self.root))
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(_canonical_json(record) + b"\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return copy.deepcopy(record)

    @staticmethod
    def _validate_stored(record: Any) -> None:
        if not isinstance(record, dict) or set(record) != RECEIPT_FIELDS:
            raise ActionReceiptError("receipt_invalid")
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ActionReceiptError("receipt_invalid")
        action_id = record.get("action_id")
        if not isinstance(action_id, str) or ACTION_ID_PATTERN.fullmatch(action_id) is None:
            raise ActionReceiptError("receipt_invalid")
        if record.get("device_code") != EXPECTED_DEVICE_CODE or record.get("action_source") not in SOURCES:
            raise ActionReceiptError("receipt_invalid")
        if normalize_timestamp(record.get("created_at")) is None:
            raise ActionReceiptError("receipt_invalid")
        reference = record.get("reference_action_at")
        if reference is not None and normalize_timestamp(reference) is None:
            raise ActionReceiptError("receipt_invalid")
        command = record.get("command")
        if not isinstance(command, dict) or command.get("kind") != "water":
            raise ActionReceiptError("receipt_invalid")
        seconds = command.get("pump_seconds")
        if seconds is not None:
            _positive_seconds(seconds)
        if record.get("command_status") not in COMMAND_STATUS:
            raise ActionReceiptError("receipt_invalid")
        evidence = record.get("execution_evidence")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("physical_action_confirmed"), bool):
            raise ActionReceiptError("receipt_invalid")
        binding = record.get("episode_binding")
        if not isinstance(binding, dict) or set(binding) != {"mode", "episode_id"}:
            raise ActionReceiptError("receipt_invalid")
        if binding.get("mode") == "existing":
            if not isinstance(binding.get("episode_id"), str) or EPISODE_ID_PATTERN.fullmatch(binding["episode_id"]) is None:
                raise ActionReceiptError("receipt_invalid")
        elif binding != {"mode": "create_independent", "episode_id": None}:
            raise ActionReceiptError("receipt_invalid")
        if not _valid_state(record.get("initial_state")):
            raise ActionReceiptError("receipt_invalid")
        trace_id = record.get("trace_id")
        if trace_id is not None and (not isinstance(trace_id, str) or TRACE_ID_PATTERN.fullmatch(trace_id) is None):
            raise ActionReceiptError("receipt_invalid")
        if record.get("source_receipt_ref") is not None:
            raise ActionReceiptError("receipt_invalid")

        source = record["action_source"]
        status = record["command_status"]
        level = evidence.get("level")
        physical = evidence["physical_action_confirmed"]
        if source == "manual_confirmed":
            if (
                status != "manual_confirmed"
                or level != "manual_confirmed"
                or physical is not True
                or not isinstance(evidence.get("confirmed_by"), str)
                or not evidence["confirmed_by"].strip()
                or reference is None
                or trace_id is not None
                or binding != {"mode": "create_independent", "episode_id": None}
                or set(command) != {"kind", "pump_seconds"}
                or seconds is None
            ):
                raise ActionReceiptError("receipt_invalid")
            return

        if source == "phase3_native":
            if trace_id is not None or binding != {"mode": "create_independent", "episode_id": None}:
                raise ActionReceiptError("receipt_invalid")
            if set(command) != {"kind", "pump_seconds"} or seconds is None:
                raise ActionReceiptError("receipt_invalid")
        elif source == "controlled_execution":
            if trace_id is None or binding.get("mode") != "existing":
                raise ActionReceiptError("receipt_invalid")
            if set(command) != {"kind", "pump_seconds", "approval_id"}:
                raise ActionReceiptError("receipt_invalid")
            if not isinstance(command.get("approval_id"), str) or not command["approval_id"]:
                raise ActionReceiptError("receipt_invalid")
        else:
            raise ActionReceiptError("receipt_invalid")

        if status == "prepared":
            valid = level == "intent_only" and physical is False and reference is None
        elif status == "command_completed":
            valid = (
                level == "command_completed"
                and physical is False
                and reference is not None
                and seconds is not None
                and normalize_timestamp(evidence.get("mqtt_on_published_at")) is not None
                and normalize_timestamp(evidence.get("mqtt_off_published_at")) is not None
            )
        elif status == "command_incomplete":
            valid = (
                level == "command_incomplete"
                and physical is False
                and isinstance(evidence.get("reason"), str)
                and bool(evidence["reason"])
            )
        else:
            valid = False
        if not valid:
            raise ActionReceiptError("receipt_invalid")
