"""Read-only-to-claim bridge for soil3 Phase3 experience validation.

The bridge does not compute a water duration and never publishes MQTT.  It can
only attach a validation trial to a positive action already selected by soil3's
normal Phase3 decision path.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional


PLANT_AGENT_ROOT = Path("/root/agent/plant_agent")
if not PLANT_AGENT_ROOT.exists():
    PLANT_AGENT_ROOT = Path(__file__).resolve().parents[1] / "plant_agent"
if str(PLANT_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(PLANT_AGENT_ROOT))

from cross_plant_experience import (  # noqa: E402
    ExperienceRepository,
    ExperienceStorageError,
    validation_block_reasons,
)


TARGET_DEVICE = "soil3"
MANUAL_EVENT_LOOKBACK_HOURS = 6


def _flag_active(value: Any) -> bool:
    return bool(value.get("active")) if isinstance(value, dict) else bool(value)


class ExperienceValidationBridge:
    def __init__(self, *, repository: Optional[ExperienceRepository] = None) -> None:
        self.repository = repository or ExperienceRepository()

    @staticmethod
    def _safety_flags(state: Dict[str, Any]) -> Dict[str, bool]:
        return {
            "water_delivery_suspect": _flag_active(state.get("water_delivery_suspect")),
            "reservoir_empty_suspect": _flag_active(state.get("reservoir_empty_suspect")),
            "sensor_fault": _flag_active(state.get("sensor_fault")),
        }

    def claim_if_eligible(
        self,
        reading: Any,
        state: Dict[str, Any],
        *,
        action_sec: float,
        plan_label: str,
    ) -> Optional[Dict[str, Any]]:
        """Claim a waiting request or return None without affecting Phase3."""

        since = (datetime.now() - timedelta(hours=MANUAL_EVENT_LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            manual_event, probe_event = self.repository.recent_human_events(TARGET_DEVICE, since)
            reasons = validation_block_reasons(
                action_sec=action_sec,
                sensor_fresh=not bool(getattr(reading, "sensor_stale", False) or getattr(reading, "sensor_stale_hard", False)),
                pending_soak=bool(state.get("pending_soak")),
                safety_flags=self._safety_flags(state),
                recent_manual_event=manual_event,
                recent_probe_event=probe_event,
            )
            if reasons:
                return None
            return self.repository.claim_for_phase3(
                action_sec=action_sec,
                plan_label=plan_label,
                humidity_before=float(reading.humidity),
            )
        except (ExperienceStorageError, OSError, ValueError):
            # Experience storage is optional metadata.  Phase3 must retain its
            # original safe behavior if it is unavailable or malformed.
            return None

    def record_execution(self, claim: Optional[Dict[str, Any]], reading: Any, *, action_sec: float, plan_label: str) -> None:
        if not claim:
            return
        try:
            self.repository.record_execution(
                str(claim["trial_id"]),
                action_sec=action_sec,
                humidity_before=float(reading.humidity),
                plan_label=plan_label,
            )
        except (ExperienceStorageError, OSError, ValueError):
            # The pump action was selected by Phase3 independent of this label;
            # logging failure must not turn it into a second control path.
            return

    def release_failed_execution(self, claim: Optional[Dict[str, Any]]) -> None:
        if not claim:
            return
        try:
            self.repository.release_claim(str(claim["trial_id"]))
        except (ExperienceStorageError, OSError, ValueError):
            return
