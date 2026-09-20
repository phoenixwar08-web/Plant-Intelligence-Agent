"""Episode V1: a structured record of one complete plant-care experience.

An episode links one embedded read-only ``state.v1`` snapshot to the
caller-supplied strategy, gate result, executed actions, feedback, and outcome
for a single care cycle (Issue #17). The store records only facts handed to it:
a section nobody supplied stays ``None``/empty and is listed in
``missing_facts`` at close instead of being invented. The model's hidden
thought process is never recorded — only structured outputs such as
``reason_summary``, ``confidence``, and the gate decision, and payloads that
carry reasoning-shaped keys are rejected rather than trimmed.

Boundaries: depends only on the frozen ``state.v1`` data contract. It creates
no strategies, grants no execution, publishes no MQTT, controls no pump, and
never imports or modifies Phase3 or historical data.
"""
from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.soil3.telemetry.common import atomic_write_json, load_json

# Reused so an episode's binding hash and timestamp notation are by construction
# the same ones strategy.v1 binds with: a later consumer can cross-check
# strategy.state_sha256 against the episode's embedded snapshot without a
# second, divergent hashing or normalization code path.
from services.soil3.cloud_strategy.validator import (
    fingerprint,
    normalize_timestamp,
    parse_timestamp,
)


SCHEMA_VERSION = "episode.v1"
STATE_SCHEMA_VERSION = "state.v1"
EXPECTED_DEVICE_CODE = "soil3"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

EPISODE_ID_PATTERN = re.compile(r"^ep-[0-9a-f]{24}$")

# Decision sections: stored at most once per episode so a recorded decision
# cannot be silently rewritten while the episode is open. Re-setting the
# identical value is a no-op; a different value is refused.
SET_ONCE_SECTIONS = ("strategy", "gate_result", "outcome")
# Fact sections: append-only lists of caller-supplied observations. Appended
# entries are facts and are never edited or removed by the store.
APPEND_SECTIONS = ("executed_actions", "feedback")
# Sections whose absence is stated explicitly in missing_facts at close.
TRACKED_SECTIONS = ("strategy", "gate_result", "executed_actions", "feedback", "outcome")

# episode.v1 records structured results, never the model's hidden thought
# process. A payload carrying one of these keys is refused outright so an
# upstream bug that forwards reasoning text fails loudly instead of leaking
# into the experience record.
FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "chain_of_thought",
    "hidden_reasoning",
    "raw_reasoning",
    "reasoning_trace",
    "thinking",
})

# gate.v1 is implemented, but Issue #17 deliberately does not make Episode a
# Gate consumer. The store accepts the caller's structured result as-is; when a
# decision is present it must use the three-outcome vocabulary. A field the
# caller did not supply is never invented.
GATE_DECISIONS = ("allow", "allow_with_warning", "deny")

# Caller-supplied fact timestamps normalized into the project's one RFC 3339
# UTC notation when parseable. An unparseable value is left exactly as the
# producer wrote it, so the store reports reality instead of inventing a time.
NORMALIZED_SECTION_TIMESTAMPS = {
    "executed_actions": ("executed_at",),
    "feedback": ("observed_at",),
}


class EpisodeError(Exception):
    """An episode operation was refused: a machine ``code`` plus ``reasons``."""

    def __init__(self, code: str, reasons: Optional[List[str]] = None):
        self.code = code
        self.reasons = list(reasons or [])
        detail = f": {', '.join(self.reasons)}" if self.reasons else ""
        super().__init__(f"{code}{detail}")


