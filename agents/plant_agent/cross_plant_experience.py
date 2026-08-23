#!/usr/bin/env python3
"""Safe, fixed-pair cross-plant experience domain rules.

This module deliberately contains no pump command path.  It creates and
interprets evidence records for the fixed soil2 -> soil3 pilot only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SOURCE_DEVICE = "soil2"
TARGET_DEVICE = "soil3"
DB_NAME = "soil_data"
DB_PORT = "7654"
# This window labels a snapshot as current or historical for explanation only.
# It is deliberately not an eligibility gate for cross-plant experience.
CURRENT_REFERENCE_WINDOW_SEC = 15 * 60
CANDIDATE_TTL_MINUTES = 15
VALIDATION_TTL_HOURS = 24
PHASE3_ROOT = Path("/root/water/phase3")
PLANT_AGENT_OUTPUTS = Path("/root/agent/plant_agent/outputs")
MAIN_FACT_SENDER = Path(
    "/root/.openclaw/workspace/skills/cross-plant-dialogue-source-skill/send_fact_turn.py"
)
CONFIRMATION_RE = re.compile(
    r"^经验(采纳|仅保存|拒绝)\s+(XP-[0-9A-Z-]{8,64})$", re.IGNORECASE
)
CONVERSATION_KEY_RE = re.compile(r"^agent:qqbot4:qqbot:group:([A-Za-z0-9_-]{1,128})$")


class CandidateError(ValueError):
    """A candidate cannot be safely formed from the supplied evidence."""


class CandidateStateError(ValueError):
    """A terminal or already-claimed candidate cannot transition again."""


class ExperienceStorageError(RuntimeError):
    """The audit store is unavailable; callers must fail closed."""


def group_id_from_conversation_key(conversation_key: str) -> str:
    """Extract the platform group identifier from a trusted QQBot4 session key.

    The raw user message never supplies this value.  The ingress router obtains
    it from OpenClaw's resolved session context and passes it through the fixed
    Skill adapter.  Restricting the shape here prevents the main-bot fact sender
    from receiving an arbitrary delivery target.
    """

    match = CONVERSATION_KEY_RE.fullmatch(str(conversation_key or ""))
    if not match:
        raise CandidateError("invalid_conversation_key")
    group_id = match.group(1)
    # QQ group OpenIDs are hexadecimal and the public group-message endpoint
    # expects their canonical uppercase representation.  Preserve non-hex test
    # identifiers so this parser remains a pure session-key validator.
    return group_id.upper() if re.fullmatch(r"[0-9A-Fa-f]{16,64}", group_id) else group_id


def select_unique_pending(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve an anonymous choice only when exactly one pending row exists."""

    if not rows:
        raise CandidateStateError("candidate_not_found")
    if len(rows) != 1:
        raise CandidateStateError("candidate_ambiguous")
    return rows[0]


def send_main_fact_turn(conversation_key: str, candidate: Dict[str, Any]) -> None:
    """Ask main's narrow companion Skill to publish one fact-derived turn."""

    group_id = group_id_from_conversation_key(conversation_key)
    dialogue = candidate.get("dialogue") or build_dialogue(candidate)
    completed = subprocess.run(
        [
            "/usr/bin/python3",
            str(MAIN_FACT_SENDER),
            "--group-id",
            group_id,
            "--text",
            str(dialogue["main_turn"]),
        ],
        text=True,
        capture_output=True,
        timeout=35,
        check=False,
    )
    if completed.returncode != 0:
        raise ExperienceStorageError("dialogue_delivery_failed")


def safe_error_text(error: Exception) -> str:
    """Return a user-safe, non-executable failure explanation."""

    code = str(error)
    if code == "stale_snapshot":
        return "跨盆经验候选未生成：数据快照不完整，未进入验证。"
    if code in {"missing_sensor", "invalid_sensor"}:
        return "跨盆经验候选未生成：缺少可用的两盆传感器数据，未进入验证。"
    if code in {"source_irrigation_not_settled", "source_result_missing", "source_result_not_supported"}:
        return "跨盆经验候选未生成：soil2 尚无可用的已结算本地浇水结果，未进入验证。"
    if code == "source_result_safety_anomaly":
        return "跨盆经验候选未生成：soil2 的来源结果存在水路安全异常，未进入验证。"
    if code in {"candidate_not_found", "candidate_not_awaiting_user_choice", "candidate_ambiguous"}:
        return "该跨盆经验候选已不存在、已过期或已被处理，不会进入验证。"
    if code == "dialogue_delivery_failed":
        return "跨盆经验交流未完整送达，本次不会保留待确认候选，也不会进入验证。"
    return "跨盆经验请求未处理：服务暂不可用或数据不完整，不会进入验证。"


def _require_snapshot(snapshot: Dict[str, Any], expected_device: str) -> None:
    if snapshot.get("device_code") != expected_device:
        raise CandidateError("unexpected_device")
    humidity = (snapshot.get("sensor") or {}).get("humidity")
    if not isinstance(humidity, (int, float)):
        raise CandidateError("missing_humidity")


def _trend_similarity(source: Dict[str, Any], target: Dict[str, Any]) -> str:
    source_label = str((source.get("trend") or {}).get("label") or "unknown")
    target_label = str((target.get("trend") or {}).get("label") or "unknown")
    if source_label == target_label and source_label != "unknown":
        return "humidity_trend_direction_matches"
    return "humidity_trend_direction_differs"


