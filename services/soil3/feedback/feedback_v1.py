"""Feedback V1: multi-timescale factual observations after a care action.

``feedback.v1`` records what was actually observed after an episode's action,
at four observation windows — 30min, 2-3h, 6-12h, 24h (Issue #18): soil facts,
visual facts, recovery, sustained dryness, sustained wetness, rewater need,
and data quality. Assessments stay independent; the module computes no unified
reward. Observations nobody supplied stay ``null``/``"unknown"`` and are listed
in ``missing_observations`` instead of being invented. Contradictory data is
preserved and flagged in ``contradictions`` — the store states conflicts, it
never reconciles, repairs, or deletes them.

Boundaries: records facts only. It controls no pump, publishes no MQTT, never
imports or modifies Phase3, fabricates no observation or timestamp, and does
not depend on Experience or Bridge work. It builds on the Day-3 ``episode.v1``
contract: every feedback record names the episode it belongs to. Incremental
attach keeps that Episode open; explicit finalization writes its one aggregate
Outcome and closes it, respecting episode.v1's set-once and immutable-closed
semantics.
"""
from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.soil3.telemetry.common import atomic_write_json, load_json

# Reused so feedback records and episode bindings cannot drift apart: the same
# timestamp notation, the same episode-id pattern, and the same refusal of
# reasoning-shaped payload keys as episode.v1.
from services.soil3.cloud_strategy.validator import (
    normalize_timestamp,
    parse_timestamp,
)
from services.soil3.episode.episode_v1 import (
    EPISODE_ID_PATTERN,
    FORBIDDEN_PAYLOAD_KEYS,
    EpisodeError,
    EpisodeStore,
    utc_now,
)


SCHEMA_VERSION = "feedback.v1"
EXPECTED_DEVICE_CODE = "soil3"

FEEDBACK_ID_PATTERN = re.compile(r"^fb-[0-9a-f]{24}$")

# The four observation windows, in canonical chronological order.
WINDOWS = ("30min", "2-3h", "6-12h", "24h")

# Generous plausibility ranges in minutes after ``reference_action_at``. An
# observation outside its window's range is still stored — it is a fact — but
# it is flagged, so a mistimed collection is visible instead of silently mixed
# into the wrong window. Ranges are intentionally wider than the nominal
# window so ordinary scheduler jitter is not flagged.
WINDOW_RANGES_MINUTES = {
    "30min": (15.0, 60.0),
    "2-3h": (90.0, 240.0),
    "6-12h": (300.0, 840.0),
    "24h": (1080.0, 1800.0),
}

UNKNOWN = "unknown"

# Independent assessment vocabularies. "unknown" is the value for "nobody
# supplied this"; it is never replaced by a guess.
RECOVERY_VALUES = ("good", "partial", "poor", "none", UNKNOWN)
VISUAL_RECOVERY_VALUES = ("improved", "unchanged", "worse", UNKNOWN)
DATA_QUALITY_VALUES = ("good", "degraded", "poor", UNKNOWN)

# Assessments whose value is true / false / "unknown".
BOOL_LIKE_ASSESSMENTS = ("sustained_dry", "sustained_wet", "rewater_needed")
ASSESSMENT_KEYS = (
    "recovery",
    "sustained_dry",
    "sustained_wet",
    "rewater_needed",
    "visual_recovery",
    "data_quality",
)
# Assessments that claim a soil fact and therefore need a soil observation.
SOIL_ASSESSMENT_KEYS = ("recovery", "sustained_dry", "sustained_wet", "rewater_needed")

OBSERVATION_KEYS = ("soil", "vision")

# Fields a caller may supply. Everything else on a stored record is
# store-computed (ids, timestamps, contradictions, missing_observations) and
# is refused from callers so it cannot be forged.
CALLER_FIELDS = frozenset({
    "episode_id",
    "window",
    "device_code",
    "observed_at",
    "reference_action_at",
    "observations",
    "assessments",
})

# Machine-readable contradiction codes. A flagged record is kept unchanged;
# the code only states that the supplied facts conflict.
CONTRADICTION_SUSTAINED_DRY_AND_WET = "sustained_dry_and_wet"
CONTRADICTION_NO_SOIL_OBSERVATION = "assessment_without_soil_observation"
CONTRADICTION_NO_VISION_OBSERVATION = "assessment_without_vision_observation"
CONTRADICTION_BEFORE_REFERENCE = "observed_at_before_reference"
CONTRADICTION_OUTSIDE_WINDOW = "observed_at_outside_window"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class FeedbackError(Exception):
    """A feedback operation was refused: a machine ``code`` plus ``reasons``."""

    def __init__(self, code: str, reasons: Optional[List[str]] = None):
        self.code = code
        self.reasons = list(reasons or [])
        detail = f": {', '.join(self.reasons)}" if self.reasons else ""
        super().__init__(f"{code}{detail}")