def utc_now() -> str:
    """The one timestamp notation the store writes, matching state.v1's generated_at."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_episode_id() -> str:
    return "ep-" + uuid.uuid4().hex[:24]


def _forbidden_keys(value: Any) -> List[str]:
    """Find reasoning-shaped keys at every dict/list level of one payload."""
    found = set()

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                if isinstance(key, str) and key.lower() in FORBIDDEN_PAYLOAD_KEYS:
                    found.add(key)
                visit(child)
        elif isinstance(current, list):
            for child in current:
                visit(child)

    visit(value)
    return sorted(found)


def _is_confidence(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and 0.0 <= float(value) <= 1.0
    )


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and item.strip() for item in value
    )


def validate_initial_state(value: Any) -> List[str]:
    """Structural reasons why ``value`` cannot open an episode; empty means usable."""
    if not isinstance(value, dict):
        return ["initial_state_not_object"]
    reasons = [
        f"initial_state_forbidden_key:{key}" for key in _forbidden_keys(value)
    ]
    if value.get("schema_version") != STATE_SCHEMA_VERSION:
        reasons.append("initial_state_not_state_v1")
    if value.get("device_code") != EXPECTED_DEVICE_CODE:
        reasons.append("initial_state_invalid_device_code")
    return reasons


def validate_strategy(value: Any, *, state_sha256: str, initial_state: Dict[str, Any]) -> List[str]:
    """Envelope and binding checks for a caller-supplied strategy.v1 record.

    The deep strategy contract belongs to the Cloud Strategy Validator; this
    check exists so an episode cannot link a strategy that was built from a
    different snapshot, and so structured reason and confidence survive storage
    in a usable form. All checks apply only to fields the caller actually
    supplied — nothing is required that strategy.v1 does not already mandate,
    and nothing absent is fabricated.
    """
    if not isinstance(value, dict):
        return ["strategy_not_object"]
    reasons = [f"strategy_forbidden_key:{key}" for key in _forbidden_keys(value)]
    schema_version = value.get("schema_version")
    if schema_version is not None and schema_version != "strategy.v1":
        reasons.append("strategy_invalid_schema_version")
    device_code = value.get("device_code")
    if device_code is not None and device_code != EXPECTED_DEVICE_CODE:
        reasons.append("strategy_invalid_device_code")
    claimed_hash = value.get("state_sha256")
    if claimed_hash is not None and claimed_hash != state_sha256:
        reasons.append("strategy_state_sha256_mismatch")
    if "state_observed_at" in value:
        claimed = parse_timestamp(value.get("state_observed_at"))
        actual = parse_timestamp(initial_state.get("observed_at"))
        if actual is None:
            reasons.append("state_has_no_usable_observed_at")
        elif claimed != actual:
            reasons.append("strategy_state_observed_at_mismatch")
    confidence = value.get("confidence")
    if confidence is not None and not _is_confidence(confidence):
        reasons.append("strategy_invalid_confidence")
    summary = value.get("reason_summary")
    if summary is not None and not _is_string_list(summary):
        reasons.append("strategy_invalid_reason_summary")
    return reasons


def validate_gate_result(value: Any) -> List[str]:
    if not isinstance(value, dict):
        return ["gate_result_not_object"]
    reasons = [f"gate_result_forbidden_key:{key}" for key in _forbidden_keys(value)]
    decision = value.get("decision")
    if decision is not None and decision not in GATE_DECISIONS:
        reasons.append("gate_result_unknown_decision")
    reason_codes = value.get("reason_codes")
    if reason_codes is not None and not _is_string_list(reason_codes):
        reasons.append("gate_result_invalid_reason_codes")
    return reasons


def validate_outcome(value: Any) -> List[str]:
    """Outcome is the caller's factual evaluation (Issue #18 owns its contract).

    The store preserves the independent evaluations exactly as supplied and
    computes no unified reward of its own.
    """
    if not isinstance(value, dict):
        return ["outcome_not_object"]
    return [f"outcome_forbidden_key:{key}" for key in _forbidden_keys(value)]


def validate_entries(section: str, entries: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Validate one append-only fact section; returns (prepared entries, reasons)."""
    if not isinstance(entries, list):
        return [], [f"{section}_not_list"]
    reasons: List[str] = []
    prepared: List[Dict[str, Any]] = []
    timestamp_keys = NORMALIZED_SECTION_TIMESTAMPS.get(section, ())
    for index, entry in enumerate(entries):
        prefix = f"{section}[{index}]"
        if not isinstance(entry, dict):
            reasons.append(f"{prefix}:not_object")
            continue
        forbidden = _forbidden_keys(entry)
        if forbidden:
            reasons.extend(f"{prefix}:forbidden_key:{key}" for key in forbidden)
            continue
        item = dict(entry)
        for key in timestamp_keys:
            if key in item:
                canonical = normalize_timestamp(item[key])
                if canonical is not None:
                    item[key] = canonical
        prepared.append(item)
    return prepared, reasons


