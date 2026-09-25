"""Public repository-internal Day 2 Vision V1 API.

Non-executing: observations are recorded per plant zone and nothing here
schedules, commands, or controls a device.
"""

from services.soil3.vision.vision_capture import EvidenceStore, crop_to_zone
from services.soil3.vision.vision_service import (
    VisionConfigurationError,
    VisionService,
    capture_and_analyze_once,
)
from services.soil3.vision.vision_v1 import (
    OBSERVATION_FIELDS,
    CaptureOutcome,
    PlantZone,
    VisionArtifactRef,
    VisionRunResult,
    VisionRunValidationError,
    parse_zones,
    validate_vision_record,
    validate_vision_run_manifest,
)

__all__ = [
    "EvidenceStore",
    "OBSERVATION_FIELDS",
    "CaptureOutcome",
    "PlantZone",
    "VisionArtifactRef",
    "VisionRunResult",
    "VisionRunValidationError",
    "VisionConfigurationError",
    "VisionService",
    "capture_and_analyze_once",
    "crop_to_zone",
    "parse_zones",
    "validate_vision_record",
    "validate_vision_run_manifest",
]