def build_candidate(
    source: Dict[str, Any],
    target: Dict[str, Any],
    *,
    candidate_id: str,
) -> Dict[str, Any]:
    """Build a structured, non-executable candidate from two fact snapshots.

    The result intentionally does not contain source pump seconds, thresholds,
    gains, or any executable instruction.  A later Phase3 cycle may only label
    an action that it already selected for the target pot independently.
    """

    _require_snapshot(source, SOURCE_DEVICE)
    _require_snapshot(target, TARGET_DEVICE)
    if not candidate_id.startswith("XP-"):
        raise CandidateError("invalid_candidate_id")

    irrigation = source.get("recent_irrigation") or {}
    if not irrigation.get("observed") or not irrigation.get("settled"):
        raise CandidateError("source_irrigation_not_settled")
    source_delta = irrigation.get("humidity_delta")
    if not isinstance(source_delta, (int, float)):
        raise CandidateError("source_result_missing")
    if float(source_delta) <= 0:
        raise CandidateError("source_result_not_supported")
    if bool((source.get("safety") or {}).get("water_delivery_suspect")):
        raise CandidateError("source_result_safety_anomaly")

    source_sensor = (source.get("recent_irrigation") or {}).get("pre_sensor") or source["sensor"]
    target_sensor = target["sensor"]
    visual = {
        "source_available": bool((source.get("visual") or {}).get("available")),
        "target_available": bool((target.get("visual") or {}).get("available")),
    }
    source_current = bool(source.get("fresh"))
    target_current = bool(target.get("fresh"))
    candidate = {
        "candidate_id": candidate_id,
        "source_device": SOURCE_DEVICE,
        "target_device": TARGET_DEVICE,
        "condition_key": "soil2_to_soil3:{}:{}".format(
            str((source.get("trend") or {}).get("label") or "unknown"),
            str((target.get("trend") or {}).get("label") or "unknown"),
        ),
        "status": "awaiting_user_choice",
        "recommendation": "eligible_for_one_local_validation_only",
        "evidence_mode": (
            "current_snapshot_reference"
            if source_current and target_current
            else "historical_reference_only"
        ),
        "evidence_freshness": {
            "source_current": source_current,
            "target_current": target_current,
            "phase3_rechecks_target_before_any_action": True,
        },
        "source_conditions": {
            "humidity": float(source_sensor["humidity"]),
            "temperature": source_sensor.get("temperature"),
            "air_humidity": source_sensor.get("air_humidity"),
            "lux": source_sensor.get("lux"),
            "trend": source.get("trend") or {},
        },
        "target_conditions": {
            "humidity": float(target_sensor["humidity"]),
            "temperature": target_sensor.get("temperature"),
            "air_humidity": target_sensor.get("air_humidity"),
            "lux": target_sensor.get("lux"),
            "trend": target.get("trend") or {},
        },
        "source_action": "local_legal_irrigation_observed",
        "source_result": {
            "humidity_delta": float(source_delta),
            "settled": True,
            "safety_anomaly": bool((source.get("safety") or {}).get("water_delivery_suspect")),
        },
        "similarities": [_trend_similarity(source, target)],
        "differences": [
            "potting_medium_water_path_and_probe_position_may_differ",
            "target_uses_its_own_phase3_safety_rules",
        ],
        "visual": visual,
        "source_snapshot_at": source.get("captured_at"),
        "target_snapshot_at": target.get("captured_at"),
    }
    candidate["dialogue"] = build_dialogue(candidate)
    return candidate


def parse_confirmation(text: str) -> Optional[Tuple[str, str]]:
    """Accept only namespaced experience confirmations, never legacy A/B/C."""

    matched = CONFIRMATION_RE.fullmatch((text or "").strip())
    if not matched:
        return None
    action = {"采纳": "adopt", "仅保存": "save_only", "拒绝": "reject"}[matched.group(1)]
    return action, matched.group(2).upper()


