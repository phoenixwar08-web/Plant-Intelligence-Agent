#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/root/.openclaw/workspace"))
DEFAULT_CONFIG = ROOT / "config" / "plants.json"


def load_registry(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path or os.environ.get("OPENCLAW_PLANT_CONFIG", DEFAULT_CONFIG))
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data.get("devices"), dict) or not data["devices"]:
        raise ValueError("plants.json must contain at least one device")
    default_device = data.get("default_device")
    if default_device not in data["devices"]:
        raise ValueError("default_device must exist in devices")
    return data


def device_config(
    device_code: str | None = None,
    path: str | Path | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    registry = load_registry(path)
    selected = device_code or registry["default_device"]
    if selected not in registry["devices"]:
        raise ValueError(f"unknown device: {selected}")
    return selected, dict(registry["devices"][selected]), registry


def report_devices(path: str | Path | None = None) -> list[tuple[str, dict[str, Any]]]:
    registry = load_registry(path)
    return [
        (code, dict(cfg))
        for code, cfg in registry["devices"].items()
        if cfg.get("daily_report", False)
    ]
