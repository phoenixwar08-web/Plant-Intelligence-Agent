"""Receipt-only scheduling boundary for soil3 factual feedback collection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict

from services.soil3.episode.episode_v1 import EpisodeStore
from services.soil3.feedback_collector.action_receipt_v1 import ActionReceiptStore
from services.soil3.telemetry.common import atomic_write_json, load_json


TRACKING_SCHEMA = "feedback_collection_tracking.v1"
COLLECTABLE_EVIDENCE = {"command_completed", "manual_confirmed", "device_confirmed"}
WINDOWS = ("30min", "2-3h", "6-12h", "24h")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CollectorConfig:
    receipt_dir: Path
    tracking_dir: Path
    episode_dir: Path
    feedback_dir: Path

    def __post_init__(self) -> None:
        for field in ("receipt_dir", "tracking_dir", "episode_dir", "feedback_dir"):
            object.__setattr__(self, field, Path(getattr(self, field)))


class FeedbackCollector:
    """Bind only explicit durable action receipts to Episodes exactly once."""

    def __init__(self, config: CollectorConfig, *, clock: Callable[[], datetime] = utc_now):
        self.config = config
        self.clock = clock
        self.receipts = ActionReceiptStore(config.receipt_dir)
        self.episodes = EpisodeStore(config.episode_dir)

    def run_once(self) -> Dict[str, Any]:
        tracked = 0
        for receipt in self.receipts.iter_receipts():
            evidence = receipt["execution_evidence"].get("level")
            if evidence not in COLLECTABLE_EVIDENCE:
                continue
            tracking = self._load_or_create_tracking(receipt)
            self._ensure_episode_action(receipt, tracking)
            tracked += 1
        return {"schema_version": "feedback_collector_run.v1", "tracked_actions": tracked}

    def tracking_path(self, action_id: str) -> Path:
        return self.config.tracking_dir / f"{action_id}.json"

    def _load_or_create_tracking(self, receipt: Dict[str, Any]) -> Dict[str, Any]:
        action_id = receipt["action_id"]
        path = self.tracking_path(action_id)
        if path.exists():
            value = load_json(path)
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != TRACKING_SCHEMA
                or value.get("action_id") != action_id
            ):
                raise ValueError("feedback collector tracking is invalid")
            return value

        binding = receipt["episode_binding"]
        if binding["mode"] == "existing":
            episode_id = binding["episode_id"]
            self.episodes.read(episode_id)
        else:
            episode_id = self.episodes.create(receipt["initial_state"])["episode_id"]
        value = {
            "schema_version": TRACKING_SCHEMA,
            "action_id": action_id,
            "episode_id": episode_id,
            "windows": {window: "pending" for window in WINDOWS},
            "finalized": False,
        }
        atomic_write_json(path, value)
        return value

    def _ensure_episode_action(self, receipt: Dict[str, Any], tracking: Dict[str, Any]) -> None:
        episode = self.episodes.read(tracking["episode_id"])
        existing = {
            item.get("action_id")
            for item in episode.get("executed_actions", [])
            if isinstance(item, dict)
        }
        if receipt["action_id"] in existing:
            return
        action = {
            "kind": "water",
            "action_id": receipt["action_id"],
            "action_source": receipt["action_source"],
            "executed_at": receipt["reference_action_at"],
            "pump_seconds": receipt["command"]["pump_seconds"],
            "execution_evidence": receipt["execution_evidence"],
            "receipt_ref": self.receipts.artifact_ref(receipt["action_id"]),
        }
        self.episodes.update(tracking["episode_id"], executed_actions=[action])
