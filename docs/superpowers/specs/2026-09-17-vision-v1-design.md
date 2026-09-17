# Vision V1 Day 2 Design

## Purpose and scope

Issue #12 provides one repository-internal Python API named capture_and_analyze_once(). A successful call captures one JPEG frame from RTSP, stores the whole frame as immutable evidence, crops each configured plant zone out of it, sends each crop to qwen3-vl-flash, validates the returned JSON locally, saves one vision.v1 record per zone, and returns them.

This is observation only. It adds no HTTP endpoint, scheduler, Episode integration, state.v1 mutation, Phase3 change, MQTT call, or pump action.

The camera currently shows two plants whose foliage overlaps in the middle of the picture, so the frame as a whole is never judged. Each plant has its own fixed region of interest, is analysed on its own, is recorded on its own, and is compared only with its own earlier crop.

## Configuration and persistence

Read only these environment variables: SOIL3_CAMERA_RTSP_URL, QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL, SOIL3_VISION_DATA_DIR, and SOIL3_VISION_ZONES_PATH. QWEN_MODEL defaults to qwen3-vl-flash. Never commit or log a credential, RTSP address, authorization header, Base64 payload, prompt text, or raw provider response.

SOIL3_VISION_DATA_DIR is mandatory; no repository or production runtime path is inferred. A run may write:

    <data-dir>/frames/YYYY-MM-DD/<source_frame_id>.jpg     the whole captured frame, once per run
    <data-dir>/images/YYYY-MM-DD/<image_id>.jpg            one crop per zone, what the model saw
    <data-dir>/records/YYYY-MM-DD/<image_id>.json          one validated observation per zone
    <data-dir>/failures/YYYY-MM-DD/<attempt_id>.json       one sanitised failure

SOIL3_VISION_ZONES_PATH names a JSON document of one to eight zones, each `{"id", "rect", "label"}` with rect `[x, y, width, height]` as fractions of the frame, so a zone survives a resolution change and excludes the camera's burned-in timestamp. See config/vision_zones.example.json. A configuration file is used rather than hard-coded constants because the rects are measured from the real picture and are expected to be re-measured when the camera moves.

Rectangles only build the model's input. The original frame is always stored complete, so a wrong rect stays reviewable instead of silently destroying the only evidence.

Stored JPEGs are immutable. Every identifier is a UUID generated after a decodable image has been saved, and every hash is the SHA-256 of the exact bytes of that file. Records store only relative paths.

## Components

vision_v1.py owns data classes, allowed values, PlantZone, parse_zones, CaptureOutcome, VisionRunResult, and the local semantic validator. vision_capture.py owns a single-frame capture port, the OpenCV RTSP implementation, crop_to_zone, and an EvidenceStore holding frames and crops. qwen_vision.py owns one OpenAI-compatible Qwen request and JSON-only response parsing. vision_service.py composes them and exposes the entry point.

The Qwen request sends the zone crop as a Base64 Data URL, preceded by that same zone's stored earlier crop when one exists so change_vs_previous is grounded in images rather than invented. Its prompt requires JSON only, enumerates legal field values, prohibits care advice and hidden reasoning, and requires null when the picture gives no evidence. The local validator is authoritative; model output cannot create a record until it passes.

Capturing RTSP needs an OpenCV build with the FFmpeg backend. The RPM shipped by the operating system does not have it and fails with RTSP_OPEN_FAILED, so requirements.txt pins the opencv-python-headless wheel and it is installed in a dedicated virtual environment that leaves the system interpreter used by the running services untouched.

## vision.v1 contract

A successful record has:

- Identity and provenance: schema_version fixed to vision.v1; device_code; plant_zone containing id, rect, and label; image_id; image_path; image_sha256; source_frame_id; source_frame_path; source_frame_sha256; previous_image_id; captured_at; analyzed_at; and model metadata containing provider, name, and prompt_version.
- The 17 observation fields the model must return: image_quality; target_detected; target_ambiguity; leaf_droop; leaf_spread; wilting; yellowing; visible_damage; browning; leaf_curl; spots_or_lesions; leaf_loss; stem_posture; occlusion; overall_visual_state; change_vs_previous; and confidence.

Allowed values are:

- image_quality: good, poor, unusable.
- target_detected: true, false, or null.
- target_ambiguity and the nine graded observations leaf_droop, yellowing, visible_damage, browning, leaf_curl, spots_or_lesions, leaf_loss, stem_posture, occlusion: none, mild, moderate, severe, or null.
- leaf_spread: closed, normal, wide, or null.
- wilting: true, false, or null.
- overall_visual_state: normal, mild_abnormality, obvious_abnormality, severe_abnormality, unavailable. It describes appearance only; a health verdict is not a legal value, and neither is advice.
- change_vs_previous: improved, stable, worsened, unknown.
- confidence: number in [0, 1] or null.

Null is a required answer, not an absence: a field the picture does not support stays null instead of defaulting to false or none. The validator rejects claims that outrun their evidence:

- If image_quality is unusable, every other observation field and confidence is null, overall_visual_state is unavailable, and change_vs_previous is unknown.
- If target_detected is not true, the zone is reported the same way; an unconfirmed plant is never recorded as looking normal.
- Any overall_visual_state other than unavailable must cite at least one non-null observation field.
- A change other than unknown is impossible without a previous_image_id, and the service only ever fills that from the most recent persisted record of the same device and the same plant_zone. The validator rejects a record that points at itself.
- image_path is a crop and source_frame_path is the frame it came from; they are always two distinct stored files.

## Outcome semantics

One run returns a VisionRunResult holding the frame id and one CaptureOutcome per zone. CaptureOutcome is not a forced vision.v1 record.

| Status | Image evidence | vision.v1 |
| --- | --- | --- |
| capture_failed | None for that zone | None |
| image_unusable | Crop plus hash | Persisted |
| analysis_failed | Crop plus hash | None |
| success | Crop plus hash | Persisted |

capture_failed means RTSP did not provide a decodable image or the zone could not be cut out of it. Its failure record contains attempt ID, timestamp, and a sanitised error code, plus plant_zone when a zone was reached. analysis_failed means the capture, crop, and storage worked but the model timed out, the request failed, invalid JSON came back, or semantic validation rejected it; its failure record additionally contains image_id, image_sha256, plant_zone, and source_frame_id. Neither failure status may be relabelled as image quality or fabricate visual facts.

Failure is isolated per zone: a zone whose crop cannot be produced, or whose analysis fails, does not discard the other zone's observation. The run status is the shared zone status, or partial when the zones did not all end the same way.

## Verification and future boundary

Tests use fake capture and analyzer dependencies, temporary data directories, and no real RTSP or Qwen request. They cover all four per-zone outcomes and failure isolation, whole-frame versus crop storage and hashing, rect-to-pixel mapping across resolutions, invalid JSON, every semantic rule above, the prompt staying aligned with the contract, and per-zone history: the earlier image offered to a zone is that zone's own most recent readable crop, never the other plant's. A real camera and model smoke test is manual, requires explicit user approval, and its zone rectangles are checked by eye against the saved crops.

Day 3 or later may use the same Python entry point behind an Episode scheduler, periodic scheduler, CLI, or HTTP wrapper. Those later callers do not duplicate Day 2 logic.
