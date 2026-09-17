"""Single-frame capture, zone cropping, and image evidence storage for Vision V1."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np


class CaptureError(ValueError):
    """A sanitised failure to obtain, crop, or persist an image."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ImageEvidence:
    """One stored JPEG and where it lives beneath the evidence root."""

    image_id: str
    image_path: str
    image_sha256: str


@dataclass(frozen=True)
class CapturedFrame:
    """One JPEG frame and its UTC capture time."""

    jpeg_bytes: bytes
    captured_at: datetime


class OpenCvRtspFrameCapture:
    """Capture exactly one RTSP frame and always release the connection."""

    def __init__(self, rtsp_url: str, capture_factory=cv2.VideoCapture) -> None:
        self._rtsp_url = rtsp_url
        self._capture_factory = capture_factory

    def capture_one(self) -> CapturedFrame:
        try:
            stream = self._capture_factory(self._rtsp_url)
        except Exception as error:
            raise CaptureError("RTSP_OPEN_FAILED") from error
        try:
            try:
                opened = stream is not None and stream.isOpened()
            except Exception as error:
                raise CaptureError("RTSP_OPEN_FAILED") from error
            if not opened:
                raise CaptureError("RTSP_OPEN_FAILED")
            try:
                received, frame = stream.read()
            except Exception as error:
                raise CaptureError("RTSP_READ_FAILED") from error
            if not received or frame is None:
                raise CaptureError("RTSP_READ_FAILED")
            return CapturedFrame(
                jpeg_bytes=_encode_jpeg(frame),
                captured_at=datetime.now(timezone.utc),
            )
        finally:
            try:
                stream.release()
            except Exception:
                pass


def _decode(jpeg_bytes: bytes) -> Any:
    if not jpeg_bytes:
        raise CaptureError("JPEG_INVALID")
    try:
        decoded = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as error:
        raise CaptureError("JPEG_INVALID") from error
    if decoded is None:
        raise CaptureError("JPEG_INVALID")
    return decoded


def _encode_jpeg(frame: Any) -> bytes:
    try:
        encoded, jpeg = cv2.imencode(".jpg", frame)
    except Exception as error:
        raise CaptureError("JPEG_ENCODE_FAILED") from error
    if not encoded:
        raise CaptureError("JPEG_ENCODE_FAILED")
    return jpeg.tobytes()


def crop_to_zone(jpeg_bytes: bytes, rect: Sequence[float]) -> bytes:
    """Crop one zone's normalized rect out of a decoded frame.

    The full frame is the stored evidence; this crop is only ever what the model
    is shown, so a wrong rect is visible in the record instead of silent.
    """
    frame = _decode(jpeg_bytes)
    height, width = frame.shape[0], frame.shape[1]
    x0, x1, y0, y1 = _pixel_bounds(rect, width, height)
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise CaptureError("ROI_EMPTY")
    return _encode_jpeg(frame[y0:y1, x0:x1])


def _pixel_bounds(rect: Sequence[float], width: int, height: int) -> tuple[int, int, int, int]:
    if len(rect) != 4:
        raise CaptureError("ROI_INVALID")
    try:
        left, top, rect_width, rect_height = (float(number) for number in rect)
    except (TypeError, ValueError) as error:
        raise CaptureError("ROI_INVALID") from error
    if not 0.0 <= left < 1.0 or not 0.0 <= top < 1.0 or rect_width <= 0.0 or rect_height <= 0.0:
        raise CaptureError("ROI_INVALID")
    if left + rect_width > 1.000001 or top + rect_height > 1.000001:
        raise CaptureError("ROI_OUT_OF_BOUNDS")
    x0 = min(max(int(left * width), 0), width)
    x1 = min(max(int(round((left + rect_width) * width)), x0), width)
    y0 = min(max(int(top * height), 0), height)
    y1 = min(max(int(round((top + rect_height) * height)), y0), height)
    return x0, x1, y0, y1


class EvidenceStore:
    """Store the whole frame and each zone crop atomically beneath one evidence root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def save_frame(self, jpeg_bytes: bytes, captured_at: datetime) -> ImageEvidence:
        return self._save("frames", jpeg_bytes, captured_at)

    def save_crop(self, jpeg_bytes: bytes, captured_at: datetime) -> ImageEvidence:
        # Re-encoding happens before storage, so this also proves the crop decodes.
        _decode(jpeg_bytes)
        return self._save("images", jpeg_bytes, captured_at)

    def load(self, evidence: ImageEvidence) -> bytes:
        try:
            value = (self._root / evidence.image_path).read_bytes()
        except OSError as error:
            raise CaptureError("IMAGE_EVIDENCE_UNAVAILABLE") from error
        if hashlib.sha256(value).hexdigest() != evidence.image_sha256:
            raise CaptureError("IMAGE_EVIDENCE_CORRUPTED")
        return value

    def _save(self, folder: str, jpeg_bytes: bytes, captured_at: datetime) -> ImageEvidence:
        if not jpeg_bytes:
            raise CaptureError("JPEG_INVALID")
        image_id = str(uuid4())
        relative = Path(folder) / captured_at.date().isoformat() / f"{image_id}.jpg"
        target = self._root / relative
        temporary = target.with_suffix(".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(jpeg_bytes)
            os.replace(temporary, target)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CaptureError("IMAGE_STORE_FAILED") from error
        return ImageEvidence(
            image_id=image_id,
            image_path=relative.as_posix(),
            image_sha256=hashlib.sha256(jpeg_bytes).hexdigest(),
        )