def transition_candidate(
    candidate: Dict[str, Any],
    action: str,
    expires_at: str,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Apply an explicit user choice without creating a pump instruction."""

    if candidate.get("status") != "awaiting_user_choice":
        raise CandidateStateError("candidate_not_awaiting_user_choice")
    if action not in {"adopt", "save_only", "reject"}:
        raise CandidateStateError("invalid_choice")

    updated = dict(candidate)
    if action == "save_only":
        updated["status"] = "saved_without_validation"
        return updated, None
    if action == "reject":
        updated["status"] = "rejected_by_user"
        return updated, None

    updated["status"] = "approved_waiting_for_safe_window"
    request = {
        "validation_request_id": "VR-" + str(candidate["candidate_id"])[3:],
        "candidate_id": candidate["candidate_id"],
        "source_device": candidate["source_device"],
        "target_device": candidate["target_device"],
        "status": "approved_waiting_for_safe_window",
        "expires_at": expires_at,
        "rule": "target_phase3_must_independently_select_a_legal_local_action",
    }
    return updated, request


def classify_settlement(
    humidity_before: float,
    humidity_after: float,
    target_historical_median_delta: float,
    *,
    manual_event: bool,
    safety_anomaly: bool,
    sensor_fresh: bool,
) -> str:
    """Return an auditable outcome, never a control recommendation."""

    if manual_event or safety_anomaly or not sensor_fresh:
        return "inconclusive"
    observed_delta = float(humidity_after) - float(humidity_before)
    if observed_delta <= 0:
        return "not_supported"
    if observed_delta >= max(0.1, float(target_historical_median_delta)):
        return "supported_once"
    return "not_supported"


def confirmed_library_status(supported_count: int) -> Optional[str]:
    """One success is evidence, not a stable local rule."""

    return "confirmed_local_experience" if int(supported_count) >= 3 else None


def validation_block_reasons(
    *,
    action_sec: float,
    sensor_fresh: bool,
    pending_soak: bool,
    safety_flags: Dict[str, Any],
    recent_manual_event: bool,
    recent_probe_event: bool,
) -> List[str]:
    """List reasons a Phase3-selected action cannot be labelled as a trial."""

    reasons: List[str] = []
    if float(action_sec) <= 0:
        reasons.append("no_local_phase3_action")
    if not sensor_fresh:
        reasons.append("sensor_stale")
    if pending_soak:
        reasons.append("pending_soak")
    for key in ("water_delivery_suspect", "reservoir_empty_suspect", "sensor_fault"):
        if bool(safety_flags.get(key)):
            reasons.append(key)
    if recent_manual_event:
        reasons.append("recent_manual_event")
    if recent_probe_event:
        reasons.append("recent_probe_event")
    return reasons


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_wall_time(value: str) -> datetime:
    normalized = str(value).strip().replace("T", " ").split("+")[0]
    return datetime.strptime(normalized[:19], "%Y-%m-%d %H:%M:%S")


def sensor_timestamp_iso(value: str) -> str:
    """Represent a database sensor timestamp in the deployment's +08:00 zone."""

    return _parse_wall_time(value).replace(tzinfo=timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_compact_json(value).encode("utf-8")).hexdigest()


class ExperienceRepository:
    """Narrow OpenGauss adapter for the fixed soil2 -> soil3 pilot.

    This adapter never publishes MQTT and has no pump-control method.  Its only
    execution-facing operation is atomically claiming an already-approved
    request for Phase3 to label after Phase3 selected its own legal action.
    """

    def _gsql(self, sql: str) -> List[str]:
        command = " ".join(
            [
                "gsql",
                "-d",
                shlex.quote(DB_NAME),
                "-p",
                shlex.quote(DB_PORT),
                "-q",
                "-t",
                "-A",
                "-F",
                shlex.quote("|"),
                "-c",
                shlex.quote(sql),
            ]
        )
        result = subprocess.run(
            ["su", "-", "opengauss", "-s", "/bin/bash", "-c", command],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "gsql failed").strip()
            raise ExperienceStorageError(detail[:400])
        return [line for line in result.stdout.splitlines() if line.strip()]

    def migrate(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS cross_plant_experience_candidates (
                candidate_id VARCHAR(64) PRIMARY KEY,
                source_device VARCHAR(32) NOT NULL,
                target_device VARCHAR(32) NOT NULL,
                status VARCHAR(64) NOT NULL,
                recommendation VARCHAR(96) NOT NULL,
                condition_key VARCHAR(128) NOT NULL,
                source_snapshot_json TEXT NOT NULL,
                target_snapshot_json TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                source_fingerprint CHAR(64) NOT NULL,
                target_fingerprint CHAR(64) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                choice_at TIMESTAMP,
                actor_id VARCHAR(128),
                actor_name VARCHAR(128),
                CONSTRAINT cross_plant_candidate_pair CHECK (source_device = 'soil2' AND target_device = 'soil3')
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS cross_plant_validation_trials (
                trial_id VARCHAR(64) PRIMARY KEY,
                candidate_id VARCHAR(64) NOT NULL UNIQUE,
                source_device VARCHAR(32) NOT NULL,
                target_device VARCHAR(32) NOT NULL,
                condition_key VARCHAR(128) NOT NULL,
                status VARCHAR(64) NOT NULL,
                approved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                claimed_at TIMESTAMP,
                executed_at TIMESTAMP,
                settled_at TIMESTAMP,
                phase3_plan_label VARCHAR(128),
                actual_water_sec NUMERIC,
                humidity_before NUMERIC,
                humidity_after NUMERIC,
                historical_median_delta NUMERIC,
                outcome VARCHAR(32),
                settlement_json TEXT,
                CONSTRAINT cross_plant_trial_pair CHECK (source_device = 'soil2' AND target_device = 'soil3'),
                CONSTRAINT cross_plant_trial_candidate_fk FOREIGN KEY (candidate_id)
                    REFERENCES cross_plant_experience_candidates(candidate_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS plant_experience_library (
                experience_id VARCHAR(64) PRIMARY KEY,
                target_device VARCHAR(32) NOT NULL,
                condition_key VARCHAR(128) NOT NULL,
                status VARCHAR(64) NOT NULL,
                supported_count INTEGER NOT NULL DEFAULT 0,
                latest_trial_id VARCHAR(64),
                evidence_json TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT plant_experience_target CHECK (target_device = 'soil3'),
                CONSTRAINT plant_experience_condition_unique UNIQUE (target_device, condition_key)
            )
            """,
            "ALTER TABLE cross_plant_experience_candidates ADD COLUMN IF NOT EXISTS conversation_key VARCHAR(256)",
            "ALTER TABLE cross_plant_experience_candidates ADD COLUMN IF NOT EXISTS requester_id VARCHAR(128)",
            "CREATE INDEX IF NOT EXISTS idx_cross_plant_candidate_status ON cross_plant_experience_candidates(status, expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_cross_plant_candidate_conversation ON cross_plant_experience_candidates(conversation_key, requester_id, status, expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_cross_plant_trial_status ON cross_plant_validation_trials(status, expires_at)",
        )
        for statement in statements:
            self._gsql(statement)

    def expire_stale(self) -> None:
        self._gsql(
            "UPDATE cross_plant_experience_candidates "
            "SET status='expired' WHERE status='awaiting_user_choice' AND expires_at < CURRENT_TIMESTAMP"
        )
        self._gsql(
            "UPDATE cross_plant_validation_trials "
            "SET status='expired' WHERE status='approved_waiting_for_safe_window' AND expires_at < CURRENT_TIMESTAMP"
        )

    def create_candidate(
        self,
        candidate: Dict[str, Any],
        source: Dict[str, Any],
        target: Dict[str, Any],
        *,
        conversation_key: str,
        requester_id: str,
    ) -> None:
        group_id_from_conversation_key(conversation_key)
        if not re.fullmatch(r"[^\s\x00-\x1f]{1,128}", requester_id):
            raise CandidateError("invalid_requester")
        expires_at = datetime.now() + timedelta(minutes=CANDIDATE_TTL_MINUTES)
        self._gsql(
            "UPDATE cross_plant_experience_candidates SET status='superseded' "
            "WHERE status='awaiting_user_choice' "
            f"AND conversation_key={_sql_literal(conversation_key)} "
            f"AND requester_id={_sql_literal(requester_id)}"
        )
        sql = """
            INSERT INTO cross_plant_experience_candidates
              (candidate_id, source_device, target_device, status, recommendation,
               condition_key,
               source_snapshot_json, target_snapshot_json, candidate_json,
               source_fingerprint, target_fingerprint, conversation_key, requester_id, expires_at)
            VALUES
              ({candidate_id}, {source_device}, {target_device}, {status}, {recommendation},
               {condition_key},
               {source_json}, {target_json}, {candidate_json},
               {source_fingerprint}, {target_fingerprint}, {conversation_key}, {requester_id}, {expires_at})
        """.format(
            candidate_id=_sql_literal(candidate["candidate_id"]),
            source_device=_sql_literal(SOURCE_DEVICE),
            target_device=_sql_literal(TARGET_DEVICE),
            status=_sql_literal(candidate["status"]),
            recommendation=_sql_literal(candidate["recommendation"]),
            condition_key=_sql_literal(candidate["condition_key"]),
            source_json=_sql_literal(_compact_json(source)),
            target_json=_sql_literal(_compact_json(target)),
            candidate_json=_sql_literal(_compact_json(candidate)),
            source_fingerprint=_sql_literal(_fingerprint(source)),
            target_fingerprint=_sql_literal(_fingerprint(target)),
            conversation_key=_sql_literal(conversation_key),
            requester_id=_sql_literal(requester_id),
            expires_at=_sql_literal(expires_at.strftime("%Y-%m-%d %H:%M:%S")),
        )
        self._gsql(sql)

    def mark_delivery_failed(self, candidate_id: str) -> None:
        """Make an unseen candidate ineligible after main's fact turn fails."""

        self._gsql(
            "UPDATE cross_plant_experience_candidates SET status='delivery_failed' "
            f"WHERE candidate_id={_sql_literal(candidate_id)} AND status='awaiting_user_choice'"
        )

    def choose_active(
        self,
        *,
        conversation_key: str,
        action: str,
        actor_id: str,
        actor_name: str,
    ) -> Dict[str, Any]:
        """Resolve a plain-language choice only for its bound session and user."""

        group_id_from_conversation_key(conversation_key)
        self.expire_stale()
        rows = self._gsql(
            "SELECT candidate_id FROM cross_plant_experience_candidates "
            "WHERE status='awaiting_user_choice' "
            f"AND conversation_key={_sql_literal(conversation_key)} "
            f"AND requester_id={_sql_literal(actor_id)} "
            "ORDER BY created_at DESC LIMIT 2"
        )
        selected = select_unique_pending([{"candidate_id": row} for row in rows])
        return self.choose(str(selected["candidate_id"]), action, actor_id, actor_name)

    def _candidate_row(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        rows = self._gsql(
            "SELECT candidate_id,status,candidate_json,expires_at FROM cross_plant_experience_candidates "
            f"WHERE candidate_id={_sql_literal(candidate_id)}"
        )
        if not rows:
            return None
        fields = rows[0].split("|", 3)
        if len(fields) != 4:
            raise ExperienceStorageError("candidate row parse failed")
        return {
            "candidate_id": fields[0],
            "status": fields[1],
            "candidate": json.loads(fields[2]),
            "expires_at": fields[3],
        }

    def choose(self, candidate_id: str, action: str, actor_id: str, actor_name: str) -> Dict[str, Any]:
        self.expire_stale()
        row = self._candidate_row(candidate_id)
        if row is None:
            raise CandidateStateError("candidate_not_found")
        candidate = dict(row["candidate"])
        candidate["status"] = row["status"]
        expires_at = (datetime.now() + timedelta(hours=VALIDATION_TTL_HOURS)).astimezone().isoformat(timespec="seconds")
        updated, request = transition_candidate(candidate, action, expires_at)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if action in {"save_only", "reject"}:
            rows = self._gsql(
                "UPDATE cross_plant_experience_candidates SET "
                f"status={_sql_literal(updated['status'])},choice_at={_sql_literal(now)},"
                f"actor_id={_sql_literal(actor_id)},actor_name={_sql_literal(actor_name)} "
                f"WHERE candidate_id={_sql_literal(candidate_id)} AND status='awaiting_user_choice' "
                "RETURNING candidate_id"
            )
            if not rows:
                raise CandidateStateError("candidate_not_awaiting_user_choice")
            return {"candidate": updated, "validation_request": None}

        assert request is not None
        trial_id = request["validation_request_id"]
        sql = """
            WITH changed AS (
                UPDATE cross_plant_experience_candidates
                SET status='approved_waiting_for_safe_window', choice_at={now},
                    actor_id={actor_id}, actor_name={actor_name}
                WHERE candidate_id={candidate_id} AND status='awaiting_user_choice'
                RETURNING candidate_id
            )
            INSERT INTO cross_plant_validation_trials
              (trial_id,candidate_id,source_device,target_device,condition_key,status,expires_at)
            SELECT {trial_id}, candidate_id, 'soil2', 'soil3', {condition_key},
                   'approved_waiting_for_safe_window', {expires_at}
            FROM changed
            RETURNING trial_id
        """.format(
            now=_sql_literal(now),
            actor_id=_sql_literal(actor_id),
            actor_name=_sql_literal(actor_name),
            candidate_id=_sql_literal(candidate_id),
            trial_id=_sql_literal(trial_id),
            condition_key=_sql_literal(candidate["condition_key"]),
            expires_at=_sql_literal(expires_at.replace("T", " ")[:19]),
        )
        rows = self._gsql(sql)
        if not rows:
            raise CandidateStateError("candidate_not_awaiting_user_choice")
        return {"candidate": updated, "validation_request": request}

    def recent_human_events(self, device_code: str, since: str) -> Tuple[bool, bool]:
        external = self._gsql(
            "SELECT 1 FROM external_watering_events "
            f"WHERE device_code={_sql_literal(device_code)} AND event_time >= {_sql_literal(since)}::timestamp "
            "LIMIT 1"
        )
        probe = self._gsql(
            "SELECT 1 FROM manual_check_events "
            f"WHERE device_code={_sql_literal(device_code)} AND occurred_at >= {_sql_literal(since)}::timestamp "
            "AND (check_code ILIKE '%probe%' OR check_code ILIKE '%sensor%' OR note ILIKE '%探头%') LIMIT 1"
        )
        return bool(external), bool(probe)

    def claim_for_phase3(self, *, action_sec: float, plan_label: str, humidity_before: float) -> Optional[Dict[str, Any]]:
        """Atomically claim one waiting request after Phase3 selected a local action.

        This method does not cause an action.  It is called only after the
        caller passed every local Phase3 safety gate and obtained a positive
        action duration from the normal decision path.
        """

        self.expire_stale()
        sql = """
            WITH ready AS (
                SELECT trial_id
                FROM cross_plant_validation_trials
                WHERE target_device='soil3'
                  AND status='approved_waiting_for_safe_window'
                  AND expires_at >= CURRENT_TIMESTAMP
                ORDER BY approved_at ASC
                LIMIT 1
            )
            UPDATE cross_plant_validation_trials trial
            SET status='claimed', claimed_at=CURRENT_TIMESTAMP,
                phase3_plan_label={plan_label}, humidity_before={humidity_before}
            FROM ready
            WHERE trial.trial_id=ready.trial_id
              AND trial.status='approved_waiting_for_safe_window'
            RETURNING trial.trial_id,trial.candidate_id
        """.format(
            plan_label=_sql_literal(plan_label[:128]),
            humidity_before=float(humidity_before),
        )
        rows = self._gsql(sql)
        if not rows:
            return None
        fields = rows[0].split("|", 1)
        if len(fields) != 2:
            raise ExperienceStorageError("trial claim parse failed")
        return {"trial_id": fields[0], "candidate_id": fields[1], "action_sec": float(action_sec)}

    def release_claim(self, trial_id: str) -> None:
        self._gsql(
            "UPDATE cross_plant_validation_trials SET status='approved_waiting_for_safe_window', "
            "claimed_at=NULL,phase3_plan_label=NULL,humidity_before=NULL "
            f"WHERE trial_id={_sql_literal(trial_id)} AND status='claimed'"
        )

    def record_execution(self, trial_id: str, *, action_sec: float, humidity_before: float, plan_label: str) -> None:
        rows = self._gsql(
            "UPDATE cross_plant_validation_trials SET status='executed_waiting_settlement', "
            "executed_at=CURRENT_TIMESTAMP,actual_water_sec=" + str(float(action_sec)) + ","
            f"humidity_before={float(humidity_before)},phase3_plan_label={_sql_literal(plan_label[:128])} "
            f"WHERE trial_id={_sql_literal(trial_id)} AND status='claimed' RETURNING trial_id"
        )
        if not rows:
            raise ExperienceStorageError("trial execution update failed")

    def pending_settlement_rows(self) -> List[Dict[str, str]]:
        rows = self._gsql(
            "SELECT trial_id,candidate_id,condition_key,to_char(executed_at,'YYYY-MM-DD HH24:MI:SS'),"
            "humidity_before,actual_water_sec FROM cross_plant_validation_trials "
            "WHERE status='executed_waiting_settlement' ORDER BY executed_at ASC"
        )
        return _rows_to_dicts(rows, ("trial_id", "candidate_id", "condition_key", "executed_at", "humidity_before", "actual_water_sec"))

    def record_settlement(
        self,
        trial_id: str,
        *,
        outcome: str,
        humidity_after: float,
        historical_median_delta: float,
        settlement: Dict[str, Any],
    ) -> None:
        rows = self._gsql(
            "UPDATE cross_plant_validation_trials SET status=" + _sql_literal(outcome) + ","
            "outcome=" + _sql_literal(outcome) + ",settled_at=CURRENT_TIMESTAMP,"
            f"humidity_after={float(humidity_after)},historical_median_delta={float(historical_median_delta)},"
            f"settlement_json={_sql_literal(_compact_json(settlement))} "
            f"WHERE trial_id={_sql_literal(trial_id)} AND status='executed_waiting_settlement' RETURNING trial_id"
        )
        if not rows:
            raise ExperienceStorageError("trial settlement update failed")

    def update_library(self, *, condition_key: str, trial_id: str, settlement: Dict[str, Any]) -> Optional[str]:
        rows = self._gsql(
            "SELECT COUNT(*) FROM cross_plant_validation_trials "
            "WHERE target_device='soil3' AND condition_key=" + _sql_literal(condition_key) + " "
            "AND outcome='supported_once'"
        )
        try:
            supported_count = int(rows[0]) if rows else 0
        except ValueError:
            raise ExperienceStorageError("supported trial count parse failed")
        status = confirmed_library_status(supported_count)
        if status is None:
            return None
        existing = self._gsql(
            "SELECT experience_id FROM plant_experience_library "
            "WHERE target_device='soil3' AND condition_key=" + _sql_literal(condition_key)
        )
        evidence = {
            "supported_count": supported_count,
            "latest_trial_id": trial_id,
            "latest_settlement": settlement,
        }
        if existing:
            self._gsql(
                "UPDATE plant_experience_library SET status='confirmed_local_experience',"
                f"supported_count={supported_count},latest_trial_id={_sql_literal(trial_id)},"
                f"evidence_json={_sql_literal(_compact_json(evidence))},updated_at=CURRENT_TIMESTAMP "
                f"WHERE experience_id={_sql_literal(existing[0])}"
            )
        else:
            self._gsql(
                "INSERT INTO plant_experience_library "
                "(experience_id,target_device,condition_key,status,supported_count,latest_trial_id,evidence_json) VALUES "
                f"({_sql_literal('PE-' + uuid.uuid4().hex[:12].upper())},'soil3',{_sql_literal(condition_key)},"
                f"'confirmed_local_experience',{supported_count},{_sql_literal(trial_id)},"
                f"{_sql_literal(_compact_json(evidence))})"
            )
        return status


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _rows_to_dicts(lines: Iterable[str], names: Tuple[str, ...]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for line in lines:
        parts = line.split("|")
        if len(parts) != len(names):
            continue
        result.append(dict(zip(names, parts)))
    return result


def _phase3_safety_flags(device_code: str) -> Dict[str, bool]:
    state = _load_json(PHASE3_ROOT / device_code / "system_state.json")
    state = state if isinstance(state, dict) else {}
    return {
        "pending_soak": bool(state.get("pending_soak")),
        "water_delivery_suspect": bool((state.get("water_delivery_suspect") or {}).get("active")),
        "reservoir_empty_suspect": bool((state.get("reservoir_empty_suspect") or {}).get("active")),
        "sensor_fault": bool((state.get("sensor_fault") or {}).get("active")),
    }


def _historical_trials(device_code: str) -> List[Dict[str, Any]]:
    raw = _load_json(PHASE3_ROOT / device_code / "irrigation_trials.json")
    return raw if isinstance(raw, list) else []


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.1
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def settle_pending_trials(repository: ExperienceRepository) -> List[Dict[str, Any]]:
    """Settle completed trials only after Phase3 has cleared its soak sentinel."""

    outcomes: List[Dict[str, Any]] = []
    safety = _phase3_safety_flags(TARGET_DEVICE)
    if safety["pending_soak"]:
        return outcomes
    trial_history = _historical_trials(TARGET_DEVICE)
    for pending in repository.pending_settlement_rows():
        try:
            executed_wall = _parse_wall_time(pending["executed_at"])
            executed_epoch = executed_wall.timestamp()
            before = float(pending["humidity_before"])
        except (KeyError, TypeError, ValueError):
            continue
        matched = [
            item for item in trial_history
            if item.get("status") == "accepted"
            and isinstance(item.get("timestamp"), (int, float))
            and float(item["timestamp"]) >= executed_epoch - 1
            and isinstance(item.get("delta_m"), (int, float))
        ]
        if not matched:
            continue
        observed = matched[0]
        after = float(observed.get("humidity_after", before))
        prior_deltas = [
            float(item["delta_m"]) for item in trial_history
            if item.get("status") == "accepted"
            and isinstance(item.get("timestamp"), (int, float))
            and float(item["timestamp"]) < executed_epoch - 1
            and isinstance(item.get("delta_m"), (int, float))
            and float(item["delta_m"]) > 0
        ]
        median_delta = _median(prior_deltas)
        manual_event, probe_event = repository.recent_human_events(TARGET_DEVICE, pending["executed_at"])
        try:
            sensor_fresh = bool(build_live_snapshot(TARGET_DEVICE, repository).get("fresh"))
        except (CandidateError, ExperienceStorageError, OSError, ValueError):
            sensor_fresh = False
        outcome = classify_settlement(
            before,
            after,
            median_delta,
            manual_event=manual_event or probe_event,
            safety_anomaly=any(value for key, value in safety.items() if key != "pending_soak"),
            sensor_fresh=sensor_fresh,
        )
        target_visual = _load_json(PLANT_AGENT_OUTPUTS / "vision" / "soil3_vision.json")
        settlement = {
            "humidity_before": before,
            "humidity_after": after,
            "observed_delta": after - before,
            "historical_median_delta": median_delta,
            "phase3_trial_timestamp": observed.get("timestamp"),
            "manual_event": manual_event,
            "probe_event": probe_event,
            "safety_flags": safety,
            "visual": {"available": False, "reason": "no_fresh_soil3_visual", "observed_at": target_visual.get("observed_at")},
        }
        repository.record_settlement(
            pending["trial_id"],
            outcome=outcome,
            humidity_after=after,
            historical_median_delta=median_delta,
            settlement=settlement,
        )
        library_status = None
        if outcome == "supported_once":
            library_status = repository.update_library(
                condition_key=pending["condition_key"],
                trial_id=pending["trial_id"],
                settlement=settlement,
            )
        outcomes.append({"trial_id": pending["trial_id"], "outcome": outcome, "library_status": library_status})
    return outcomes


def build_live_snapshot(device_code: str, repository: ExperienceRepository) -> Dict[str, Any]:
    if device_code not in {SOURCE_DEVICE, TARGET_DEVICE}:
        raise CandidateError("unsupported_device")
    sensor_rows = _rows_to_dicts(
        repository._gsql(
            "SELECT to_char(recv_time,'YYYY-MM-DD HH24:MI:SS'),humidity,temp,air_humidity,lux "
            "FROM soil_sensor_readings "
            f"WHERE device_code={_sql_literal(device_code)} ORDER BY recv_time DESC LIMIT 6"
        ),
        ("recv_time", "humidity", "temperature", "air_humidity", "lux"),
    )
    if not sensor_rows:
        raise CandidateError("missing_sensor")
    latest = sensor_rows[0]
    try:
        age_sec = (datetime.now() - _parse_wall_time(latest["recv_time"])).total_seconds()
        humidity = float(latest["humidity"])
    except (KeyError, TypeError, ValueError):
        raise CandidateError("invalid_sensor")
    oldest = sensor_rows[-1]
    try:
        delta = humidity - float(oldest["humidity"])
    except (TypeError, ValueError):
        delta = 0.0
    trend = "wetting" if delta > 0.2 else "drying" if delta < -0.2 else "stable"

    state = _load_json(PHASE3_ROOT / device_code / "system_state.json")
    state = state if isinstance(state, dict) else {}
    flags = {
        "pending_soak": bool(state.get("pending_soak")),
        "water_delivery_suspect": bool((state.get("water_delivery_suspect") or {}).get("active")),
        "reservoir_empty_suspect": bool((state.get("reservoir_empty_suspect") or {}).get("active")),
        "sensor_fault": bool((state.get("sensor_fault") or {}).get("active")),
    }
    irrigation_rows = _rows_to_dicts(
        repository._gsql(
            "SELECT to_char(command_time,'YYYY-MM-DD HH24:MI:SS') "
            "FROM irrigation_events "
            f"WHERE device_code={_sql_literal(device_code)} AND status LIKE 'issued%' "
            "ORDER BY command_time DESC LIMIT 1"
        ),
        ("command_time",),
    )
    irrigation: Dict[str, Any] = {"observed": False, "settled": False, "humidity_delta": None}
    if irrigation_rows:
        event_time = irrigation_rows[0]["command_time"]
        after_rows = _rows_to_dicts(
            repository._gsql(
                "SELECT humidity FROM soil_sensor_readings "
                f"WHERE device_code={_sql_literal(device_code)} "
                f"AND recv_time >= {_sql_literal(event_time)}::timestamp + interval '10 minutes' "
                f"AND recv_time <= {_sql_literal(event_time)}::timestamp + interval '2 hours' "
                "ORDER BY recv_time ASC LIMIT 1"
            ),
            ("humidity",),
        )
        before_rows = _rows_to_dicts(
            repository._gsql(
                "SELECT humidity,temp,air_humidity,lux FROM soil_sensor_readings "
                f"WHERE device_code={_sql_literal(device_code)} "
                f"AND recv_time <= {_sql_literal(event_time)}::timestamp "
                "ORDER BY recv_time DESC LIMIT 1"
            ),
            ("humidity", "temperature", "air_humidity", "lux"),
        )
        delta_after = None
        if before_rows and after_rows:
            try:
                delta_after = float(after_rows[0]["humidity"]) - float(before_rows[0]["humidity"])
            except (TypeError, ValueError):
                delta_after = None
        irrigation = {
            "observed": delta_after is not None,
            "settled": not flags["pending_soak"],
            "humidity_delta": delta_after,
            "observed_at": event_time,
            "pre_sensor": before_rows[0] if before_rows else None,
        }

    visual_path = PLANT_AGENT_OUTPUTS / "vision" / f"{device_code}_vision.json"
    visual_doc = _load_json(visual_path)
    visual_doc = visual_doc if isinstance(visual_doc, dict) else {}
    visual = {"available": False, "reason": "no_fresh_per_pot_visual"}
    observed_at = visual_doc.get("observed_at")
    if device_code == SOURCE_DEVICE and observed_at:
        visual = {"available": True, "observed_at": observed_at}

    return {
        "device_code": device_code,
        "captured_at": sensor_timestamp_iso(latest["recv_time"]),
        "fresh": age_sec <= CURRENT_REFERENCE_WINDOW_SEC,
        "sensor": {
            "humidity": humidity,
            "temperature": float(latest["temperature"]) if latest.get("temperature") else None,
            "air_humidity": float(latest["air_humidity"]) if latest.get("air_humidity") else None,
            "lux": float(latest["lux"]) if latest.get("lux") else None,
        },
        "trend": {"label": trend, "delta": delta},
        "recent_irrigation": irrigation,
        "safety": flags,
        "visual": visual,
    }


def _reference_label(candidate: Dict[str, Any]) -> str:
    return "历史参考" if candidate.get("evidence_mode") == "historical_reference_only" else "当前快照"


def build_dialogue(candidate: Dict[str, Any]) -> Dict[str, str]:
    """Build the two bounded, fact-derived dialogue turns and the final summary.

    This is deliberately deterministic: no model is allowed to infer a trend,
    invent a result, or continue the discussion beyond this fixed exchange.
    """

    source = candidate["source_conditions"]
    target = candidate["target_conditions"]
    source_delta = candidate["source_result"]["humidity_delta"]
    reference_label = _reference_label(candidate)
    if candidate.get("evidence_mode") == "historical_reference_only":
        reference_text = (
            "本次引用的是历史快照（奶龙：{}；植境智养：{}），不是当前实时判断。"
        ).format(
            candidate.get("source_snapshot_at") or "时间未知",
            candidate.get("target_snapshot_at") or "时间未知",
        )
    else:
        reference_text = "本次引用的是两盆当前快照；验证时仍须重新检查目标盆本地状态。"

    main_turn = (
        "我这边的{}湿度是 {:.1f}%，趋势为 {}。看到奶龙这边曾在 {:.1f}%、{}时出现稳定回升；"
        "两盆的盆土和探头位置不同，我只把它作为一次对比参考。"
    ).format(
        reference_label,
        target["humidity"],
        target["trend"].get("label"),
        source["humidity"],
        source["trend"].get("label"),
    )
    naolong_turn = (
        "我这边那次本地合法补水结算后，观测湿度回升 {:.1f}%。这说明我的本地结果可供参考，"
        "但不能推导出你也该使用相同的动作或参数。"
    ).format(source_delta)
    summary = "\n".join(
        [
            "🌿 本次交流总结",
            "共同点：两盆都提供了可追溯的湿度趋势参考。",
            "差异：起始湿度、趋势以及盆土、水路、探头位置可能不同。",
            "经验候选：奶龙的一次已结算本地结果，值得让植境智养在自己未来本来就满足安全条件时，做一次受限本地验证。",
            "限制：不会立刻浇水，也不会复制来源盆的秒数、阈值或控制参数；是否执行仍完全由目标盆的 Phase3 决定。",
            reference_text,
            "是否采纳这次经验？回复：采纳本次经验 / 仅保存本次经验 / 不采纳本次经验",
        ]
    )
    return {"main_turn": main_turn, "naolong_turn": naolong_turn, "summary": summary}


def render_candidate_text(candidate: Dict[str, Any]) -> str:
    """Render only the QQBot4 turn and summary; internal IDs remain hidden."""

    dialogue = candidate.get("dialogue") or build_dialogue(candidate)
    return "\n".join(
        [
            "【奶龙】" + str(dialogue["naolong_turn"]),
            str(dialogue["summary"]),
        ]
    )


def _emit(payload: Dict[str, Any]) -> None:
    text = payload.get("text")
    if isinstance(text, str):
        payload["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    payload["schema_version"] = 1
    print(_compact_json(payload))


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed-pair cross-plant experience coordinator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-schema")
    create = subparsers.add_parser("create")
    create.add_argument("--dry-run", action="store_true")
    create.add_argument("--conversation-key")
    create.add_argument("--actor-id")
    create.add_argument("--actor-name", default="")
    choose = subparsers.add_parser("choose")
    choose.add_argument("--candidate-id", required=True)
    choose.add_argument("--choice", choices=("adopt", "save_only", "reject"), required=True)
    choose.add_argument("--actor-id", required=True)
    choose.add_argument("--actor-name", default="")
    choose_active = subparsers.add_parser("choose-active")
    choose_active.add_argument("--conversation-key", required=True)
    choose_active.add_argument("--choice", choices=("adopt", "save_only", "reject"), required=True)
    choose_active.add_argument("--actor-id", required=True)
    choose_active.add_argument("--actor-name", default="")
    subparsers.add_parser("expire")
    subparsers.add_parser("settle")
    args = parser.parse_args()

    repository = ExperienceRepository()
    if args.command == "init-schema":
        repository.migrate()
        _emit({"ok": True, "operation": "init_schema", "text": "跨盆经验数据表已初始化"})
        return
    if args.command == "expire":
        repository.expire_stale()
        _emit({"ok": True, "operation": "expire", "text": "已清理过期跨盆经验请求"})
        return
    if args.command == "settle":
        repository.expire_stale()
        outcomes = settle_pending_trials(repository)
        _emit({"ok": True, "operation": "settle", "outcomes": outcomes, "text": "已检查待结算跨盆验证"})
        return
    if args.command == "create":
        try:
            if not args.dry_run:
                group_id_from_conversation_key(args.conversation_key or "")
                if not re.fullmatch(r"[^\s\x00-\x1f]{1,128}", args.actor_id or ""):
                    raise CandidateError("invalid_requester")
            source = build_live_snapshot(SOURCE_DEVICE, repository)
            target = build_live_snapshot(TARGET_DEVICE, repository)
            candidate = build_candidate(source, target, candidate_id="XP-" + uuid.uuid4().hex[:12].upper())
        except (CandidateError, ExperienceStorageError, OSError, ValueError) as error:
            _emit({"ok": False, "operation": "create", "text": safe_error_text(error)})
            return
        text = render_candidate_text(candidate)
        if not args.dry_run:
            repository.expire_stale()
            repository.create_candidate(
                candidate,
                source,
                target,
                conversation_key=args.conversation_key,
                requester_id=args.actor_id,
            )
            try:
                send_main_fact_turn(args.conversation_key, candidate)
            except (ExperienceStorageError, OSError, subprocess.SubprocessError):
                repository.mark_delivery_failed(candidate["candidate_id"])
                _emit({"ok": False, "operation": "create", "text": safe_error_text(ExperienceStorageError("dialogue_delivery_failed"))})
                return
        _emit({"ok": True, "operation": "create", "dry_run": bool(args.dry_run), "candidate": candidate, "text": text})
        return
    try:
        if args.command == "choose-active":
            result = repository.choose_active(
                conversation_key=args.conversation_key,
                action=args.choice,
                actor_id=args.actor_id,
                actor_name=args.actor_name,
            )
        else:
            result = repository.choose(args.candidate_id.upper(), args.choice, args.actor_id, args.actor_name)
    except (CandidateStateError, ExperienceStorageError, OSError, ValueError) as error:
        _emit({"ok": False, "operation": args.command, "text": safe_error_text(error)})
        return
    request = result["validation_request"]
    if request:
        text = "已记录经验采纳：不会立即浇水；soil3 只会在下一次本来就满足自身安全条件时，使用自己的合法动作完成一次验证。"
    elif args.choice == "save_only":
        text = "已保存这条跨盆交流，仅作后续解释和参考，不会进入验证。"
    else:
        text = "已拒绝这条跨盆经验，不会进入验证。"
    _emit({"ok": True, "operation": args.command, "choice": args.choice, "result": result, "text": text})


if __name__ == "__main__":
    main()
