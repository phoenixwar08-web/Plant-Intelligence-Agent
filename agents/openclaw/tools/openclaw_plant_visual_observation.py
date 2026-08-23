#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CONFIG_PATH = Path("/root/.openclaw/openclaw.json")
MAIN_AGENT_MODELS_PATH = Path("/root/.openclaw/agents/main/agent/models.json")
DEFAULT_RTSP = "rtsp://admin:N6-506arm@192.168.3.251:554/h264/ch1/main/av_stream"
DEFAULT_CAPTURE = Path("/tmp/openclaw_plant_visual_observation.jpg")
DEFAULT_ROI_CONFIG = Path("/root/.openclaw/workspace/config/plant_camera_rois.json")


PROMPT = (
    "你是室内植物视觉观察模块。只根据图片可见内容回答，不要猜植物品种，"
    "不要说你能闻到、触摸到或知道根系情况。请输出严格 JSON，不要 markdown。"
    "本次默认目标是画面中间那一盆植物；如果画面里有多盆植物，"
    "其它盆只当背景，不要纳入 risk_level、user_friendly_summary 或主要字段评分。"
    "所有字段都只描述目标盆位，除非目标盆位被遮挡或无法判断。"
    "字段必须包括：overall_visual_state, visible_leaf_state, wilt_or_droop, "
    "yellowing_or_browning, soil_surface, pot_probe_tube_visible, image_quality, "
    "risk_level, confidence, caveats, user_friendly_summary。risk_level 只能是 "
    "low/medium/high。confidence 是 0 到 1。user_friendly_summary 用中文第一人称，"
    "像植物在说话，幽默一点，但不要编造植物名字、品种或情绪。"
    "只靠图片不能判断我渴不渴、根部湿度或是否需要浇水；除非画面有明确积水/干裂，"
    "否则不要说我想喝水、我不渴、我没喊渴、需要浇水、土壤很干或土壤很湿。"
    "浇水建议必须交给传感器和自动养护判断。"
)


def load_roi(config_path: Path, device: str) -> dict[str, Any] | None:
    if not config_path.exists():
        return None
    data = json.loads(config_path.read_text(encoding="utf-8"))
    rois = data.get("devices") if isinstance(data, dict) else None
    roi = (rois or {}).get(device)
    if not isinstance(roi, dict):
        return None
    required = ("x", "y", "w", "h")
    if not all(isinstance(roi.get(key), (int, float)) for key in required):
        return None
    return {key: float(roi[key]) for key in required}


def clear_proxy_env() -> urllib.request.OpenerDirector:
    for key in list(os.environ):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            os.environ.pop(key, None)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_config() -> dict[str, Any]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if MAIN_AGENT_MODELS_PATH.exists():
        main_models = json.loads(MAIN_AGENT_MODELS_PATH.read_text(encoding="utf-8"))
        for provider, provider_cfg in main_models.get("providers", {}).items():
            cfg.setdefault("models", {}).setdefault("providers", {})[provider] = {
                **cfg.get("models", {}).get("providers", {}).get(provider, {}),
                **provider_cfg,
            }
    return cfg


def capture_frame(rtsp_url: str, path: Path, timeout: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-rtsp_transport",
            "tcp",
            "-i",
            rtsp_url,
            "-vframes",
            "1",
            "-q:v",
            "2",
            str(path),
        ],
        capture_output=True,
        timeout=timeout,
    )
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError("camera_capture_failed")
    return path


def crop_roi(image_path: Path, roi: dict[str, Any], timeout: int) -> Path:
    crop_path = image_path.with_name(image_path.stem + "_roi.jpg")
    x = roi["x"]
    y = roi["y"]
    w = roi["w"]
    h = roi["h"]
    vf = f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(image_path), "-vf", vf, "-q:v", "2", str(crop_path)],
        capture_output=True,
        timeout=timeout,
    )
    if not crop_path.exists() or crop_path.stat().st_size <= 0:
        raise RuntimeError("roi_crop_failed")
    return crop_path


def model_candidates(cfg: dict[str, Any], override: str | None) -> list[str]:
    if override:
        return [override]
    image_model = cfg["agents"]["defaults"].get("imageModel", {})
    primary = image_model.get("primary", "alibaba/qwen3-omni-flash")
    return [primary] + list(image_model.get("fallbacks", []))


def strip_json_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.S)
    if m:
        return m.group(1).strip()
    return text


def call_model(
    cfg: dict[str, Any],
    opener: urllib.request.OpenerDirector,
    model_full: str,
    image_path: Path,
    timeout: int,
) -> dict[str, Any]:
    provider, model_id = model_full.split("/", 1)
    provider_cfg = cfg["models"]["providers"][provider]
    url = provider_cfg["baseUrl"].rstrip("/") + "/chat/completions"
    image_b64 = base64.b64encode(image_path.read_bytes()).decode()
    body = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + image_b64},
                    },
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 900,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + provider_cfg["apiKey"],
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {body}") from exc
    content = data["choices"][0]["message"].get("content", "").strip()
    parsed = None
    parse_error = None
    try:
        parsed = json.loads(strip_json_fence(content))
    except Exception as exc:
        parse_error = f"{type(exc).__name__}: {exc}"
    return {
        "model": model_full,
        "raw_content": content,
        "parsed": parsed,
        "parse_error": parse_error,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw plant visual observation via imageModel")
    parser.add_argument("--image", help="Use an existing image instead of capturing RTSP")
    parser.add_argument("--capture-path", default=str(DEFAULT_CAPTURE))
    parser.add_argument("--rtsp-url", default=DEFAULT_RTSP)
    parser.add_argument("--device", default="soil3", help="Internal device code for ROI lookup")
    parser.add_argument("--roi-config", default=str(DEFAULT_ROI_CONFIG))
    parser.add_argument("--no-roi", action="store_true", help="Disable configured ROI crop")
    parser.add_argument("--model", help="Override model, e.g. alibaba/qwen3.5-omni-plus")
    parser.add_argument("--capture-timeout", type=int, default=25)
    parser.add_argument("--api-timeout", type=int, default=70)
    args = parser.parse_args()

    cfg = load_config()
    opener = clear_proxy_env()
    image_path = Path(args.image) if args.image else capture_frame(args.rtsp_url, Path(args.capture_path), args.capture_timeout)
    if not image_path.exists():
        raise SystemExit(json.dumps({"ok": False, "error": "image_missing", "image_path": str(image_path)}, ensure_ascii=False))
    roi = None
    source_image_path = image_path
    if not args.no_roi:
        roi = load_roi(Path(args.roi_config), args.device)
        if roi:
            image_path = crop_roi(image_path, roi, args.capture_timeout)

    errors: list[dict[str, str]] = []
    for model_full in model_candidates(cfg, args.model):
        try:
            result = call_model(cfg, opener, model_full, image_path, args.api_timeout)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "source": "openclaw_image_model",
                        "image_path": str(image_path),
                        "source_image_path": str(source_image_path),
                        "image_size_bytes": image_path.stat().st_size,
                        "target_device": args.device,
                        "roi": roi,
                        "roi_applied": bool(roi),
                        **result,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        except Exception as exc:
            errors.append({"model": model_full, "error": type(exc).__name__, "detail": str(exc)[:1000]})

    print(json.dumps({"ok": False, "source": "openclaw_image_model", "errors": errors}, ensure_ascii=False, indent=2))
    raise SystemExit(1)


if __name__ == "__main__":
    main()
