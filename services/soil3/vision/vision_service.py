"""One-shot, non-control orchestration for Vision V1 across plant zones."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Sequence
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
    VisionArtifactRef,
    VisionRunResult,
    VisionValidationError,
    parse_zones,
    validate_vision_record,
    validate_vision_run_manifest,
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
            return parse_zones(payload)
        except (OSError, ValueError) as error:
            raise VisionConfigurationError() from error


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _provenance_for(service: "VisionService", raw: Mapping[str, object], zone: PlantZone, evidence: ImageEvidence, frame: CapturedFrame) -> dict[str, object]:
    """Add server-owned provenance to one model answer; the model never supplies any."""
    return {
        **dict(raw),
        "schema_version": "vision.v1",
        "device_code": service._device_code,
        "plant_zone": {"id": zone.zone_id, "rect": list(zone.rect), "label": zone.label},
        "image_id": evidence.image_id,
        "image_path": evidence.image_path,
        "image_sha256": evidence.image_sha256,
        "source_frame_id": service._frame_id,
        "source_frame_path": service._frame_path,
        "source_frame_sha256": service._frame_sha256,
        "previous_image_id": service._previous_id,
        "captured_at": _utc_timestamp(frame.captured_at),
        "analyzed_at": _utc_timestamp(datetime.now(timezone.utc)),
        "model": {"provider": "qwen", "name": service._model_name, "prompt_version": "vision.v1"},
    }


def _previous_crop(service: "VisionService", zone_id: str) -> tuple[str | None, bytes | None]:
    """Return the latest earlier crop of this same zone, if it can still be read.

    Zones are tracked separately so a plant is never compared against a different
    plant in another zone. A history record whose image is gone or corrupted is
    dropped entirely rather than reported as a change the model was never shown, so a
    failure to read history must never be recorded as a capture or analysis failure of
    the current attempt.
    """
    latest: tuple[str, ImageEvidence] | None = None
    records_root = service._data_root / "records"
    if records_root.is_dir():
        for path in records_root.rglob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, Mapping) or record.get("device_code") != service._device_code:
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
        return latest[1].image_id, service._store.load(latest[1])
    except CaptureError:
        return None, None


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
        # Provenance of the frame being observed, shared by every zone in one run.
        self._frame_id: str | None = None
        self._frame_path: str | None = None
        self._frame_sha256: str | None = None
        # The earlier crop of the zone being observed, set by _observe_zone._previous.
        self._previous_id: str | None = None

    def capture_and_analyze_once(self) -> VisionRunResult:
        try:
            frame = self._capture.capture_one()
            evidence = self._store.save_frame(frame.jpeg_bytes, frame.captured_at)
        except CaptureError as error:
            self._persist_failure("capture_failed", error.code)
            return VisionRunResult("capture_failed", None, ())

        self._frame_id, self._frame_path, self._frame_sha256 = (
            evidence.image_id,
            evidence.image_path,
            evidence.image_sha256,
        )
        outcomes = tuple(self._observe_zone(zone, frame) for zone in self._zones)
        distinct = {outcome.status for outcome in outcomes}
        status = distinct.pop() if len(distinct) == 1 else "partial"
        manifest_ref = None
        if any(outcome.artifact_ref is not None for outcome in outcomes):
            manifest_ref = self._persist_manifest(status, evidence.image_id, outcomes)
        return VisionRunResult(status, evidence.image_id, outcomes, manifest_ref)

    def _observe_zone(self, zone: PlantZone, frame: CapturedFrame) -> CaptureOutcome:
        """Observe one zone, writing every failure record about this zone alone."""

        def _previous() -> bytes | None:
            self._previous_id, previous_jpeg = _previous_crop(self, zone.zone_id)
            return previous_jpeg

        def _attach(raw: Mapping[str, object]) -> dict[str, object]:
            if set(raw) != OBSERVATION_FIELDS:
                raise VisionValidationError()
            return _provenance_for(self, raw, zone, evidence, frame)

        def _fail(status: str, error: Exception, image_id: str | None, image_sha256: str | None) -> CaptureOutcome:
            diagnostics = _provider_diagnostics(error)
            self._persist_failure(
                status,
                error.code,
                zone=zone,
                source_frame_id=self._frame_id,
                image_id=image_id,
                image_sha256=image_sha256,
                **diagnostics,
            )
            return replace(
                CaptureOutcome(status, zone.zone_id, image_id, None, error.code),
                **diagnostics,
            )

        try:
            evidence = self._store.save_crop(crop_to_zone(frame.jpeg_bytes, zone.rect), frame.captured_at)
        except CaptureError as error:
            return _fail("capture_failed", error, None, None)

        try:
            raw = self._analyzer.analyze(evidence, _previous())
            record = validate_vision_record(_attach(raw))
        except (AnalysisError, VisionValidationError) as error:
            return _fail("analysis_failed", error, evidence.image_id, evidence.image_sha256)

        artifact_ref = self._persist_record(record)
        status = "image_unusable" if record["image_quality"] == "unusable" else "success"
        return CaptureOutcome(status, zone.zone_id, evidence.image_id, record, None, artifact_ref=artifact_ref)

    def _persist_record(self, record: Mapping[str, object]) -> VisionArtifactRef:
        timestamp = datetime.fromisoformat(str(record["captured_at"]).replace("Z", "+00:00"))
        path = self._data_root / "records" / timestamp.date().isoformat() / f"{record['image_id']}.json"
        self._write_json(path, record)
        return self._artifact_ref("vision.v1", str(record["image_id"]), path)

    def _persist_manifest(
        self,
        status: str,
        frame_id: str,
        outcomes: Sequence[CaptureOutcome],
    ) -> VisionArtifactRef:
        created_at = datetime.now(timezone.utc)
        run_id = str(uuid4())
        value = {
            "schema_version": "vision_run.v1",
            "run_id": run_id,
            "device_code": self._device_code,
            "created_at": _utc_timestamp(created_at),
            "status": status,
            "frame_id": frame_id,
            "outcomes": [
                {
                    "zone_id": outcome.zone_id,
                    "status": outcome.status,
                    "artifact_ref": (
                        asdict(outcome.artifact_ref)
                        if outcome.artifact_ref is not None
                        else None
                    ),
                    "error_code": outcome.error_code,
                    "http_status": outcome.http_status,
                    "provider_error_code": outcome.provider_error_code,
                }
                for outcome in outcomes
            ],
        }
        value = validate_vision_run_manifest(value)
        path = self._data_root / "runs" / created_at.date().isoformat() / f"{run_id}.json"
        self._write_json(path, value)
        return self._artifact_ref("vision_run.v1", run_id, path)

    @staticmethod
    def _artifact_ref(schema_version: str, record_id: str, path: Path) -> VisionArtifactRef:
        return VisionArtifactRef(
            schema_version=schema_version,
            record_id=record_id,
            path=str(path.resolve()),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def _persist_failure(
        self,
        status: str,
        error_code: str,
        *,
        zone: PlantZone | None = None,
        source_frame_id: str | None = None,
        image_id: str | None = None,
        image_sha256: str | None = None,
        http_status: int | None = None,
        provider_error_code: str | None = None,
    ) -> None:
        occurred_at = datetime.now(timezone.utc)
        value: dict[str, object] = {
            "status": status,
            "attempt_id": str(uuid4()),
            "occurred_at": _utc_timestamp(occurred_at),
            "error_code": error_code,
        }
        if status == "analysis_failed":
            # An analysis failure always says what the provider replied, or that it never did.
            value["http_status"] = http_status
            value["provider_error_code"] = provider_error_code
        if zone is not None:
            value["plant_zone"] = zone.zone_id
        if source_frame_id is not None:
            value["source_frame_id"] = source_frame_id
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


def _provider_diagnostics(error: Exception) -> dict[str, object]:
    """What the provider itself reported, or two nulls when it never answered or only local rules failed."""
    if isinstance(error, AnalysisError):
        return {"http_status": error.http_status, "provider_error_code": error.provider_error_code}
    return {"http_status": None, "provider_error_code": None}


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
