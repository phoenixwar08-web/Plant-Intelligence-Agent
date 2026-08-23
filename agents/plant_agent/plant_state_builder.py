#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from schemas.plant_status import PlantStatusSummary
from schemas.sensor import SensorStatus
from schemas.visual import VisualStatus
from schemas.control import ControlStatus
from schemas.safety import SafetyStatus
from schemas.assessment import AssessmentStatus
from repositories.visual_repository import load_visual_status
from services.database_access import run_reader_sql

BASE_PHASE3_DIR = Path("/root/water/phase3")
OUTPUT_DIR = Path("/root/agent/plant_agent/outputs")
DB_NAME = "soil_data"
SHANGHAI = ZoneInfo("Asia/Shanghai")

SUPPORTED_DEVICES = {
    "soil1",
    "soil2",
    "soil3",
    "soil_test",
}

ALL_DEVICES = sorted(SUPPORTED_DEVICES)

def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def run_gsql(sql: str) -> List[List[str]]:
    """Execute only fixed application SQL through the read-only DB role."""
    return run_reader_sql(sql.strip())

def parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None

    value = value.strip()
    if value == "":
        return None

    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None

    value = value.strip()
    if value == "":
        return None

    try:
        return int(value)
    except ValueError:
        return None

def calculate_sensor_freshness(
    recv_time: Optional[str],
    stale_threshold_sec: int = 600,
) -> Dict[str, Any]:
    if not recv_time:
        return {
            "sensor_time": None,
            "sensor_age_sec": None,
            "stale_threshold_sec": stale_threshold_sec,
            "is_stale": True,
            "reason": "missing_sensor_time",
        }

    try:
        # 数据库字段没有时区，按开发板本地时间解释
        sensor_dt = datetime.strptime(
            recv_time,
            "%Y-%m-%d %H:%M:%S",
        ).replace(
            tzinfo=datetime.now().astimezone().tzinfo
        )

        now = datetime.now().astimezone()
        age_sec = max(
            0,
            int((now - sensor_dt).total_seconds()),
        )

        return {
            "sensor_time": recv_time,
            "sensor_age_sec": age_sec,
            "stale_threshold_sec": stale_threshold_sec,
            "is_stale": age_sec > stale_threshold_sec,
            "reason": (
                "sensor_data_stale"
                if age_sec > stale_threshold_sec
                else "sensor_data_fresh"
            ),
        }

    except ValueError:
        return {
            "sensor_time": recv_time,
            "sensor_age_sec": None,
            "stale_threshold_sec": stale_threshold_sec,
            "is_stale": True,
            "reason": "invalid_sensor_time_format",
        }

def get_latest_sensor(device_code: str) -> Optional[Dict[str, Any]]:
    sql = f"""
SELECT
    id,
    device_code,
    recv_time,
    temp,
    humidity,
    ec,
    lux,
    watering_flag,
    watering_sec,
    source,
    air_humidity
FROM soil_sensor_readings
WHERE device_code = '{device_code}'
ORDER BY recv_time DESC
LIMIT 1;
"""

    rows = run_gsql(sql)

    if not rows:
        return None

    row = rows[0]

    if len(row) < 11:
        raise RuntimeError(
            f"Unexpected sensor row length: {len(row)}, row={row}"
        )

    return {
        "id": parse_int(row[0]),
        "device_code": row[1],
        "recv_time": row[2],
        "soil_temperature": parse_float(row[3]),
        "soil_humidity": parse_float(row[4]),
        "ec": parse_float(row[5]),
        "lux": parse_float(row[6]),
        "watering_flag": parse_int(row[7]),
        "watering_sec": parse_float(row[8]),
        "source": row[9],
        "air_humidity": parse_float(row[10]),
    }