def new_feedback_id() -> str:
    return "fb-" + uuid.uuid4().hex[:24]


def _forbidden_paths(value: Any, path: str = "") -> List[str]:
    """Locations of reasoning-shaped keys anywhere inside ``value``.

    feedback.v1 stores structured facts, never the model's hidden thought
    process. The scan is recursive so a reasoning leak nested inside a soil or
    vision observation also fails loudly instead of entering the record.
    """
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() in FORBIDDEN_PAYLOAD_KEYS:
                found.append(key_path)
            found.extend(_forbidden_paths(item, key_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_forbidden_paths(item, f"{path}[{index}]"))
    return found


def _assessment_value_ok(key: str, value: Any) -> bool:
    if key in BOOL_LIKE_ASSESSMENTS:
        return isinstance(value, bool) or value == UNKNOWN
    if key == "recovery":
        return value in RECOVERY_VALUES
    if key == "visual_recovery":
        return value in VISUAL_RECOVERY_VALUES
    if key == "data_quality":
        return value in DATA_QUALITY_VALUES
    return False


def validate_feedback_payload(payload: Any) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Validate one caller payload; returns (prepared fields, reasons).

    Only fields the caller actually supplied are checked; anything absent
    stays absent (and becomes ``null``/``"unknown"`` on the stored record).
    Nothing is invented here: an unparseable ``observed_at`` is refused rather
    than replaced, because window placement and ordering depend on it, while an
    unparseable ``reference_action_at`` string is kept exactly as written and
    the timing check is simply skipped.
    """
    if not isinstance(payload, dict):
        return None, ["payload_not_object"]

    reasons: List[str] = []
    for key in sorted(payload):
        if key not in CALLER_FIELDS:
            reasons.append(f"unknown_field:{key}")
    for path in sorted(_forbidden_paths(payload)):
        reasons.append(f"forbidden_key:{path}")

    episode_id = payload.get("episode_id")
    if episode_id is None:
        reasons.append("episode_id_required")
    elif not isinstance(episode_id, str) or not EPISODE_ID_PATTERN.match(episode_id):
        reasons.append("invalid_episode_id")

    window = payload.get("window")
    if window not in WINDOWS:
        reasons.append("unknown_window")

    device_code = payload.get("device_code", EXPECTED_DEVICE_CODE)
    if device_code != EXPECTED_DEVICE_CODE:
        reasons.append("invalid_device_code")

    observed_raw = payload.get("observed_at")
    observed_at = normalize_timestamp(observed_raw) if observed_raw is not None else None
    if observed_raw is None:
        reasons.append("observed_at_required")
    elif observed_at is None:
        reasons.append("observed_at_unparseable")

    reference_raw = payload.get("reference_action_at")
    reference_at = None
    if reference_raw is not None:
        reference_at = normalize_timestamp(reference_raw)
        if reference_at is None:
            if isinstance(reference_raw, str):
                reference_at = reference_raw  # kept exactly as the producer wrote it
            else:
                reasons.append("reference_action_at_unparseable")

    observations: Dict[str, Any] = {key: None for key in OBSERVATION_KEYS}
    raw_observations = payload.get("observations")
    if raw_observations is not None:
        if not isinstance(raw_observations, dict):
            reasons.append("observations_not_object")
        else:
            for key, value in raw_observations.items():
                if key not in OBSERVATION_KEYS:
                    reasons.append(f"unknown_observation:{key}")
                    continue
                if value is None:
                    continue
                if not isinstance(value, dict):
                    reasons.append(f"observation_not_object:{key}")
                    continue
                observations[key] = copy.deepcopy(value)

    assessments: Dict[str, Any] = {key: UNKNOWN for key in ASSESSMENT_KEYS}
    raw_assessments = payload.get("assessments")
    if raw_assessments is not None:
        if not isinstance(raw_assessments, dict):
            reasons.append("assessments_not_object")
        else:
            for key, value in raw_assessments.items():
                if key not in ASSESSMENT_KEYS:
                    reasons.append(f"unknown_assessment:{key}")
                    continue
                if _assessment_value_ok(key, value):
                    assessments[key] = value
                else:
                    reasons.append(f"invalid_assessment:{key}")

    if reasons:
        return None, reasons

    return {
        "device_code": device_code,
        "episode_id": episode_id,
        "window": window,
        "observed_at": observed_at,
        "reference_action_at": reference_at,
        "observations": observations,
        "assessments": assessments,
    }, []


def compute_contradictions(
    *,
    window: str,
    observed_at: Any,
    reference_action_at: Any,
    observations: Dict[str, Any],
    assessments: Dict[str, Any],
) -> List[str]:
    """Contradiction codes implied by the supplied facts; empty means consistent.

    Checks only what the caller actually supplied. A flag never edits or drops
    the conflicting data — both facts are stored exactly as given.
    """
    codes = set()
    if assessments["sustained_dry"] is True and assessments["sustained_wet"] is True:
        codes.add(CONTRADICTION_SUSTAINED_DRY_AND_WET)
    if observations["soil"] is None and any(
        assessments[key] != UNKNOWN for key in SOIL_ASSESSMENT_KEYS
    ):
        codes.add(CONTRADICTION_NO_SOIL_OBSERVATION)
    if observations["vision"] is None and assessments["visual_recovery"] != UNKNOWN:
        codes.add(CONTRADICTION_NO_VISION_OBSERVATION)

    observed = parse_timestamp(observed_at)
    reference = parse_timestamp(reference_action_at)
    if observed is not None and reference is not None:
        offset_minutes = (observed - reference).total_seconds() / 60.0
        if offset_minutes < 0:
            codes.add(CONTRADICTION_BEFORE_REFERENCE)
        else:
            low, high = WINDOW_RANGES_MINUTES[window]
            if not low <= offset_minutes <= high:
                codes.add(CONTRADICTION_OUTSIDE_WINDOW)
    return sorted(codes)


class FeedbackStore:
    """Record, read, list, and aggregate feedback.v1 records under one directory.

    One JSON file per record (``fb-<24 hex>.json``), written atomically. The
    caller chooses the directory; the store never infers, creates, or edits
    production runtime/data/log paths. Single-writer access per directory is
    assumed. Returned records are deep copies, so mutating them cannot alter
    stored data. Records are facts: the store appends and reads them, and
    never edits or deletes one.
    """

    def __init__(self, root_dir: Any):
        self.root = Path(root_dir)

    # -- operations ---------------------------------------------------------

    def record(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Store one window observation for an episode and return the record.

        The store computes ``feedback_id``, ``created_at``,
        ``contradictions``, and ``missing_observations`` itself; a caller
        cannot supply them. Validation runs before any write, so a refused
        payload changes nothing on disk.
        """
        fields, reasons = validate_feedback_payload(payload)
        if reasons:
            raise FeedbackError("validation_failed", reasons)
        assert fields is not None  # reasons empty implies fields prepared
        record = {
            "schema_version": SCHEMA_VERSION,
            "feedback_id": new_feedback_id(),
            "device_code": fields["device_code"],
            "episode_id": fields["episode_id"],
            "window": fields["window"],
            "observed_at": fields["observed_at"],
            "reference_action_at": fields["reference_action_at"],
            "created_at": utc_now(),
            "observations": fields["observations"],
            "assessments": fields["assessments"],
            "contradictions": compute_contradictions(
                window=fields["window"],
                observed_at=fields["observed_at"],
                reference_action_at=fields["reference_action_at"],
                observations=fields["observations"],
                assessments=fields["assessments"],
            ),
            "missing_observations": [
                key for key in OBSERVATION_KEYS if fields["observations"][key] is None
            ],
        }
        self._write(record)
        return copy.deepcopy(record)

    def read(self, feedback_id: str) -> Dict[str, Any]:
        """Return one stored record, unchanged."""
        return copy.deepcopy(self._load(feedback_id))

    def list_for_episode(self, episode_id: str) -> List[Dict[str, Any]]:
        """All records for one episode, oldest observation first.

        The episode id is pattern-checked before any filesystem access. A
        corrupt record file fails loudly instead of being skipped, so the
        listing can never silently lose a fact.
        """
        self._check_episode_id(episode_id)
        if not self.root.exists():
            return []
        records: List[Dict[str, Any]] = []
        for path in sorted(self.root.glob("fb-*.json")):
            if not FEEDBACK_ID_PATTERN.match(path.stem):
                continue  # not a feedback.v1 record file
            record = self._load(path.stem)
            if record.get("episode_id") == episode_id:
                records.append(record)
        records.sort(key=self._chronological_key)
        return [copy.deepcopy(record) for record in records]

    def outcome(self, episode_id: str) -> Dict[str, Any]:
        """Aggregate the episode's records into one outcome payload.

        Per assessment key, the value from the latest window that reported a
        known value wins; a key nobody ever reported stays ``"unknown"`` with a
        ``null`` source. Windows with no record are listed in
        ``windows_missing``. Contradiction codes from every record of the
        episode — including records superseded by a later one in the same
        window — are unioned into the outcome, so a conflict is not hidden by
        aggregation. The independent evaluations are preserved as-is; no
        unified reward is computed. The payload is returned, not written; it is
        shaped for episode.v1's set-once ``outcome`` section.
        """
        self._check_episode_id(episode_id)
        records = self.list_for_episode(episode_id)
        latest_by_window: Dict[str, Dict[str, Any]] = {}
        for record in records:  # chronological, so the last one per window wins
            latest_by_window[record["window"]] = record

        included = [window for window in WINDOWS if window in latest_by_window]
        missing = [window for window in WINDOWS if window not in latest_by_window]

        assessments: Dict[str, Any] = {}
        sources: Dict[str, Optional[str]] = {}
        for key in ASSESSMENT_KEYS:
            value: Any = UNKNOWN
            source: Optional[str] = None
            for window in reversed(WINDOWS):
                record = latest_by_window.get(window)
                if record is not None and record["assessments"][key] != UNKNOWN:
                    value, source = record["assessments"][key], window
                    break
            assessments[key] = value
            sources[key] = source

        return {
            "source_schema": SCHEMA_VERSION,
            "episode_id": episode_id,
            "evaluated_at": utc_now(),
            "record_count": len(records),
            "windows_included": included,
            "windows_missing": missing,
            "feedback_ids": [latest_by_window[window]["feedback_id"] for window in included],
            "assessments": assessments,
            "assessment_sources": sources,
            "contradictions": sorted({code for record in records for code in record["contradictions"]}),
        }

    def attach_to_episode(
        self,
        episode_id: str,
        episode_store: Any,
        *,
        finalize: bool = False,
    ) -> Dict[str, Any]:
        """Append feedback records and optionally finalize the open episode.

        ``episode_store`` is an ``EpisodeStore`` or a directory path for one.
        Idempotent: records already present on the episode (by ``feedback_id``)
        are not appended twice, and an outcome the episode already holds is
        never rewritten — episode.v1's outcome is set-once, and this module
        does not change that. Outcome is written only when ``finalize=True``;
        the same operation then closes the Episode through ``EpisodeStore``.
        Attaching to a closed or unknown episode is refused. Returns a summary
        of what the episode now holds.
        """
        if not isinstance(finalize, bool):
            raise FeedbackError("invalid_finalize")
        self._check_episode_id(episode_id)
        records = self.list_for_episode(episode_id)
        if not records:
            raise FeedbackError("no_feedback_for_episode")
        store = episode_store if isinstance(episode_store, EpisodeStore) else EpisodeStore(episode_store)
        episode = store.read(episode_id)  # EpisodeError propagates: episode_not_found
        if episode.get("status") != "open":
            raise FeedbackError("episode_not_open")

        existing_ids = {
            entry.get("feedback_id")
            for entry in episode.get("feedback", [])
            if isinstance(entry, dict)
        }
        new_records = [record for record in records if record["feedback_id"] not in existing_ids]
        already_present = [
            record["feedback_id"] for record in records if record["feedback_id"] in existing_ids
        ]

        outcome_was_present = episode.get("outcome") is not None
        update_kwargs: Dict[str, Any] = {}
        if new_records:
            update_kwargs["feedback"] = new_records
        if finalize and not outcome_was_present:
            update_kwargs["outcome"] = self.outcome(episode_id)
        if update_kwargs:
            store.update(episode_id, **update_kwargs)
        if finalize:
            store.close(episode_id)

        return {
            "episode_id": episode_id,
            "attached_feedback_ids": [record["feedback_id"] for record in new_records],
            "already_present_feedback_ids": already_present,
            "outcome_set": bool(update_kwargs.get("outcome") is not None) and not outcome_was_present,
            "outcome_already_present": outcome_was_present,
            "finalized": finalize,
            "episode_status": "closed" if finalize else "open",
        }

    # -- storage -------------------------------------------------------------

    def feedback_path(self, feedback_id: str) -> Path:
        if not isinstance(feedback_id, str) or not FEEDBACK_ID_PATTERN.match(feedback_id):
            # Checked before any filesystem access, so a crafted id cannot
            # escape the store directory.
            raise FeedbackError("invalid_feedback_id")
        return self.root / f"{feedback_id}.json"

    def _check_episode_id(self, episode_id: str) -> None:
        if not isinstance(episode_id, str) or not EPISODE_ID_PATTERN.match(episode_id):
            raise FeedbackError("invalid_episode_id")

    @staticmethod
    def _chronological_key(record: Dict[str, Any]) -> Tuple[datetime, str, str]:
        observed = parse_timestamp(record.get("observed_at")) or _EPOCH
        return (observed, str(record.get("created_at", "")), str(record.get("feedback_id", "")))

    def _write(self, record: Dict[str, Any]) -> None:
        atomic_write_json(self.feedback_path(record["feedback_id"]), record)

    def _load(self, feedback_id: str) -> Dict[str, Any]:
        path = self.feedback_path(feedback_id)
        if not path.exists():
            raise FeedbackError("feedback_not_found")
        try:
            record = load_json(path)
        except (OSError, ValueError) as error:  # ValueError covers JSONDecodeError
            raise FeedbackError("feedback_file_corrupt", [str(error)]) from error
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise FeedbackError("feedback_file_corrupt")
        return record