class EpisodeStore:
    """Create, update, read, and close episode.v1 records under one directory.

    One JSON file per episode (``ep-<24 hex>.json``), written atomically. The
    caller chooses the directory; the store never infers, creates, or edits
    production runtime/data/log paths. Single-writer access per directory is
    assumed — concurrent writers are outside the Issue #17 scope. Returned
    records are deep copies, so mutating them cannot alter stored data.
    """

    def __init__(self, root_dir: Any):
        self.root = Path(root_dir)

    # -- lifecycle operations -------------------------------------------

    def create(self, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        """Open one episode from a real state.v1 snapshot.

        The binding hash is computed here from the snapshot actually stored, so
        a caller can never choose which state the episode is linked to.
        """
        reasons = validate_initial_state(initial_state)
        if reasons:
            raise EpisodeError("validation_failed", reasons)
        snapshot = copy.deepcopy(initial_state)
        now = utc_now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "episode_id": new_episode_id(),
            "device_code": EXPECTED_DEVICE_CODE,
            "status": STATUS_OPEN,
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
            "state_binding": {
                "state_sha256": fingerprint(snapshot),
                "state_observed_at": normalize_timestamp(snapshot.get("observed_at")),
                "state_generated_at": normalize_timestamp(snapshot.get("generated_at")),
            },
            "initial_state": snapshot,
            "strategy": None,
            "gate_result": None,
            "executed_actions": [],
            "feedback": [],
            "outcome": None,
            "missing_facts": [],
        }
        self._write(record)
        return copy.deepcopy(record)

    def read(self, episode_id: str) -> Dict[str, Any]:
        """Return a stored record with its lifecycle status, unchanged."""
        return copy.deepcopy(self._load(episode_id))

    def update(
        self,
        episode_id: str,
        *,
        strategy: Optional[Dict[str, Any]] = None,
        gate_result: Optional[Dict[str, Any]] = None,
        executed_actions: Optional[List[Dict[str, Any]]] = None,
        feedback: Optional[List[Dict[str, Any]]] = None,
        outcome: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Attach caller-supplied facts to an open episode.

        ``strategy``, ``gate_result``, and ``outcome`` are set-once;
        ``executed_actions`` and ``feedback`` append. Updating a closed episode
        is refused: after close the record is immutable. An update is applied
        only when every supplied section validates, so a refused call changes
        nothing on disk.
        """
        record = self._load(episode_id)
        if record.get("status") != STATUS_OPEN:
            raise EpisodeError("episode_not_open")

        reasons: List[str] = []
        pending_sets: Dict[str, Any] = {}
        supplied = (("strategy", strategy), ("gate_result", gate_result), ("outcome", outcome))
        for section, value in supplied:
            if value is None:
                continue
            if section == "strategy":
                reasons.extend(validate_strategy(
                    value,
                    state_sha256=record["state_binding"]["state_sha256"],
                    initial_state=record["initial_state"],
                ))
            elif section == "gate_result":
                reasons.extend(validate_gate_result(value))
            else:
                reasons.extend(validate_outcome(value))
            existing = record.get(section)
            if existing is not None and existing != value:
                reasons.append(f"{section}_already_set")
            pending_sets[section] = value

        pending_appends: Dict[str, List[Dict[str, Any]]] = {}
        for section, entries in (("executed_actions", executed_actions), ("feedback", feedback)):
            if entries is None:
                continue
            prepared, entry_reasons = validate_entries(section, entries)
            reasons.extend(entry_reasons)
            pending_appends[section] = prepared

        if reasons:
            raise EpisodeError("validation_failed", reasons)
        if not pending_sets and not pending_appends:
            raise EpisodeError("nothing_to_update")

        for section, value in pending_sets.items():
            record[section] = copy.deepcopy(value)
        for section, entries in pending_appends.items():
            record[section].extend(copy.deepcopy(entries))
        record["updated_at"] = utc_now()
        self._write(record)
        return copy.deepcopy(record)

    def close(self, episode_id: str, *, outcome: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Finalize an episode: status ``closed``, ``closed_at`` set, absence stated.

        Whatever nobody supplied is listed in ``missing_facts`` instead of being
        filled in — a closed episode distinguishes "deny, nothing executed" from
        "facts never reported" only by what its caller actually recorded. Both
        successful and failed episodes are closable and must be kept.
        """
        record = self._load(episode_id)
        if record.get("status") == STATUS_CLOSED:
            raise EpisodeError("episode_already_closed")

        reasons: List[str] = []
        if outcome is not None:
            reasons.extend(validate_outcome(outcome))
            existing = record.get("outcome")
            if existing is not None and existing != outcome:
                reasons.append("outcome_already_set")
        if reasons:
            raise EpisodeError("validation_failed", reasons)

        if outcome is not None:
            record["outcome"] = copy.deepcopy(outcome)
        now = utc_now()
        record["status"] = STATUS_CLOSED
        record["closed_at"] = now
        record["updated_at"] = now
        record["missing_facts"] = [
            section
            for section in TRACKED_SECTIONS
            if record.get(section) is None
            or (isinstance(record.get(section), list) and not record[section])
        ]
        self._write(record)
        return copy.deepcopy(record)

    # -- storage ----------------------------------------------------------

    def episode_path(self, episode_id: str) -> Path:
        if not isinstance(episode_id, str) or not EPISODE_ID_PATTERN.match(episode_id):
            # Checked before any filesystem access, so a crafted id cannot
            # escape the store directory.
            raise EpisodeError("invalid_episode_id")
        return self.root / f"{episode_id}.json"

    def _write(self, record: Dict[str, Any]) -> None:
        atomic_write_json(self.episode_path(record["episode_id"]), record)

    def _load(self, episode_id: str) -> Dict[str, Any]:
        path = self.episode_path(episode_id)
        if not path.exists():
            raise EpisodeError("episode_not_found")
        try:
            record = load_json(path)
        except (OSError, ValueError) as error:  # ValueError covers JSONDecodeError
            raise EpisodeError("episode_file_corrupt", [str(error)]) from error
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise EpisodeError("episode_file_corrupt")
        return record
