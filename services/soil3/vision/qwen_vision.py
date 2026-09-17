"""One-request Qwen vision boundary for Vision V1."""
from __future__ import annotations

import base64
from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

import requests

from services.soil3.vision.vision_capture import ImageEvidence


class AnalysisError(ValueError):
    """A sanitised failure to obtain a structured model analysis."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_PROMPT = """Return JSON only, with no markdown or care recommendation.
Assess the current plant image using these fields only: image_quality (good, poor,
unusable), leaf_droop (none, mild, moderate, severe, or null), leaf_spread
(closed, normal, wide, or null), wilting (true, false, or null), yellowing
(none, mild, moderate, severe, or null), visible_damage (none, mild, moderate,
severe, or null), overall_visual_state (healthy, attention, poor, unavailable),
change_vs_previous (improved, stable, worsened, unknown), and confidence (0 to 1
or null). There is no comparison image, so change_vs_previous must be unknown.
If the image is unusable, all observation fields and confidence must be null and
overall_visual_state must be unavailable."""


class QwenVisionAnalyzer:
    """Send one saved JPEG to an OpenAI-compatible Qwen endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        image_root: Path,
        model: str = "qwen3-vl-flash",
        timeout_seconds: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._image_root = Path(image_root)
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._session = session if session is not None else requests.Session()

    def analyze(self, evidence: ImageEvidence, previous_image_id: str | None) -> dict[str, Any]:
        del previous_image_id
        try:
            jpeg_bytes = (self._image_root / evidence.image_path).read_bytes()
        except OSError as error:
            raise AnalysisError("IMAGE_EVIDENCE_UNAVAILABLE") from error
        image_url = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")
        payload = {
            "model": self._model,
            "response_format": {"type": "json_object"},
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
        }
        try:
            response = self._session.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except requests.Timeout as error:
            raise AnalysisError("MODEL_TIMEOUT") from error
        except requests.RequestException as error:
            raise AnalysisError("MODEL_REQUEST_FAILED") from error
        try:
            value = json.loads(response.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise AnalysisError("MODEL_INVALID_JSON") from error
        if not isinstance(value, Mapping):
            raise AnalysisError("MODEL_INVALID_JSON")
        return dict(value)
