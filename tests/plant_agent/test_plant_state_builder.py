"""Regression tests for plant state normalization."""

import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import plant_state_builder as builder
from schemas.control import ControlStatus


def test_missing_trigger_guard_flags_default_to_false():
    summary = builder.build_control_summary({})

    assert summary["trigger_guard"]["active"] is False
    assert summary["trigger_guard"]["blocked"] is False

    validated = ControlStatus(**summary)
    assert validated.trigger_guard.active is False
    assert validated.trigger_guard.blocked is False


def build_status_with_visual(monkeypatch, vision_data):
    monkeypatch.setattr(builder, "get_phase3_state", lambda device_code: {})
    monkeypatch.setattr(builder, "get_irrigation_profile", lambda device_code: {})
    monkeypatch.setattr(
        builder,
        "get_latest_sensor",
        lambda device_code: {"recv_time": None},
    )
    monkeypatch.setattr(
        builder,
        "get_recent_irrigation_events",
        lambda device_code, limit: [],
    )
    monkeypatch.setattr(builder, "first_real_irrigation_event", lambda events: None)
    monkeypatch.setattr(
        builder,
        "get_recent_human_watering_events",
        lambda device_code, limit: [],
    )
    monkeypatch.setattr(
        builder,
        "get_recent_manual_check_events",
        lambda device_code, limit: [],
    )
    monkeypatch.setattr(builder, "load_visual_status", lambda device_code: vision_data)
    monkeypatch.setattr(builder, "build_control_summary", lambda state: {})
    monkeypatch.setattr(builder, "build_safety_summary", lambda state: {})
    monkeypatch.setattr(
        builder,
        "build_learning_summary",
        lambda state, profile: {},
    )
    monkeypatch.setattr(
        builder,
        "calculate_sensor_freshness",
        lambda recv_time: {},
    )
    monkeypatch.setattr(builder, "build_health_assessment", lambda **kwargs: {})
    monkeypatch.setattr(
        builder,
        "build_primary_message",
        lambda sensor, control, safety: {},
    )

    return builder.build_plant_status("soil2")


def test_visual_observed_at_is_preserved_from_vision_document(monkeypatch):
    observed_at = "2026-07-16T18:00:00+08:00"

    status = build_status_with_visual(
        monkeypatch,
        {
            "observed_at": observed_at,
            "visual": {"plant_health": "normal"},
        },
    )

    assert status["visual"]["observed_at"] == observed_at
    assert status["visual"]["plant_health"] == "normal"


def test_visual_observed_at_is_null_when_vision_document_has_no_timestamp(
    monkeypatch,
):
    status = build_status_with_visual(
        monkeypatch,
        {"visual": {"plant_health": "normal"}},
    )

    assert status["visual"]["observed_at"] is None


def test_missing_vision_document_keeps_status_buildable(monkeypatch):
    status = build_status_with_visual(monkeypatch, None)

    assert status["visual"]["observed_at"] is None


def test_manual_watering_is_exposed_only_in_human_events(monkeypatch):
    monkeypatch.setattr(builder, "get_phase3_state", lambda device_code: {})
    monkeypatch.setattr(builder, "get_irrigation_profile", lambda device_code: {})
    monkeypatch.setattr(builder, "get_latest_sensor", lambda device_code: {"recv_time": None})
    monkeypatch.setattr(builder, "get_recent_irrigation_events", lambda device_code, limit: [])
    monkeypatch.setattr(builder, "first_real_irrigation_event", lambda events: None)
    manual = {"id": 9, "occurred_at": "2026-07-17T18:00:00+08:00", "duration_sec": 30.0, "trust_status": "attested"}
    manual_check = {"id": 10, "occurred_at": "2026-07-21T15:30:00+08:00", "check_code": "sensor_stale", "result": "no_issue", "confirmation_status": "confirmed", "trust_status": "attested"}
    monkeypatch.setattr(builder, "get_recent_human_watering_events", lambda device_code, limit: [manual])
    monkeypatch.setattr(builder, "get_recent_manual_check_events", lambda device_code, limit: [manual_check])
    monkeypatch.setattr(builder, "load_visual_status", lambda device_code: None)
    monkeypatch.setattr(builder, "build_control_summary", lambda state: {})
    monkeypatch.setattr(builder, "build_safety_summary", lambda state: {})
    monkeypatch.setattr(builder, "build_learning_summary", lambda state, profile: {})
    monkeypatch.setattr(builder, "calculate_sensor_freshness", lambda recv_time: {})
    monkeypatch.setattr(builder, "build_health_assessment", lambda **kwargs: {})
    monkeypatch.setattr(builder, "build_primary_message", lambda sensor, control, safety: {})

    status = builder.build_plant_status("soil2")

    assert status["history"]["last_real_irrigation"] is None
    assert status["human_events"]["last_external_watering"] == manual
    assert status["human_events"]["last_manual_check"] == manual_check
    assert status["human_events"]["recent_manual_checks"] == [manual_check]


def test_legacy_unconfirmed_human_event_is_excluded_from_status(monkeypatch):
    captured = {}

    def fake_gsql(sql):
        captured["sql"] = sql
        return [["1", "2026-07-17T18:26:00+08:00", "30", "100", "legacy", "user", "name", "legacy_unconfirmed", "legacy_unconfirmed", "legacy_unverified"]]

    monkeypatch.setattr(builder, "run_gsql", fake_gsql)

    assert builder.get_recent_human_watering_events("soil2") == []
    assert "trust_status IN ('attested', 'legacy_verified')" in captured["sql"]
    assert "confirmation_status = 'confirmed'" in captured["sql"]


def test_only_confirmed_manual_checks_are_exposed_from_their_own_table(monkeypatch):
    captured = {}

    def fake_gsql(sql):
        captured["sql"] = sql
        return [[
            "10", "2026-07-21T15:30:00+08:00", "sensor_stale", "sensor", "stale",
            "2026-07-21T15:29:00+08:00", "no_issue", "normal", "user", "name", "qqbot", "confirmed", "attested",
        ]]

    monkeypatch.setattr(builder, "run_gsql", fake_gsql)
    checks = builder.get_recent_manual_check_events("soil2")

    assert checks[0]["check_code"] == "sensor_stale"
    assert checks[0]["confirmation_status"] == "confirmed"
    assert "FROM manual_check_events" in captured["sql"]
    assert "confirmation_status = 'confirmed'" in captured["sql"]
    assert "trust_status IN ('attested', 'legacy_verified')" in captured["sql"]
