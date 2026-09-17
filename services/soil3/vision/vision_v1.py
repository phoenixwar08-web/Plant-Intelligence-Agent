"""Validation for structured, non-executing Vision V1 observations.

Vision V1 describes what the camera shows. It never recommends or commands
anything, and it reports one record per fixed plant zone.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import PurePosixPath
import re
from typing import Any
from uuid import UUID


class VisionValidationError(ValueError):
    """Raised when a value cannot represent a Vision V1 observation."""

    code = "INVALID_VISION_RECORD"

    def __init__(self) -> None:
        super().__init__(self.code)


class ZoneConfigurationError(ValueError):
    """Raised when the plant-zone configuration file is absent or invalid."""

    code = "VISION_ZONES_INVALID"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class CaptureOutcome:
    """The result of observing one plant zone, whether or not a record exists."""

    status: str
    zone_id: str | None
    image_id: str | None
    vision: dict[str, Any] | None
    error_code: str | None
    http_status: int | None = None
    provider_error_code: str | None = None


@dataclass(frozen=True)
class VisionRunResult:
    """The result of one capture cycle across every configured plant zone."""

    status: str
    frame_id: str | None
    outcomes: tuple[CaptureOutcome, ...]


@dataclass(frozen=True)
class PlantZone:
    """A fixed region of the camera frame that is assessed on its own."""

    zone_id: str
    rect: tuple[float, float, float, float]
    label: str = ""


SEVERITY_FIELDS = (
    "leaf_droop",
    "yellowing",
    "visible_damage",
    "browning",
    "leaf_curl",
    "spots_or_lesions",
    "leaf_loss",
    "stem_posture",
    "occlusion",
    "target_ambiguity",
)

# The exact field set the model must return for one zone.
OBSERVATION_FIELDS = frozenset(
    {
        "image_quality",
        "target_detected",
        *SEVERITY_FIELDS,
        "leaf_spread",
        "wilting",
        "overall_visual_state",
        "change_vs_previous",
        "confidence",
    }
)

# Evidence fields of one zone, excluding quality, state, change and confidence.
_ASSESSABLE_FIELDS = (*SEVERITY_FIELDS, "leaf_spread", "wilting")

# The persisted record: the model's observations plus server-side provenance.
REQUIRED_FIELDS = OBSERVATION_FIELDS | {
    "schema_version",
    "device_code",
    "plant_zone",
    "image_id",
    "image_path",
    "image_sha256",
    "source_frame_id",
    "source_frame_path",
    "source_frame_sha256",
    "previous_image_id",
    "captured_at",
    "analyzed_at",
    "model",
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZONE_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_IMAGE_QUALITY = frozenset({"good", "poor", "unusable"})
_SEVERITY = frozenset({"none", "mild", "moderate", "severe"})
_SPREAD = frozenset({"closed", "normal", "wide"})
_VISUAL_STATE = frozenset(
    {"normal", "mild_abnormality", "obvious_abnormality", "severe_abnormality", "unavailable"}
)
_CHANGE = frozenset({"improved", "stable", "worsened", "unknown"})


def _valid_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except (ValueError, TypeError):
        return False
    return True


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_nullable_enum(value: Any, allowed: frozenset[str]) -> bool:
    return value is None or value in allowed


def _valid_nullable_boolean(value: Any) -> bool:
    return value is None or isinstance(value, bool)


def _valid_image_path(value: Any, folder: str) -> bool:
    return (
        isinstance(value, str)
        and PurePosixPath(value).parts[:1] == (folder,)
        and PurePosixPath(value).suffix.lower() in {".jpg", ".jpeg"}
    )


def _valid_model(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"provider", "name", "prompt_version"}
        and value.get("provider") == "qwen"
        and all(isinstance(value.get(key), str) and value[key] for key in ("provider", "name", "prompt_version"))
    )


def _valid_plant_zone(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"id", "rect", "label"}:
        return False
    if not isinstance(value["id"], str) or not _ZONE_ID.fullmatch(value["id"]):
        return False
    if not isinstance(value["label"], str):
        return False
    return _valid_rect(value["rect"])


def _valid_rect(value: Any) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return False
    if any(not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number) for number in value):
        return False
    x, y, width, height = (float(number) for number in value)
    return 0.0 <= x < 1.0 and 0.0 <= y < 1.0 and 0.0 < width <= 1.0 and 0.0 < height <= 1.0 and x + width <= 1.000001 and y + height <= 1.000001


def parse_zones(payload: Any) -> tuple[PlantZone, ...]:
    """Validate the plant-zone configuration document into ordered zones."""
    if not isinstance(payload, Mapping) or set(payload) != {"zones"}:
        raise ZoneConfigurationError()
    entries = payload["zones"]
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)) or not 1 <= len(entries) <= 8:
        raise ZoneConfigurationError()
    zones: list[PlantZone] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"id", "rect"} | ({"label"} if "label" in entry else set()):
            raise ZoneConfigurationError()
        zone_id = entry["id"]
        label = entry.get("label", "")
        if not isinstance(zone_id, str) or not _ZONE_ID.fullmatch(zone_id) or zone_id in seen:
            raise ZoneConfigurationError()
        if not isinstance(label, str) or not _valid_rect(entry["rect"]):
            raise ZoneConfigurationError()
        seen.add(zone_id)
        zones.append(PlantZone(zone_id=zone_id, rect=tuple(float(number) for number in entry["rect"]), label=label))
    return tuple(zones)


def _consistent_with_no_target(value: Mapping[str, Any]) -> bool:
    return (
        all(value[key] is None for key in _ASSESSABLE_FIELDS)
        and value["confidence"] is None
        and value["overall_visual_state"] == "unavailable"
        and value["change_vs_previous"] == "unknown"
    )


def _valid_confidence(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _validate_observation_consistency(value: Mapping[str, Any]) -> None:
    """Reject records whose claims are not backed by the fields they report."""
    if value["image_quality"] == "unusable":
        # An unusable image cannot support any observation at all.
        if not _consistent_with_no_target(value):
            raise VisionValidationError()
        return
    if value["target_detected"] is not True:
        # No confirmed plant: the zone is reported as unavailable, never as healthy.
        if not _consistent_with_no_target(value):
            raise VisionValidationError()
        return
    if value["overall_visual_state"] != "unavailable" and all(
        value[key] is None for key in _ASSESSABLE_FIELDS
    ):
        # A described visual state must cite at least one observed field.
        raise VisionValidationError()


def validate_vision_record(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated copy of one persisted Vision V1 zone observation."""
    if not isinstance(value, Mapping) or set(value) != REQUIRED_FIELDS:
        raise VisionValidationError()
    if value["schema_version"] != "vision.v1":
        raise VisionValidationError()
    if not isinstance(value["device_code"], str) or not value["device_code"]:
        raise VisionValidationError()
    if not _valid_plant_zone(value["plant_zone"]):
        raise VisionValidationError()
    if not _valid_uuid(value["image_id"]):
        raise VisionValidationError()
    if value["previous_image_id"] is not None and not _valid_uuid(value["previous_image_id"]):
        raise VisionValidationError()
    if value["previous_image_id"] == value["image_id"]:
        # A zone is tracked against an earlier observation, never against itself.
        raise VisionValidationError()
    if not _valid_timestamp(value["captured_at"]) or not _valid_timestamp(value["analyzed_at"]):
        raise VisionValidationError()
    if not isinstance(value["image_sha256"], str) or not _SHA256.fullmatch(value["image_sha256"]):
        raise VisionValidationError()
    if not _valid_image_path(value["image_path"], "images"):
        raise VisionValidationError()
    for key in ("source_frame_id", "source_frame_path", "source_frame_sha256"):
        if not isinstance(value[key], str) or not value[key]:
            raise VisionValidationError()
    if not _valid_uuid(value["source_frame_id"]):
        raise VisionValidationError()
    if not _valid_image_path(value["source_frame_path"], "frames"):
        raise VisionValidationError()
    if value["source_frame_id"] == value["image_id"] or value["source_frame_path"] == value["image_path"]:
        # The whole frame and the zone crop are always two distinct stored files.
        raise VisionValidationError()
    if not _SHA256.fullmatch(value["source_frame_sha256"]):
        raise VisionValidationError()
    if value["image_quality"] not in _IMAGE_QUALITY:
        raise VisionValidationError()
    if not _valid_nullable_boolean(value["target_detected"]):
        raise VisionValidationError()
    for key in SEVERITY_FIELDS:
        if not _valid_nullable_enum(value[key], _SEVERITY):
            raise VisionValidationError()
    if not _valid_nullable_enum(value["leaf_spread"], _SPREAD):
        raise VisionValidationError()
    if value["wilting"] is not None and not isinstance(value["wilting"], bool):
        raise VisionValidationError()
    if value["overall_visual_state"] not in _VISUAL_STATE or value["change_vs_previous"] not in _CHANGE:
        raise VisionValidationError()
    if value["confidence"] is not None and not _valid_confidence(value["confidence"]):
        raise VisionValidationError()
    if not _valid_model(value["model"]):
        raise VisionValidationError()
    if value["previous_image_id"] is None and value["change_vs_previous"] != "unknown":
        # A change claim requires the earlier image of this same zone to have been supplied.
        raise VisionValidationError()
    _validate_observation_consistency(value)
    return dict(value)
