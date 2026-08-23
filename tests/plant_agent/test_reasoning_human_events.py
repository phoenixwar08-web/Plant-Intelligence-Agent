"""Manual records may explain a decision but cannot change the decision fields."""

import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from agent.reasoning_agent import reasoning


def _status(human_events):
    return {
        "device_code": "soil2",
        "sensor": {"soil_humidity": 40},
        "visual": {"yellow_leaf_ratio": 0.01, "disease_suspected": False},
        "control": {"decision": "observe", "decision_reason": "normal"},
        "safety": {"automatic_watering_allowed": True},
        "history": {"recent_irrigation_events": []},
        "human_events": human_events,
    }


def test_human_watering_is_explanatory_and_cannot_change_action_or_water_seconds():
    without_record = reasoning(_status({"last_external_watering": None}))
    with_record = reasoning(_status({"last_external_watering": {
        "occurred_at": "2026-07-17T18:00:00+08:00",
        "duration_sec": 30.0,
        "volume_ml": 100.0,
        "trust_status": "attested",
        "note": "手浇",
    }}))

    assert with_record["decision"]["action"] == without_record["decision"]["action"]
    assert with_record["decision"]["water_sec"] == without_record["decision"]["water_sec"]
    assert "最近人工浇水记录" in with_record["decision"]["reasoning"][-1]


def test_legacy_unconfirmed_human_event_is_not_used_for_reasoning():
    without_record = reasoning(_status({"last_external_watering": None}))
    legacy = reasoning(_status({"last_external_watering": {
        "occurred_at": "2026-07-17T18:00:00+08:00",
        "duration_sec": 30.0,
        "source": "legacy_unconfirmed",
        "trust_status": "legacy_unverified",
    }}))

    assert legacy["decision"]["action"] == without_record["decision"]["action"]
    assert legacy["decision"]["water_sec"] == without_record["decision"]["water_sec"]
    assert "2026-07-17T18:00:00+08:00" not in legacy["decision"]["reasoning"]


def test_non_confirmed_human_event_is_not_used_for_reasoning():
    result = reasoning(_status({"last_external_watering": {
        "occurred_at": "2026-07-17T18:00:00+08:00",
        "duration_sec": 30.0,
        "confirmation_status": "legacy_unconfirmed",
    }}))

    assert "2026-07-17T18:00:00+08:00" not in result["decision"]["reasoning"]


def test_confirmed_manual_check_is_explanatory_and_cannot_change_control_fields():
    without_check = reasoning(_status({"last_external_watering": None, "last_manual_check": None}))
    with_check = reasoning(_status({
        "last_external_watering": None,
        "last_manual_check": {
            "occurred_at": "2026-07-21T15:30:00+08:00",
            "check_code": "sensor_stale",
            "result": "no_issue",
            "note": "探针连接正常",
            "confirmation_status": "confirmed",
            "trust_status": "attested",
        },
    }))

    assert with_check["decision"]["action"] == without_check["decision"]["action"]
    assert with_check["decision"]["water_sec"] == without_check["decision"]["water_sec"]
    assert "检查项 sensor_stale" in with_check["decision"]["reasoning"][-1]
    assert "未发现异常" in with_check["decision"]["reasoning"][-1]


def test_pending_or_invalid_manual_check_is_not_used_for_reasoning():
    result = reasoning(_status({
        "last_external_watering": None,
        "last_manual_check": {
            "occurred_at": "2026-07-21T15:30:00+08:00",
            "check_code": "sensor_stale",
            "result": "no_issue",
            "confirmation_status": "pending",
        },
    }))

    assert "检查项 sensor_stale" not in result["decision"]["reasoning"]
