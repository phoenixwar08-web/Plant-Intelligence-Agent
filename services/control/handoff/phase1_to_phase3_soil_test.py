#!/usr/bin/env python3
"""Phase1 -> Phase3 handoff for the test pot.

Triggered by systemd timer. It checks Phase1 state once and exits.
When Phase1 has completed, it stops water_test.service, sends a pump-off
fallback to esp32/pump_test/cmd, then starts phase3_soil_test.service.
"""

from __future__ import annotations

import fcntl
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - deployment fallback
    mqtt = None  # type: ignore[assignment]

STATE_PATH = Path("/root/water/phase1_test/phase1_data_test.json")
PHASE3_PARAMS = Path("/root/water/phase3/soil_test/evolving_params.json")
LOCK_PATH = Path("/run/phase1_to_phase3_soil_test.lock")
LOG_PATH = Path("/root/water/phase_handoff/phase1_to_phase3_soil_test.log")

PHASE1_SERVICE = "water_test.service"
PHASE3_SERVICE = "phase3_soil_test.service"
MQTT_BROKER = "localhost"
PUMP_TOPIC = "esp32/pump_test/cmd"
MIN_COMPLETED_CYCLES = 3

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("phase_handoff")


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    logger.info("run: %s", " ".join(cmd))
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def is_active(service: str) -> bool:
    result = subprocess.run(["systemctl", "is-active", "--quiet", service])
    return result.returncode == 0


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        raise RuntimeError(f"Phase1 state file not found: {STATE_PATH}")
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def valid_phase1_genes(state: dict[str, Any]) -> bool:
    required = [
        state.get("learned_FC"),
        state.get("learned_target_low"),
        state.get("irrigation_gain_kp"),
    ]
    return all(v is not None for v in required)


def phase1_complete(state: dict[str, Any]) -> bool:
    # water_test.py sets handover_complete only after writing final Phase1 output.
    if not state.get("handover_complete", False):
        return False
    if int(state.get("completed_cycles", 0) or 0) < MIN_COMPLETED_CYCLES:
        return False
    return valid_phase1_genes(state)


def publish_pump_off() -> None:
    if mqtt is None:
        logger.warning("paho-mqtt is unavailable; skip MQTT pump-off fallback")
        return
    client = mqtt.Client()
    try:
        client.connect(MQTT_BROKER, 1883, 10)
        client.loop_start()
        for _ in range(3):
            info = client.publish(PUMP_TOPIC, "off", qos=1)
            info.wait_for_publish(timeout=2)
            time.sleep(0.2)
        logger.info("published pump off fallback to %s", PUMP_TOPIC)
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


def ensure_phase3_params_ready(force_rebuild: bool = False) -> None:
    # Build Phase3 params from the final Phase1 output. During handoff, force a
    # rebuild so stale params created before Phase1 completion cannot survive.
    if PHASE3_PARAMS.exists() and not force_rebuild:
        return
    if PHASE3_PARAMS.exists():
        backup = PHASE3_PARAMS.with_suffix(f".json.bak_handoff_{int(time.time())}")
        PHASE3_PARAMS.rename(backup)
        logger.info("backed up stale Phase3 params to %s", backup)
    code = (
        "from config_manager import ensure_data_files_exist, ConfigManager; "
        "ensure_data_files_exist(); ConfigManager()"
    )
    run([
        "/root/water/decision310/bin/python",
        "-c",
        code,
    ], check=True)


def perform_handoff(state: dict[str, Any]) -> None:
    if is_active(PHASE3_SERVICE):
        logger.info("%s is already active; nothing to do", PHASE3_SERVICE)
        if is_active(PHASE1_SERVICE):
            run(["systemctl", "stop", PHASE1_SERVICE], check=False)
        return

    logger.info(
        "Phase1 complete: cycles=%s FC=%s target_low=%s Kp=%s; switching to Phase3",
        state.get("completed_cycles"),
        state.get("learned_FC"),
        state.get("learned_target_low"),
        state.get("irrigation_gain_kp"),
    )

    ensure_phase3_params_ready(force_rebuild=True)
    publish_pump_off()
    run(["systemctl", "stop", PHASE1_SERVICE], check=False)
    publish_pump_off()
    run(["systemctl", "start", PHASE3_SERVICE], check=True)
    run(["systemctl", "disable", PHASE1_SERVICE], check=False)
    logger.info("handoff complete: %s stopped, %s started", PHASE1_SERVICE, PHASE3_SERVICE)


def main() -> int:
    with LOCK_PATH.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("another handoff check is running; exit")
            return 0

        state = load_state()
        logger.info(
            "check: phase=%s cycles=%s handover_complete=%s phase3_active=%s",
            state.get("phase"),
            state.get("completed_cycles"),
            state.get("handover_complete"),
            is_active(PHASE3_SERVICE),
        )

        if not phase1_complete(state):
            logger.info("Phase1 is not complete; no handoff")
            return 0

        perform_handoff(state)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
