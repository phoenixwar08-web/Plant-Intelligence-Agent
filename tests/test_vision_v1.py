import hashlib
import importlib
from datetime import datetime, timezone
import json
import os
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


def _jpeg(width: int, height: int) -> bytes:
    """A horizontally graded frame, so a crop of each side has its own bytes."""
    ramp = np.tile(np.linspace(0, 255, width, dtype=np.uint8).reshape(1, width, 1), (height, 1, 3))
    encoded, buffer = cv2.imencode(".jpg", np.ascontiguousarray(ramp))
    assert encoded
    return buffer.tobytes()


FRAME_JPEG = _jpeg(100, 60)
WIDE_JPEG = _jpeg(200, 120)
ZONE_RECT = [0.1, 0.1, 0.5, 0.5]


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def observation(**overrides):
    """One complete, null-heavy set of the fields the model must return."""
    value = {
        "image_quality": "unusable",
        "target_detected": None,
        "target_ambiguity": None,
        "leaf_droop": None,
        "leaf_spread": None,
        "wilting": None,
        "yellowing": None,
        "visible_damage": None,
        "browning": None,
        "leaf_curl": None,
        "spots_or_lesions": None,
        "leaf_loss": None,
        "stem_posture": None,
        "occlusion": None,
        "overall_visual_state": "unavailable",
        "change_vs_previous": "unknown",
        "confidence": None,
    }
    value.update(overrides)
    return value


def assessed(**overrides):
    """An observation of a visible plant, with one field left without evidence."""
    value = observation(
        image_quality="good",
        target_detected=True,
        target_ambiguity="none",
        leaf_droop="none",
        leaf_spread="normal",
        wilting=False,
        yellowing="none",
        visible_damage="none",
        browning="mild",
        leaf_curl="none",
        spots_or_lesions=None,
        leaf_loss="none",
        stem_posture="none",
        occlusion="mild",
        overall_visual_state="mild_abnormality",
        confidence=0.7,
    )
    value.update(overrides)
    return value


def vision_record(zone_id="plant_zone_1", **overrides):
    record = {
        **observation(),
        "schema_version": "vision.v1",
        "device_code": "soil3",
        "plant_zone": {"id": zone_id, "rect": list(ZONE_RECT), "label": "left plant"},
        "image_id": "3f275b48-b86a-4db5-b720-3820ed84bdce",
        "image_path": "images/2026-09-17/3f275b48-b86a-4db5-b720-3820ed84bdce.jpg",
        "image_sha256": hashlib.sha256(b"crop").hexdigest(),
        "source_frame_id": "c9f3c1f4f6f14b8d9d7f0b2f6f0a0001",
        "source_frame_path": "frames/2026-09-17/c9f3c1f4f6f14b8d9d7f0b2f6f0a0001.jpg",
        "source_frame_sha256": hashlib.sha256(b"frame").hexdigest(),
        "previous_image_id": None,
        "captured_at": "2026-09-17T00:00:00Z",
        "analyzed_at": "2026-09-17T00:00:01Z",
        "model": {"provider": "qwen", "name": "qwen3-vl-flash", "prompt_version": "vision.v1"},
    }
    record.update(overrides)
    return record


