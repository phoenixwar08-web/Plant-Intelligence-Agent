from pathlib import Path
import json
from typing import Optional, Dict, Any


VISION_DIR = Path(
    "/root/agent/plant_agent/outputs/vision"
)


def load_visual_status(
    device_code: str
) -> Optional[Dict[str, Any]]:

    path = (
        VISION_DIR /
        f"{device_code}_vision.json"
    )

    if not path.exists():
        return None


    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:
        return json.load(f)
