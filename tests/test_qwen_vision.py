import base64
import hashlib
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import requests

from services.soil3.vision.vision_capture import ImageEvidence
from services.soil3.vision.vision_v1 import OBSERVATION_FIELDS, SEVERITY_FIELDS


try:
    _qwen = importlib.import_module("services.soil3.vision.qwen_vision")
except ModuleNotFoundError:
    _qwen = None


class FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self._response


class TimeoutSession:
    def post(self, url, **kwargs):
        raise requests.Timeout()


class FailingSession:
    def __init__(self, error) -> None:
        self._error = error

    def post(self, url, **kwargs):
        raise self._error


CROP = b"crop-jpeg-bytes"
PREVIOUS_CROP = b"earlier-crop-of-the-same-zone"


class QwenVisionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(_qwen, "services.soil3.vision.qwen_vision must define QwenVisionAnalyzer")

    def _evidence(self, root: Path, jpeg: bytes = CROP) -> ImageEvidence:
        relative = "images/2026-09-17/3f275b48-b86a-4db5-b720-3820ed84bdce.jpg"
        image = root / relative
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(jpeg)
        return ImageEvidence(
            image_id="3f275b48-b86a-4db5-b720-3820ed84bdce",
            image_path=relative,
            image_sha256=hashlib.sha256(jpeg).hexdigest(),
        )

    def _analyzer(self, root: Path, session) -> "object":
        return _qwen.QwenVisionAnalyzer(
            base_url="https://provider.example/compatible-mode/v1",
            api_key="configured-test-key",
            image_root=root,
            session=session,
        )

    def test_the_saved_zone_crop_is_sent_as_a_data_url(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = FakeSession(FakeResponse(json.dumps({"image_quality": "unusable"})))
            result = self._analyzer(root, session).analyze(self._evidence(root))

        self.assertEqual(result["image_quality"], "unusable")
        request = session.requests[0]
        self.assertEqual(request["url"], "https://provider.example/compatible-mode/v1/chat/completions")
        self.assertEqual(request["json"]["model"], "qwen3-vl-flash")
        content = request["json"]["messages"][0]["content"]
        self.assertEqual(len(content), 2, "one prompt and one image for a first observation")
        self.assertEqual(self._decoded(content[1]), CROP)

    def test_the_earlier_crop_of_the_same_zone_is_sent_first(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = FakeSession(FakeResponse(json.dumps({"image_quality": "good"})))
            self._analyzer(root, session).analyze(self._evidence(root), previous_jpeg=PREVIOUS_CROP)

        content = session.requests[0]["json"]["messages"][0]["content"]
        self.assertEqual(len(content), 3)
        self.assertEqual(self._decoded(content[1]), PREVIOUS_CROP, "the earlier image comes before the current one")
        self.assertEqual(self._decoded(content[2]), CROP)

    def test_the_prompt_names_every_contract_field(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = FakeSession(FakeResponse("{}"))
            self._analyzer(root, session).analyze(self._evidence(root))

        prompt = session.requests[0]["json"]["messages"][0]["content"][0]["text"]
        for field in sorted(OBSERVATION_FIELDS):
            self.assertIn(field, prompt, "the prompt and the validator must not drift apart")
        self.assertNotIn("healthy", prompt)
        self.assertIn("null", prompt, "missing evidence must be reported as null")
        self.assertIn("unavailable", prompt)
        self.assertIn("JSON only", prompt)

    def test_the_prompt_describes_only_the_new_observations(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = FakeSession(FakeResponse("{}"))
            self._analyzer(root, session).analyze(self._evidence(root))

        prompt = session.requests[0]["json"]["messages"][0]["content"][0]["text"]
        for field in ("browning", "leaf_curl", "spots_or_lesions", "leaf_loss", "stem_posture", "occlusion", "target_detected", "target_ambiguity"):
            with self.subTest(field=field):
                self.assertIn(field, prompt)
        self.assertIn("severe_abnormality", prompt)
        self.assertIn("mild_abnormality", prompt)

    def test_no_credential_or_endpoint_detail_leaks_into_the_request_body(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = FakeSession(FakeResponse("{}"))
            self._analyzer(root, session).analyze(self._evidence(root))

        request = session.requests[0]
        body = json.dumps(request["json"])
        self.assertNotIn("configured-test-key", body)
        self.assertNotIn("rtsp://", body)
        self.assertEqual(request["headers"]["Authorization"], "Bearer configured-test-key")

    def test_timeout_is_analysis_failure_not_image_quality(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(_qwen.AnalysisError) as raised:
                self._analyzer(root, TimeoutSession()).analyze(self._evidence(root))
        self.assertEqual(raised.exception.code, "MODEL_TIMEOUT")

    def test_a_provider_error_status_is_sanitized(self):
        error = requests.HTTPError("403 Client Error")
        error.response = type("Response", (), {"status_code": 403, "text": "insufficient_quota here"})()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(_qwen.AnalysisError) as raised:
                self._analyzer(root, FailingSession(error)).analyze(self._evidence(root))
        self.assertEqual(raised.exception.code, "MODEL_REQUEST_FAILED")
        self.assertNotIn("insufficient_quota", str(raised.exception))

    def test_a_non_json_answer_is_rejected(self):
        for content in ("not json", '"a string"', "[]", "null"):
            with self.subTest(content=content):
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    session = FakeSession(FakeResponse(content))
                    with self.assertRaises(_qwen.AnalysisError) as raised:
                        self._analyzer(root, session).analyze(self._evidence(root))
                self.assertEqual(raised.exception.code, "MODEL_INVALID_JSON")

    def test_an_unreadable_crop_is_reported_before_any_request(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = self._evidence(root)
            (root / evidence.image_path).unlink()
            session = FakeSession(FakeResponse("{}"))
            with self.assertRaises(_qwen.AnalysisError) as raised:
                self._analyzer(root, session).analyze(evidence)
        self.assertEqual(raised.exception.code, "IMAGE_EVIDENCE_UNAVAILABLE")
        self.assertEqual(session.requests, [])

    @staticmethod
    def _decoded(part) -> bytes:
        url = part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        return base64.b64decode(url.split(",", 1)[1])


if __name__ == "__main__":
    unittest.main()
