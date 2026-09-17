"""Validation for structured, non-executing Vision V1 observations."""
from __future__ import annotations

from collections.abc import Mapping
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


REQUIRED_FIELDS = frozenset({
    "schema_version",
    "device_code",
    "image_id",
    "previous_image_id",
    "captured_at",
    "analyzed_at",
    "image_sha256",
    "image_path",
    "image_quality",
    "leaf_droop",
    "leaf_spread",
    "wilting",
    "yellowing",
    "visible_damage",
    "overall_visual_state",
    "change_vs_previous",
    "confidence",
    "model",
})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_QUALITY = frozenset({"good", "poor", "unusable"})
_DROOP = frozenset({"none", "mild", "moderate", "severe"})
_SPREAD = frozenset({"closed", "normal", "wide"})
_VISUAL_STATE = frozenset({"healthy", "attention", "poor", "unavailable"})
_CHANGE = frozenset({"improved", "stable", "worsened", "unknown"})


def _valid_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _valid_nullable_enum(value: Any, allowed: frozenset[str]) -> bool:
    return value is None or value in allowed


def _valid_image_path(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def _valid_model(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("provider") == "qwen"
        and all(isinstance(value.get(key), str) and value[key] for key in ("provider", "name", "prompt_version"))
    )


def _valid_confidence(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _valid_unusable_record(value: Mapping[str, Any]) -> bool:
    return (
        all(value[key] is None for key in ("leaf_droop", "leaf_spread", "wilting", "yellowing", "visible_damage"))
        and value["confidence"] is None
        and value["overall_visual_state"] == "unavailable"
        and value["change_vs_previous"] == "unknown"
    )


def validate_vision_record(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated copy of one persisted Vision V1 observation."""
    if not isinstance(value, Mapping) or set(value) != REQUIRED_FIELDS:
        raise VisionValidationError()
    if value["schema_version"] != "vision.v1":
        raise VisionValidationError()
    if not isinstance(value["device_code"], str) or not value["device_code"]:
        raise VisionValidationError()
    if not _valid_uuid(value["image_id"]):
        raise VisionValidationError()
    if value["previous_image_id"] is not None and not _valid_uuid(value["previous_image_id"]):
        raise VisionValidationError()
    if not _valid_timestamp(value["captured_at"]) or not _valid_timestamp(value["analyzed_at"]):
        raise VisionValidationError()
    if not isinstance(value["image_sha256"], str) or not _SHA256.fullmatch(value["image_sha256"]):
        raise VisionValidationError()
    if not _valid_image_path(value["image_path"]):
        raise VisionValidationError()
    if value["image_quality"] not in _IMAGE_QUALITY:
        raise VisionValidationError()
    if not _valid_nullable_enum(value["leaf_droop"], _DROOP):
        raise VisionValidationError()
    if not _valid_nullable_enum(value["leaf_spread"], _SPREAD):
        raise VisionValidationError()
    if value["wilting"] is not None and not isinstance(value["wilting"], bool):
        raise VisionValidationError()
    if not _valid_nullable_enum(value["yellowing"], _DROOP):
        raise VisionValidationError()
    if not _valid_nullable_enum(value["visible_damage"], _DROOP):
        raise VisionValidationError()
    if value["overall_visual_state"] not in _VISUAL_STATE or value["change_vs_previous"] not in _CHANGE:
        raise VisionValidationError()
    if value["confidence"] is not None and not _valid_confidence(value["confidence"]):
        raise VisionValidationError()
    if not _valid_model(value["model"]):
        raise VisionValidationError()
    if value["previous_image_id"] is None and value["change_vs_previous"] != "unknown":
        raise VisionValidationError()
    if value["image_quality"] == "unusable" and not _valid_unusable_record(value):
        raise VisionValidationError()
    return dict(value)
