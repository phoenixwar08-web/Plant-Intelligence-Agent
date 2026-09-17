import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import requests

from services.soil3.vision.vision_capture import ImageEvidence


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


class QwenVisionTests(unittest.TestCase):
    def _evidence(self, root: Path) -> ImageEvidence:
        relative = "images/2026-09-17/3f275b48-b86a-4db5-b720-3820ed84bdce.jpg"
        image = root / relative
        image.parent.mkdir(parents=True)
        image.write_bytes(b"jpeg-evidence")
        return ImageEvidence(
            image_id="3f275b48-b86a-4db5-b720-3820ed84bdce",
            image_path=relative,
            image_sha256="0" * 64,
        )

    def test_adapter_sends_saved_jpeg_as_data_url(self):
        self.assertIsNotNone(_qwen, "services.soil3.vision.qwen_vision must define QwenVisionAnalyzer")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = FakeSession(FakeResponse(json.dumps({"image_quality": "unusable"})))
            analyzer = _qwen.QwenVisionAnalyzer(
                base_url="https://provider.example/api/v1",
                api_key="configured-test-key",
                image_root=root,
                session=session,
            )

            result = analyzer.analyze(self._evidence(root), previous_image_id=None)

        self.assertEqual(result["image_quality"], "unusable")
        request = session.requests[0]
        self.assertEqual(request["json"]["model"], "qwen3-vl-flash")
        url = request["json"]["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(request["url"], "https://provider.example/api/v1/chat/completions")

    def test_timeout_is_analysis_failure_not_image_quality(self):
        self.assertIsNotNone(_qwen, "services.soil3.vision.qwen_vision must define QwenVisionAnalyzer")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyzer = _qwen.QwenVisionAnalyzer(
                base_url="https://provider.example/api/v1",
                api_key="configured-test-key",
                image_root=root,
                session=TimeoutSession(),
            )

            with self.assertRaises(_qwen.AnalysisError) as raised:
                analyzer.analyze(self._evidence(root), previous_image_id=None)

        self.assertEqual(raised.exception.code, "MODEL_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
