"""One-shot, non-control orchestration for Vision V1."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from services.soil3.vision.qwen_vision import AnalysisError, QwenVisionAnalyzer
from services.soil3.vision.vision_capture import CaptureError, ImageStore, OpenCvRtspFrameCapture
from services.soil3.vision.vision_v1 import CaptureOutcome, VisionValidationError, validate_vision_record


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
    model: str = "qwen3-vl-flash"

    @classmethod
    def from_environment(cls) -> "VisionSettings":
        required = {
            "rtsp_url": os.environ.get("SOIL3_CAMERA_RTSP_URL"),
            "api_key": os.environ.get("QWEN_API_KEY"),
            "base_url": os.environ.get("QWEN_BASE_URL"),
            "data_root": os.environ.get("SOIL3_VISION_DATA_DIR"),
        }
        if not all(required.values()):
            raise VisionConfigurationError()
        return cls(
            rtsp_url=required["rtsp_url"],
            api_key=required["api_key"],
            base_url=required["base_url"],
            data_root=Path(required["data_root"]),
            model=os.environ.get("QWEN_MODEL", "qwen3-vl-flash"),
        )


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class VisionService:
    """Compose capture, evidence, analysis, validation, and persistence once."""

    def __init__(
        self,
        *,
        data_root: Path,
        device_code: str,
        capture,
        image_store: ImageStore,
        analyzer,
        model_name: str = "qwen3-vl-flash",
    ) -> None:
        self._data_root = Path(data_root)
        self._device_code = device_code
        self._capture = capture
        self._image_store = image_store
        self._analyzer = analyzer
        self._model_name = model_name

    def capture_and_analyze_once(self) -> CaptureOutcome:
        try:
            frame = self._capture.capture_one()
            evidence = self._image_store.save(frame.jpeg_bytes, frame.captured_at)
        except CaptureError as error:
            self._persist_failure("capture_failed", error.code)
            return CaptureOutcome("capture_failed", None, None, error.code)

        try:
            raw = self._analyzer.analyze(evidence, self._previous_image_id())
            record = validate_vision_record(self._attach_evidence(raw, evidence, frame.captured_at))
        except (AnalysisError, VisionValidationError) as error:
            self._persist_failure(
                "analysis_failed",
                error.code,
                image_id=evidence.image_id,
                image_sha256=evidence.image_sha256,
            )
            return CaptureOutcome("analysis_failed", evidence.image_id, None, error.code)

        self._persist_record(record)
        status = "image_unusable" if record["image_quality"] == "unusable" else "success"
        return CaptureOutcome(status, evidence.image_id, record, None)

    def _attach_evidence(self, raw: Mapping[str, object], evidence, captured_at: datetime) -> dict[str, object]:
        observation_fields = {
            "image_quality",
            "leaf_droop",
            "leaf_spread",
            "wilting",
            "yellowing",
            "visible_damage",
            "overall_visual_state",
            "change_vs_previous",
            "confidence",
        }
        if set(raw) != observation_fields:
            raise VisionValidationError()
        return {
            **raw,
            "schema_version": "vision.v1",
            "device_code": self._device_code,
            "image_id": evidence.image_id,
            "previous_image_id": self._previous_image_id(),
            "captured_at": _utc_timestamp(captured_at),
            "analyzed_at": _utc_timestamp(datetime.now(timezone.utc)),
            "image_sha256": evidence.image_sha256,
            "image_path": evidence.image_path,
            "model": {
                "provider": "qwen",
                "name": self._model_name,
                "prompt_version": "vision.v1",
            },
        }

    def _previous_image_id(self) -> str | None:
        candidates: list[tuple[str, str]] = []
        records_root = self._data_root / "records"
        if not records_root.is_dir():
            return None
        for path in records_root.rglob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("device_code") == self._device_code:
                    candidates.append((record["captured_at"], record["image_id"]))
            except (OSError, KeyError, TypeError, ValueError):
                continue
        return max(candidates, default=("", None))[1]

    def _persist_record(self, record: Mapping[str, object]) -> None:
        timestamp = datetime.fromisoformat(record["captured_at"].replace("Z", "+00:00"))
        path = self._data_root / "records" / timestamp.date().isoformat() / f"{record['image_id']}.json"
        self._write_json(path, record)

    def _persist_failure(
        self,
        status: str,
        error_code: str,
        *,
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


def capture_and_analyze_once() -> CaptureOutcome:
    """Run the Day 2 API using environment-only configuration."""
    settings = VisionSettings.from_environment()
    return VisionService(
        data_root=settings.data_root,
        device_code="soil3",
        capture=OpenCvRtspFrameCapture(settings.rtsp_url),
        image_store=ImageStore(settings.data_root),
        analyzer=QwenVisionAnalyzer(
            base_url=settings.base_url,
            api_key=settings.api_key,
            image_root=settings.data_root,
            model=settings.model,
        ),
        model_name=settings.model,
    ).capture_and_analyze_once()