def get_recent_irrigation_events(
    device_code: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    limit = max(1, min(limit, 50))

    sql = f"""
SELECT
    id,
    device_code,
    command_time,
    water_sec,
    reason,
    plan_label,
    source,
    status,
    operator_id
FROM irrigation_events
WHERE device_code = '{device_code}'
ORDER BY command_time DESC
LIMIT {limit};
"""

    rows = run_gsql(sql)
    events: List[Dict[str, Any]] = []

    for row in rows:
        if len(row) < 9:
            continue

        events.append(
            {
                "id": parse_int(row[0]),
                "device_code": row[1],
                "command_time": row[2],
                "water_sec": parse_float(row[3]),
                "reason": row[4] or None,
                "plan_label": row[5] or None,
                "source": row[6] or None,
                "status": row[7] or None,
                "operator_id": row[8] or None,
            }
        )

    return events


def get_recent_human_watering_events(
    device_code: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Fetch recorded manual watering without contaminating MQTT irrigation history."""
    limit = max(1, min(limit, 50))
    sql = f"""
SELECT id, occurred_at, duration_sec, volume_ml, note, operator_id, operator_name, source, confirmation_status, trust_status
FROM human_events
WHERE device_code = '{device_code}'
  AND event_type = 'manual_watering'
  AND confirmation_status = 'confirmed'
  AND trust_status IN ('attested', 'legacy_verified')
ORDER BY occurred_at DESC
LIMIT {limit};
"""
    try:
        rows = run_gsql(sql)
    except Exception:
        # Human history is explanatory only; an unavailable history source must not
        # prevent the existing state/Phase3 data path from being built.
        return []

    events: List[Dict[str, Any]] = []
    for row in rows:
        if len(row) < 10:
            continue
        if (row[8] or None) != "confirmed" or (row[9] or None) not in {"attested", "legacy_verified"}:
            continue
        raw_occurred_at = (row[1] or "").replace("Z", "+00:00")
        if len(raw_occurred_at) >= 3 and raw_occurred_at[-3] in "+-" and raw_occurred_at[-2:].isdigit():
            raw_occurred_at += ":00"
        try:
            occurred_at = datetime.fromisoformat(raw_occurred_at).astimezone(SHANGHAI).isoformat()
        except (TypeError, ValueError):
            continue
        events.append({
            "id": parse_int(row[0]),
            "occurred_at": occurred_at,
            "duration_sec": parse_float(row[2]),
            "volume_ml": parse_float(row[3]),
            "note": row[4] or None,
            "operator_id": row[5] or None,
            "operator_name": row[6] or None,
            "source": row[7] or None,
            "confirmation_status": row[8] or None,
            "trust_status": row[9] or None,
        })
    return events


def get_recent_manual_check_events(
    device_code: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Fetch confirmed manual-check facts without changing control inputs."""
    limit = max(1, min(limit, 50))
    sql = f"""
SELECT id, occurred_at, check_code, check_source, check_evidence, check_generated_at,
       result, note, operator_id, operator_name, source, confirmation_status, trust_status
FROM manual_check_events
WHERE device_code = '{device_code}'
  AND confirmation_status = 'confirmed'
  AND trust_status IN ('attested', 'legacy_verified')
ORDER BY occurred_at DESC
LIMIT {limit};
"""
    try:
        rows = run_gsql(sql)
    except Exception:
        # Manual checks are explanatory audit facts. Missing history must not
        # prevent sensor/control state construction, including before migration.
        return []

    events: List[Dict[str, Any]] = []
    for row in rows:
        if len(row) < 13 or (row[11] or None) != "confirmed" or (row[12] or None) not in {"attested", "legacy_verified"}:
            continue
        raw_occurred_at = (row[1] or "").replace("Z", "+00:00")
        if len(raw_occurred_at) >= 3 and raw_occurred_at[-3] in "+-" and raw_occurred_at[-2:].isdigit():
            raw_occurred_at += ":00"
        try:
            occurred_at = datetime.fromisoformat(raw_occurred_at).astimezone(SHANGHAI).isoformat()
        except (TypeError, ValueError):
            continue
        events.append({
            "id": parse_int(row[0]),
            "occurred_at": occurred_at,
            "check_code": row[2] or None,
            "check_source": row[3] or None,
            "check_evidence": row[4] or None,
            "check_generated_at": row[5] or None,
            "result": row[6] or None,
            "note": row[7] or None,
            "operator_id": row[8] or None,
            "operator_name": row[9] or None,
            "source": row[10] or None,
            "confirmation_status": row[11] or None,
            "trust_status": row[12] or None,
        })
    return events


def first_real_irrigation_event(
    events: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for event in events:
        water_sec = event.get("water_sec") or 0.0
        status = event.get("status")
        source = event.get("source")

        if water_sec <= 0:
            continue

        if status == "unattributed_direct_cmd":
            continue

        if source == "unknown_direct_mqtt":
            continue

        return event

    return None


def get_phase3_state(device_code: str) -> Dict[str, Any]:
    path = BASE_PHASE3_DIR / device_code / "system_state.json"
    return load_json(path)


def get_irrigation_profile(device_code: str) -> Dict[str, Any]:
    path = BASE_PHASE3_DIR / device_code / "irrigation_profile.json"

    if not path.exists():
        return {}

    return load_json(path)


def get_bool_active(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    return bool(obj.get("active", False))


def build_safety_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    water_delivery = state.get("water_delivery_suspect", {})
    low_recovery = state.get("low_wet_recovery_suspect", {})
    reservoir = state.get("reservoir_empty_suspect", {})
    predictor = state.get("predictor_circuit", {})

    water_delivery_active = get_bool_active(water_delivery)
    low_recovery_active = get_bool_active(low_recovery)
    reservoir_active = get_bool_active(reservoir)

    blocking_reasons: List[str] = []

    if water_delivery_active:
        blocking_reasons.append("water_delivery_suspect")

    if low_recovery_active:
        blocking_reasons.append("low_wet_recovery_suspect")

    if reservoir_active:
        blocking_reasons.append("reservoir_empty_suspect")

    if predictor.get("state") == "OPEN":
        blocking_reasons.append("predictor_circuit_open")

    return {
        "water_delivery_suspect": {
            "active": water_delivery_active,
            "reason": water_delivery.get("reason"),
            "clear_reason": water_delivery.get("clear_reason"),
        },
        "low_wet_recovery_suspect": {
            "active": low_recovery_active,
            "reason": low_recovery.get("reason"),
            "clear_reason": low_recovery.get("clear_reason"),
        },
        "reservoir_empty_suspect": {
            "active": reservoir_active,
            "reason": reservoir.get("reason"),
            "clear_reason": reservoir.get("clear_reason"),
        },
        "predictor_circuit": {
            "state": predictor.get("state"),
            "fail_count": predictor.get("fail_count"),
        },
        "automatic_watering_allowed": len(blocking_reasons) == 0,
        "blocking_reasons": blocking_reasons,
    }


def build_control_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    cooldown = state.get("dynamic_cooldown", {})
    trigger_guard = state.get("watering_trigger_guard", {})
    style = state.get("irrigation_style_experiment", {})
    window_guard = style.get("watering_window_guard", {})

    return {
        "pump_active": bool(state.get("pump_active", False)),
        "pump_last_command_sec": state.get("pump_last_command_sec"),
        "pump_total_cycles": state.get("pump_total_cycles"),
        "total_water_sec_dispensed": state.get(
            "total_water_sec_dispensed"
        ),
        "decision": (
            style.get("current_arm")
            or trigger_guard.get("candidate_plan")
            or (
                "observe"
                if cooldown.get("reason")
                else None
            )
            or "unknown"
        ),
        "decision_reason": (
            style.get("current_arm_reason")
            or trigger_guard.get("reason")
            or cooldown.get("reason")
            or "unknown"
        ),
        "trigger_guard": {
            "active": bool(
                trigger_guard.get("active", False)
            ),
            "blocked": bool(
                trigger_guard.get("blocked", False)
            ),
            "reason": trigger_guard.get("reason"),
            "candidate_plan": trigger_guard.get("candidate_plan"),
            "candidate_water_sec": trigger_guard.get(
                "candidate_water_sec"
            ),
        },
        "cooldown": {
            "cooldown_sec": cooldown.get("cooldown_sec"),
            "base_sec": cooldown.get("base_sec"),
            "reason": cooldown.get("reason"),
            "last_delta_m": cooldown.get("last_delta_m"),
        },
        "watering_window": {
            "level": window_guard.get("level"),
            "reason": window_guard.get("reason"),
            "max_sec": window_guard.get("max_sec"),
            "allow_large_pulse": window_guard.get(
                "allow_large_pulse"
            ),
            "allow_explore": window_guard.get("allow_explore"),
        },
        "pending_soak": None,
    }

def build_health_assessment(
    sensor: Dict[str, Any],
    freshness: Dict[str, Any],
    safety: Dict[str, Any],
    visual: Dict[str, Any],
) -> Dict[str, Any]:

    risk_score = 0
    risk_sources = []

    # 1. 数据新鲜度风险
    if freshness.get("is_stale"):
        risk_score += 20
        risk_sources.append(
            "sensor_data_stale"
        )

    # 2. 灌溉安全风险
    blocking_reasons = safety.get(
        "blocking_reasons",
        [],
    )

    if blocking_reasons:
        risk_score += 50
        risk_sources.extend(
            blocking_reasons
        )

    # 3. 视觉风险（预留）
    yellow_ratio = visual.get(
        "yellow_leaf_ratio"
    )

    if (
        yellow_ratio is not None
        and yellow_ratio > 0.3
    ):
        risk_score += 20
        risk_sources.append(
            "yellow_leaf_high"
        )

    # 风险等级
    if risk_score >= 50:
        level = "warning"
    elif risk_score >= 20:
        level = "attention"
    else:
        level = "normal"

    confidence = 1.0

    if freshness.get("is_stale"):
        confidence -= 0.2

    return {
        "level": level,
        "risk_score": risk_score,
        "risk_sources": risk_sources,
        "confidence": round(
            max(confidence, 0),
            2,
        ),
    }

def build_learning_summary(
    state: Dict[str, Any],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    style = state.get("irrigation_style_experiment", {})
    nightly = state.get("nightly_learning_advice", {})

    return {
        "current_arm": style.get("current_arm"),
        "current_arm_reason": style.get("current_arm_reason"),
        "experiment_reason": style.get("reason"),
        "nightly_advice": {
            "reason": nightly.get("reason"),
            "phase2_reason": (
                nightly.get("phase2", {}).get("reason")
                if isinstance(nightly.get("phase2"), dict)
                else None
            ),
            "exploration_reason": (
                nightly.get("exploration", {}).get("reason")
                if isinstance(nightly.get("exploration"), dict)
                else None
            ),
            "may_directly_pump": (
                nightly.get("control_boundary", {}).get(
                    "may_directly_pump"
                )
                if isinstance(
                    nightly.get("control_boundary"),
                    dict,
                )
                else None
            ),
        },
        "profile_zones": sorted(
            list(profile.get("zones", {}).keys())
        )
        if isinstance(profile.get("zones"), dict)
        else [],
    }


def build_primary_message(
    sensor: Optional[Dict[str, Any]],
    control: Dict[str, Any],
    safety: Dict[str, Any],
) -> Dict[str, Any]:
    if sensor is None:
        return {
            "status_level": "unknown",
            "primary_message": "没有读取到最新传感器数据。",
            "recommended_action": "检查数据采集链路",
        }

    if safety["blocking_reasons"]:
        reasons = "、".join(safety["blocking_reasons"])
        return {
            "status_level": "warning",
            "primary_message": (
                f"当前存在安全保护：{reasons}，"
                "系统不应自动重复浇水。"
            ),
            "recommended_action": "进行人工检查",
        }

    if control["pump_active"]:
        return {
            "status_level": "active",
            "primary_message": "当前水泵正在运行。",
            "recommended_action": "等待本轮浇水完成",
        }

    decision = control.get("decision")
    reason = control.get("decision_reason")

    if decision == "cooldown_observe":
        return {
            "status_level": "normal",
            "primary_message": (
                "当前处于正常浇水冷却观察期，"
                "系统暂不重复浇水。"
            ),
            "recommended_action": "继续观察",
        }

    return {
        "status_level": "normal",
        "primary_message": (
            f"当前决策为 {decision}，原因是 {reason}。"
        ),
        "recommended_action": "按 Phase3 当前策略继续运行",
    }

def build_plant_status(device_code: str) -> Dict[str, Any]:
    if device_code not in SUPPORTED_DEVICES:
        raise ValueError(...)

    state = get_phase3_state(device_code)
    profile = get_irrigation_profile(device_code)
    sensor = get_latest_sensor(device_code)
    recent_events = get_recent_irrigation_events(
        device_code,
        limit=10,
    )
    last_real_event = first_real_irrigation_event(recent_events)
    recent_human_events = get_recent_human_watering_events(device_code, limit=5)
    recent_manual_checks = get_recent_manual_check_events(device_code, limit=5)
    vision_data = load_visual_status(
        device_code
    )

    if vision_data:
        visual = dict(
            vision_data.get(
                "visual",
                {}
            )
        )
        visual["observed_at"] = vision_data.get("observed_at")
    else:
        visual = {
            "observed_at": None,
            "plant_health": None,
            "disease_suspected": False,
            "visual_stress_level": None,
            "yellow_leaf_ratio": None,
            "green_leaf_area_px": None,
            "flower_count": None,
            "fruit_count": None,
            "suspected_issues": [],
        }
    control = build_control_summary(state)
    safety = build_safety_summary(state)
    learning = build_learning_summary(state, profile)

    freshness = calculate_sensor_freshness(
        sensor.get("recv_time") if sensor else None
    )
    health_assessment = build_health_assessment(
        sensor=sensor,
        freshness=freshness,
        safety=safety,
        visual=visual,
    )
    summary = build_primary_message(
        sensor,
        control,
        safety,
    )
    
    result = {
        "device_code": device_code,
        "generated_at": datetime.now().astimezone().isoformat(),
        "data_freshness": freshness,
        "sensor": sensor,
        "control": control,
        "safety": safety,
        "learning": learning,
        "history": {
            "last_real_irrigation": last_real_event,
            "recent_irrigation_events": recent_events[:5],
        },
        "visual": visual,
        "human_events": {
            "last_external_watering": (
                recent_human_events[0] if recent_human_events else None
            ),
            "last_manual_check": recent_manual_checks[0] if recent_manual_checks else None,
            "recent_external_watering": recent_human_events,
            "recent_manual_checks": recent_manual_checks,
        },
        "assessment": health_assessment,
        "summary": summary,
        "sources": [
            {
                "type": "gaussdb",
                "database": DB_NAME,
                "table": "soil_sensor_readings",
            },
            {
                "type": "gaussdb",
                "database": DB_NAME,
                "table": "irrigation_events",
            },
            {
                "type": "gaussdb",
                "database": DB_NAME,
                "table": "human_events",
            },
            {
                "type": "gaussdb",
                "database": DB_NAME,
                "table": "manual_check_events",
            },
            {
                "type": "phase3_state",
                "path": str(
                    BASE_PHASE3_DIR
                    / device_code
                    / "system_state.json"
                ),
            },
            {
                "type": "phase3_profile",
                "path": str(
                    BASE_PHASE3_DIR
                    / device_code
                    / "irrigation_profile.json"
                ),
            },
        ],
    }
    return result
def build_all_plants_status() -> Dict[str, Any]:
    plants = []

    normal_count = 0
    warning_count = 0
    stale_devices = []

    for device_code in ALL_DEVICES:
        try:
            status = build_plant_status(device_code)

            plants.append(status)

            level = (
                status
                .get("summary", {})
                .get("status_level")
            )

            if level == "warning":
                warning_count += 1
            else:
                normal_count += 1

            freshness = (
                status
                .get("data_freshness", {})
            )

            if freshness.get("is_stale"):
                stale_devices.append(device_code)

        except Exception as e:
            plants.append(
                {
                    "device_code": device_code,
                    "error": str(e),
                    "status_level": "error",
                }
            )

            warning_count += 1


    return {
        "generated_at": datetime.now().astimezone().isoformat(),

        "plants": plants,

        "system_summary": {
            "total": len(ALL_DEVICES),
            "normal": normal_count,
            "warning": warning_count,
            "stale_devices": stale_devices,
        },
    }

def convert_to_schema(data: Dict[str, Any]) -> Dict[str, Any]:

    status = PlantStatusSummary(
        device_code=data["device_code"],
        generated_at=data["generated_at"],

        sensor=SensorStatus(
            **data["sensor"]
        ),

        visual=VisualStatus(
            **data["visual"]
        ),

        control=ControlStatus(
            **data["control"]
        ),

        safety=SafetyStatus(
            **data["safety"]
        ),

        assessment=AssessmentStatus(
            **data["assessment"]
        ),

        learning=data.get(
            "learning",
            {}
        ),

        history=data.get(
            "history",
            {}
        ),

        summary=data.get(
            "summary",
            {}
        ),

        human_events=data.get(
            "human_events",
            {}
        )
    )

    return status.model_dump()

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build structured plant status for Agent."
    )
    parser.add_argument(
        "device_code",
        choices=sorted(SUPPORTED_DEVICES) + ["all"],
        help="Plant device code",
    )
    parser.add_argument(
        "--output",
        help="Optional output JSON path",
    )
    args = parser.parse_args()

    if args.device_code == "all":
        status = build_all_plants_status()
    else:
        raw_status = build_plant_status(
            args.device_code
        )
        
        status = convert_to_schema(
            raw_status
        )

    output_path = (
        Path(args.output)
        if args.output
        else OUTPUT_DIR / (
            "all_plants_status.json"
            if args.device_code == "all"
            else f"{args.device_code}_status.json"
        )
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"\n[OK] Saved to: {output_path}")


if __name__ == "__main__":
    main()