class VisionV1ContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")

    def test_unusable_image_is_a_valid_unknown_observation(self):
        record = _contract.validate_vision_record(vision_record())
        self.assertEqual(record["image_quality"], "unusable")
        self.assertIsNone(record["wilting"])
        self.assertEqual(record["overall_visual_state"], "unavailable")

    def test_existing_run_result_callers_default_to_no_manifest(self):
        result = _contract.VisionRunResult("capture_failed", None, ())

        self.assertIsNone(result.manifest_ref)

    def test_assessed_record_reports_every_field(self):
        record = _contract.validate_vision_record(vision_record(**assessed()))
        self.assertEqual(record["overall_visual_state"], "mild_abnormality")
        self.assertEqual(record["browning"], "mild")
        self.assertIsNone(record["spots_or_lesions"], "a field without evidence stays null")

    def test_an_absent_plant_is_reported_as_unavailable(self):
        record = _contract.validate_vision_record(vision_record(target_detected=False))
        self.assertIsNone(record["yellowing"])
        self.assertEqual(record["overall_visual_state"], "unavailable")

    def test_first_image_cannot_claim_change_without_a_previous_image(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(**assessed(change_vs_previous="worsened")))

    def test_confidence_must_be_a_finite_probability(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(**assessed(confidence=1.01)))

    def test_unusable_image_cannot_claim_wilting(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(wilting=True))

    def test_an_unconfirmed_target_cannot_describe_a_visual_state(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(**assessed(target_detected=False)))

    def test_a_visual_state_must_cite_at_least_one_observed_field(self):
        without_evidence = {key: None for key in _contract.SEVERITY_FIELDS}
        without_evidence.update(leaf_spread=None, wilting=None)
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(**assessed(**without_evidence)))

    def test_severity_uses_only_the_allowed_scale(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(**assessed(browning="catastrophic")))

    def test_a_health_diagnosis_is_not_a_visual_state(self):
        for state in ("healthy", "attention", "poor", "needs_water"):
            with self.subTest(state=state):
                with self.assertRaises(_contract.VisionValidationError):
                    _contract.validate_vision_record(vision_record(**assessed(overall_visual_state=state)))

    def test_rejects_an_unknown_field(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(unreviewed_note="not allowed"))

    def test_a_zone_cannot_be_tracked_against_itself(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(previous_image_id=vision_record()["image_id"]))

    def test_a_crop_must_name_the_frame_it_was_cut_from(self):
        original = vision_record()
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(
                vision_record(source_frame_id=original["image_id"], source_frame_path=original["image_path"])
            )

    def test_the_model_input_is_a_crop_never_the_stored_frame(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(image_path=vision_record()["source_frame_path"]))

    def test_record_requires_the_zone_identity(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(plant_zone={"rect": list(ZONE_RECT), "label": "x"}))

    def test_a_zone_rect_is_a_fraction_of_the_frame(self):
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(plant_zone={"id": "plant_zone_1", "rect": [0, 0, 960, 540], "label": ""}))


class PlantZoneConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define parse_zones")

    def test_example_zone_configuration_is_valid(self):
        zones = _contract.parse_zones(_read_json(Path("config/vision_zones.example.json")))
        self.assertEqual([zone.zone_id for zone in zones], ["plant_zone_1", "plant_zone_2"])
        self.assertTrue(all(len(zone.rect) == 4 for zone in zones))
        self.assertTrue(all(zone.label for zone in zones), "the example zones say which plant each is")

    def test_zones_are_independent_rectangles_within_the_frame(self):
        zones = _contract.parse_zones(
            {
                "zones": [
                    {"id": "plant_zone_1", "rect": [0.0, 0.0, 0.5, 1.0]},
                    {"id": "plant_zone_2", "rect": [0.5, 0.0, 0.5, 1.0]},
                ]
            }
        )
        self.assertEqual(zones[1].rect, (0.5, 0.0, 0.5, 1.0))
        self.assertEqual(zones[1].label, "", "a label is optional")

    def test_a_rect_reaching_past_the_frame_is_rejected(self):
        with self.assertRaises(_contract.ZoneConfigurationError):
            _contract.parse_zones({"zones": [{"id": "plant_zone_1", "rect": [0.6, 0.0, 0.6, 0.5]}]})

    def test_duplicate_zone_ids_are_rejected(self):
        with self.assertRaises(_contract.ZoneConfigurationError):
            _contract.parse_zones(
                {
                    "zones": [
                        {"id": "plant_zone_1", "rect": [0.0, 0.0, 0.4, 0.4]},
                        {"id": "plant_zone_1", "rect": [0.5, 0.5, 0.4, 0.4]},
                    ]
                }
            )

    def test_an_unnamed_or_oversized_zone_set_is_rejected(self):
        one = [{"id": "plant_zone_1", "rect": [0.0, 0.0, 0.4, 0.4]}]
        for payload in (
            {"zones": []},
            {"zones": [{"id": f"zone_{index}", "rect": [0.0, 0.0, 0.1, 0.1]} for index in range(9)]},
            {"zones": one, "extra": True},
            {"zones": [{"id": "Zone One", "rect": [0.0, 0.0, 0.4, 0.4]}]},
            {"zones": {"id": "plant_zone_1", "rect": [0.0, 0.0, 0.4, 0.4]}},
            [{"id": "plant_zone_1", "rect": [0.0, 0.0, 0.4, 0.4]}],
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(_contract.ZoneConfigurationError):
                    _contract.parse_zones(payload)


class ZoneCropTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(_capture, "services.soil3.vision.vision_capture must define crop_to_zone")

    def _size(self, jpeg: bytes):
        decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(decoded)
        return decoded.shape[1], decoded.shape[0]

    def test_a_zone_crop_keeps_only_that_part_of_the_frame(self):
        self.assertEqual(self._size(_capture.crop_to_zone(FRAME_JPEG, [0.0, 0.0, 0.5, 0.5])), (50, 30))

    def test_the_same_rect_maps_to_the_same_fraction_at_any_resolution(self):
        self.assertEqual(self._size(_capture.crop_to_zone(FRAME_JPEG, ZONE_RECT)), (50, 30))
        self.assertEqual(self._size(_capture.crop_to_zone(WIDE_JPEG, ZONE_RECT)), (100, 60))

    def test_the_two_zones_of_one_frame_are_different_images(self):
        left = _capture.crop_to_zone(FRAME_JPEG, [0.0, 0.0, 0.5, 1.0])
        right = _capture.crop_to_zone(FRAME_JPEG, [0.5, 0.0, 0.5, 1.0])
        self.assertNotEqual(left, right)
        self.assertNotEqual(left, FRAME_JPEG)

    def test_an_empty_zone_is_rejected(self):
        with self.assertRaises(_capture.CaptureError) as raised:
            _capture.crop_to_zone(FRAME_JPEG, [0.0, 0.0, 0.0001, 0.0001])
        self.assertEqual(raised.exception.code, "ROI_EMPTY")

    def test_a_zone_outside_the_frame_is_rejected(self):
        with self.assertRaises(_capture.CaptureError) as raised:
            _capture.crop_to_zone(FRAME_JPEG, [0.6, 0.0, 0.5, 0.5])
        self.assertEqual(raised.exception.code, "ROI_OUT_OF_BOUNDS")

    def test_an_unusable_rect_or_frame_is_rejected(self):
        with self.assertRaises(_capture.CaptureError) as raised:
            _capture.crop_to_zone(FRAME_JPEG, [0.0, 0.0, 0.0])
        self.assertEqual(raised.exception.code, "ROI_INVALID")
        with self.assertRaises(_capture.CaptureError) as raised:
            _capture.crop_to_zone(b"", ZONE_RECT)
        self.assertEqual(raised.exception.code, "JPEG_INVALID")


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(_capture, "services.soil3.vision.vision_capture must define EvidenceStore")
        self.captured_at = datetime(2026, 9, 17, tzinfo=timezone.utc)

    def test_the_frame_and_each_zone_crop_are_stored_separately(self):
        crop = _capture.crop_to_zone(FRAME_JPEG, ZONE_RECT)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _capture.EvidenceStore(root)
            frame = store.save_frame(FRAME_JPEG, self.captured_at)
            stored_crop = store.save_crop(crop, self.captured_at)

            self.assertTrue(frame.image_path.startswith("frames/2026-09-17/"))
            self.assertTrue(stored_crop.image_path.startswith("images/2026-09-17/"))
            self.assertEqual((root / frame.image_path).read_bytes(), FRAME_JPEG)
            self.assertEqual(stored_crop.image_sha256, hashlib.sha256(crop).hexdigest())
            self.assertEqual(store.load(frame), FRAME_JPEG)
            self.assertEqual(store.load(stored_crop), crop)

    def test_a_missing_or_altered_evidence_file_is_refused(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _capture.EvidenceStore(root)
            evidence = store.save_frame(FRAME_JPEG, self.captured_at)

            with self.assertRaises(_capture.CaptureError) as raised:
                store.load(_capture.ImageEvidence(evidence.image_id, "frames/nope.jpg", evidence.image_sha256))
            self.assertEqual(raised.exception.code, "IMAGE_EVIDENCE_UNAVAILABLE")

            (root / evidence.image_path).write_bytes(b"tampered")
            with self.assertRaises(_capture.CaptureError) as raised:
                store.load(evidence)
            self.assertEqual(raised.exception.code, "IMAGE_EVIDENCE_CORRUPTED")

    def test_storing_an_undecodable_crop_is_refused(self):
        with TemporaryDirectory() as temporary:
            store = _capture.EvidenceStore(Path(temporary))
            with self.assertRaises(_capture.CaptureError) as raised:
                store.save_crop(b"not-a-jpeg", self.captured_at)
            self.assertEqual(raised.exception.code, "JPEG_INVALID")

    def test_evidence_storage_failure_is_sanitized(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "not-a-directory"
            root.write_bytes(b"file")
            with self.assertRaises(_capture.CaptureError) as raised:
                _capture.EvidenceStore(root).save_frame(FRAME_JPEG, self.captured_at)
        self.assertEqual(raised.exception.code, "IMAGE_STORE_FAILED")


class RtspCaptureTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(_capture, "services.soil3.vision.vision_capture must define OpenCvRtspFrameCapture")

    def test_rtsp_adapter_releases_connection_after_one_frame(self):
        class FakeVideoCapture:
            def __init__(self) -> None:
                self.released = False

            def isOpened(self) -> bool:
                return True

            def read(self):
                return True, np.zeros((60, 100, 3), dtype=np.uint8)

            def release(self) -> None:
                self.released = True

        handle = FakeVideoCapture()
        capture = _capture.OpenCvRtspFrameCapture("rtsp://camera.example/stream", capture_factory=lambda _: handle)

        frame = capture.capture_one()

        self.assertTrue(handle.released)
        self.assertEqual(frame.jpeg_bytes[:2], b"\xff\xd8")
        self.assertEqual(frame.captured_at.tzinfo, timezone.utc)

    def test_rtsp_read_failure_is_sanitized_and_released(self):
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
        capture = _capture.OpenCvRtspFrameCapture("rtsp://camera.example/stream", capture_factory=lambda _: handle)

        with self.assertRaises(_capture.CaptureError) as raised:
            capture.capture_one()

        self.assertEqual(raised.exception.code, "RTSP_READ_FAILED")
        self.assertTrue(handle.released)

    def test_rtsp_open_exception_is_sanitized(self):
        capture = _capture.OpenCvRtspFrameCapture(
            "rtsp://camera.example/stream",
            capture_factory=lambda _: (_ for _ in ()).throw(RuntimeError("untrusted detail")),
        )
        with self.assertRaises(_capture.CaptureError) as raised:
            capture.capture_one()
        self.assertEqual(raised.exception.code, "RTSP_OPEN_FAILED")


def observed_change(evidence, previous_jpeg=None):
    """Claim a change only when the model was actually given an earlier image."""
    return assessed(change_vs_previous="stable" if previous_jpeg is not None else "unknown")


class VisionServiceTests(unittest.TestCase):
    ZONES = (
        ("plant_zone_1", (0.0, 0.0, 0.5, 1.0), "left plant"),
        ("plant_zone_2", (0.5, 0.0, 0.5, 1.0), "right plant"),
    )

    def setUp(self):
        self.assertIsNotNone(_service, "services.soil3.vision.vision_service must define VisionService")
        self.zones = tuple(_contract.PlantZone(zone_id=zone_id, rect=rect, label=label) for zone_id, rect, label in self.ZONES)

    class FakeCapture:
        def __init__(self, result) -> None:
            self._result = result

        def capture_one(self):
            if isinstance(self._result, Exception):
                raise self._result
            return self._result

    class FakeAnalyzer:
        """Records what each zone was shown, so history can be asserted."""

        def __init__(self, result) -> None:
            self._result = result
            self.seen = []

        def analyze(self, evidence, previous_jpeg=None):
            self.seen.append((evidence.image_path, previous_jpeg))
            if isinstance(self._result, Exception):
                raise self._result
            if callable(self._result):
                return dict(self._result(evidence, previous_jpeg))
            return dict(self._result)

    def _frame(self, jpeg: bytes = FRAME_JPEG):
        return _capture.CapturedFrame(jpeg_bytes=jpeg, captured_at=datetime(2026, 9, 17, tzinfo=timezone.utc))

    def _service(self, root, capture_result, analyzer_result=None, zones=None, analyzer=None):
        return _service.VisionService(
            data_root=root,
            device_code="soil3",
            capture=self.FakeCapture(capture_result),
            store=_capture.EvidenceStore(root),
            analyzer=analyzer if analyzer is not None else self.FakeAnalyzer(analyzer_result),
            zones=self.zones if zones is None else zones,
        )

    def test_capture_failure_returns_no_zone_outcomes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._service(root, _capture.CaptureError("RTSP_OPEN_FAILED")).capture_and_analyze_once()
            failures = [_read_json(path) for path in (root / "failures").rglob("*.json")]

        self.assertEqual(result.status, "capture_failed")
        self.assertIsNone(result.frame_id)
        self.assertEqual(result.outcomes, ())
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error_code"], "RTSP_OPEN_FAILED")
        self.assertNotIn("plant_zone", failures[0], "no zone was reached")

    def test_successful_run_returns_a_hash_verified_public_manifest(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._service(
                root, self._frame(), assessed()
            ).capture_and_analyze_once()

            self.assertIsNotNone(result.manifest_ref)
            manifest_path = Path(result.manifest_ref.path)
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)

        self.assertEqual("vision_run.v1", result.manifest_ref.schema_version)
        self.assertEqual(result.manifest_ref.record_id, manifest["run_id"])
        self.assertEqual(
            hashlib.sha256(manifest_bytes).hexdigest(),
            result.manifest_ref.sha256,
        )
        self.assertEqual("vision_run.v1", manifest["schema_version"])
        self.assertEqual("soil3", manifest["device_code"])
        self.assertEqual(result.status, manifest["status"])
        self.assertEqual(result.frame_id, manifest["frame_id"])
        self.assertEqual(2, len(manifest["observation_refs"]))
        self.assertTrue(
            all(
                reference["schema_version"] == "vision.v1"
                and Path(reference["path"]).is_absolute()
                and len(reference["sha256"]) == 64
                for reference in manifest["observation_refs"]
            )
        )
        self.assertTrue(all(outcome.artifact_ref is not None for outcome in result.outcomes))

    def test_all_failed_zone_run_has_no_manifest(self):
        import services.soil3.vision.qwen_vision as qwen_vision

        with TemporaryDirectory() as temporary:
            result = self._service(
                Path(temporary),
                self._frame(),
                qwen_vision.AnalysisError("MODEL_TIMEOUT"),
            ).capture_and_analyze_once()

        self.assertEqual("analysis_failed", result.status)
        self.assertTrue(result.outcomes)
        self.assertTrue(all(outcome.vision is None for outcome in result.outcomes))
        self.assertIsNone(result.manifest_ref)

    def test_unusable_analysis_persists_one_record_per_zone(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._service(root, self._frame(), observation()).capture_and_analyze_once()
            records = sorted((_read_json(path) for path in (root / "records").rglob("*.json")), key=lambda record: record["plant_zone"]["id"])

        self.assertEqual(result.status, "image_unusable")
        self.assertEqual([outcome.zone_id for outcome in result.outcomes], ["plant_zone_1", "plant_zone_2"])
        self.assertEqual(len(records), 2)
        self.assertEqual([record["plant_zone"]["id"] for record in records], ["plant_zone_1", "plant_zone_2"])
        self.assertEqual([record["image_id"] for record in records], [outcome.image_id for outcome in result.outcomes])

    def test_one_frame_is_stored_whole_and_cropped_per_zone(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._service(root, self._frame(), observation()).capture_and_analyze_once()
            frames = list((root / "frames").rglob("*.jpg"))
            crops = {outcome.zone_id: (root / outcome.vision["image_path"]).read_bytes() for outcome in result.outcomes}
            stored = [(frame.read_bytes(), frame.stem) for frame in frames]

        self.assertEqual(len(stored), 1, "the frame is captured once for all zones")
        self.assertEqual(stored[0][0], FRAME_JPEG, "the original image is kept complete")
        self.assertEqual(len(crops), 2, "one model input per zone")
        self.assertEqual(result.frame_id, stored[0][1])
        for outcome in result.outcomes:
            rect = outcome.vision["plant_zone"]["rect"]
            self.assertEqual(outcome.vision["source_frame_path"], f"frames/2026-09-17/{result.frame_id}.jpg")
            self.assertEqual(outcome.vision["source_frame_sha256"], hashlib.sha256(FRAME_JPEG).hexdigest())
            self.assertEqual(crops[outcome.zone_id], _capture.crop_to_zone(FRAME_JPEG, rect))

    def test_a_broken_zone_does_not_stop_the_other_zone(self):
        tiny = _contract.PlantZone(zone_id="plant_zone_2", rect=(0.0, 0.0, 0.0001, 0.0001))
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._service(root, self._frame(), observation(), zones=(self.zones[0], tiny)).capture_and_analyze_once()
            records = list((root / "records").rglob("*.json"))
            failures = [_read_json(path) for path in sorted((root / "failures").rglob("*.json"))]

        self.assertEqual(result.status, "partial")
        self.assertEqual([outcome.status for outcome in result.outcomes], ["image_unusable", "capture_failed"])
        self.assertEqual(result.outcomes[1].error_code, "ROI_EMPTY")
        self.assertEqual(len(records), 1)
        self.assertEqual(failures[0]["plant_zone"], "plant_zone_2")
        self.assertEqual(failures[0]["source_frame_id"], result.frame_id)
        self.assertNotIn("http_status", failures[0], "a capture failure never reached the provider")
        self.assertNotIn("provider_error_code", failures[0])

    def test_analysis_failure_keeps_the_crop_but_writes_no_record(self):
        from services.soil3.vision import qwen_vision

        provider_failure = qwen_vision.AnalysisError(
            "MODEL_REQUEST_FAILED",
            http_status=429,
            provider_error_code="throttling_allocation_quota",
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._service(root, self._frame(), provider_failure).capture_and_analyze_once()
            crops = list((root / "images").rglob("*.jpg"))
            records = list((root / "records").rglob("*.json"))
            failures = sorted((_read_json(path) for path in (root / "failures").rglob("*.json")), key=lambda value: value["plant_zone"])

        self.assertEqual(result.status, "analysis_failed")
        self.assertEqual(len(crops), 2)
        self.assertEqual(records, [])
        self.assertEqual([failure["plant_zone"] for failure in failures], ["plant_zone_1", "plant_zone_2"])
        self.assertTrue(all(failure["image_id"] and failure["source_frame_id"] for failure in failures))
        for failure in failures:
            self.assertEqual(failure["error_code"], "MODEL_REQUEST_FAILED", "the local classification is kept")
            self.assertEqual(failure["http_status"], 429)
            self.assertEqual(failure["provider_error_code"], "throttling_allocation_quota")
        for outcome in result.outcomes:
            self.assertEqual(outcome.http_status, 429, "the caller sees the same diagnostics as the record")
            self.assertEqual(outcome.provider_error_code, "throttling_allocation_quota")

    def test_a_timeout_and_a_local_rejection_are_told_apart_by_nulls(self):
        from services.soil3.vision import qwen_vision

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._service(root, self._frame(), qwen_vision.AnalysisError("MODEL_TIMEOUT")).capture_and_analyze_once()
            partial = assessed()
            for key in _contract.SEVERITY_FIELDS:
                partial.pop(key)
            validation = self._service(root, self._frame(), partial).capture_and_analyze_once()
            failures = sorted((_read_json(path) for path in (root / "failures").rglob("*.json")), key=lambda value: value["error_code"])

        timeout = [failure for failure in failures if failure["error_code"] == "MODEL_TIMEOUT"]
        self.assertEqual(len(timeout), 2)
        for failure in timeout:
            self.assertIsNone(failure["http_status"], "no reply means no status to report")
            self.assertIsNone(failure["provider_error_code"])
        rejected = [failure for failure in failures if failure["error_code"] == "INVALID_VISION_RECORD"]
        self.assertEqual(len(rejected), 2, "the model answered, but the local validator refused")
        for failure in rejected:
            self.assertIsNone(failure["http_status"], "a local rejection is not a provider failure")
            self.assertIsNone(failure["provider_error_code"])
        self.assertEqual([outcome.error_code for outcome in validation.outcomes], ["INVALID_VISION_RECORD"] * 2)

    def test_each_zone_is_tracked_against_its_own_earlier_crop(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._service(root, self._frame(), analyzer=self.FakeAnalyzer(observed_change))
            first_result = first.capture_and_analyze_once()
            second = self._service(root, self._frame(), analyzer=self.FakeAnalyzer(observed_change))
            second_result = second.capture_and_analyze_once()
            own_crop = {outcome.zone_id: (root / outcome.vision["image_path"]).read_bytes() for outcome in first_result.outcomes}

        self.assertEqual(first_result.status, "success")
        self.assertEqual(second_result.status, "success")
        for index, (zone_id, _, _) in enumerate(self.ZONES):
            self.assertIsNone(first._analyzer.seen[index][1], "a first observation gets no second image")
            self.assertIsNone(first_result.outcomes[index].vision["previous_image_id"])
            self.assertEqual(first_result.outcomes[index].vision["change_vs_previous"], "unknown")
            self.assertEqual(second._analyzer.seen[index][1], own_crop[zone_id])
            self.assertEqual(second_result.outcomes[index].vision["previous_image_id"], first_result.outcomes[index].image_id)
            self.assertEqual(second_result.outcomes[index].vision["change_vs_previous"], "stable")
        self.assertNotEqual(second._analyzer.seen[0][1], second._analyzer.seen[1][1], "the two zones get different history")

    def test_a_lost_history_image_degrades_to_no_comparison(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._service(root, self._frame(), analyzer=self.FakeAnalyzer(observed_change)).capture_and_analyze_once()
            for path in (root / "images").rglob("*.jpg"):
                path.write_bytes(b"tampered")
            result = self._service(root, self._frame(), analyzer=self.FakeAnalyzer(observed_change)).capture_and_analyze_once()

        self.assertEqual(result.status, "success")
        for outcome in result.outcomes:
            self.assertIsNone(outcome.vision["previous_image_id"])
            self.assertEqual(outcome.vision["change_vs_previous"], "unknown")

    def test_the_most_recent_record_of_a_zone_is_the_one_compared(self):
        def frame_at(hour: int):
            return _capture.CapturedFrame(
                jpeg_bytes=FRAME_JPEG,
                captured_at=datetime(2026, 9, 17, hour, tzinfo=timezone.utc),
            )

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            noon = {}
            noon_crop = {}
            for hour in (8, 12):
                outcomes = self._service(root, frame_at(hour), analyzer=self.FakeAnalyzer(observed_change)).capture_and_analyze_once().outcomes
                if hour == 12:
                    noon = {outcome.zone_id: outcome.image_id for outcome in outcomes}
                    noon_crop = {outcome.zone_id: (root / outcome.vision["image_path"]).read_bytes() for outcome in outcomes}
            last = self.FakeAnalyzer(observed_change)
            evening_result = self._service(root, frame_at(18), analyzer=last).capture_and_analyze_once()
            frame_count = len(list((root / "frames").rglob("*.jpg")))

        self.assertEqual(frame_count, 3, "one whole frame per run")
        for index, (zone_id, _, _) in enumerate(self.ZONES):
            self.assertEqual(evening_result.outcomes[index].vision["previous_image_id"], noon[zone_id])
            self.assertEqual(last.seen[index][1], noon_crop[zone_id])

    def test_records_of_another_device_or_schema_are_not_history(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _capture.EvidenceStore(root)
            self._foreign_record(store, root, device_code="soil9", schema_version="vision.v1", zone_id="plant_zone_1")
            self._foreign_record(store, root, device_code="soil3", schema_version="vision.v0", zone_id="plant_zone_1")
            self._foreign_record(store, root, device_code="soil3", schema_version="vision.v1", zone_id="unconfigured_zone")
            result = self._service(root, self._frame(), analyzer=self.FakeAnalyzer(observed_change)).capture_and_analyze_once()

        self.assertEqual([outcome.vision["previous_image_id"] for outcome in result.outcomes], [None, None])
        self.assertEqual([outcome.vision["change_vs_previous"] for outcome in result.outcomes], ["unknown", "unknown"])

    @staticmethod
    def _foreign_record(store, root: Path, *, device_code: str, schema_version: str, zone_id: str) -> None:
        """A history record that must be skipped: it is not this device's zone today."""
        rect = [0.0, 0.0, 0.5, 1.0]
        evidence = store.save_crop(_capture.crop_to_zone(FRAME_JPEG, rect), datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
        path = root / "records" / "2026-09-17" / f"{evidence.image_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "device_code": device_code,
                    "schema_version": schema_version,
                    "plant_zone": {"id": zone_id, "rect": rect, "label": ""},
                    "captured_at": "2026-09-17T12:00:00Z",
                    "image_id": evidence.image_id,
                    "image_path": evidence.image_path,
                    "image_sha256": evidence.image_sha256,
                }
            ),
            encoding="utf-8",
        )

    def test_model_cannot_override_evidence_provenance(self):
        malicious = assessed(image_id="c9f3c1f4f6f14b8d9d7f0b2f6f0a0002")
        with TemporaryDirectory() as temporary:
            result = self._service(Path(temporary), self._frame(), malicious).capture_and_analyze_once()
        self.assertEqual(result.status, "analysis_failed")
        self.assertTrue(all(outcome.vision is None for outcome in result.outcomes))

    def test_an_incomplete_observation_is_rejected_not_defaulted(self):
        partial = assessed()
        for key in _contract.SEVERITY_FIELDS:
            partial.pop(key)
        with TemporaryDirectory() as temporary:
            result = self._service(Path(temporary), self._frame(), partial).capture_and_analyze_once()
        self.assertEqual(result.status, "analysis_failed")
        self.assertEqual(result.outcomes[0].error_code, "INVALID_VISION_RECORD")

    def test_a_service_without_zones_is_a_configuration_error(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaises(_service.VisionConfigurationError):
                self._service(Path(temporary), self._frame(), observation(), zones=())


class VisionConfigurationTests(unittest.TestCase):
    NAMES = (
        "SOIL3_CAMERA_RTSP_URL",
        "QWEN_API_KEY",
        "QWEN_BASE_URL",
        "QWEN_MODEL",
        "SOIL3_VISION_DATA_DIR",
        "SOIL3_VISION_ZONES_PATH",
    )

    def setUp(self):
        self.assertIsNotNone(_service, "services.soil3.vision.vision_service must define VisionSettings")

    def test_example_config_names_variables_without_secret_values(self):
        text = Path("config/soil3.example.json").read_text(encoding="utf-8")
        for name in self.NAMES:
            self.assertIn(name, text)
        self.assertNotIn("rtsp://", text)
        self.assertIn("/compatible-mode/v1", text, "the example must warn about the endpoint suffix")

    def test_zones_are_read_from_the_file_named_by_the_environment(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            zones_path = root / "zones.json"
            zones_path.write_text(Path("config/vision_zones.example.json").read_text(encoding="utf-8"), encoding="utf-8")
            settings = _service.VisionSettings(
                rtsp_url="rtsp://camera.example/stream",
                api_key="configured-test-key",
                base_url="https://provider.example/compatible-mode/v1",
                data_root=root,
                zones_path=zones_path,
            )
            self.assertEqual([zone.zone_id for zone in settings.load_zones()], ["plant_zone_1", "plant_zone_2"])

            zones_path.write_text("{", encoding="utf-8")
            with self.assertRaises(_service.VisionConfigurationError):
                settings.load_zones()
            zones_path.unlink()
            with self.assertRaises(_service.VisionConfigurationError):
                settings.load_zones()

    def test_every_credential_and_the_zone_file_are_required(self):
        required = tuple(name for name in self.NAMES if name != "QWEN_MODEL")
        previous = {name: os.environ.get(name) for name in self.NAMES}
        try:
            for missing in required:
                for name in required:
                    if name != missing:
                        os.environ[name] = "/configured/test-value"
                os.environ.pop(missing, None)
                with self.subTest(missing=missing):
                    with self.assertRaises(_service.VisionConfigurationError):
                        _service.VisionSettings.from_environment()
            for name in required:
                os.environ[name] = "/configured/test-value"
            os.environ.pop("QWEN_MODEL", None)
            settings = _service.VisionSettings.from_environment()
            self.assertEqual(settings.model, "qwen3-vl-flash", "the model name has a default")
            self.assertEqual(settings.zones_path.name, "test-value")
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
