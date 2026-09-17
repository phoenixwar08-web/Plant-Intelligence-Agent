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

try:
    _service = importlib.import_module("services.soil3.vision.vision_service")
except ModuleNotFoundError:
    _service = None


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


class VisionServiceTests(unittest.TestCase):
    class FakeCapture:
        def __init__(self, result) -> None:
            self._result = result

        def capture_one(self):
            if isinstance(self._result, Exception):
                raise self._result
            return self._result

    class FakeAnalyzer:
        def __init__(self, result) -> None:
            self._result = result

        def analyze(self, evidence, previous_image_id):
            if isinstance(self._result, Exception):
                raise self._result
            return dict(self._result)

    @staticmethod
    def _unusable_observation():
        return {
            "image_quality": "unusable",
            "leaf_droop": None,
            "leaf_spread": None,
            "wilting": None,
            "yellowing": None,
            "visible_damage": None,
            "overall_visual_state": "unavailable",
            "change_vs_previous": "unknown",
            "confidence": None,
        }

    @staticmethod
    def _good_observation(change="unknown"):
        return {
            "image_quality": "good",
            "leaf_droop": "none",
            "leaf_spread": "normal",
            "wilting": False,
            "yellowing": "none",
            "visible_damage": "none",
            "overall_visual_state": "healthy",
            "change_vs_previous": change,
            "confidence": 0.9,
        }

    def _service_with(self, root, capture_result, analyzer_result):
        return _service.VisionService(
            data_root=root,
            device_code="soil3",
            capture=self.FakeCapture(capture_result),
            image_store=_capture.ImageStore(root),
            analyzer=self.FakeAnalyzer(analyzer_result),
        )

    def _frame(self):
        return _capture.CapturedFrame(
            jpeg_bytes=ONE_PIXEL_JPEG,
            captured_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        )

    def test_capture_failure_writes_no_image_or_vision(self):
        self.assertIsNotNone(_service, "services.soil3.vision.vision_service must define VisionService")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            outcome = self._service_with(
                root,
                _capture.CaptureError("RTSP_OPEN_FAILED"),
                self._unusable_observation(),
            ).capture_and_analyze_once()

            failures = list((root / "failures").rglob("*.json"))

        self.assertEqual(outcome.status, "capture_failed")
        self.assertIsNone(outcome.image_id)
        self.assertIsNone(outcome.vision)
        self.assertEqual(outcome.error_code, "RTSP_OPEN_FAILED")
        self.assertEqual(len(failures), 1)

    def test_unusable_analysis_persists_a_vision_record(self):
        self.assertIsNotNone(_service, "services.soil3.vision.vision_service must define VisionService")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            outcome = self._service_with(root, self._frame(), self._unusable_observation()).capture_and_analyze_once()
            records = list((root / "records").rglob("*.json"))

        self.assertEqual(outcome.status, "image_unusable")
        self.assertEqual(outcome.vision["image_quality"], "unusable")
        self.assertEqual(len(records), 1)

    def test_analysis_failure_keeps_image_but_writes_no_vision_record(self):
        self.assertIsNotNone(_service, "services.soil3.vision.vision_service must define VisionService")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            outcome = self._service_with(
                root,
                self._frame(),
                _service.AnalysisError("MODEL_INVALID_JSON"),
            ).capture_and_analyze_once()
            images = list((root / "images").rglob("*.jpg"))
            records = list((root / "records").rglob("*.json")) if (root / "records").exists() else []
            failures = list((root / "failures").rglob("*.json"))

        self.assertEqual(outcome.status, "analysis_failed")
        self.assertIsNotNone(outcome.image_id)
        self.assertIsNone(outcome.vision)
        self.assertEqual(len(images), 1)
        self.assertEqual(len(records), 0)
        self.assertEqual(len(failures), 1)

    def test_successful_second_record_references_previous_image(self):
        self.assertIsNotNone(_service, "services.soil3.vision.vision_service must define VisionService")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._service_with(root, self._frame(), self._good_observation()).capture_and_analyze_once()
            second = self._service_with(
                root,
                self._frame(),
                self._good_observation(change="stable"),
            ).capture_and_analyze_once()

        self.assertEqual(first.status, "success")
        self.assertEqual(second.status, "success")
        self.assertEqual(second.vision["previous_image_id"], first.image_id)


class VisionConfigurationTests(unittest.TestCase):
    def test_example_config_names_variables_without_secret_values(self):
        text = Path("config/soil3.example.json").read_text(encoding="utf-8")

        self.assertIn("SOIL3_CAMERA_RTSP_URL", text)
        self.assertIn("QWEN_API_KEY", text)
        self.assertIn("QWEN_BASE_URL", text)
        self.assertIn("QWEN_MODEL", text)
        self.assertIn("SOIL3_VISION_DATA_DIR", text)
        self.assertNotIn("rtsp://", text)


if __name__ == "__main__":
    unittest.main()
