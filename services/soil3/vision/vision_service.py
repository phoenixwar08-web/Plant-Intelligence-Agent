"""One-shot, non-control orchestration for Vision V1 across plant zones."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from services.soil3.vision.qwen_vision import AnalysisError, QwenVisionAnalyzer
from services.soil3.vision.vision_capture import (
    CaptureError,
    CapturedFrame,
    EvidenceStore,
    ImageEvidence,
    OpenCvRtspFrameCapture,
    crop_to_zone,
)
from services.soil3.vision.vision_v1 import (
    OBSERVATION_FIELDS,
    CaptureOutcome,
    PlantZone,
    VisionRunResult,
    VisionValidationError,
    parse_zones,
    validate_vision_record,
)


class VisionConfigurationError(ValueError):
    """Required Day 2 configuration is absent or invalid."""

    code = "VISION_CONFIGURATION_INVALID"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class VisionSettings:
    rtsp_url: str
    api_key: str
    base_url: str
    data_root: Path
    zones_path: Path
    model: str = "qwen3-vl-flash"

    @classmethod
    def from_environment(cls) -> "VisionSettings":
        required = {
            "rtsp_url": os.environ.get("SOIL3_CAMERA_RTSP_URL"),
            "api_key": os.environ.get("QWEN_API_KEY"),
            "base_url": os.environ.get("QWEN_BASE_URL"),
            "data_root": os.environ.get("SOIL3_VISION_DATA_DIR"),
            "zones_path": os.environ.get("SOIL3_VISION_ZONES_PATH"),
        }
        if not all(required.values()):
            raise VisionConfigurationError()
        return cls(
            rtsp_url=required["rtsp_url"],
            api_key=required["api_key"],
            base_url=required["base_url"],
            data_root=Path(required["data_root"]),
            zones_path=Path(required["zones_path"]),
            model=os.environ.get("QWEN_MODEL", "qwen3-vl-flash"),
        )

    def load_zones(self) -> tuple[PlantZone, ...]:
        try:
            payload = json.loads(self.zones_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise VisionConfigurationError() from error
        return parse_zones(payload)


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class VisionService:
    """Observe every configured zone from one frame, isolating per-zone failures."""

    def __init__(
        self,
        *,
        data_root: Path,
        device_code: str,
        capture,
        store: EvidenceStore,
        analyzer,
        zones: Sequence[PlantZone],
        model_name: str = "qwen3-vl-flash",
    ) -> None:
        self._zones = tuple(zones)
        if not self._zones:
            raise VisionConfigurationError()
        self._data_root = Path(data_root)
        self._device_code = device_code
        self._capture = capture
        self._store = store
        self._analyzer = analyzer
        self._model_name = model_name

    def capture_and_analyze_once(self) -> VisionRunResult:
        try:
            frame = self._capture.capture_one()
            frame_evidence = self._store.save_frame(frame.jpeg_bytes, frame.captured_at)
        except CaptureError as error:
            self._persist_failure("capture_failed", error.code)
            return VisionRunResult("capture_failed", None, ())

        outcomes = tuple(self._observe_zone(zone, frame, frame_evidence) for zone in self._zones)
        distinct = {outcome.status for outcome in outcomes}
        return VisionRunResult(distinct.pop() if len(distinct) == 1 else "partial", frame_evidence.image_id, outcomes)

    def _observe_zone(self, zone: PlantZone, frame: CapturedFrame, frame_evidence: ImageEvidence) -> CaptureOutcome:
        try:
            evidence = self._store.save_crop(crop_to_zone(frame.jpeg_bytes, zone.rect), frame.captured_at)
        except CaptureError as error:
            self._persist_failure("capture_failed", error.code, zone=zone, frame_evidence=frame_evidence)
            return CaptureOutcome("capture_failed", zone.zone_id, None, None, error.code)

        previous_id, previous_jpeg = self._previous(zone.zone_id)
        try:
            raw = self._analyzer.analyze(evidence, previous_jpeg)
            record = validate_vision_record(
                self._attach_provenance(raw, zone, evidence, frame_evidence, previous_id, frame.captured_at)
            )
        except (AnalysisError, VisionValidationError) as error:
            self._persist_failure(
                "analysis_failed",
                error.code,
                zone=zone,
                frame_evidence=frame_evidence,
                image_id=evidence.image_id,
                image_sha256=evidence.image_sha256,
            )
            return CaptureOutcome("analysis_failed", zone.zone_id, evidence.image_id, None, error.code)

        self._persist_record(record)
        status = "image_unusable" if record["image_quality"] == "unusable" else "success"
        return CaptureOutcome(status, zone.zone_id, evidence.image_id, record, None)

    def _attach_provenance(
        self,
        raw: Mapping[str, object],
        zone: PlantZone,
        evidence: ImageEvidence,
        frame_evidence: ImageEvidence,
        previous_id: str | None,
        captured_at: datetime,
    ) -> dict[str, object]:
        if set(raw) != OBSERVATION_FIELDS:
            raise VisionValidationError()
        return {
            **raw,
            "schema_version": "vision.v1",
            "device_code": self._device_code,
            "plant_zone": {"id": zone.zone_id, "rect": list(zone.rect), "label": zone.label},
            "image_id": evidence.image_id,
            "image_path": evidence.image_path,
            "image_sha256": evidence.image_sha256,
            "source_frame_id": frame_evidence.image_id,
            "source_frame_path": frame_evidence.image_path,
            "source_frame_sha256": frame_evidence.image_sha256,
            "previous_image_id": previous_id,
            "captured_at": _utc_timestamp(captured_at),
            "analyzed_at": _utc_timestamp(datetime.now(timezone.utc)),
            "model": {"provider": "qwen", "name": self._model_name, "prompt_version": "vision.v1"},
        }

    def _previous(self, zone_id: str) -> tuple[str | None, bytes | None]:
        """Return the latest earlier observation of this same zone, if its crop is readable.

        Zones are tracked separately so two plants never compare against each other.
        A history record whose image can no longer be read is dropped entirely rather
        than reported as a change the model was never shown.
        """
        latest: tuple[str, ImageEvidence] | None = None
        records_root = self._data_root / "records"
        if records_root.is_dir():
            for path in records_root.rglob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(record, Mapping) or record.get("device_code") != self._device_code:
                    continue
                if record.get("schema_version") != "vision.v1":
                    continue
                zone = record.get("plant_zone")
                if not isinstance(zone, Mapping) or zone.get("id") != zone_id:
                    continue
                captured_at = record.get("captured_at")
                evidence = _evidence_from(record)
                if not isinstance(captured_at, str) or evidence is None:
                    continue
                if latest is None or captured_at > latest[0]:
                    latest = (captured_at, evidence)
        if latest is None:
            return None, None
        try:
            return latest[1].image_id, self._store.load(latest[1])
        except CaptureError:
            return None, None

    def _persist_record(self, record: Mapping[str, object]) -> None:
        timestamp = datetime.fromisoformat(str(record["captured_at"]).replace("Z", "+00:00"))
        path = self._data_root / "records" / timestamp.date().isoformat() / f"{record['image_id']}.json"
        self._write_json(path, record)

    def _persist_failure(
        self,
        status: str,
        error_code: str,
        *,
        zone: PlantZone | None = None,
        frame_evidence: ImageEvidence | None = None,
        image_id: str | None = None,
        image_sha256: str | None = None,
    ) -> None:
        occurred_at = datetime.now(timezone.utc)
        value: dict[str, object] = {
            "status": status,
            "attempt_id": str(uuid4()),
            "occurred_at": _utc_timestamp(occurred_at),
            "error_code": error_code,
        }
        if zone is not None:
            value["plant_zone"] = zone.zone_id
        if frame_evidence is not None:
            value["source_frame_id"] = frame_evidence.image_id
        if image_id is not None:
            value["image_id"] = image_id
            value["image_sha256"] = image_sha256
        path = self._data_root / "failures" / occurred_at.date().isoformat() / f"{value['attempt_id']}.json"
        self._write_json(path, value)

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)


def _evidence_from(record: Mapping[str, object]) -> ImageEvidence | None:
    """Rebuild stored-image provenance from a persisted record, if it is complete."""
    values = [record.get(key) for key in ("image_id", "image_path", "image_sha256")]
    if any(not isinstance(value, str) or not value for value in values):
        return None
    return ImageEvidence(image_id=values[0], image_path=values[1], image_sha256=values[2])


def capture_and_analyze_once() -> VisionRunResult:
    """Run the Day 2 API using environment-only configuration."""
    settings = VisionSettings.from_environment()
    return VisionService(
        data_root=settings.data_root,
        device_code="soil3",
        capture=OpenCvRtspFrameCapture(settings.rtsp_url),
        store=EvidenceStore(settings.data_root),
        analyzer=QwenVisionAnalyzer(
            base_url=settings.base_url,
            api_key=settings.api_key,
            image_root=settings.data_root,
            model=settings.model,
        ),
        zones=settings.load_zones(),
        model_name=settings.model,
    ).capture_and_analyze_once()
