"""Single-frame image evidence storage for Vision V1."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np


class CaptureError(ValueError):
    """A sanitised failure to obtain or persist an image."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ImageEvidence:
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
            try:
                encoded, jpeg = cv2.imencode(".jpg", frame)
            except Exception as error:
                raise CaptureError("JPEG_ENCODE_FAILED") from error
            if not encoded:
                raise CaptureError("JPEG_ENCODE_FAILED")
            return CapturedFrame(
                jpeg_bytes=jpeg.tobytes(),
                captured_at=datetime.now(timezone.utc),
            )
        finally:
            try:
                stream.release()
            except Exception:
                pass


class ImageStore:
    """Store a decoded JPEG atomically beneath one configured evidence root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def save(self, jpeg_bytes: bytes, captured_at: datetime) -> ImageEvidence:
        if not jpeg_bytes:
            raise CaptureError("JPEG_INVALID")
        try:
            decoded = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception as error:
            raise CaptureError("JPEG_INVALID") from error
        if decoded is None:
            raise CaptureError("JPEG_INVALID")
        image_id = str(uuid4())
        relative = Path("images") / captured_at.date().isoformat() / f"{image_id}.jpg"
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
