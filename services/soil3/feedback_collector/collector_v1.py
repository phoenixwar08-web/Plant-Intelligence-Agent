"""Receipt-only scheduling boundary for soil3 factual feedback collection."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from services.soil3.cloud_strategy.validator import parse_timestamp
from services.soil3.episode.episode_v1 import EpisodeStore
from services.soil3.feedback_collector.action_receipt_v1 import ActionReceiptStore
from services.soil3.feedback.feedback_v1 import FeedbackStore, WINDOW_RANGES_MINUTES
from services.soil3.state.state_v1 import StateBuilder
from services.soil3.telemetry.events import build_health_snapshot
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
    phase3_state_path: Optional[Path] = None
    sensor_log_path: Optional[Path] = None
    irrigation_trials_path: Optional[Path] = None
    service_unit: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    vision_enabled: bool = False
    lock_stale_seconds: float = 1800.0

    def __post_init__(self) -> None:
        for name in (
            "receipt_dir", "tracking_dir", "episode_dir", "feedback_dir",
            "phase3_state_path", "sensor_log_path", "irrigation_trials_path",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value))
        if not isinstance(self.parameters, dict):
            raise ValueError("collector parameters must be an object")
        if not isinstance(self.vision_enabled, bool):
            raise ValueError("collector vision_enabled must be boolean")
        if (
            isinstance(self.lock_stale_seconds, bool)
            or not isinstance(self.lock_stale_seconds, (int, float))
            or not math.isfinite(float(self.lock_stale_seconds))
            or float(self.lock_stale_seconds) <= 0
        ):
            raise ValueError("collector lock_stale_seconds must be positive")

    @classmethod
    def from_dict(cls, value: Any) -> "CollectorConfig":
        if not isinstance(value, dict):
            raise ValueError("collector config must be an object")
        allowed = {
            "receipt_dir", "tracking_dir", "episode_dir", "feedback_dir",
            "phase3_state_path", "sensor_log_path", "irrigation_trials_path",
            "service_unit", "parameters", "vision_enabled", "lock_stale_seconds",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("collector config has unknown fields")
        required = {
            "receipt_dir", "tracking_dir", "episode_dir", "feedback_dir", "phase3_state_path",
        }
        if any(not isinstance(value.get(name), str) or not value[name] for name in required):
            raise ValueError("collector config has missing required paths")
        return cls(
            receipt_dir=Path(value["receipt_dir"]),
            tracking_dir=Path(value["tracking_dir"]),
            episode_dir=Path(value["episode_dir"]),
            feedback_dir=Path(value["feedback_dir"]),
            phase3_state_path=Path(value["phase3_state_path"]),
            sensor_log_path=(Path(value["sensor_log_path"]) if value.get("sensor_log_path") else None),
            irrigation_trials_path=(
                Path(value["irrigation_trials_path"])
                if value.get("irrigation_trials_path") else None
            ),
            service_unit=value.get("service_unit"),
            parameters=value.get("parameters", {}),
            vision_enabled=value.get("vision_enabled", False),
            lock_stale_seconds=value.get("lock_stale_seconds", 1800.0),
        )


class _CollectorRunLock:
    """Crash-recoverable, cross-platform single-instance lock for one collector root."""

    def __init__(self, path: Path, stale_seconds: float):
        self.path = path
        self.stale_seconds = float(stale_seconds)
        self.acquired = False
        self.token = uuid.uuid4().hex
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

    @property
    def owner_path(self) -> Path:
        return self.path / "owner.json"

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(self.path)
            return self._record_owner()
        except FileExistsError:
            owner = self._read_owner()
            if owner is not None and self._owner_pid_is_alive(owner.get("pid")):
                return False
            try:
                age = max(0.0, time.time() - self.path.stat().st_mtime)
            except OSError:
                return False
            if age <= self.stale_seconds:
                return False
            try:
                if self.owner_path.exists():
                    self.owner_path.unlink()
                os.rmdir(self.path)
            except OSError:
                return False
            try:
                os.mkdir(self.path)
            except OSError:
                return False
            return self._record_owner()

    def _record_owner(self) -> bool:
        payload = {
            "pid": os.getpid(),
            "token": self.token,
            "started_at": time.time(),
        }
        try:
            with self.owner_path.open("x", encoding="utf-8") as output:
                json.dump(payload, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            try:
                os.rmdir(self.path)
            except OSError:
                pass
            return False
        self.acquired = True
        return True

    def _read_owner(self) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _owner_pid_is_alive(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return False
        try:
            os.kill(value, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def start_heartbeat(self) -> None:
        if not self.acquired:
            return
        interval = min(30.0, max(1.0, self.stale_seconds / 3.0))

        def heartbeat() -> None:
            while not self._heartbeat_stop.wait(interval):
                owner = self._read_owner()
                if owner is None or owner.get("token") != self.token:
                    return
                try:
                    os.utime(self.path, None)
                except OSError:
                    return

        self._heartbeat_thread = threading.Thread(
            target=heartbeat,
            name="soil3-feedback-collector-lock",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def release(self) -> None:
        if self.acquired:
            self._heartbeat_stop.set()
            if self._heartbeat_thread is not None:
                self._heartbeat_thread.join(timeout=1.0)
            try:
                owner = self._read_owner()
                if owner is not None and owner.get("token") == self.token:
                    self.owner_path.unlink()
                    os.rmdir(self.path)
            finally:
                self.acquired = False


def capture_current_state(config: CollectorConfig) -> Dict[str, Any]:
    """Build a read-only State V1 from canonical telemetry health facts only."""
    if config.phase3_state_path is None:
        raise ValueError("collector phase3_state_path is required for state capture")
    snapshot = build_health_snapshot(
        "soil3",
        str(config.phase3_state_path),
        sensor_log_path=(str(config.sensor_log_path) if config.sensor_log_path else None),
        irrigation_trials_path=(
            str(config.irrigation_trials_path)
            if config.irrigation_trials_path else None
        ),
        service_unit=config.service_unit,
    )
    return StateBuilder("soil3").build_from_health_snapshot(
        snapshot,
        parameters=config.parameters,
    )


def capture_current_vision(config: CollectorConfig) -> Optional[Dict[str, Any]]:
    """Return validated public Vision facts, or ``None`` when Vision is unavailable."""
    if not config.vision_enabled:
        return None
    # Keep the optional-dependency failure boundary valid even when importing
    # the Vision package itself fails before it can export this public error.
    VisionConfigurationError = ValueError
    try:
        from services.soil3.vision import (
            VisionConfigurationError,
            capture_and_analyze_once,
            validate_vision_record,
            validate_vision_run_manifest,
        )

        result = capture_and_analyze_once()
        reference = result.manifest_ref
        if (
            reference is None
            or reference.schema_version != "vision_run.v1"
            or not isinstance(reference.path, str)
            or not isinstance(reference.sha256, str)
            or not isinstance(reference.record_id, str)
        ):
            return None
        manifest_bytes = Path(reference.path).read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != reference.sha256:
            return None
        manifest = validate_vision_run_manifest(json.loads(manifest_bytes))
        if (
            manifest["device_code"] != "soil3"
            or manifest["run_id"] != reference.record_id
            or manifest["frame_id"] != result.frame_id
            or manifest["status"] != result.status
        ):
            return None
        outcomes = {outcome.zone_id: outcome for outcome in result.outcomes}
        if len(outcomes) != len(result.outcomes):
            return None
        facts = []
        for declared in manifest["outcomes"]:
            outcome = outcomes.get(declared["zone_id"])
            artifact_ref = declared["artifact_ref"]
            if outcome is None or outcome.status != declared["status"]:
                return None
            if artifact_ref is None:
                if outcome.vision is not None or outcome.artifact_ref is not None:
                    return None
                continue
            public_ref = outcome.artifact_ref
            if public_ref is None or {
                "schema_version": public_ref.schema_version,
                "record_id": public_ref.record_id,
                "path": public_ref.path,
                "sha256": public_ref.sha256,
            } != artifact_ref:
                return None
            observation_bytes = Path(artifact_ref["path"]).read_bytes()
            if hashlib.sha256(observation_bytes).hexdigest() != artifact_ref["sha256"]:
                return None
            fact = validate_vision_record(json.loads(observation_bytes))
            if (
                fact != outcome.vision
                or fact["device_code"] != manifest["device_code"]
                or fact["image_id"] != artifact_ref["record_id"]
                or fact["source_frame_id"] != manifest["frame_id"]
                or fact["plant_zone"]["id"] != declared["zone_id"]
            ):
                return None
            facts.append(fact)
        if not facts:
            return None
        return {
            "manifest_ref": {
                "schema_version": reference.schema_version,
                "record_id": reference.record_id,
                "path": reference.path,
                "sha256": reference.sha256,
            },
            "facts": facts,
        }
    except (ImportError, OSError, ValueError, TypeError, AttributeError, VisionConfigurationError):
        return None


class FeedbackCollector:
    """Bind only explicit durable action receipts to Episodes exactly once."""

    def __init__(
        self,
        config: CollectorConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
        state_supplier: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.config = config
        self.clock = clock
        self.receipts = ActionReceiptStore(config.receipt_dir)
        self.episodes = EpisodeStore(config.episode_dir)
        self.feedback = FeedbackStore(config.feedback_dir)
        self.state_supplier = state_supplier or (lambda: capture_current_state(config))

    def run_once(self) -> Dict[str, Any]:
        lock = _CollectorRunLock(
            self.config.tracking_dir / ".feedback_collector.lock",
            self.config.lock_stale_seconds,
        )
        if not lock.acquire():
            return {
                "schema_version": "feedback_collector_run.v1",
                "tracked_actions": 0,
                "skipped_due_to_lock": True,
            }
        lock.start_heartbeat()
        tracked = 0
        try:
            for receipt in self.receipts.iter_receipts():
                evidence = receipt["execution_evidence"].get("level")
                if evidence not in COLLECTABLE_EVIDENCE:
                    continue
                tracking = self._load_or_create_tracking(receipt)
                self._ensure_episode_action(receipt, tracking)
                self._collect_due_windows(receipt, tracking)
                tracked += 1
            return {
                "schema_version": "feedback_collector_run.v1",
                "tracked_actions": tracked,
                "skipped_due_to_lock": False,
            }
        finally:
            lock.release()

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
            episode_id = "ep-" + receipt["action_id"][4:]
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
        if (
            receipt["episode_binding"]["mode"] == "create_independent"
            and not self.episodes.episode_path(tracking["episode_id"]).exists()
        ):
            self.episodes.create(
                receipt["initial_state"],
                episode_id=tracking["episode_id"],
            )
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

    def _collect_due_windows(self, receipt: Dict[str, Any], tracking: Dict[str, Any]) -> None:
        if tracking.get("finalized") is True:
            return
        reference = parse_timestamp(receipt["reference_action_at"])
        now = self.clock()
        if reference is None or now.tzinfo is None:
            raise ValueError("collector clock or reference action time is invalid")
        offset_minutes = (now.astimezone(timezone.utc) - reference).total_seconds() / 60.0
        changed = False
        for window in WINDOWS:
            if tracking["windows"].get(window) != "pending":
                continue
            lower, upper = WINDOW_RANGES_MINUTES[window]
            if offset_minutes > upper:
                tracking["windows"][window] = "missed"
                changed = True
                continue
            if offset_minutes < lower:
                continue
            existing = self._existing_window_feedback(
                tracking["episode_id"],
                window,
                receipt["reference_action_at"],
            )
            if existing is not None:
                self.feedback.attach_to_episode(
                    tracking["episode_id"], self.episodes, finalize=False
                )
                tracking["windows"][window] = "recorded"
                tracking.setdefault("feedback_ids", {})[window] = existing["feedback_id"]
                changed = True
                break
            if self.state_supplier is None:
                raise ValueError("collector state supplier is unavailable")
            state = self.state_supplier()
            vision = capture_current_vision(self.config)
            record = self.feedback.record(
                self._feedback_payload(
                    receipt, tracking["episode_id"], window, state, vision
                )
            )
            self.feedback.attach_to_episode(tracking["episode_id"], self.episodes, finalize=False)
            tracking["windows"][window] = "recorded"
            tracking.setdefault("feedback_ids", {})[window] = record["feedback_id"]
            changed = True
            break
        if tracking["windows"].get("24h") in {"recorded", "missed"}:
            episode_id = tracking["episode_id"]
            if self.feedback.list_for_episode(episode_id):
                self.feedback.attach_to_episode(episode_id, self.episodes, finalize=True)
            else:
                # The scheduler may have been down past every observation
                # window.  Close with FeedbackStore's explicit empty factual
                # outcome rather than inventing a late observation.
                self.episodes.close(
                    episode_id,
                    outcome=self.feedback.outcome(episode_id),
                )
            tracking["finalized"] = True
            changed = True
        if changed:
            atomic_write_json(self.tracking_path(receipt["action_id"]), tracking)

    def _existing_window_feedback(
        self,
        episode_id: str,
        window: str,
        reference_action_at: Any,
    ) -> Optional[Dict[str, Any]]:
        matches = [
            record
            for record in self.feedback.list_for_episode(episode_id)
            if (
                record.get("window") == window
                and record.get("reference_action_at") == reference_action_at
            )
        ]
        if len(matches) > 1:
            raise ValueError("collector found duplicate feedback for one action window")
        return matches[0] if matches else None

    @staticmethod
    def _feedback_payload(
        receipt: Dict[str, Any],
        episode_id: str,
        window: str,
        state: Dict[str, Any],
        vision: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        soil = state.get("soil") if isinstance(state, dict) else None
        safety = state.get("safety") if isinstance(state, dict) else None
        humidity = soil.get("humidity_percent") if isinstance(soil, dict) else None
        target_low = safety.get("target_low") if isinstance(safety, dict) else None
        field_capacity = safety.get("field_capacity") if isinstance(safety, dict) else None
        dry = humidity <= target_low if _numbers(humidity, target_low) else "unknown"
        wet = humidity >= field_capacity if _numbers(humidity, field_capacity) else "unknown"
        recovery = "poor" if True in (dry, wet) else (
            "good" if dry is False and wet is False else "unknown"
        )
        return {
            "device_code": "soil3",
            "episode_id": episode_id,
            "window": window,
            "observed_at": state.get("observed_at") if isinstance(state, dict) else None,
            "reference_action_at": receipt["reference_action_at"],
            "observations": {"soil": state, "vision": vision},
            "assessments": {
                "recovery": recovery,
                "sustained_dry": dry,
                "sustained_wet": wet,
                "rewater_needed": dry,
                "visual_recovery": "unknown",
                "data_quality": "unknown",
            },
        }


def _numbers(*values: Any) -> bool:
    return all(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        for value in values
    )
