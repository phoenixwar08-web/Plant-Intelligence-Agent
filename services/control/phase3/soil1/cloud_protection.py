"""Fail-closed cloud protection input for soil1 Phase3."""
import os
import sys
from pathlib import Path


IOT_AGENT_ROOT = os.environ.get("PHASE3_IOT_AGENT_ROOT", "/root/water/wyc/IOT")
if IOT_AGENT_ROOT not in sys.path:
    sys.path.insert(0, IOT_AGENT_ROOT)

from iot_agent.control import read_control, watering_allowed  # noqa: E402


DEVICE_NAME = "soil1"
CONTROL_FILE = Path(
    os.environ.get(
        "PHASE3_CLOUD_CONTROL_FILE",
        "/root/water/wyc/IOT/data/control/soil1.json",
    )
)


def cloud_protection_enabled():
    return os.environ.get(
        "PHASE3_CLOUD_PROTECTION_ENABLED", "true"
    ).strip().lower() in ("1", "true", "yes", "on")


def _alert_only_control():
    return {
        "auto_watering_enabled": True,
        "safety_mode": "normal",
        "risk_level": "ok",
        "reason": "cloud_alert_only",
        "source": "local_config",
        "updated_at": None,
        "expires_at": None,
    }


def load_cloud_control():
    """Load and validate the latest cloud protection state.

    Missing, malformed, illegal, or expired input is converted to a critical
    fail-safe state by ``read_control``.
    """
    if not cloud_protection_enabled():
        return _alert_only_control()
    return read_control(str(CONTROL_FILE), device_name=DEVICE_NAME, fail_safe=True)


def cloud_watering_allowed(control, emergency=False, sensor_trusted=True):
    if not cloud_protection_enabled():
        return True, "cloud_alert_only"
    return watering_allowed(
        control,
        emergency=emergency,
        sensor_trusted=sensor_trusted,
    )


def protection_mode(control):
    if not control.get("auto_watering_enabled", False):
        return "paused"
    if control.get("safety_mode") == "conservative":
        return "conservative"
    return "normal"


def audit_view(control):
    return {
        "mode": protection_mode(control),
        "risk_level": control.get("risk_level", "critical"),
        "reason": control.get("reason"),
        "command_id": control.get("command_id") or control.get("event_id"),
        "updated_at": control.get("updated_at"),
        "expires_at": control.get("expires_at"),
        "source": control.get("source"),
    }
