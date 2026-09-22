"""Trace V1: local analysis records with no decision or execution authority."""
from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.soil3.telemetry.common import atomic_write_json, load_json


SCHEMA_VERSION = "trace.v1"
TRACE_ID_PATTERN = re.compile(r"^tr-[0-9a-f]{24}$")


class TraceError(Exception):
    """A Trace operation was refused with a stable code and reason list."""

    def __init__(self, code: str, reasons: list[str] | None = None):
        self.code = code
        self.reasons = list(reasons or [])
        detail = f": {', '.join(self.reasons)}" if self.reasons else ""
        super().__init__(f"{code}{detail}")


def utc_now() -> str:
    """Return the project's canonical UTC timestamp representation."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_trace_id() -> str:
    return "tr-" + uuid.uuid4().hex[:24]


def _new_record() -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "trace_id": new_trace_id(),
        "created_at": now,
        "updated_at": now,
        "decision": {
            "state_ref": None,
            "vision": {"availability": "not_requested", "ref": None},
            "experience": {"availability": "not_requested", "ref": None},
            "model_metrics": {
                "provider": None,
                "model": None,
                "token_usage": None,
                "cost": None,
                "latency": None,
            },
            "strategy_ref": None,
            "gate_ref": None,
            "gate_decision": None,
            "gate_reason_codes": None,
            "runner_ref": None,
            "bridge_ref": None,
            "episode_ref": None,
        },
        "execution": {
            "phase3_called": False,
            "physical_actions_performed": False,
        },
        "feedback_refs": [],
        "outcome_ref": None,
    }


class TraceStore:
    """Persist one trace record per generated identifier under a caller-owned root."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def create(self) -> dict[str, Any]:
        record = _new_record()
        self._write(record)
        return copy.deepcopy(record)

    def read(self, trace_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._load(trace_id))

    def trace_path(self, trace_id: str) -> Path:
        if not isinstance(trace_id, str) or not TRACE_ID_PATTERN.fullmatch(trace_id):
            raise TraceError("invalid_trace_id")
        return self.root / f"{trace_id}.json"

    def _write(self, record: dict[str, Any]) -> None:
        atomic_write_json(self.trace_path(record["trace_id"]), record)

    def _load(self, trace_id: str) -> dict[str, Any]:
        path = self.trace_path(trace_id)
        if not path.exists():
            raise TraceError("trace_not_found")
        value = load_json(path)
        if not isinstance(value, dict):
            raise TraceError("trace_invalid")
        return value
