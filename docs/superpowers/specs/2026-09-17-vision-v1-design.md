# Vision V1 Day 2 Design

## Purpose and scope

Issue #12 provides one repository-internal Python API named capture_and_analyze_once(). A successful call captures one JPEG frame from RTSP, saves immutable image evidence, sends the saved image to qwen3-vl-flash, validates the returned JSON locally, saves a vision.v1 record, and returns it.

This is observation only. It adds no HTTP endpoint, scheduler, Episode integration, state.v1 mutation, Phase3 change, MQTT call, or pump action.

## Configuration and persistence

Read only these environment variables: SOIL3_CAMERA_RTSP_URL, QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL, and SOIL3_VISION_DATA_DIR. QWEN_MODEL defaults to qwen3-vl-flash. Never commit or log a credential, RTSP address, authorization header, Base64 payload, prompt text, or raw provider response.

SOIL3_VISION_DATA_DIR is mandatory; no repository or production runtime path is inferred. A run may write:

    <data-dir>/images/YYYY-MM-DD/<image_id>.jpg
    <data-dir>/records/YYYY-MM-DD/<image_id>.json
    <data-dir>/failures/YYYY-MM-DD/<attempt_id>.json

The saved JPEG is immutable. image_id is a UUID generated after a decodable image has been saved, and image_sha256 is the SHA-256 of its exact bytes. Records store only a relative image path.

## Components

vision_v1.py owns data classes, allowed values, CaptureOutcome, and the local semantic validator. vision_capture.py owns a single-frame capture port, the OpenCV RTSP implementation, and filesystem storage. qwen_vision.py owns one OpenAI-compatible Qwen request and JSON-only response parsing. vision_service.py composes them and exposes the entry point.

The Qwen request sends the saved JPEG as a Base64 Data URL. Its prompt requires JSON only, enumerates legal field values, prohibits care advice and hidden reasoning, and requires unknown fields when the image is unusable. The local validator is authoritative; model output cannot create a record until it passes.

## vision.v1 contract

A successful record has: schema_version fixed to vision.v1; device_code; image_id; previous_image_id; captured_at; analyzed_at; image_sha256; image_path; image_quality; leaf_droop; leaf_spread; wilting; yellowing; visible_damage; overall_visual_state; change_vs_previous; confidence; and model metadata containing provider, name, and prompt_version.

Allowed values are:
- image_quality: good, poor, unusable.
- leaf_droop: none, mild, moderate, severe, or null.
- leaf_spread: closed, normal, wide, or null.
- yellowing and visible_damage: none, mild, moderate, severe, or null.
- overall_visual_state: healthy, attention, poor, unavailable.
- change_vs_previous: improved, stable, worsened, unknown.
- confidence: number in [0, 1] or null.

If image_quality is unusable, all plant observation fields and confidence are null, overall_visual_state is unavailable, and change_vs_previous is unknown. A first record has previous_image_id null and change_vs_previous unknown. Any non-null previous_image_id must name a persisted record for the same device.

## Outcome semantics

CaptureOutcome is not a forced vision.v1 record.

| Status | Image evidence | vision.v1 |
| --- | --- | --- |
| capture_failed | None | None |
| image_unusable | JPEG plus hash | Persisted |
| analysis_failed | JPEG plus hash | None |
| success | JPEG plus hash | Persisted |

capture_failed means RTSP did not provide a decodable image. Its failure record contains attempt ID, timestamp, and a sanitised error code only. analysis_failed means the capture and storage worked but the model timed out, request failed, returned invalid JSON, or failed semantic validation. Its failure record additionally contains image_id and image_sha256. Neither failure status may be relabelled as image quality or fabricate visual facts.

## Verification and future boundary

Tests use fake capture and analyzer dependencies, temporary data directories, and no real RTSP or Qwen request. They cover all four outcomes, immutable image hashing, invalid JSON, invalid semantics, and previous-image rules. A real camera/model smoke test is manual and requires later explicit user approval.

Day 3 or later may use the same Python entry point behind an Episode scheduler, periodic scheduler, CLI, or HTTP wrapper. Those later callers do not duplicate Day 2 logic.
