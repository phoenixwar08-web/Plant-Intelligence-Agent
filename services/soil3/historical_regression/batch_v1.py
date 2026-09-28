"""Batch historical regression over existing replay_sample.v1 artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_FIELDS = {"schema_version", "cases"}
CASE_FIELDS = {"case_id", "replay_sample", "fixture_response", "labels"}
SUPPORTED_MODES = {"fixture", "live-provider"}


@dataclass(frozen=True)
class BatchCase:
    case_id: str
    replay_sample_path: Path
    fixture_response_path: Path | None
    labels: tuple[str, ...]


@dataclass(frozen=True)
class BatchManifest:
    schema_version: str
    path: Path
    cases: tuple[BatchCase, ...]


@dataclass(frozen=True)
class LoadedCaseInput:
    case: BatchCase
    sample_id: str
    sample_sha256: str
    state: dict[str, Any]


def _object_from_bytes(payload: bytes, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{name} must contain a JSON object") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return value


def _resolve(base: Path, value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def load_batch_manifest(path: Path, *, mode: str = "fixture") -> BatchManifest:
    """Load a historical regression manifest."""
    if mode not in SUPPORTED_MODES:
        raise ValueError("mode must be fixture or live-provider")
    path = Path(path).resolve()
    value = _object_from_bytes(path.read_bytes(), name="manifest")
    if set(value) != MANIFEST_FIELDS:
        raise ValueError("manifest fields are invalid")
    if value.get("schema_version") != "historical_regression_manifest.v1":
        raise ValueError("invalid historical regression manifest schema_version")
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("manifest cases must be a non-empty list")

    cases: list[BatchCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict) or not set(raw) <= CASE_FIELDS:
            raise ValueError("case fields are invalid")
        required = {"case_id", "replay_sample"}
        if mode == "fixture":
            required.add("fixture_response")
        if not required <= set(raw):
            missing = sorted(required - set(raw))
            raise ValueError("missing case field: " + ",".join(missing))

        case_id = raw.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        if case_id in seen:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen.add(case_id)

        labels = raw.get("labels", [])
        if (
            not isinstance(labels, list)
            or any(not isinstance(label, str) or not label.strip() for label in labels)
        ):
            raise ValueError("labels must be non-empty strings")
        fixture = raw.get("fixture_response")
        cases.append(
            BatchCase(
                case_id=case_id,
                replay_sample_path=_resolve(
                    path.parent, raw.get("replay_sample"), field="replay_sample"
                ),
                fixture_response_path=(
                    _resolve(path.parent, fixture, field="fixture_response")
                    if fixture is not None
                    else None
                ),
                labels=tuple(labels),
            )
        )
    return BatchManifest(value["schema_version"], path, tuple(cases))


def load_case_input(case: BatchCase) -> LoadedCaseInput:
    """Load and validate one case so malformed samples remain case-local."""
    payload = case.replay_sample_path.read_bytes()
    sample = _object_from_bytes(payload, name="replay sample")
    if sample.get("schema_version") != "replay_sample.v1":
        raise ValueError("case input must be replay_sample.v1")
    sample_id = sample.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise ValueError("replay sample_id must be a non-empty string")
    state = sample.get("state")
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != "state.v1"
        or state.get("device_code") != "soil3"
    ):
        raise ValueError("case input must contain a soil3 state.v1")
    return LoadedCaseInput(
        case=case,
        sample_id=sample_id,
        sample_sha256=hashlib.sha256(payload).hexdigest(),
        state=state,
    )
