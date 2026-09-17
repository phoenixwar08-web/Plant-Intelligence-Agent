import hashlib
import importlib
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import cv2
import numpy as np

try:
    _contract = importlib.import_module("services.soil3.vision.vision_v1")
except ModuleNotFoundError:
    _contract = None

try:
    _capture = importlib.import_module("services.soil3.vision.vision_capture")
except ModuleNotFoundError:
    _capture = None


_encoded, ONE_PIXEL_JPEG = cv2.imencode(".jpg", np.zeros((1, 1, 3), dtype=np.uint8))
assert _encoded
ONE_PIXEL_JPEG = ONE_PIXEL_JPEG.tobytes()


def vision_record(**overrides):
    record = {
        "schema_version": "vision.v1",
        "device_code": "soil3",
        "image_id": "3f275b48-b86a-4db5-b720-3820ed84bdce",
        "previous_image_id": None,
        "captured_at": "2026-09-17T00:00:00Z",
        "analyzed_at": "2026-09-17T00:00:01Z",
        "image_sha256": hashlib.sha256(b"image").hexdigest(),
        "image_path": "images/2026-09-17/3f275b48-b86a-4db5-b720-3820ed84bdce.jpg",
        "image_quality": "unusable",
        "leaf_droop": None,
        "leaf_spread": None,
        "wilting": None,
        "yellowing": None,
        "visible_damage": None,
        "overall_visual_state": "unavailable",
        "change_vs_previous": "unknown",
        "confidence": None,
        "model": {
            "provider": "qwen",
            "name": "qwen3-vl-flash",
            "prompt_version": "vision.v1",
        },
    }
    record.update(overrides)
    return record


class VisionV1Tests(unittest.TestCase):
    def test_unusable_image_is_a_valid_unknown_observation(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        record = _contract.validate_vision_record(vision_record())
        self.assertEqual(record["image_quality"], "unusable")
        self.assertIsNone(record["wilting"])
        self.assertEqual(record["overall_visual_state"], "unavailable")

    def test_first_image_cannot_claim_change_without_a_previous_image(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(
                vision_record(change_vs_previous="worsened")
            )

    def test_confidence_must_be_a_finite_probability(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(
                vision_record(
                    image_quality="good",
                    leaf_droop="none",
                    leaf_spread="normal",
                    wilting=False,
                    yellowing="none",
                    visible_damage="none",
                    overall_visual_state="healthy",
                    confidence=1.01,
                )
            )

    def test_unusable_image_cannot_claim_wilting(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(wilting=True))

    def test_rejects_an_unknown_field(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(unreviewed_note="not allowed"))


class ImageEvidenceTests(unittest.TestCase):
    def test_store_writes_immutable_jpeg_and_sha256_trace(self):
        self.assertIsNotNone(_capture, "services.soil3.vision.vision_capture must define ImageStore")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _capture.ImageStore(root).save(
                ONE_PIXEL_JPEG,
                datetime(2026, 9, 17, tzinfo=timezone.utc),
            )
            target = root / evidence.image_path
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), ONE_PIXEL_JPEG)
            self.assertEqual(evidence.image_sha256, hashlib.sha256(ONE_PIXEL_JPEG).hexdigest())
            self.assertTrue(evidence.image_path.startswith("images/2026-09-17/"))

    def test_rtsp_adapter_releases_connection_after_one_frame(self):
        self.assertIsNotNone(
            _capture,
            "services.soil3.vision.vision_capture must define OpenCvRtspFrameCapture",
        )

        class FakeVideoCapture:
            def __init__(self) -> None:
                self.released = False

            def isOpened(self) -> bool:
                return True

            def read(self):
                return True, np.zeros((1, 1, 3), dtype=np.uint8)

            def release(self) -> None:
                self.released = True

        handle = FakeVideoCapture()
        capture = _capture.OpenCvRtspFrameCapture(
            "rtsp://camera.example/stream",
            capture_factory=lambda _: handle,
        )

        frame = capture.capture_one()

        self.assertTrue(handle.released)
        self.assertEqual(frame.jpeg_bytes[:2], b"\xff\xd8")
        self.assertEqual(frame.captured_at.tzinfo, timezone.utc)

    def test_rtsp_read_failure_is_sanitized_and_released(self):
        self.assertIsNotNone(_capture)

        class UnreadableVideoCapture:
            def __init__(self) -> None:
                self.released = False

            def isOpened(self) -> bool:
                return True

            def read(self):
                return False, None

            def release(self) -> None:
                self.released = True

        handle = UnreadableVideoCapture()
        capture = _capture.OpenCvRtspFrameCapture(
            "rtsp://camera.example/stream",
            capture_factory=lambda _: handle,
        )

        with self.assertRaises(_capture.CaptureError) as raised:
            capture.capture_one()

        self.assertEqual(raised.exception.code, "RTSP_READ_FAILED")
        self.assertTrue(handle.released)


if __name__ == "__main__":
    unittest.main()
