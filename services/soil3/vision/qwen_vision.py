"""One-request Qwen vision boundary for Vision V1, per plant zone."""
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


_PROMPT = """Return JSON only, with no markdown and no care, watering, or treatment recommendation.

You are shown one fixed region of a camera frame that may contain a potted plant. When two images
are given, the FIRST is the same region at an earlier time and the LAST is the current one: assess
only the current image, and use the earlier image solely to judge change_vs_previous. When only one
image is given, change_vs_previous must be "unknown". Ignore any timestamp or text the camera burns
into the corner of the frame, and do not describe anything outside the region you were given.

Report exactly these 17 fields and no others:
- image_quality: "good", "poor", or "unusable" - whether the current image can be assessed visually.
- target_detected: true, false, or null - whether the plant of this region is actually visible in it.
- target_ambiguity: "none", "mild", "moderate", "severe", or null - how unsure you are that the leaves
  and stems you are describing belong to that one plant rather than a neighbouring plant, a curtain,
  a window reflection, a support stake, or background objects.
- leaf_droop: "none", "mild", "moderate", "severe", or null - leaves hanging lower than their normal posture.
- leaf_spread: "closed", "normal", "wide", or null - how far the leaves spread from the crown.
- wilting: true, false, or null - overall limpness of the plant.
- yellowing: "none", "mild", "moderate", "severe", or null
- visible_damage: "none", "mild", "moderate", "severe", or null - tears, holes, chewed or broken leaves.
- browning: "none", "mild", "moderate", "severe", or null - brown leaf tips, scorched edges, dead leaves.
- leaf_curl: "none", "mild", "moderate", "severe", or null - curled, cupped, or twisted leaf blades.
- spots_or_lesions: "none", "mild", "moderate", "severe", or null - spots, patches, or coated areas.
- leaf_loss: "none", "mild", "moderate", "severe", or null - missing leaves or a visibly thinner plant.
- stem_posture: "none", "mild", "moderate", "severe", or null - bent, leaning, fallen, or bare stems.
- occlusion: "none", "mild", "moderate", "severe", or null - how much of the plant is hidden from view.
- overall_visual_state: "normal", "mild_abnormality", "obvious_abnormality", "severe_abnormality", or
  "unavailable" - a description of how the foliage looks. It is not a health diagnosis, a cause, or advice.
- change_vs_previous: "improved", "stable", "worsened", or "unknown"
- confidence: a number from 0 to 1, or null

Use null whenever the image does not give enough evidence for a field. Never substitute false,
"none", or a plausible guess for missing evidence. Reddish new growth, dry soil, a pot, a sensor
probe, or a support stake is not by itself damage. If image_quality is "unusable", set every other
field and confidence to null and overall_visual_state to "unavailable". Do the same when
target_detected is false or null."""


class QwenVisionAnalyzer:
    """Send one saved zone crop, and optionally its earlier counterpart, to Qwen."""

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

    def analyze(self, evidence: ImageEvidence, previous_jpeg: bytes | None = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": _PROMPT}]
        if previous_jpeg:
            content.append(self._image_part(previous_jpeg))
        content.append(self._image_part(self._read(evidence)))
        payload = {
            "model": self._model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
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

    def _read(self, evidence: ImageEvidence) -> bytes:
        try:
            return (self._image_root / evidence.image_path).read_bytes()
        except OSError as error:
            raise AnalysisError("IMAGE_EVIDENCE_UNAVAILABLE") from error

    @staticmethod
    def _image_part(jpeg_bytes: bytes) -> dict[str, Any]:
        return {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")},
        }
