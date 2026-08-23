#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Plant Vision Agent

RTSP snapshot -> Qwen Vision API -> VisualStatus JSON

"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.append(
    str(BASE_DIR)
)
import json
import base64
import fcntl
import os
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from vision.leaf_analyzer import analyze_leaf

import requests


BASE_DIR = Path(__file__).resolve().parent.parent


IMAGE_DIR = (
    BASE_DIR
    /
    "outputs"
    /
    "vision"
    /
    "images"
)


OUTPUT_DIR = (
    BASE_DIR
    /
    "outputs"
    /
    "vision"
)


VISION_HISTORY_DIR = OUTPUT_DIR / "history"
VISUAL_HISTORY_DEVICE = "soil2"
VISUAL_HISTORY_FIELDS = (
    "plant_health",
    "disease_suspected",
    "visual_stress_level",
    "yellow_leaf_ratio",
    "green_leaf_area_px",
    "flower_count",
    "fruit_count",
    "suspected_issues",
    "image_quality",
)


# ==============================
# Alibaba OpenAI Compatible API
# ==============================


API_URL = (
    "https://ws-d5yw23tz0yzwob1l."
    "cn-beijing.maas.aliyuncs.com/"
    "compatible-mode/v1/chat/completions"
)


# Deployment-local credential. It must be supplied outside version control.
API_KEY = os.environ.get("PLANT_VISION_API_KEY", "")


MODEL = "qwen3-omni-flash"



# ==============================
# image encode
# ==============================


def image_to_base64(path):

    data = path.read_bytes()

    return base64.b64encode(
        data
    ).decode("utf-8")



# ==============================
# call vision model
# ==============================


def analyze_image(image_path):

    image_base64 = image_to_base64(
        image_path
    )


    payload = {

        "model": MODEL,

        "messages": [

            {
                "role": "system",
                "content": (
                    "你是植物视觉检测专家。"
                    "分析盆栽状态。"
                    "只输出JSON。"
                )
            },


            {

                "role": "user",

                "content": [

                    {
                        "type": "text",
                        "text": """
分析这张植物照片。

只输出JSON：

{
 plant_health:
 normal/stressed/dead,

 disease_suspected:
 true/false,

 visual_stress_level:
 none/light/moderate/severe,

 flower_count:
 integer,

 fruit_count:
 integer,

 suspected_issues:
 []
}

不要输出解释文字。
"""
                    },


                    {

                        "type":
                        "image_url",

                        "image_url":
                        {
                            "url":
                            f"data:image/jpeg;base64,{image_base64}"
                        }

                    }

                ]

            }

        ],

        "temperature":0.1

    }



    headers = {

        "Authorization":
        f"Bearer {API_KEY}",

        "Content-Type":
        "application/json"

    }



    response = requests.post(

        API_URL,

        headers=headers,

        json=payload,

        timeout=120

    )


    response.raise_for_status()


    result = response.json()


    text = (
        result["choices"][0]
        ["message"]
        ["content"]
    )


    return text



# ==============================
# parse result
# ==============================


def parse_visual(text):

    try:

        text = text.strip()


        # 去除 markdown json 标记

        if text.startswith("```"):

            text = (
                text
                .replace("```json", "")
                .replace("```", "")
                .strip()
            )


        # 截取第一个 { 到最后一个 }

        start = text.find("{")
        end = text.rfind("}")


        if start != -1 and end != -1:

            text = text[start:end+1]


        data = json.loads(text)


        return {

            "plant_health":
                data.get(
                    "plant_health",
                    "unknown"
                ),

            "disease_suspected":
                bool(
                    data.get(
                        "disease_suspected",
                        False
                    )
                ),

            "visual_stress_level":
                data.get(
                    "visual_stress_level",
                    "unknown"
                ),

            "yellow_leaf_ratio":
                data.get(
                    "yellow_leaf_ratio"
                ),

            "green_leaf_area_px":
                None,
                
            "flower_count":
                data.get(
                    "flower_count",
                    0
                ),

            "fruit_count":
                data.get(
                    "fruit_count",
                    0
                ),

            "suspected_issues":
                data.get(
                    "suspected_issues",
                    []
                )

        }


    except Exception:


        return {

            "plant_health":
            "unknown",

            "disease_suspected":
            False,

            "visual_stress_level":
            "unknown",

            "yellow_leaf_ratio":
            None,

            "green_leaf_area_px":
            None,

            "flower_count":
            0,

            "fruit_count":
            0,

            "suspected_issues":
            [
                "model_output_parse_failed"
            ],

            "raw":
            text

        }

# ==============================
# main
# ==============================


def current_observed_at():
    """返回视觉观察发生时刻，统一使用上海时区。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def append_visual_history(device_code, observed_at, visual):
    """为 soil2 成功视觉观察追加可用于趋势分析的结构化记录。"""
    if device_code != VISUAL_HISTORY_DEVICE:
        return False

    history_visual = {
        field: visual.get(field)
        for field in VISUAL_HISTORY_FIELDS
    }
    if history_visual["disease_suspected"] is None:
        history_visual["disease_suspected"] = False
    if not isinstance(history_visual["suspected_issues"], list):
        history_visual["suspected_issues"] = []

    record = {
        "schema_version": 1,
        "device_code": device_code,
        "observed_at": observed_at,
        "model": MODEL,
        "visual": history_visual,
    }
    history_path = VISION_HISTORY_DIR / f"{device_code}.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    with history_path.open("a", encoding="utf-8") as history_file:
        fcntl.flock(history_file.fileno(), fcntl.LOCK_EX)
        try:
            history_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            history_file.flush()
            os.fsync(history_file.fileno())
        finally:
            fcntl.flock(history_file.fileno(), fcntl.LOCK_UN)

    return True


def main():


    device_code = sys.argv[1]


    image_path = (
        IMAGE_DIR
        /
        f"{device_code}.jpg"
    )


    if not image_path.exists():

        raise FileNotFoundError(
            image_path
        )


    result_text = analyze_image(
        image_path
    )


    visual = parse_visual(
        result_text
    )
    
    
    leaf_result = analyze_leaf(
        str(image_path)
    )
    
    
    visual.update({
    
        "green_leaf_area_px":
            leaf_result.get(
                "green_leaf_area_px"
            ),
    
        "yellow_leaf_ratio":
            leaf_result.get(
                "yellow_pixel_ratio"
            )
    
    })
    

    output = {


        "device_code":
        device_code,


        "observed_at":
        current_observed_at(),


        "visual":
        visual

    }



    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


    out_file = (
        OUTPUT_DIR
        /
        f"{device_code}_vision.json"
    )


    out_file.write_text(

        json.dumps(

            output,

            ensure_ascii=False,

            indent=2

        ),

        encoding="utf-8"

    )

    try:
        append_visual_history(
            device_code,
            output["observed_at"],
            output["visual"],
        )
    except Exception as exc:
        print(
            f"[WARN] 视觉历史写入失败: {exc}",
            file=sys.stderr,
        )


    print(

        json.dumps(

            output,

            ensure_ascii=False,

            indent=2

        )

    )


    print(
        f"\n[OK] Saved: {out_file}"
    )



if __name__ == "__main__":

    main()
