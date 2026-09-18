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
    def __init__(self, content: str, status_code: int = 200) -> None:
        self._content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


LEAKY_DETAIL = "leaked-account-sk-abcdefghijklmnop https://provider.example/v1/chat/completions"


class ProviderErrorResponse:
    """An HTTP failure reply: a status, a machine code, and a message that must never be stored."""

    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class ProviderErrorSession:
    def __init__(self, response: ProviderErrorResponse) -> None:
        self._response = response
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self._response


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
        self.assertIsNone(raised.exception.http_status, "a timeout never got a reply")
        self.assertIsNone(raised.exception.provider_error_code)

    def test_a_connection_failure_keeps_the_endpoint_out_of_the_chain(self):
        error = requests.ConnectionError(f"Failed to establish https://provider.example: {LEAKY_DETAIL}")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(_qwen.AnalysisError) as raised:
                self._analyzer(root, FailingSession(error)).analyze(self._evidence(root))
        exception = raised.exception
        self.assertEqual(exception.code, "MODEL_REQUEST_FAILED")
        self.assertIsNone(exception.http_status)
        self.assertIsNone(exception.provider_error_code)
        self.assertIsNone(exception.__cause__, "the requests error text carries the endpoint URL")
        self.assertNotIn("provider.example", str(exception))

    def test_every_provider_status_is_diagnosable_without_the_response_body(self):
        cases = (
            (400, {"error": {"code": "invalid_request_error", "message": LEAKY_DETAIL}}, "invalid_request_error"),
            (401, {"error": {"message": LEAKY_DETAIL, "code": None, "type": "invalid_authentication_error"}}, "invalid_authentication_error"),
            (403, {"code": "insufficient_quota", "message": LEAKY_DETAIL}, "insufficient_quota"),
            (429, {"error": {"code": "throttling_allocation_quota", "message": LEAKY_DETAIL}}, "throttling_allocation_quota"),
            (500, {"error": "InternalError"}, "InternalError"),
            (503, None, None),
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = self._evidence(root)
            for status_code, payload, expected_code in cases:
                with self.subTest(status_code=status_code):
                    session = ProviderErrorSession(ProviderErrorResponse(status_code, payload))
                    with self.assertRaises(_qwen.AnalysisError) as raised:
                        self._analyzer(root, session).analyze(evidence)
                    exception = raised.exception
                    self.assertEqual(exception.code, "MODEL_REQUEST_FAILED", "the local classification stays stable")
                    self.assertEqual(exception.http_status, status_code)
                    self.assertEqual(exception.provider_error_code, expected_code)
                    self.assertNotIn("leaked", str(exception))
                    self.assertNotIn("provider.example", str(exception))
                    self.assertEqual(len(session.requests), 1, "one request per zone, no retry")

    def test_only_a_bounded_machine_token_is_accepted_as_an_error_code(self):
        shapes = (
            ({"error": {"message": LEAKY_DETAIL}}, None, "a human message is not an error code"),
            ({"error": {"code": "too many requests on your account"}}, None, "a code with spaces is rejected"),
            ({"error": {"code": "x" * 65}}, None, "an unbounded code is rejected"),
            ({"error": {"code": "bad-key; rm -rf /"}}, None, "punctuation outside the token set is rejected"),
            ({"code": 401}, None, "a numeric code is not a provider error token"),
            ([{"code": "RealErrorCode"}], None, "a non-object body carries no code"),
            ({"error": {"code": "DataInspectionFailed", "message": LEAKY_DETAIL}}, "DataInspectionFailed", "the code wins over the message"),
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = self._evidence(root)
            for payload, expected, reason in shapes:
                with self.subTest(reason):
                    session = ProviderErrorSession(ProviderErrorResponse(400, payload))
                    with self.assertRaises(_qwen.AnalysisError) as raised:
                        self._analyzer(root, session).analyze(evidence)
                    self.assertEqual(raised.exception.http_status, 400)
                    self.assertEqual(raised.exception.provider_error_code, expected, reason)
                    self.assertNotIn("leaked", str(raised.exception))

    def test_a_reply_that_is_not_the_expected_shape_reports_the_status_it_got(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = FakeSession(FakeResponse("not json", status_code=200))
            with self.assertRaises(_qwen.AnalysisError) as raised:
                self._analyzer(root, session).analyze(self._evidence(root))
        self.assertEqual(raised.exception.code, "MODEL_INVALID_JSON")
        self.assertEqual(raised.exception.http_status, 200, "the provider did answer")
        self.assertIsNone(raised.exception.provider_error_code)

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
