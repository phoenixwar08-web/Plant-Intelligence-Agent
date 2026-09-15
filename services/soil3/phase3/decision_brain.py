"""
=============================================================================
 decision_brain.py
 ─────────────────────────────────────────────────────────────────────────
 【第二部分：决策大脑 —— 六层闭环控制逻辑】

 职责：
   · Layer 0  传感器读取与 VPD / EC_norm 计算
   · Layer 1  物理天花板计算（基于当前 K_p 和 FC）
   · Layer 2  安全态势评估（安全区 / 战区 / 紧急区）
   · Layer 3  门卫拦截 + 考卷生成（激进 / 保守 / 对照三组候选）
   · Layer 4  异步 LLM 推理接口（/dev/shm 非阻塞通信）（已经修改为pase2阶段）
   · Layer 5  动态最高法庭：多目标代价函数 J 选出最优方案
   · Layer 6  执行 + 短期物理进化（EMA 更新 K_p）

 【对齐修改记录】（对应 config_manager.py 的修复）：
   Align-1  所有 cfg.get("WATER_SEC_MAX_HARD") / cfg.get("SOAK_WAIT_SEC") 等
            硬件常量改为 cfg.get_constant()，明确区分进化参数与工程常量。
   Align-2  cfg.get("WATER_SEC_MIN") 同上，改为 cfg.get_constant()。
   Align-3  cfg.get("EC_NORM_REF_TEMP") / cfg.get("EC_TEMP_COEFF") 同上。
   Align-4  cfg.get("RESPIRATION_LIMIT") / cfg.get("TARGET_LOW") / cfg.get("EC_SALT_STRESS")
            改为直接访问属性，因为新 config 已为这三个提供了 @property。
   Align-5  Phase2Predictor.__init__ 中对 SYSTEM_CONSTANTS 的读取改为 cfg.get_constant()。
   Align-6  _evolve_kp 中 K_P_EMA_ALPHA 从 cfg.get() 读取（它是可进化参数，正确）；
            同时对 K_p 下界加更明确的注释说明为何不用 WATER_SEC_MIN 约束。
   Align-7  删除旧版中对不存在的 SYSTEM_PARAMS / append_sensor_log /
            load_pattern_memory / save_pattern_memory / load_system_state /
            save_system_state 的导入，改为从新 config 的正确接口导入或内联实现。
   Align-8  LARGE_WATER_THRESHOLD_SEC 原来从 SYSTEM_PARAMS 读取，
            新 config 无此键，改为从 SYSTEM_CONSTANTS 读取（已在新 config 中新增）。
   Align-9  execute_pump 中 get_constant("LARGE_WATER_THRESHOLD_SEC") 再次对齐：
            该常量已由 config_manager Upgrade-6 彻底删除，改为动态属性
            cfg.LARGE_WATER_THRESHOLD（T_full × LARGE_WATER_RATIO），不再有默认值兜底。
   Align-10 _append_sensor_log 写 CSV 前加 fcntl.LOCK_EX 排他锁，与
            auditor.py 的 LOCK_SH 读锁配合，消除午夜并发的文件指针竞争。

 【缺陷修复记录】：
   Fix-A  30 分钟线程假死（Layer 6）
          原设计：浇水后 time.sleep(SOAK_WAIT_SEC=1800s) 彻底阻塞主线程，
          期间无法响应紧急事件、信号退出或湿度越界。
          修复：execute_and_evolve() 不再阻塞等待，而是立即返回
          PendingSoak 哨兵对象。主控大脑在 run_cycle() 中：
            · 先检查上一轮是否有待处理的渗透任务（_pending_soak）
            · 若渗透时间未到，本轮跳过浇水直接返回 SOAK_PENDING 状态
            · 渗透时间到后，完成采样 + K_p 更新 + pattern_memory 写入
          整个等待期间主循环每 5 分钟仍正常运转，可随时响应紧急情况。

   Fix-B  经验库污染（Layer 6 Pattern Memory 滥用）
          原设计：每次浇水后无条件写入 pattern_memory，包含以下污染源：
            1. 紧急满灌（非正常决策，秒数不代表最优）
            2. delta_m < 0（浇水后湿度反而下降，传感器噪声或渗透未完成）
            3. delta_m 异常大（传感器尖峰，如 >FC 的读数）
          修复：_update_pattern_memory() 增加三重质量门控，
          任意一条未通过则丢弃本次记录并打 WARNING，拒绝污染经验库。
          同时为记录追加 quality_score 字段，供 Layer 3 加权查询。

   Fix-C  代价函数 J 量纲未对齐（Layer 5）
          原设计：
            loss_survival / loss_respiration 的量纲是 (%²)，
            数量级约为 (0~1.7)² × 12步 ≈ 0~34。
            cost_mechanical = water_sec / 60，量纲是 (秒/分钟)，
            数量级约为 0~0.5。
            γ=10 时机械惩罚最大仅 5，而生存损失可达 34×α，
            γ 实际上几乎不起作用，审计员调整 γ 也毫无意义。
          修复：cost_mechanical 改为在战区宽度²的量纲空间内归一化：
            cost_mechanical = (water_sec / WATER_SEC_MAX_HARD)²
                              × battle_zone²
          这使三项惩罚都在同一量纲 (%²) 下竞争，
          γ 从此真正控制"机械磨损 vs 植物安全"的权衡比例。

   Fix-Timing  PendingSoak 哨兵计时全部替换为 time.monotonic()。
               ready / remaining_sec / pump_end_time 不再受 NTP 跳变影响；
               持久化时转为 wall-clock（pump_end_time_wall）存盘，
               _restore_pending_soak 恢复时计算已过时长后映射回 monotonic 轴，
               保证断电重启后渗透剩余时间正确延续。

   Fix-Penalty 经验库惩罚系数闭环。
               _update_pattern_memory 新增 expected_delta_m 参数（来自
               Layer 5 选定方案的预测轨迹终点），与实际 delta_m 对比：
               · 误差 <20%          → penalty = 0（无惩罚）
               · 误差 20-60%        → penalty 线性 0→0.5
               · 误差 >60%          → penalty = 0.5
               · 实际 < 预期（高估）→ 惩罚再 ×1.5（最大 0.75）
               penalty 写入 JSON；_get_reference_sec 综合代价评分中
               叠加 penalty × 5.0，让历史上"预测高估 Δm"的记录在
               Layer 3 对照组查询时自然靠后，而非简单丢弃——
               实现"吃一堑长一智"而非"忽略坏数据"。
=============================================================================
"""

import json
import math
import os
import smtplib
import subprocess
import time
import logging
import uuid
from dataclasses import dataclass, field
from email.header import Header
from email.message import EmailMessage
from email.utils import formataddr
from enum import Enum, auto
from pathlib import Path
from statistics import median
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
except ImportError:
    mqtt = None  # type: ignore[assignment, misc]

# 与 phase1_test/water-test-soil3.py 对齐；可用环境变量覆盖
MQTT_BROKER_DEFAULT = os.environ.get("IRRIGATION_MQTT_BROKER", "localhost")
MQTT_TOPIC_PUMP_CMD_DEFAULT = os.environ.get(
    "IRRIGATION_MQTT_TOPIC_PUMP", "esp32/pump3/cmd"
)
DEVICE_CODE_DEFAULT = "soil3"

# Align-7：新 config 的正确导入接口
from config_manager import (
    ConfigManager,
    SYSTEM_CONSTANTS,
    FILE_PATHS,
)

from runtime_io import (
    append_csv_row_locked,
    load_json_locked,
    read_csv_dicts_locked,
    save_json_locked,
    update_json_locked,
)
from adaptive_evidence import HARD_INVALID, classify_trial

logger = logging.getLogger("decision_brain")

_NIGHTLY_ADVICE_COST_SCALE = 3.0


# ---------------------------------------------------------------------------
# GaussDB parameter change log for online K_P evolution
# ---------------------------------------------------------------------------
PARAMETER_CHANGE_SYSTEM = f"phase3_{DEVICE_CODE_DEFAULT}"


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(float(value))
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _gsql_exec(sql: str) -> None:
    sql_path = Path(f"/tmp/soil3_gsql_{uuid.uuid4().hex}.sql")
    sql_path.write_text(sql, encoding="utf-8")
    os.chmod(sql_path, 0o644)
    try:
        result = subprocess.run(
            ["su", "-", "opengauss", "-c", f"gsql -d soil_data -p 7654 -f {sql_path}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    finally:
        try:
            sql_path.unlink()
        except OSError:
            pass


def _record_parameter_change(
    parameter_name: str,
    old_value: Optional[float],
    new_value: Optional[float],
    change_reason: str,
    change_source: str,
    evidence: dict[str, Any],
) -> None:
    try:
        old_num = float(old_value) if old_value is not None else None
        new_num = float(new_value) if new_value is not None else None
        delta_num = (
            round(new_num - old_num, 6)
            if old_num is not None and new_num is not None
            else None
        )
        evidence_text = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        sql = (
            "INSERT INTO parameter_change_log "
            "(change_time, device_code, system_name, parameter_name, old_value, new_value, "
            "delta_value, change_reason, change_source, audit_date, evidence) VALUES ("
            + ", ".join(
                [
                    "CURRENT_TIMESTAMP",
                    _sql_literal(DEVICE_CODE_DEFAULT),
                    _sql_literal(PARAMETER_CHANGE_SYSTEM),
                    _sql_literal(parameter_name),
                    _sql_literal(old_num),
                    _sql_literal(new_num),
                    _sql_literal(delta_num),
                    _sql_literal(change_reason),
                    _sql_literal(change_source),
                    "CURRENT_DATE",
                    _sql_literal(evidence_text),
                ]
            )
            + ");"
        )
        _gsql_exec(sql)
    except Exception as e:
        logger.error(
            "[parameter_change_log] write failed for %s %s -> %s: %s",
            parameter_name,
            old_value,
            new_value,
            e,
        )


class PumpExecutionError(RuntimeError):
    """Raised when a pump command could not be completed safely."""


def _send_alert_email(subject: str, body: str) -> bool:
    """Send an alert email through a locally configured SMTP server."""
    to_addr = SYSTEM_CONSTANTS.get("ALERT_EMAIL_TO") or "3278326875@qq.com"
    host = os.environ.get("IRRIGATION_ALERT_SMTP_HOST", "localhost")
    port = int(os.environ.get("IRRIGATION_ALERT_SMTP_PORT", "25"))
    username = os.environ.get("IRRIGATION_ALERT_SMTP_USER")
    password = os.environ.get("IRRIGATION_ALERT_SMTP_PASS")
    from_addr = os.environ.get("IRRIGATION_ALERT_FROM", username or f"soil3@{os.uname().nodename}")
    from_name = os.environ.get("IRRIGATION_ALERT_FROM_NAME", "智养中心")

    msg = EmailMessage()
    msg["From"] = formataddr((str(Header(from_name, "utf-8")), from_addr))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if os.environ.get("IRRIGATION_ALERT_SMTP_TLS", "0") == "1":
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
        logger.critical(f"[Alert] 已发送邮件告警至 {to_addr}: {subject}")
        return True
    except (smtplib.SMTPException, OSError, TimeoutError) as e:
        logger.critical(
            f"[Alert] 邮件告警发送失败: {e}. "
            f"请配置本机 sendmail/SMTP 或设置 IRRIGATION_ALERT_SMTP_* 环境变量。"
        )
        return False


# ===========================================================================
# 辅助 I/O（config_manager 新版未导出这些函数，内联实现保持 decision_brain 自洽）
# ===========================================================================

def _append_sensor_log(
    humidity: float, temperature: float,
    ec_raw: float, ec_norm: float,
    vpd: float, action_sec: float = 0.0,
) -> None:
    """追加一行传感器数据到 sensor_log.csv。"""
    from datetime import datetime

    header = [
        "timestamp", "humidity", "temperature",
        "ec_raw", "ec_norm", "vpd", "action_sec",
    ]
    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        f"{humidity:.2f}", f"{temperature:.2f}",
        f"{ec_raw:.4f}", f"{ec_norm:.4f}",
        f"{vpd:.4f}", f"{action_sec:.1f}",
    ]
    try:
        append_csv_row_locked(FILE_PATHS["SENSOR_LOG"], header, row)
    except OSError as e:
        logger.error(f"[sensor_log] 写入失败: {e}")


def _load_json(path: Path, default):
    return load_json_locked(path, default)


def _save_json(path: Path, data) -> None:
    try:
        save_json_locked(path, data)
    except OSError as e:
        logger.error(f"[save_json] 写入失败 {path}: {e}")


_SYSTEM_STATE_DEFAULT = {
    "pump_total_cycles": 0,
    "total_water_sec_dispensed": 0.0,
    "last_large_water_timestamp": None,
}


def _merge_system_state(raw: Optional[dict]) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    return {**_SYSTEM_STATE_DEFAULT, **raw}


def _load_system_state() -> dict:
    return _merge_system_state(_load_json(FILE_PATHS["SYSTEM_STATE"], {}))


def _save_system_state(state: dict) -> None:
    _save_json(FILE_PATHS["SYSTEM_STATE"], _merge_system_state(state))


def _update_system_state(mutator) -> dict:
    def apply(raw):
        state = _merge_system_state(raw)
        updated = mutator(state)
        return state if updated is None else _merge_system_state(updated)
    try:
        return update_json_locked(FILE_PATHS["SYSTEM_STATE"], {}, apply)
    except OSError as e:
        logger.error(f"[system_state] 原子更新失败: {e}")
        raise


def _state_water_path_recovered_epoch(state: Optional[dict]) -> float:
    if not isinstance(state, dict):
        return 0.0
    epochs = []
    for key in ("water_path_recovered_at", "post_repair_epoch"):
        try:
            epochs.append(float(state.get(key) or 0.0))
        except (TypeError, ValueError):
            pass
    for key in ("water_delivery_suspect", "reservoir_empty_suspect", "low_wet_recovery_suspect"):
        suspect = state.get(key)
        if not isinstance(suspect, dict):
            continue
        for ts_key in ("cleared_at", "repair_retest_completed_at", "repair_confirmed_at"):
            try:
                epochs.append(float(suspect.get(ts_key) or 0.0))
            except (TypeError, ValueError):
                pass
    return max(epochs or [0.0])


def _advice_is_stale_after_water_repair(advice: dict[str, Any], state: dict[str, Any]) -> bool:
    recovered_at = _state_water_path_recovered_epoch(state)
    if recovered_at <= 0:
        return False
    try:
        created_at = float(advice.get("created_at") or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0
    if created_at >= recovered_at:
        return False
    sample_quality = advice.get("sample_quality") if isinstance(advice, dict) else {}
    return bool(isinstance(sample_quality, dict) and sample_quality.get("exclude_new_learning"))


def _suspect_blocks_learning_after_repair(suspect: Any, recovered_at: float) -> bool:
    if not isinstance(suspect, dict):
        return False
    active = bool(suspect.get("active"))
    excluded = bool(suspect.get("exclude_from_learning"))
    if not active and not excluded:
        return False
    try:
        updated_at = float(suspect.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    try:
        cleared_at = float(suspect.get("cleared_at") or 0.0)
    except (TypeError, ValueError):
        cleared_at = 0.0
    if not active and recovered_at and cleared_at >= recovered_at and updated_at <= recovered_at:
        return False
    return True


def _load_nightly_learning_advice(state: Optional[dict] = None) -> dict[str, Any]:
    if state is None:
        state = _load_system_state()
    advice = state.get("nightly_learning_advice")
    if not isinstance(advice, dict):
        return {"active": False, "reason": "missing"}
    expires_at = advice.get("expires_at")
    try:
        expired = expires_at is not None and float(expires_at) < time.time()
    except (TypeError, ValueError):
        expired = False
    if expired:
        result = dict(advice)
        result.update({"active": False, "reason": "expired"})
        return result
    boundary = advice.get("control_boundary")
    if isinstance(boundary, dict) and (
        boundary.get("may_directly_pump")
        or boundary.get("may_override_phase3")
        or boundary.get("may_cross_hard_safety")
    ):
        result = dict(advice)
        result.update({"active": False, "reason": "unsafe_boundary_claim"})
        return result
    if _advice_is_stale_after_water_repair(advice, state):
        result = dict(advice)
        result.update({"active": False, "reason": "stale_after_water_path_recovery"})
        return result
    result = dict(advice)
    result.update({"active": True, "reason": "active"})
    return result


def _learning_advice_arm_name(plan: "ActionPlan") -> str:
    label = str(getattr(plan, "label", "") or "").lower()
    if "style_pulse_10" in label or "large_pulse" in label or "budget_pulse" in label:
        return "large_pulse"
    if "style_pulse_3" in label or "micro" in label:
        return "micro_pulse"
    if "style_pulse_6" in label or "medium" in label:
        return "medium_pulse"
    if "style_pulse_9" in label or "strong" in label:
        return "strong_pulse"
    if "low_wet_recovery" in label or "drydown_recovery" in label:
        return "drydown_recovery"
    if "drydown" in label:
        return "drydown_cycle"
    if "wet_hold" in label:
        return "wet_hold_refill"
    if "observe" in label:
        return "observe"
    return label or "unknown"


def _mark_predictor_probe(called: bool, success: bool = False) -> None:
    def update(state):
        state["predictor_last_probe"] = {
            "called": called,
            "success": success,
            "timestamp": time.time(),
        }
    _update_system_state(update)


def _response_matches_request(resp: Any, request_id: str, device_code: str) -> bool:
    return (
        isinstance(resp, dict)
        and resp.get("request_id") == request_id
        and resp.get("device_code") == device_code
    )


def _settle_window_expected_delta(
    plan: "ActionPlan", current_humidity: float, settle_window_minutes: float
) -> Optional[float]:
    if plan.predicted_peak is None or plan.predicted_minutes_to_peak is None:
        return None
    peak = float(plan.predicted_peak)
    minutes = float(plan.predicted_minutes_to_peak)
    if not math.isfinite(peak) or not math.isfinite(minutes) or minutes < 0:
        return None
    if minutes > float(settle_window_minutes):
        return None
    return max(0.0, peak - float(current_humidity))


def _load_pattern_memory() -> list:
    return _load_json(FILE_PATHS["PATTERN_MEMORY"], [])


def _save_pattern_memory(records: list) -> None:
    new_record = records[-1] if records else None

    def append_latest(current):
        if not isinstance(current, list):
            current = []
        if new_record is not None:
            current.append(new_record)
        return current[-100:]

    try:
        update_json_locked(FILE_PATHS["PATTERN_MEMORY"], [], append_latest)
    except OSError as e:
        logger.error(f"[pattern_memory] 原子追加失败: {e}")


def _trial_log_path() -> Path:
    return FILE_PATHS.get(
        "IRRIGATION_TRIALS",
        FILE_PATHS["PATTERN_MEMORY"].with_name("irrigation_trials.json"),
    )


def _load_irrigation_trials() -> list:
    return _load_json(_trial_log_path(), [])


def _save_irrigation_trials(records: list) -> None:
    new_record = records[-1] if records else None

    def append_latest(current):
        if not isinstance(current, list):
            current = []
        if new_record is not None:
            current.append(new_record)
        return current[-500:]

    try:
        update_json_locked(_trial_log_path(), [], append_latest)
    except OSError as e:
        logger.error(f"[irrigation_trials] 原子追加失败: {e}")


def _trial_learning_valid_legacy(record: dict) -> bool:
    """Infer strategy-learning usability for records written before quality tags."""
    if not isinstance(record, dict):
        return False
    if record.get("learning_valid") is False or record.get("excluded_from_strategy_scoring"):
        return False
    status = str(record.get("status") or "").lower()
    reason = str(record.get("reason") or "").lower()
    label = str(record.get("plan_label") or "").lower()
    joined = "|".join((status, reason, label))
    blocked_tokens = (
        "emergency",
        "reservoir",
        "water_delivery",
        "low_wet_recovery_suspect",
        "post_h_above_fc_tolerance",
        "delta_m_above_physical_max",
        "sensor_fault",
        "sensor_stale",
        "sensor_spike",
        "manual",
        "unknown_direct_mqtt",
        "observation_",
        "forced_exploration_quarantined",
    )
    if any(token in joined for token in blocked_tokens):
        return False
    return status in {"accepted", "rejected"}


def _phase2_response_predictions_path() -> Path:
    return FILE_PATHS.get(
        "PHASE2_RESPONSE_PREDICTIONS",
        Path("/root/water/wyc_training/phase3_response_predictions.json"),
    )


def _record_phase2_selected_prediction(
    plan: "ActionPlan", reading: "SensorReading",
) -> None:
    """Queue the exact Phase3-selected formal/shadow forecast for later real labels."""
    if plan.label in {"reservoir_retest_probe", "reservoir_empty_pause", "low_wet_recovery_paused"}:
        return
    if not plan.request_id or not plan.predicted_trajectory:
        return
    record = {
        "request_id": plan.request_id,
        "device_code": plan.device_code or "soil3",
        "source": "phase3_response",
        "source_timestamp": float(plan.prediction_timestamp or time.time()),
        "source_humidity": round(float(reading.humidity), 4),
        "selected_label": plan.label,
        "water_sec": round(float(plan.water_sec), 3),
        "zone": plan.prediction_zone,
        "formal_trajectory": list(plan.predicted_trajectory),
        "formal_peak": plan.predicted_peak,
        "formal_h12": plan.predicted_h12,
        "shadow_trajectory": list(plan.shadow_predicted_trajectory),
        "shadow_peak": plan.shadow_predicted_peak,
        "shadow_h12": plan.shadow_predicted_h12,
        "recorded_at": time.time(),
    }

    def append_unique(current):
        if not isinstance(current, list):
            current = []
        current = [
            item for item in current
            if item.get("request_id") != record["request_id"]
        ]
        current.append(record)
        return current[-1000:]

    try:
        update_json_locked(_phase2_response_predictions_path(), [], append_unique)
    except OSError as exc:
        logger.error(f"[Phase2] 响应预测入队失败: {exc}")


def _profile_path() -> Path:
    return FILE_PATHS.get(
        "IRRIGATION_PROFILE",
        FILE_PATHS["PATTERN_MEMORY"].with_name("irrigation_profile.json"),
    )


def _empty_zone_profile() -> dict:
    return {
        "stable_success": 0,
        "failure_count": 0,
        "max_allowed_sec": 3.0,
        "kp_ema": None,
        "last_reason": None,
        "last_updated": None,
    }


def _load_irrigation_profile() -> dict:
    profile = _load_json(_profile_path(), {})
    zones = profile.setdefault("zones", {})
    for name in ("low", "mid", "high"):
        base = _empty_zone_profile()
        base.update(zones.get(name, {}))
        zones[name] = base
    daily = profile.setdefault("daily_exploration", {})
    today = time.strftime("%Y-%m-%d")
    if daily.get("date") != today:
        profile["daily_exploration"] = {"date": today, "used": 0}
    return profile


def _save_irrigation_profile(profile: dict) -> None:
    _save_json(_profile_path(), profile)


def _humidity_zone_name(cfg: ConfigManager, humidity: float) -> str:
    low = float(cfg.TARGET_LOW)
    high = float(cfg.M_SAFE_SLEEP)
    width = max(high - low, 0.1)
    if humidity <= low + width / 3.0:
        return "low"
    if humidity >= high - width / 3.0:
        return "high"
    return "mid"


def _zone_kp_key(zone_name: str) -> str:
    return {
        "low": "K_P_LOW",
        "mid": "K_P_MID",
        "high": "K_P_HIGH",
    }.get(zone_name, "K_P_MID")


def _zone_kp(cfg: ConfigManager, zone_name: str) -> float:
    value = cfg.get(_zone_kp_key(zone_name))
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    profile = _load_irrigation_profile()
    kp = profile.get("zones", {}).get(zone_name, {}).get("kp_ema")
    if isinstance(kp, (int, float)) and kp > 0:
        return float(kp)
    return float(cfg.K_P)


RESPONSE_GUARD_TTL_SEC = 6 * 60 * 60
RESPONSE_GUARD_MIN_DELTA = 2.5
RESPONSE_GUARD_MIN_DPS = 0.75


def _recent_response_guard(zone_name: Optional[str] = None) -> Optional[dict]:
    state = _load_system_state()
    guard = state.get("recent_response_guard")
    if not isinstance(guard, dict):
        return None
    expires_at = guard.get("expires_at")
    if not isinstance(expires_at, (int, float)) or time.time() >= float(expires_at):
        return None
    if zone_name and guard.get("zone") not in {zone_name, "all"}:
        return None
    dps = guard.get("delta_per_sec")
    if not isinstance(dps, (int, float)) or float(dps) <= 0:
        return None
    return guard


def _guarded_zone_kp(cfg: ConfigManager, zone_name: str) -> tuple[float, Optional[dict]]:
    base_kp = _zone_kp(cfg, zone_name)
    guard = _recent_response_guard(zone_name)
    if guard:
        return max(base_kp, float(guard["delta_per_sec"])), guard
    return base_kp, None


def _record_recent_response_guard(
    *,
    cfg: ConfigManager,
    status: str,
    reason: str,
    plan_label: str,
    zone_name: str,
    pre_h: float,
    post_h: float,
    water_sec: float,
    delta_m: float,
) -> None:
    if water_sec <= 0 or delta_m <= 0:
        return
    delta_per_sec = float(delta_m) / max(float(water_sec), 0.1)
    safe_sleep = float(cfg.M_SAFE_SLEEP)
    strong = (
        delta_m >= RESPONSE_GUARD_MIN_DELTA
        or delta_per_sec >= RESPONSE_GUARD_MIN_DPS
        or post_h >= safe_sleep - 0.2
    )
    if not strong:
        return

    now = time.time()
    payload = {
        "active": True,
        "created_at": now,
        "expires_at": now + RESPONSE_GUARD_TTL_SEC,
        "zone": zone_name,
        "status": status,
        "reason": reason,
        "plan_label": plan_label,
        "water_sec": round(float(water_sec), 3),
        "humidity_before": round(float(pre_h), 3),
        "humidity_after": round(float(post_h), 3),
        "delta_m": round(float(delta_m), 3),
        "delta_per_sec": round(float(delta_per_sec), 5),
        "safe_sleep": round(safe_sleep, 3),
        "response_strength": "strong",
        "micro_pulse_risk": "high" if float(water_sec) <= 3.25 else "elevated",
    }

    def update(state):
        previous = state.get("recent_response_guard")
        if isinstance(previous, dict) and previous.get("expires_at", 0) > now:
            prev_dps = previous.get("delta_per_sec")
            if isinstance(prev_dps, (int, float)) and float(prev_dps) > delta_per_sec:
                return
        state["recent_response_guard"] = payload

    _update_system_state(update)
    logger.warning(
        "[ResponseGuard] strong short-term response recorded: "
        f"zone={zone_name} {plan_label}/{water_sec:.1f}s "
        f"H={pre_h:.1f}->{post_h:.1f} Δm={delta_m:+.3f}% "
        f"dps={delta_per_sec:.3f}%/s ttl={RESPONSE_GUARD_TTL_SEC/3600:.1f}h"
    )


def _bootstrap_high_zone_kp(cfg: ConfigManager) -> Optional[float]:
    """Initialize K_P_HIGH from robust, accepted high-zone trial measurements."""
    existing = cfg.get("K_P_HIGH")
    measured = []
    high_outcomes = []
    for item in _load_irrigation_trials()[-200:]:
        if item.get("zone") == "high" and item.get("status") in {"accepted", "rejected"}:
            high_outcomes.append(item)
        if item.get("status") != "accepted" or item.get("zone") != "high":
            continue
        water_sec = item.get("water_sec")
        delta_m = item.get("delta_m")
        if not isinstance(water_sec, (int, float)) or not isinstance(delta_m, (int, float)):
            continue
        if float(water_sec) <= 0 or float(delta_m) <= 0:
            continue
        measured.append(float(delta_m) / float(water_sec))
    has_existing = isinstance(existing, (int, float)) and float(existing) > 0
    if not has_existing and len(measured) < 3:
        logger.info(
            "[Profile] K_P_HIGH bootstrap waiting for 3 accepted high-zone trials; have=%s",
            len(measured),
        )
        return None
    value = (
        float(existing) if has_existing
        else round(max(0.001, min(5.0, float(median(measured[-20:])))), 5)
    )
    if not has_existing:
        cfg.update({"K_P_HIGH": value})
        _record_parameter_change(
            "K_P_HIGH",
            None,
            value,
            "auto_bootstrap_high_zone_kp",
            "decision_brain",
            {
                "measured_count": len(measured),
                "recent_high_outcomes": len(high_outcomes),
                "source": "bootstrap_high_zone_kp",
            },
        )
    profile = _load_irrigation_profile()
    high = profile["zones"]["high"]
    if has_existing and int(high.get("trial_reconciliation_version") or 0) >= 1:
        return value
    if int(high.get("trial_reconciliation_version") or 0) < 1:
        stable_success = 0
        failure_count = 0
        strategies = {}
        for item in high_outcomes:
            if item.get("status") == "accepted":
                stable_success += 1
                failure_count = max(failure_count - 1, 0)
            else:
                failure_count += 1
                if item.get("reason") in {
                    "post_h_above_fc_tolerance", "delta_m_above_physical_max",
                }:
                    stable_success = 0
                else:
                    stable_success = max(stable_success - 1, 0)
            key = str(item.get("plan_label") or f"{float(item.get('water_sec') or 0):.1f}s")
            stat = strategies.setdefault(key, {
                "attempts": 0, "accepted": 0, "rejected": 0, "emergency": 0,
                "delta_values": [], "last_reason": "", "last_water_sec": None,
                "last_updated": None,
            })
            stat["attempts"] += 1
            stat[item["status"]] += 1
            if isinstance(item.get("delta_m"), (int, float)):
                stat["delta_values"].append(float(item["delta_m"]))
            stat["last_reason"] = item.get("reason") or ""
            stat["last_water_sec"] = item.get("water_sec")
            stat["last_updated"] = item.get("timestamp")
        for stat in strategies.values():
            values = stat.pop("delta_values")
            stat["avg_delta_m"] = round(sum(values) / len(values), 3) if values else None
        high["stable_success"] = stable_success
        high["failure_count"] = failure_count
        high["strategy_stats"] = strategies
        high["trial_reconciliation_version"] = 1
    high["kp_ema"] = value
    high["last_reason"] = "bootstrapped_from_high_zone_trials"
    high["last_updated"] = time.time()
    _save_irrigation_profile(profile)
    logger.warning(
        "[Profile] K_P_HIGH initialized from %s accepted high-zone trials: %.5f",
        len(measured), value,
    )
    return value


def _predictor_profile(cfg: ConfigManager, reading: "SensorReading") -> dict:
    """Build a bounded, per-request device profile; never copy raw history."""
    zone_name = _humidity_zone_name(cfg, reading.humidity)
    state = _load_system_state()
    irrigation_profile = _load_irrigation_profile()
    zone_stats = irrigation_profile.get("zones", {}).get(zone_name, {})
    trials = _load_irrigation_trials()
    terminal = [
        item for item in trials[-100:]
        if item.get("status") in {"accepted", "rejected", "emergency"}
    ]
    last_delta = None
    if terminal:
        value = terminal[-1].get("delta_m")
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            last_delta = round(float(value), 4)
    peak_errors = [
        float(item["peak_error_observed"])
        for item in terminal
        if isinstance(item.get("peak_error_observed"), (int, float))
        and math.isfinite(float(item["peak_error_observed"]))
    ][-50:]
    suspect = state.get("water_delivery_suspect") or {}
    air_fallback = state.get("air_humidity_fallback") or {}
    return {
        "fc": round(float(cfg.FC), 5),
        "target_low": round(float(cfg.TARGET_LOW), 5),
        "kp": round(float(cfg.K_P), 5),
        "kp_low": cfg.get("K_P_LOW"),
        "kp_mid": cfg.get("K_P_MID"),
        "kp_high": cfg.get("K_P_HIGH"),
        "zone": zone_name,
        "recent_slope": (
            round(float(reading.recent_slope), 5)
            if reading.recent_slope is not None and math.isfinite(float(reading.recent_slope)) else None
        ),
        "last_delta_m": last_delta,
        "zone_stats": {
            "stable_success": int(zone_stats.get("stable_success") or 0),
            "failure_count": int(zone_stats.get("failure_count") or 0),
            "max_allowed_sec": float(zone_stats.get("max_allowed_sec") or 0.0),
        },
        "history": {
            "accepted_samples": sum(item.get("status") == "accepted" for item in terminal),
            "rejected_samples": sum(item.get("status") == "rejected" for item in terminal),
            "peak_error_samples": len(peak_errors),
            "peak_error_median": round(float(median(peak_errors)), 4) if peak_errors else None,
        },
        "water_delivery_suspect": {
            "active": bool(suspect.get("active", False)),
            "reason": str(suspect.get("reason") or "")[:128],
        },
        "air_humidity_fallback": {
            "active": bool(air_fallback.get("active", False)),
            "source": str(air_fallback.get("source") or "")[:128],
            "age_sec": (
                round(float(air_fallback["age_sec"]), 1)
                if isinstance(air_fallback.get("age_sec"), (int, float)) else None
            ),
        },
    }


def _phase2_peak_reliability_summary() -> dict:
    """Summarize recent Phase2 peak forecast bias from settled irrigation trials."""
    errors: list[float] = []
    recent_trials = [
        item for item in _load_irrigation_trials()[-200:]
        if item.get("status") in {"accepted", "rejected", "emergency"}
        and isinstance(item.get("peak_error_observed"), (int, float))
    ]
    for item in recent_trials:
        try:
            value = float(item["peak_error_observed"])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            errors.append(value)
    errors = errors[-30:]
    if not errors:
        return {
            "sample_count": 0,
            "median_error": None,
            "mean_error": None,
            "overestimate_rate": 0.0,
            "severe_overestimate_count": 0,
            "overestimate_suspect": False,
            "confidence": 0.0,
            "note": "no_settled_phase2_peak_samples",
        }

    over_threshold = 1.0
    severe_threshold = 1.5
    over_count = sum(1 for value in errors if value <= -over_threshold)
    severe_count = sum(1 for value in errors if value <= -severe_threshold)
    over_rate = over_count / len(errors)
    median_error = float(median(errors))
    mean_error = sum(errors) / len(errors)
    confidence = min(1.0, len(errors) / 10.0)
    suspect = (
        len(errors) >= 3
        and (
            median_error <= -over_threshold
            or over_rate >= 0.6
            or severe_count >= 2
        )
    )
    return {
        "sample_count": len(errors),
        "median_error": round(median_error, 4),
        "mean_error": round(mean_error, 4),
        "overestimate_rate": round(over_rate, 4),
        "severe_overestimate_count": severe_count,
        "overestimate_suspect": bool(suspect),
        "confidence": round(confidence, 4),
        "note": "negative_error_means_phase2_overestimated_peak",
    }


# ===========================================================================
# ① 数据结构定义
# ===========================================================================

class ZoneStatus(Enum):
    """土壤湿度所处区域。"""
    SAFE_SLEEP   = auto()   # 安全区，直接放行休眠
    BATTLE_ZONE  = auto()   # 战区，唤醒大模型推理
    EMERGENCY    = auto()   # 紧急区，跳过推理；补水时长见 EMERGENCY_WATER_SEC（非硬上限满灌）
    SOAK_PENDING = auto()   # 渗透等待中，本轮跳过浇水决策（Fix-A）
    SENSOR_STALE = auto()   # 传感器有效读数超时，本轮禁止自动浇水


@dataclass
class PendingSoak:
    """
    Fix-A：浇水后的"渗透任务"哨兵，取代 time.sleep(1800) 阻塞。

    浇水执行完毕后立即创建并挂在 DecisionBrain._pending_soak 上。
    主循环每 5 分钟检测一次：
      · 渗透未完成 → 返回 SOAK_PENDING，跳过本轮浇水决策
      · 渗透完成   → 采样 + K_p EMA + pattern_memory 写入，清除哨兵

    字段：
      water_sec          本次浇水秒数（用于 K_p 计算）
      pre_humidity       浇水前湿度（用于 Δm 计算）
      pump_end_time      水泵停止的 monotonic 时间戳（与 ready/remaining_sec 同轴）
      soak_duration      需要等待的总渗透秒数
      is_emergency       是否为紧急满灌（紧急满灌不写入 pattern_memory）
      expected_delta_m   Layer 5 选定方案的预测 Δm（轨迹终点 - pre_humidity），
                         用于惩罚系数计算；None 表示无预测（物理外推场景）
      plan_label        本次执行采用的策略标签，用于 trial/profile 分策略统计
      observation_marks 已记录的中途观测秒点，避免 5/15 分钟重复写入 trial log
    """
    water_sec:        float
    pre_humidity:     float
    pump_end_time:    float
    soak_duration:    float
    is_emergency:     bool  = False
    expected_delta_m: Optional[float] = None
    plan_label:       str = ""
    emergency_interrupts: int = 0
    last_interrupt_time: Optional[float] = None
    observation_marks: list[int] = field(default_factory=list)
    request_id: Optional[str] = None
    device_code: Optional[str] = None
    prediction_zone: Optional[str] = None
    predicted_peak: Optional[float] = None
    raw_model_peak: Optional[float] = None
    predicted_h12: Optional[float] = None
    predicted_trajectory: list[float] = field(default_factory=list)
    shadow_predicted_peak: Optional[float] = None
    shadow_predicted_h12: Optional[float] = None
    shadow_predicted_trajectory: list[float] = field(default_factory=list)
    predicted_minutes_to_peak: Optional[float] = None
    prediction_horizon_steps: Optional[int] = None
    prediction_timestamp: Optional[float] = None
    observed_peak: Optional[float] = None
    watering_window: dict[str, Any] = field(default_factory=dict)
    forced_exploration: bool = False
    forced_exploration_reason: str = ""

    @property
    def ready(self) -> bool:
        """渗透时间是否已到。用 monotonic 避免 NTP 跳变干扰等待判断。"""
        return time.monotonic() >= self.pump_end_time + self.soak_duration

    @property
    def remaining_sec(self) -> float:
        """距渗透完成还剩多少秒。"""
        return max(0.0, self.pump_end_time + self.soak_duration - time.monotonic())


@dataclass
class SensorReading:
    """一次传感器采集的完整快照。"""
    humidity:    float
    temperature: float
    ec_raw:      float
    ec_norm:     float = 0.0
    vpd:         float = 0.0
    timestamp:   float = field(default_factory=time.monotonic)
    soil_timestamp: Optional[float] = None
    stale_age_sec: float = 0.0
    stale_bad_rows: int = 0
    sensor_stale: bool = False
    sensor_stale_hard: bool = False
    recent_slope: Optional[float] = None

    def __post_init__(self):
        if self.ec_norm == 0.0:
            self.ec_norm = self.ec_raw


@dataclass
class ActionPlan:
    """一个浇水方案候选。"""
    label:                str
    water_sec:            float
    predicted_trajectory: list[float] = field(default_factory=list)
    cost_J:               float = math.inf
    selected:             bool  = False
    ceiling_violation_ratio: float = 0.0
    request_id: Optional[str] = None
    device_code: Optional[str] = None
    prediction_zone: Optional[str] = None
    predicted_peak: Optional[float] = None
    raw_model_peak: Optional[float] = None
    predicted_h12: Optional[float] = None
    shadow_predicted_peak: Optional[float] = None
    shadow_predicted_h12: Optional[float] = None
    shadow_predicted_trajectory: list[float] = field(default_factory=list)
    predicted_minutes_to_peak: Optional[float] = None
    prediction_horizon_steps: Optional[int] = None
    prediction_timestamp: Optional[float] = None
    phase2_soft_risk: bool = False
    phase2_calibration_probe: bool = False
    phase2_soft_risk_reason: Optional[str] = None
    phase2_soft_risk_penalty: float = 0.0
    phase2_physical_peak: Optional[float] = None
    phase2_veto_class: Optional[str] = None
    watering_window_level: Optional[str] = None
    watering_window_reason: Optional[str] = None
    watering_window_penalty: float = 0.0
    watering_window_sample_context: Optional[str] = None
    forced_exploration: bool = False
    forced_exploration_reason: Optional[str] = None


@dataclass
class DecisionResult:
    """一次完整决策循环的输出。"""
    zone:        ZoneStatus
    chosen_plan: Optional[ActionPlan]
    action_sec:  float
    reading:     SensorReading
    notes:       str = ""


# ===========================================================================
# ② Layer 0 —— 传感器读取与物理量计算
# ===========================================================================

class SensorLayer:
    """
    Layer 0: 双源传感器读取 + VPD / EC_norm 计算。

    数据来源：
      · SOIL_CSV（与 phase1_test/water-test-soil3.py 的 DATA_PATH 一致，如 water_test_soil3.csv）
            每 ~5 分钟一行：土壤湿度(%)、温度(°C)、电导率 等
      · openGauss soil_data.soil_sensor_readings[soil3].air_humidity
            空气相对湿度（%），优先使用；
      · air_humidity.csv —— 仅作为 GaussDB 空气湿度异常时的兼容 fallback

    VPD：使用空气湿度 RH_air（优先来自 GaussDB air_humidity）与气温 T（与 soil3 脚本一致，取土壤 CSV 最新行「温度(°C)」，
         由本轮 _read_soil_raw 得到的土壤温度列传入，不依赖 air CSV 中的 temperature 列）。
    """

    # 两个数据文件的路径（固定，不参与进化）
    SOIL_CSV = Path("/root/data/water_test_soil3.csv")
    AIR_CSV  = Path("/root/data/air_humidity.csv")

    HUMIDITY_RANGE = (0.0, 100.0)
    TEMP_RANGE_C   = (0.0, 45.0)
    EC_RANGE       = (0.0, 5000.0)
    VPD_RANGE_KPA  = (0.0, 8.0)

    def __init__(self, cfg: ConfigManager):
        self.cfg = cfg
        # 缓存上一次成功读取的空气湿度，应对 30 分钟文件未更新的情况
        self._last_air_rh:   float = 60.0   # 初始保守默认值
        self._last_air_temp: float = 25.0

    def read(self, require_fresh: bool = False) -> SensorReading:
        """
        读取双源传感器，计算 EC_norm 和 VPD，返回 SensorReading 快照。

        空气数据 30 分钟才更新一次，两者时间戳不对齐是正常的；
        取最新一行即可，不需要严格时间对齐。
        """
        # ── 读取土壤传感器（每 5 分钟更新，每次都取最新行）
        humidity, soil_temp, ec_raw, soil_ts, skipped = self._read_soil_raw()
        stale_age = max(0.0, time.time() - soil_ts) if soil_ts else 0.0
        warn_sec = float(self.cfg.get_constant("SOIL_STALE_WARN_SEC") or 900)
        hard_sec = float(self.cfg.get_constant("SOIL_STALE_HARD_SEC") or 18000)
        is_stale = stale_age >= warn_sec
        is_hard_stale = stale_age >= hard_sec
        if is_stale:
            logger.warning(
                f"[Layer0] 土壤有效读数已过期 {stale_age/60:.1f} 分钟，"
                f"末尾异常行 {skipped}。最后有效读数仍会标记为 stale。"
            )
            self._maybe_alert_stale_sensor(stale_age, skipped, humidity, soil_ts)
        if require_fresh and is_hard_stale:
            raise RuntimeError(
                f"土壤传感器有效读数超时 {stale_age/3600:.2f}h，拒绝用于渗透结算。"
            )

        # ── 空气湿度优先来自 GaussDB；VPD 用气温 = 与 water-test-soil3 同源（土壤 CSV「温度(°C)」）
        air_rh, air_temp = self._read_air_raw(soil_temp_for_vpd=soil_temp)

        # ── 物理量计算
        ec_norm = self._normalize_ec(ec_raw, soil_temp)
        vpd     = self._calc_vpd(air_temp, air_rh)   # Fix-1：使用空气湿度

        reading = SensorReading(
            humidity=humidity,
            temperature=soil_temp,
            ec_raw=ec_raw,
            ec_norm=ec_norm,
            vpd=vpd,
            soil_timestamp=soil_ts,
            stale_age_sec=stale_age,
            stale_bad_rows=skipped,
            sensor_stale=is_stale,
            sensor_stale_hard=is_hard_stale,
            recent_slope=self._recent_humidity_slope(),
        )
        logger.info(
            f"[Layer0] 土壤: H={humidity:.1f}%  T_soil={soil_temp:.1f}°C  "
            f"EC_raw={ec_raw:.3f}  EC_norm={ec_norm:.3f}  |  "
            f"空气: RH={air_rh:.1f}%  T_air={air_temp:.1f}°C  VPD={vpd:.3f}kPa"
        )
        return reading

    # ------------------------------------------------------------------
    # 土壤数据读取（5 分钟采样，主循环同频）
    # ------------------------------------------------------------------

    def _read_soil_raw(self) -> tuple[float, float, float, float, int]:
        """从 openGauss 统一传感器表读取 soil3 最新一条有效土壤数据。"""
        sql = (
            "SELECT id, recv_time, temp, humidity, ec FROM soil_sensor_readings "
            "WHERE device_code='soil3' "
            "AND temp IS NOT NULL AND humidity IS NOT NULL AND ec IS NOT NULL "
            "ORDER BY recv_time DESC, id DESC LIMIT 1;"
        )
        cmd = "gsql -d soil_data -p 7654 -t -A -F \",\" -c " + repr(sql)
        try:
            result = subprocess.run(
                ["su", "-", "opengauss", "-c", cmd],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            out = result.stdout.strip()
            if not out:
                raise RuntimeError("openGauss soil_sensor_readings[soil3] 表中没有有效土壤记录")
            parts = out.split(",")
            if len(parts) < 5:
                raise RuntimeError(f"openGauss 返回格式异常: {out}")
            _row_id, recv_time, temp, humidity, ec_raw = parts[:5]
            humidity_f = float(humidity)
            temp_f = float(temp)
            ec_f = float(ec_raw)
            if not (
                max(self.HUMIDITY_RANGE[0], 5.0) < humidity_f <= self.HUMIDITY_RANGE[1]
                and self.TEMP_RANGE_C[0] <= temp_f <= self.TEMP_RANGE_C[1]
                and self.EC_RANGE[0] <= ec_f <= self.EC_RANGE[1]
            ):
                def update(state):
                    state["sensor_fault"] = {
                        "active": True,
                        "kind": "invalid_soil_reading",
                        "last_humidity": humidity_f,
                        "last_temperature": temp_f,
                        "last_ec": ec_f,
                        "last_recv_time": recv_time,
                        "updated_at": time.time(),
                    }
                _update_system_state(update)
                raise RuntimeError(
                    f"openGauss soil_sensor_readings[soil3] 最新有效记录超出物理范围: "
                    f"H={humidity_f}, T={temp_f}, EC={ec_f}"
                )
            soil_ts = self._parse_soil_timestamp(recv_time)
            return humidity_f, temp_f, ec_f, soil_ts, 0
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"openGauss soil_sensor_readings[soil3] 读取失败: {e}") from e

    @staticmethod
    def _parse_soil_timestamp(raw: Any) -> float:
        if raw is None:
            return time.time()
        text = str(raw).strip()
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                from datetime import datetime
                return datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
        return time.time()

    def _recent_humidity_slope(self) -> Optional[float]:
        """Return recent humidity slope in % per 5-minute step from sensor_log."""
        try:
            rows = []
            for row in read_csv_dicts_locked(FILE_PATHS["SENSOR_LOG"]):
                try:
                    rows.append(float(row["humidity"]))
                except (KeyError, ValueError, TypeError):
                    continue
            if len(rows) < 6:
                return None
            series = rows[-6:]
            x_mean = (len(series) - 1) / 2.0
            y_mean = sum(series) / len(series)
            denom = sum((i - x_mean) ** 2 for i in range(len(series)))
            if denom == 0:
                return None
            return sum((i - x_mean) * (series[i] - y_mean) for i in range(len(series))) / denom
        except OSError:
            return None

    def _maybe_alert_stale_sensor(
        self, stale_age: float, skipped: int, humidity: float, soil_ts: Optional[float]
    ) -> None:
        alert_sec = float(self.cfg.get_constant("SOIL_STALE_ALERT_SEC") or 18000)
        if stale_age < alert_sec:
            return
        state = _load_system_state()
        sensor_fault = state.get("sensor_fault", {})
        last_sent = float(sensor_fault.get("last_alert_sent_at") or 0.0)
        repeat_sec = float(self.cfg.get_constant("SOIL_STALE_ALERT_REPEAT_SEC") or 21600)
        now = time.time()
        if now - last_sent < repeat_sec:
            return
        subject = "soil3 报警：土壤传感器数据超时"
        body = (
            f"智养中心检测到 soil3 土壤传感器数据异常。\n\n"
            f"报警类型：土壤传感器有效数据超时\n"
            f"超时时长：{stale_age/3600:.2f} 小时\n"
            f"最后有效湿度：{humidity:.1f}%\n"
            f"最后有效时间戳：{soil_ts}\n"
            f"连续异常行数：{skipped}\n\n"
            f"系统处置：已进入传感器故障保护，禁止使用旧读数自动浇水。\n"
            f"建议操作：检查 soil3 传感器供电、接线、ESP 上报和 MQTT/CSV 数据链路。"
        )
        sent = _send_alert_email(subject, body)
        def update(state):
            state["sensor_fault"] = {
                "active": True,
                "kind": "soil_stale",
                "last_valid_soil_ts": soil_ts,
                "stale_age_sec": stale_age,
                "bad_rows": skipped,
                "last_humidity": humidity,
                "last_alert_sent_at": now if sent else last_sent,
            }
        _update_system_state(update)

    # ------------------------------------------------------------------
    # 空气数据读取（30 分钟采样，失败时使用缓存）
    # ------------------------------------------------------------------

    def _read_air_raw(self, soil_temp_for_vpd: float) -> tuple[float, float]:
        """
        优先从 openGauss soil_sensor_readings.air_humidity 读取空气相对湿度。

        空气湿度异常时不应阻塞 phase3。容错顺序：
          1. GaussDB 最新非空 air_humidity；
          2. 旧 air_humidity.csv 的最新有效空气湿度；
          3. 历史最后有效空气湿度（允许过期，但写入 fallback 状态）；
          4. 保守默认值 AIR_HUMIDITY_FALLBACK_DEFAULT。

        VPD 仍必须用 RH_air，不得用土壤含水率；气温暂取本轮土壤温度。
        """
        air_temp = float(soil_temp_for_vpd)
        stale_sec = float(self.cfg.get_constant("AIR_HUMIDITY_STALE_WARN_SEC") or 21600)
        default_rh = float(self.cfg.get_constant("AIR_HUMIDITY_FALLBACK_DEFAULT") or 63.5)

        def use_fallback(source: str, reason: str, age_sec: Optional[float] = None) -> tuple[float, float]:
            rh = self._last_air_rh if 0.0 <= float(self._last_air_rh) <= 100.0 else default_rh
            self._last_air_rh = rh
            self._last_air_temp = air_temp
            self._mark_air_fallback(True, rh, air_temp, source, reason, age_sec)
            logger.warning(
                f"[Layer0] 空气湿度使用历史 fallback: RH={rh:.1f}% "
                f"source={source} reason={reason} T_air={air_temp:.1f}°C"
            )
            return rh, air_temp

        gauss_air = self._read_latest_gauss_air_humidity()
        if gauss_air is not None:
            last_rh, air_ts = gauss_air
            age_sec = max(0.0, time.time() - air_ts) if air_ts else None
            self._last_air_rh = last_rh
            self._last_air_temp = air_temp
            if age_sec is not None and age_sec > stale_sec:
                self._mark_air_fallback(
                    True,
                    last_rh,
                    air_temp,
                    "historical_gaussdb_air_humidity",
                    f"stale_age_sec={age_sec:.0f}",
                    age_sec,
                )
                logger.warning(
                    f"[Layer0] GaussDB 空气湿度已过期 {age_sec/3600:.1f}h，"
                    f"暂用历史 RH={last_rh:.1f}% 计算 VPD。"
                )
            else:
                self._mark_air_fallback(
                    False,
                    last_rh,
                    air_temp,
                    "gaussdb_air_humidity",
                    "fresh_or_no_timestamp",
                    age_sec,
                )
                logger.info(
                    f"[Layer0] 空气湿度: 使用 GaussDB air_humidity={last_rh:.1f}%"
                )
            return last_rh, air_temp

        if not self.AIR_CSV.exists():
            return use_fallback("cache_or_default", f"file_missing:{self.AIR_CSV}")

        try:
            import csv
            with open(self.AIR_CSV, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if not rows:
                return use_fallback("cache_or_default", "file_empty")

            last_valid = None
            last_rh = None
            for row in reversed(rows):
                try:
                    last_rh = self._extract_air_rh(row)
                    last_valid = row
                    break
                except (KeyError, TypeError, ValueError):
                    continue
            if last_valid is None or last_rh is None:
                return use_fallback("cache_or_default", "no_valid_air_humidity_row")

            air_ts = self._parse_air_timestamp(last_valid)
            age_sec = max(0.0, time.time() - air_ts) if air_ts else None
            self._last_air_rh = last_rh
            self._last_air_temp = air_temp

            if age_sec is not None and age_sec > stale_sec:
                self._mark_air_fallback(
                    True,
                    last_rh,
                    air_temp,
                    "historical_air_humidity",
                    f"stale_age_sec={age_sec:.0f}",
                    age_sec,
                )
                logger.warning(
                    f"[Layer0] 空气湿度已过期 {age_sec/3600:.1f}h，"
                    f"暂用历史 RH={last_rh:.1f}% 计算 VPD。"
                )
            else:
                self._mark_air_fallback(
                    False,
                    last_rh,
                    air_temp,
                    "air_humidity_csv",
                    "fresh_or_no_timestamp",
                    age_sec,
                )
            return last_rh, air_temp

        except (OSError, csv.Error) as e:
            return use_fallback("cache_or_default", f"read_error:{e}")

    def _read_latest_gauss_air_humidity(self) -> Optional[tuple[float, float]]:
        sql = (
            "SELECT air_humidity, recv_time FROM soil_sensor_readings "
            "WHERE device_code='soil3' AND air_humidity IS NOT NULL "
            "ORDER BY recv_time DESC, id DESC LIMIT 1;"
        )
        cmd = "gsql -d soil_data -p 7654 -t -A -F \",\" -c " + repr(sql)
        try:
            result = subprocess.run(
                ["su", "-", "opengauss", "-c", cmd],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            out = result.stdout.strip()
            if not out:
                return None
            parts = out.split(",")
            if len(parts) < 2:
                raise RuntimeError(f"openGauss air_humidity 返回格式异常: {out}")
            air_rh = float(parts[0])
            if not (0.0 <= air_rh <= 100.0):
                raise ValueError(f"空气湿度读数异常: {air_rh}%")
            return air_rh, self._parse_soil_timestamp(parts[1])
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
            logger.warning(f"[Layer0] GaussDB air_humidity 读取失败，回退旧空气湿度链路: {e}")
            return None

    @staticmethod
    def _extract_air_rh(row: dict) -> float:
        for key in ("air-humidity", "air_humidity", "humidity", "RH", "rh"):
            if key in row and row[key] not in (None, ""):
                rh = float(row[key])
                if 0.0 <= rh <= 100.0:
                    return rh
                raise ValueError(f"air humidity out of range: {rh}")
        values = [v for v in row.values() if v not in (None, "")]
        if values:
            rh = float(values[-1])
            if 0.0 <= rh <= 100.0:
                return rh
        raise KeyError("air humidity column not found")

    @staticmethod
    def _parse_air_timestamp(row: dict) -> Optional[float]:
        for key in ("timestamp", "time", "datetime", "接收时间", "recv_time"):
            raw = row.get(key)
            if raw:
                text = str(raw).strip()
                break
        else:
            first = next(iter(row.values()), None)
            text = str(first).strip() if first else ""
        if not text:
            return None
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                from datetime import datetime
                return datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
        return None

    @staticmethod
    def _mark_air_fallback(
        active: bool,
        rh: float,
        air_temp: float,
        source: str,
        reason: str,
        age_sec: Optional[float],
    ) -> None:
        def update(state):
            state["air_humidity_fallback"] = {
                "active": active,
                "rh": round(float(rh), 3),
                "air_temp": round(float(air_temp), 3),
                "source": source,
                "reason": reason,
                "age_sec": round(float(age_sec), 1) if age_sec is not None else None,
                "updated_at": time.time(),
            }
        _update_system_state(update)

    # ------------------------------------------------------------------
    # 物理量计算
    # ------------------------------------------------------------------

    def _normalize_ec(self, ec_raw: float, temperature: float) -> float:
        """
        EC 温度补偿：EC_norm = EC_raw / (1 + coeff × (T - T_ref))
        剥离温度对读数的影响，还原真实盐分浓度。

        Align-3：EC_NORM_REF_TEMP / EC_TEMP_COEFF 是工程常量，
                 从 get_constant() 读取，不走可进化参数通道。
        """
        ref_temp = self.cfg.get_constant("EC_NORM_REF_TEMP") or 25.0
        coeff    = self.cfg.get_constant("EC_TEMP_COEFF")    or 0.02
        denom    = 1.0 + coeff * (temperature - ref_temp)
        return ec_raw / max(denom, 0.1)

    @staticmethod
    def _calc_vpd(air_temp: float, air_rh: float) -> float:
        """
        VPD（蒸汽压亏缺）kPa，Tetens 公式。

        Fix-1：参数明确命名为 air_temp / air_rh，强制语义绑定，
               防止未来被错误传入土壤湿度。

        公式：es(T) = 0.6108 × exp(17.27T / (T+237.3))
              VPD   = es × (1 - RH_air/100)
        """
        es  = 0.6108 * math.exp(17.27 * air_temp / (air_temp + 237.3))
        vpd = max(es * (1.0 - air_rh / 100.0), 0.0)
        return min(vpd, SensorLayer.VPD_RANGE_KPA[1])


# ===========================================================================
# ③ Layer 1-2 —— 物理天花板 + 安全态势评估
# ===========================================================================

class PhysicsLayer:
    """
    Layer 1: 计算本次允许浇水的最大秒数（物理天花板）。
    Layer 2: 根据当前湿度判断所处区域 (ZoneStatus)。
    """

    def __init__(self, cfg: ConfigManager):
        self.cfg = cfg

    def calc_water_ceiling(self, current_humidity: float) -> float:
        """
        浇水秒数天花板 = (FC - H) / K_p，受硬件硬上限约束。

        Align-1：WATER_SEC_MAX_HARD 是 B 类硬件常量，
                 通过 get_constant() 读取，不走进化参数通道。
        """
        fc      = self.cfg.FC
        zone_name = _humidity_zone_name(self.cfg, current_humidity)
        kp      = _zone_kp(self.cfg, zone_name)
        gap     = max(fc - current_humidity, 0.0)
        sec_max = gap / max(kp, 1e-6)

        # Align-1
        hard_cap = self.cfg.get_constant("WATER_SEC_MAX_HARD")
        ceiling  = min(sec_max, hard_cap)

        logger.debug(
            f"[Layer1] FC={fc}  H={current_humidity:.1f}  "
            f"K_p={kp:.4f}({zone_name})  raw_max={sec_max:.1f}s  ceiling={ceiling:.1f}s"
        )
        return ceiling

    def _hard_safety_low(self) -> tuple[float, float, float]:
        target_low = float(self.cfg.TARGET_LOW)
        target_gap = max(float(self.cfg.FC - target_low), 0.0)
        ratio = float(self.cfg.get_constant("HARD_SAFETY_GAP_RATIO") or 0.35)
        buffer_scale = max(float(self.cfg.get("BUFFER_SCALE", 1.0) or 1.0), 0.1)
        derived_line = round(target_low - target_gap * ratio / buffer_scale, 3)
        emergency_line = self._guard_hard_safety_low(derived_line, target_low, target_gap, ratio, buffer_scale)
        phase1_floor = emergency_line
        return emergency_line, phase1_floor, target_gap

    def _guard_hard_safety_low(
        self,
        derived_line: float,
        target_low: float,
        target_gap: float,
        ratio: float,
        buffer_scale: float,
    ) -> float:
        """Keep the dynamic hard line from drifting down on polluted evidence."""
        now = time.time()
        state = _load_system_state()
        exp = state.get("irrigation_style_experiment") if isinstance(state, dict) else {}
        guard = state.get("hard_safety_low_guard") if isinstance(state, dict) else {}
        exp = exp if isinstance(exp, dict) else {}
        guard = guard if isinstance(guard, dict) else {}

        previous = guard.get("effective_low")
        if previous is None:
            previous = exp.get("hard_safety_low")
        try:
            previous = float(previous) if previous is not None else None
        except (TypeError, ValueError):
            previous = None

        contamination_keys = (
            "water_delivery_suspect",
            "reservoir_empty_suspect",
            "low_wet_recovery_suspect",
            "sensor_fault",
        )
        contamination_reasons = []
        for key in contamination_keys:
            suspect = state.get(key) if isinstance(state, dict) else None
            if not isinstance(suspect, dict):
                continue
            active = bool(suspect.get("active"))
            cleared = bool(
                suspect.get("cleared_at")
                or suspect.get("repair_retest_completed_at")
                or suspect.get("repair_confirmed_at")
            )
            learning_quarantine = bool(suspect.get("exclude_from_learning")) and not cleared
            if active or learning_quarantine:
                contamination_reasons.append(key)

        effective = float(derived_line)
        guard_reason = "derived"
        if previous is not None and effective < previous:
            if contamination_reasons:
                effective = previous
                guard_reason = "freeze_downward_on_contaminated_state"
            else:
                max_down_per_day = float(
                    self.cfg.get_constant("HARD_SAFETY_DOWN_MAX_PER_DAY") or 1.0
                )
                last_ts = float(
                    guard.get("last_lowered_at")
                    or guard.get("updated_at")
                    or exp.get("updated_at")
                    or now
                )
                elapsed_days = max((now - last_ts) / 86400.0, 0.0)
                max_drop = max_down_per_day * elapsed_days
                limited = previous - max_drop
                if effective < limited:
                    effective = limited
                    guard_reason = "rate_limited_downward_shift"
        elif previous is not None and effective > previous:
            guard_reason = "raise_immediately"

        effective = round(float(effective), 3)
        lowered_now = previous is not None and effective < previous

        def update(current):
            current["hard_safety_low_guard"] = {
                "derived_low": round(float(derived_line), 3),
                "effective_low": effective,
                "previous_effective_low": round(float(previous), 3) if previous is not None else None,
                "target_low": round(float(target_low), 3),
                "target_gap": round(float(target_gap), 3),
                "ratio": round(float(ratio), 5),
                "buffer_scale": round(float(buffer_scale), 5),
                "reason": guard_reason,
                "contamination_reasons": contamination_reasons,
                "down_max_per_day": float(self.cfg.get_constant("HARD_SAFETY_DOWN_MAX_PER_DAY") or 1.0),
                "last_lowered_at": now if lowered_now else guard.get("last_lowered_at"),
                "updated_at": now,
            }

        try:
            _update_system_state(update)
        except Exception as e:
            logger.warning(f"[HardSafety] guard state update failed: {e}")

        if guard_reason != "derived":
            logger.warning(
                f"[HardSafety] dynamic line guarded: derived={derived_line:.3f}% "
                f"effective={effective:.3f}% reason={guard_reason} "
                f"contamination={contamination_reasons}"
            )
        return effective

    def assess_zone(self, reading: SensorReading) -> ZoneStatus:
        """
        Layer 2: 三区态势评估。

        判断顺序（从最严重到最轻）：
          ① H < TARGET_LOW              → EMERGENCY（跌穿枯萎线，短脉冲急救，秒数见 EMERGENCY_WATER_SEC）
          ② H > M_SAFE_SLEEP 且 EC 正常 → SAFE_SLEEP（安全区，直接放行休眠）
          ③ 其余                         → BATTLE_ZONE（1.7% 极限战区，唤醒大模型）

        Align-4：TARGET_LOW / EC_SALT_STRESS / RESPIRATION_LIMIT
                 新 config 已提供 @property，直接访问属性，不再用 cfg.get()。
        """
        h          = reading.humidity
        ec         = reading.ec_norm
        target_low = self.cfg.TARGET_LOW          # Align-4：属性访问
        safe_line  = self.cfg.M_SAFE_SLEEP        # 联动派生值，直接读属性
        ec_limit   = self.cfg.EC_SALT_STRESS      # Align-4：属性访问

        if reading.sensor_stale_hard:
            logger.critical(
                f"[Layer2] 土壤有效读数超时 {reading.stale_age_sec/3600:.2f}h，"
                f"进入传感器故障保护，本轮禁止自动浇水。"
            )
            return ZoneStatus.SENSOR_STALE

        vpd_start = float(self.cfg.get("VPD_SAFE_LINE_START", 1.2))
        vpd_boost_max = float(self.cfg.get("VPD_SAFE_LINE_MAX_BOOST", 0.8))
        if reading.vpd > vpd_start:
            safe_line = round(
                safe_line + min((reading.vpd - vpd_start) / max(2.5 - vpd_start, 0.1), 1.0) * vpd_boost_max,
                3,
            )

        if reading.recent_slope is not None and reading.recent_slope < self.cfg.SLOPE_STEEP:
            logger.warning(
                f"[Layer2] 近期失水斜率 {reading.recent_slope:.3f}%/step "
                f"< SLOPE_STEEP={self.cfg.SLOPE_STEEP}，提前进入战区。"
            )
            return ZoneStatus.BATTLE_ZONE

        emergency_line, phase1_floor, target_gap = self._hard_safety_low()

        if h < emergency_line:
            logger.warning(
                f"[Layer2] hard safety emergency: H={h:.1f}% < HARD_SAFETY_LOW={emergency_line:.1f}% "
                f"(TARGET_LOW={target_low}%, phase1_floor={phase1_floor:.1f}%)"
            )
            return ZoneStatus.EMERGENCY

        if h < target_low:
            logger.warning(
                f"[Layer2] below TARGET_LOW but above hard safety: H={h:.1f}% < "
                f"TARGET_LOW={target_low}%; keep BATTLE_ZONE for controlled refill/exploration."
            )
            return ZoneStatus.BATTLE_ZONE

        if h > safe_line and ec < ec_limit:
            logger.info(
                f"[Layer2]安全区. H={h:.1f}% > M_SAFE_SLEEP={safe_line}%  "
                f"EC={ec:.3f} < 盐分线={ec_limit:.3f}"
            )
            return ZoneStatus.SAFE_SLEEP

        logger.info(
            f"[Layer2] ⚔️  战区. H={h:.1f}%  "
            f"战区范围=[{target_low}, {safe_line}]%  宽度={(safe_line - target_low):.2f}%"
        )
        return ZoneStatus.BATTLE_ZONE


# ===========================================================================
# ④ Layer 3 —— 门卫拦截 + 考卷生成
# ===========================================================================

class GateLayer:
    """
    Layer 3: 门卫 + 考卷生成器。

    · aggressive:   0.85 × ceiling（接近物理天花板）
    · conservative: 0.40 × ceiling（保守预留缓冲）
    · reference:    pattern_memory 中相近湿度条件下的历史最优均值（对照组）
    """

    def __init__(self, cfg: ConfigManager):
        self.cfg = cfg

    def generate_exam(
        self,
        ceiling_sec: float,
        reading: SensorReading,
    ) -> list[ActionPlan]:
        ref_sec = self._get_reference_sec(reading.humidity)

        # 三组候选方案都必须遵守硬件下限
        water_min = self.cfg.get_constant("WATER_SEC_MIN")  # Align-2
        allowed_sec, zone_name, reason = self._allowed_exploration_sec(
            ceiling_sec, reading
        )
        def _cap(sec: float) -> float:
            if sec <= 0 or allowed_sec <= 0:
                return 0.0
            return max(min(round(sec, 1), allowed_sec), water_min)

        plans = [
            ActionPlan(
                label="observe",
                water_sec=0.0,
            ),
            ActionPlan(
                label="aggressive",
                water_sec=_cap(ceiling_sec * 0.85),
            ),
            ActionPlan(
                label="conservative",
                water_sec=_cap(ceiling_sec * 0.40),
            ),
            ActionPlan(
                label="reference",
                water_sec=_cap(ref_sec),
            ),
        ]
        if allowed_sec > water_min:
            plans.append(
                ActionPlan(
                    label=f"explore_{int(allowed_sec)}s_{zone_name}",
                    water_sec=allowed_sec,
                )
            )
        for plan in plans:
            if getattr(plan, "style_learning_arm", False):
                continue
            if plan.water_sec > 0 and ceiling_sec > 0 and plan.water_sec > ceiling_sec:
                plan.ceiling_violation_ratio = round((plan.water_sec - ceiling_sec) / ceiling_sec, 3)
            elif plan.water_sec > 0 and ceiling_sec <= 0:
                plan.ceiling_violation_ratio = 1.0
        logger.info(
            f"[Layer3] 考卷生成: zone={zone_name} allowed={allowed_sec:.1f}s ({reason}) | "
            + " | ".join(f"{p.label}={p.water_sec}s" for p in plans)
        )
        return plans

    def _allowed_exploration_sec(
        self,
        ceiling_sec: float,
        reading: SensorReading,
    ) -> tuple[float, str, str]:
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        hard_max = float(self.cfg.get_constant("EXPLORATION_MAX_SEC") or 5.0)
        min_fc_gap = float(self.cfg.get_constant("EXPLORATION_MIN_FC_GAP") or 5.0)
        daily_budget = int(self.cfg.get_constant("EXPLORATION_DAILY_BUDGET") or 0)
        profile = _load_irrigation_profile()
        zone_name = _humidity_zone_name(self.cfg, reading.humidity)
        zone = profile["zones"][zone_name]

        max_allowed = float(zone.get("max_allowed_sec") or water_min)
        stable = int(zone.get("stable_success") or 0)
        total_accepted = int(zone.get("total_accepted") or 0)
        stuck_rounds = int(zone.get("stuck_rounds_at_min") or 0)
        if total_accepted >= 10:
            max_allowed = max(max_allowed, 5.0)
        elif total_accepted >= 6:
            max_allowed = max(max_allowed, 4.0)
        if stable >= int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_5S") or 5):
            max_allowed = max(max_allowed, 5.0)
        elif stable >= int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_4S") or 3):
            max_allowed = max(max_allowed, 4.0)
        if stuck_rounds >= int(self.cfg.get_constant("EXPLORATION_STUCK_FORCE_ROUNDS") or 8):
            max_allowed = max(max_allowed, water_min + 1.0)

        daily_used = int(profile.get("daily_exploration", {}).get("used") or 0)
        fc_gap = self.cfg.FC - reading.humidity
        advice = _load_nightly_learning_advice()
        advice_arms = advice.get("arms") if isinstance(advice.get("arms"), dict) else {}
        advice_prefer = advice_arms.get("prefer") if isinstance(advice_arms.get("prefer"), dict) else {}
        strong_preferred = float(advice_prefer.get("strong_pulse") or 0.0) > 0.0
        large_preferred = float(advice_prefer.get("large_pulse") or 0.0) > 0.0
        if advice.get("active") and zone_name == "low":
            strong_floor = float(
                self.cfg.get_constant("STYLE_PULSE_STRONG_SEC")
                or self.cfg.get_constant("BUDGET_PULSE_STRONG_SEC")
                or 9.0
            )
            large_floor = float(
                self.cfg.get_constant("STYLE_PULSE_LARGE_SEC")
                or self.cfg.get_constant("WATER_SEC_MAX_HARD")
                or 10.0
            )
            if strong_preferred and ceiling_sec >= strong_floor:
                hard_max = max(hard_max, strong_floor)
            if large_preferred and ceiling_sec >= large_floor:
                hard_max = max(hard_max, large_floor)
        reason = (
            f"stable={stable}, total_accepted={total_accepted}, "
            f"stuck_rounds={stuck_rounds}, failures={zone.get('failure_count', 0)}"
        )
        if daily_used >= daily_budget:
            max_allowed = min(max_allowed, water_min)
            reason += ", daily_budget_exhausted"
        if fc_gap < min_fc_gap:
            # FC gap is a caution signal, not a permanent 3s lock.  Style
            # identification still needs medium-pulse evidence when the
            # physics ceiling is safe, otherwise the system regresses into
            # repeated micro-pulses and cannot compare watering styles.
            medium_floor = float(
                self.cfg.get_constant("STYLE_PULSE_MEDIUM_SEC")
                or self.cfg.get_constant("BUDGET_PULSE_MEDIUM_SEC")
                or 6.0
            )
            style_profile = profile.get("style_experiment") or {}
            style_current = style_profile.get("current") if isinstance(style_profile, dict) else {}
            style_mode = str((style_current or {}).get("mode") or "")
            medium_preferred = float(advice_prefer.get("medium_pulse") or 0) > 0
            if style_mode == "wet_hold" and medium_preferred and ceiling_sec >= medium_floor:
                max_allowed = max(max_allowed, medium_floor)
                reason += (
                    f", fc_gap={fc_gap:.1f}<{min_fc_gap:.1f}"
                    f":medium_style_probe_allowed"
                )
            else:
                max_allowed = min(max_allowed, water_min)
                reason += f", fc_gap={fc_gap:.1f}<{min_fc_gap:.1f}"
        if advice.get("active") and zone_name == "low" and (strong_preferred or large_preferred):
            reason += ", nightly_advice_lifts_exploration_cap"

        allowed = min(max_allowed, hard_max, max(ceiling_sec, 0.0))
        if allowed < water_min:
            allowed = water_min if ceiling_sec > 0 else 0.0
        return round(allowed, 1), zone_name, reason

    def _get_reference_sec(self, current_humidity: float) -> float:
        """
        从 pattern_memory 查找历史最优浇水秒数（对照组）。

        任务4：引入综合代价评分，替代原来纯湿度差排序。
        三项惩罚相加，越低越值得参考：

          · 湿度物理差异  h_diff
                当前湿度与记录湿度的绝对差（%），差异越大越不可信

          · 时间衰减惩罚  time_penalty = days_old × 0.1
                每过一天，等效于增加 0.1% 的湿度误差惩罚。
                30 天前的记录 = 额外 3% 的惩罚，半年前的几乎不会被选中。
                物理含义：根系生长导致 K_p 漂移，旧数据对当前环境的代表性递减。

          · 质量评分惩罚  quality_penalty = (1 - quality_score) × 2.0
                quality_score=1.0（完美）→ 惩罚 0
                quality_score=0.5        → 惩罚 1.0
                quality_score=0.0（最差）→ 惩罚 2.0

        取综合代价最低的 Top 3 求均值，而非原来的 Top 5 纯湿度排序。
        """
        memory    = _load_pattern_memory()
        water_min = self.cfg.get_constant("WATER_SEC_MIN")

        if not memory:
            return water_min * 3

        now = time.time()
        zone_name = _humidity_zone_name(self.cfg, current_humidity)
        zone_memory = [
            r for r in memory
            if r.get("zone") == zone_name or (
                "zone" not in r and _humidity_zone_name(self.cfg, float(r.get("humidity", current_humidity))) == zone_name
            )
        ]
        candidates = zone_memory if len(zone_memory) >= 3 else memory

        def _score(record: dict) -> float:
            evidence = classify_trial(record)
            if evidence.tier == HARD_INVALID:
                return float("inf")
            h_diff          = abs(record.get("humidity", 50.0) - current_humidity)
            days_old        = max(0.0, now - record.get("timestamp", now)) / 86400.0
            time_penalty    = days_old * 0.1
            quality_penalty = (1.0 - record.get("quality_score", 1.0)) * 2.0
            # Fix-Penalty：惩罚系数叠加，penalty=0.5 对应额外惩罚 2.5（与 quality_penalty 同量纲）
            # 让"预测曾经严重高估 Δm"的记录在综合排名中自然靠后，而不是简单丢弃。
            penalty_score   = record.get("penalty", 0.0) * 5.0
            # Weak evidence remains searchable, but needs a much closer match
            # than a complete closed-loop result before it can affect a
            # duration reference.
            weight = float(record.get("learning_weight", evidence.weight) or 0.0)
            return (h_diff + time_penalty + quality_penalty + penalty_score) / max(weight, 0.2)

        sorted_records = [r for r in sorted(candidates, key=_score) if math.isfinite(_score(r))]
        if not sorted_records:
            return water_min * 3
        top3    = sorted_records[:3]
        weights = [max(float(r.get("learning_weight", classify_trial(r).weight) or 0.0), 0.2) for r in top3]
        avg_sec = sum(float(r.get("optimal_sec", water_min)) * w for r, w in zip(top3, weights)) / sum(weights)
        return max(avg_sec, water_min)


# ===========================================================================
# ⑤ Layer 4 —— Phase 2 时序预测模型接口
# ===========================================================================

class Phase2Predictor:
    """
    Layer 4: 与 Phase 2 前向动力学预测模型进行轻量级通信。

    任务2：重构自旧版 LLM 接口。
    核心变化：
      · 模型后台自带数据流（自行读取 CSV 历史），主控不再组装历史序列
      · Payload 极简化：只发考卷（候选方案）和预测步长
      · 常量键名更新：LLM_PREDICT_HORIZON_H → PREDICT_HORIZON_STEPS

    通信协议（不变）：
      主进程 → 写 /dev/shm/pred_request.json
      Phase2 进程 → 写 /dev/shm/pred_response.json
      主进程轮询，超时则 VPD 修正物理外推保底（任务3）。
    """

    def __init__(self, cfg: ConfigManager):
        self.cfg       = cfg
        self.req_path  = Path(cfg.get_constant("SHM_REQUEST_FILE")  or "/dev/shm/pred_request.json")
        self.resp_path = Path(cfg.get_constant("SHM_RESPONSE_FILE") or "/dev/shm/pred_response.json")
        self.timeout   = cfg.get_constant("SHM_TIMEOUT_SEC")         or 30
        self.horizon   = cfg.get_constant("PREDICT_HORIZON_STEPS")   or 12

    def request_prediction(
        self,
        plans: list[ActionPlan],
        reading: SensorReading,
    ) -> dict[str, list[float]]:
        """
        下发考卷，等待模型返回 12 步预测轨迹。

        任务2：Payload 极简，只包含考卷和步长。
               模型后台自行读取 CSV 历史数据，主控无需传递历史序列。
        """
        if self.cfg.get("PREDICTOR_CIRCUIT_OPEN", False):
            logger.warning("[Layer4] 预测熔断已打开，跳过 Phase2 等待，直接使用物理外推。")
            _mark_predictor_probe(called=False, success=False)
            return self._fallback_trajectories(plans, reading)

        device_code = str(self.cfg.get("DEVICE_CODE", "soil3") or "soil3")
        request_id = f"{device_code}-{uuid.uuid4().hex}"
        payload = {
            "timestamp":    time.time(),
            "request_id":   request_id,
            "device_code":  device_code,
            "profile":      _predictor_profile(self.cfg, reading),
            "candidates":   [{"label": p.label, "water_sec": p.water_sec} for p in plans],
            "horizon_steps": self.horizon,
        }

        self.resp_path.unlink(missing_ok=True)
        _mark_predictor_probe(called=True, success=False)
        try:
            save_json_locked(self.req_path, payload)
            logger.info(f"[Layer4] 预演考卷已下发至 {self.req_path}，等待模型推演...")
        except OSError as e:
            logger.error(f"[Layer4] 写入 shm 失败: {e}，触发物理外推保底。")
            return self._fallback_trajectories(plans, reading)

        # 轮询等待预测模型响应（monotonic：NTP 跳变不影响超时判断）
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.resp_path.exists():
                try:
                    with open(self.resp_path, encoding="utf-8") as f:
                        resp = json.load(f)
                    if not _response_matches_request(resp, request_id, device_code):
                        logger.warning(
                            "[Layer4] 忽略过期或其他设备响应: request_id=%r device_code=%r",
                            resp.get("request_id"), resp.get("device_code"),
                        )
                        time.sleep(0.5)
                        continue
                    trajectories = resp.get("trajectories", {})
                    if not isinstance(trajectories, dict) or not all(p.label in trajectories for p in plans):
                        logger.warning("[Layer4] 预测响应格式或候选标签不匹配，忽略本次响应。")
                        time.sleep(0.5)
                        continue
                    metrics = resp.get("candidate_metrics", {})
                    for plan in plans:
                        item = metrics.get(plan.label, {}) if isinstance(metrics, dict) else {}
                        plan.request_id = request_id
                        plan.device_code = device_code
                        plan.prediction_zone = payload["profile"]["zone"]
                        plan.predicted_peak = item.get("predicted_peak")
                        plan.raw_model_peak = item.get("raw_predicted_peak")
                        plan.predicted_h12 = item.get("predicted_humidity_12h")
                        plan.predicted_trajectory = list(trajectories.get(plan.label, []))
                        plan.shadow_predicted_peak = item.get("shadow_model_peak")
                        plan.shadow_predicted_h12 = item.get("shadow_model_h12")
                        shadow_trajectory = item.get("shadow_model_trajectory")
                        plan.shadow_predicted_trajectory = (
                            list(shadow_trajectory) if isinstance(shadow_trajectory, list) else []
                        )
                        plan.predicted_minutes_to_peak = item.get("predicted_minutes_to_peak")
                        plan.prediction_horizon_steps = self.horizon
                        plan.prediction_timestamp = resp.get("timestamp")
                    logger.info("[Layer4] 成功获取匹配 request_id 的预测轨迹。")
                    _mark_predictor_probe(called=True, success=True)
                    return trajectories
                except (json.JSONDecodeError, OSError):
                    pass
            time.sleep(0.5)

        logger.warning(f"[Layer4] 预测模型超时 ({self.timeout}s)，切换至物理外推保底。")
        _mark_predictor_probe(called=True, success=False)
        return self._fallback_trajectories(plans, reading)

    def _fallback_trajectories(
        self,
        plans: list[ActionPlan],
        reading: SensorReading,
    ) -> dict[str, list[float]]:
        """
        任务3：模型超时保底，衰减率与真实 VPD 线性关联。

        旧版：固定衰减率，夏季高 VPD 时严重低估失水速率。
        新版：decay_rate = 0.05 + 0.2 × VPD
          · VPD=0.5kPa（温和）→ decay_rate=0.15（每步下降 0.15%）
          · VPD=1.5kPa（夏季）→ decay_rate=0.35（每步下降 0.35%，更快）
          · VPD=2.5kPa（酷暑）→ decay_rate=0.55（每步下降 0.55%，极速失水）

        这样保底策略在夏季会产生更陡峭的预测轨迹，
        促使代价函数倾向于选择更大浇水量的方案，防旱死。
        """
        result        = {}
        base_humidity = reading.humidity
        vpd           = reading.vpd
        kp            = _zone_kp(self.cfg, _humidity_zone_name(self.cfg, reading.humidity))

        # 任务3：VPD 物理修正衰减率
        decay_rate = 0.05 + 0.2 * vpd

        for plan in plans:
            peak = min(
                base_humidity + kp * plan.water_sec,
                self.cfg.FC,
            )
            trajectory = [
                max(peak - decay_rate * t, base_humidity - decay_rate * t)
                for t in range(self.horizon)
            ]
            result[plan.label] = trajectory

        logger.info(
            f"[Layer4] 物理外推保底: VPD={vpd:.3f}kPa  "
            f"decay_rate={decay_rate:.3f}%/step"
        )
        return result


# ===========================================================================
# ⑥ Layer 5 —— 动态最高法庭：多目标代价函数 J
# ===========================================================================

class CostCourt:
    """
    Layer 5: 多目标博弈代价评估，选出代价最小的方案。

    J = Σ_{t=1}^{H} [ α·Loss_survival(y_t) + β·Loss_respiration(y_t) ]
        + γ·Cost_mechanical

    Loss_survival(y_t)    = (M_WAKE_UP - y_t)²    当 y_t < M_WAKE_UP
    Loss_respiration(y_t) = (y_t - RESP_LIMIT)²   当 y_t > RESPIRATION_LIMIT
    Cost_mechanical       = water_sec / 60          有浇水动作时计入
    """

    def __init__(self, cfg: ConfigManager):
        self.cfg = cfg

    def evaluate_and_select(
        self,
        plans: list[ActionPlan],
        trajectories: dict[str, list[float]],
    ) -> ActionPlan:
        advice = _load_nightly_learning_advice()
        if advice.get("active"):
            logger.info(
                "[NightlyAdvice] active: "
                f"id={advice.get('advice_id')} "
                f"prefer={(advice.get('arms') or {}).get('prefer')} "
                f"avoid={(advice.get('arms') or {}).get('avoid')} "
                f"explore={(advice.get('exploration') or {}).get('bias_delta')}"
            )
        for plan in plans:
            traj = trajectories.get(plan.label, [])
            if not traj:
                plan.cost_J = math.inf
                continue
            if getattr(plan, "style_blocked", False):
                plan.predicted_trajectory = traj
                plan.cost_J = math.inf
                block_reason = str(
                    getattr(plan, "style_blocked_reason", None)
                    or "style experiment guard"
                )
                logger.info(
                    f"[Layer5] {plan.label:12s}: blocked by {block_reason} "
                    f"({plan.water_sec}s)"
                )
                continue
            plan.predicted_trajectory = traj
            plan.cost_J = self._calc_cost_J(traj, plan, advice)
            logger.info(f"[Layer5] {plan.label:12s}: J={plan.cost_J:.4f}  ({plan.water_sec}s)")

        best = min(plans, key=lambda p: p.cost_J)
        best.selected = True
        logger.info(
            f"[Layer5] 最高法庭判决 → {best.label}  "
            f"J={best.cost_J:.4f}  浇水={best.water_sec}s"
        )
        return best

    @staticmethod
    def _learning_advice_cost_bias(plan: ActionPlan, advice: dict[str, Any]) -> float:
        if not advice.get("active"):
            return 0.0
        arm = _learning_advice_arm_name(plan)
        arms = advice.get("arms") if isinstance(advice.get("arms"), dict) else {}
        prefer = arms.get("prefer") if isinstance(arms.get("prefer"), dict) else {}
        avoid = arms.get("avoid") if isinstance(arms.get("avoid"), dict) else {}
        bias = 0.0
        if arm in prefer:
            bias -= float(prefer.get(arm) or 0.0) * _NIGHTLY_ADVICE_COST_SCALE
        if arm in avoid:
            bias += float(avoid.get(arm) or 0.0) * _NIGHTLY_ADVICE_COST_SCALE
        return round(bias, 4)

    def _calc_cost_J(
        self,
        trajectory: list[float],
        plan: ActionPlan,
        advice: Optional[dict[str, Any]] = None,
    ) -> float:
        """
        Fix-C + 任务5：量纲对齐 + 启动惩罚税。

        cost_mechanical 拆成两部分（单位均为 %²）：

          startup_penalty（任务5 新增）：
            只要开水泵就无条件收取，固定等于 (0.5 × battle_zone)²。
            作用：逼迫系统选择"低频次、一次性浇透"，
                  惩罚"每 5 分钟开 1 秒"的高频微操方案。
            例：战区 1.7%，startup_penalty = (0.85)² ≈ 0.72 %²

          duration_penalty（Fix-C 原有）：
            pump_ratio² × battle_zone²，与浇水时长平方正相关。
            作用：在允许浇水的前提下，倾向选择时长适中的方案。

          total cost_mechanical = startup_penalty + duration_penalty

        量纲验证：
          生存损失上界 ≈ (1.7)² × 12 = 34.6 %²
          startup_penalty ≈ 0.72 %²
          γ=10 时启动惩罚贡献 ≈ 7.2，约占生存损失上界的 20%，
          足以影响决策但不会完全压制生存优先级。
        """
        alpha      = self.cfg.ALPHA
        beta       = self.cfg.BETA
        gamma      = self.cfg.GAMMA * 0.6
        wake_line  = self.cfg.M_WAKE_UP
        resp_limit = self.cfg.RESPIRATION_LIMIT
        tolerance  = self.cfg.TRAJ_TOLERANCE
        water_sec  = plan.water_sec

        loss_survival    = 0.0
        loss_respiration = 0.0
        short_steps = int(self.cfg.get_constant("COST_SHORT_HORIZON_STEPS") or 3)
        medium_steps = int(self.cfg.get_constant("COST_MEDIUM_HORIZON_STEPS") or 6)
        short_weight = float(self.cfg.get_constant("COST_SHORT_HORIZON_WEIGHT") or 1.0)
        medium_weight = float(self.cfg.get_constant("COST_MEDIUM_HORIZON_WEIGHT") or 0.4)
        long_weight_raw = self.cfg.get_constant("COST_LONG_HORIZON_WEIGHT")
        long_weight = float(long_weight_raw) if long_weight_raw is not None else 0.0

        if plan.label == "observe":
            logger.info(
                f"[Layer5] horizon weights: short={short_weight}({short_steps}) "
                f"medium={medium_weight}({medium_steps}) long={long_weight}; "
                "long horizon is audit-only when long=0."
            )

        for index, y in enumerate(trajectory):
            if index < short_steps:
                horizon_weight = short_weight
            elif index < medium_steps:
                horizon_weight = medium_weight
            else:
                horizon_weight = long_weight
            if y < wake_line - tolerance:
                loss_survival += horizon_weight * (wake_line - tolerance - y) ** 2
            if y > resp_limit + tolerance:
                loss_respiration += horizon_weight * (y - resp_limit - tolerance) ** 2

        hard_cap    = self.cfg.get_constant("WATER_SEC_MAX_HARD") or 30.0
        battle_zone = self.cfg.FC - self.cfg.TARGET_LOW   # 单位 %

        if water_sec <= 0:
            cost_mechanical = 0.0
            state = _load_system_state()
            streak = int(state.get("battle_observe_streak", 0))
            observe_penalty = min(
                streak * float(self.cfg.get("OBSERVE_STREAK_PENALTY_STEP", 0.75)),
                float(self.cfg.get("OBSERVE_STREAK_PENALTY_MAX", 6.0)),
            )
        else:
            # 任务5：固定启动惩罚（无论浇多久，只要开泵就收取）
            startup_factor = float(self.cfg.get("COST_STARTUP_FACTOR", 0.25))
            startup_penalty  = (startup_factor * battle_zone) ** 2

            # Fix-C：运行时长惩罚（量纲对齐到 %²）
            pump_ratio       = water_sec / hard_cap
            duration_penalty = pump_ratio ** 2 * battle_zone ** 2

            cost_mechanical  = startup_penalty + duration_penalty
            observe_penalty = 0.0

        ceiling_penalty = 0.0
        if plan.ceiling_violation_ratio > 0:
            ceiling_penalty = (plan.ceiling_violation_ratio ** 2) * battle_zone

        phase2_soft_penalty = float(getattr(plan, "phase2_soft_risk_penalty", 0.0) or 0.0)
        watering_window_penalty = float(getattr(plan, "watering_window_penalty", 0.0) or 0.0)
        learning_advice_bias = self._learning_advice_cost_bias(plan, advice or {})

        return (
            alpha * loss_survival
            + beta * loss_respiration
            + gamma * cost_mechanical
            + observe_penalty
            + ceiling_penalty
            + phase2_soft_penalty
            + watering_window_penalty
            + learning_advice_bias
        )


# ===========================================================================
# ⑦ Layer 6 —— 执行器 + 短期物理进化（EMA 更新 K_p）
# ===========================================================================

class ActuatorLayer:
    """
    Layer 6: 执行浇水 → 创建渗透哨兵（非阻塞）→ 采集增量 → EMA 更新 K_p。

    Fix-A：渗透等待由 PendingSoak 管理，不再用 1800s 阻塞主线程。
           MQTT 浇水段与 water-test-soil3.py 一致：on → sleep(sec) → off，
           该段会阻塞当前线程。
    """

    def __init__(self, cfg: ConfigManager, sensor: SensorLayer):
        self.cfg    = cfg
        self.sensor = sensor
        self._mqtt_broker: str     = MQTT_BROKER_DEFAULT
        self._mqtt_topic_pump: str = MQTT_TOPIC_PUMP_CMD_DEFAULT

        if mqtt is None:
            raise RuntimeError("paho-mqtt 未安装，无法初始化水泵执行器。")
        self._mqtt_client = mqtt.Client()
        self._mqtt_client.connect(self._mqtt_broker, 1883, 60)
        self._mqtt_client.loop_start()
        state = _load_system_state()
        if state.get("pump_active"):
            logger.critical("[Layer6] 启动时发现 pump_active=true，立即发送关泵兜底指令。")
            self._force_pump_off()

    # ------------------------------------------------------------------
    # Fix-A：拆分为两步——execute_pump（立即返回）+ settle（渗透完成后调用）
    # ------------------------------------------------------------------

    def execute_pump(
        self,
        water_sec: float,
        pre_humidity: float,
        is_emergency: bool = False,
        expected_delta_m: Optional[float] = None,
        plan_label: str = "",
        prediction_plan: Optional[ActionPlan] = None,
        watering_window: Optional[dict[str, Any]] = None,
    ) -> PendingSoak:
        """
        经 MQTT 完成一次浇水（on→持水 water_sec→off），再返回 PendingSoak。
        持水阶段会阻塞当前线程；渗透等待仍由哨兵非阻塞管理。

        参数：
          water_sec        : 本次浇水秒数
          pre_humidity     : 浇水前湿度（快照，用于事后计算 Δm）
          is_emergency     : 是否为紧急满灌（影响 pattern_memory 写入资格）
          expected_delta_m : Layer 5 选定方案的预测 Δm（用于惩罚系数计算），
                             None 表示无预测（物理外推场景）
          plan_label       : Layer 3/5 选中的策略标签，写入 trial/profile

        返回：
          PendingSoak  : 挂载到 DecisionBrain._pending_soak 的哨兵对象
        """
        self._activate_pump(water_sec)

        # 更新系统状态（水泵启停计数）
        large_thr = self.cfg.LARGE_WATER_THRESHOLD
        def update(state):
            state["pump_total_cycles"] = int(state.get("pump_total_cycles", 0)) + 1
            state["total_water_sec_dispensed"] = (
                float(state.get("total_water_sec_dispensed", 0.0)) + water_sec
            )
            if not is_emergency and plan_label not in {"reservoir_retest_probe"}:
                state["last_normal_irrigation_timestamp"] = time.time()
            # Align-8 / Upgrade-6：LARGE_WATER_THRESHOLD_SEC 已迁移为动态属性，
            # 按 (FC - TARGET_LOW) / K_P × LARGE_WATER_RATIO 实时推导。
            if water_sec >= large_thr:
                state["last_large_water_timestamp"] = time.time()
        _update_system_state(update)

        soak_duration = self.cfg.get_constant("SOAK_WAIT_SEC") or 1800
        soak = PendingSoak(
            water_sec=water_sec,
            pre_humidity=pre_humidity,
            pump_end_time=time.monotonic(),   # monotonic：与 ready/remaining_sec 同轴
            soak_duration=soak_duration,
            is_emergency=is_emergency,
            expected_delta_m=expected_delta_m,
            plan_label=plan_label,
            request_id=prediction_plan.request_id if prediction_plan else None,
            device_code=prediction_plan.device_code if prediction_plan else None,
            prediction_zone=prediction_plan.prediction_zone if prediction_plan else None,
            predicted_peak=prediction_plan.predicted_peak if prediction_plan else None,
            raw_model_peak=prediction_plan.raw_model_peak if prediction_plan else None,
            predicted_h12=prediction_plan.predicted_h12 if prediction_plan else None,
            predicted_trajectory=(
                list(prediction_plan.predicted_trajectory) if prediction_plan else []
            ),
            shadow_predicted_peak=(
                prediction_plan.shadow_predicted_peak if prediction_plan else None
            ),
            shadow_predicted_h12=(
                prediction_plan.shadow_predicted_h12 if prediction_plan else None
            ),
            shadow_predicted_trajectory=(
                list(prediction_plan.shadow_predicted_trajectory) if prediction_plan else []
            ),
            predicted_minutes_to_peak=(
                prediction_plan.predicted_minutes_to_peak if prediction_plan else None
            ),
            prediction_horizon_steps=(
                prediction_plan.prediction_horizon_steps if prediction_plan else None
            ),
            prediction_timestamp=(prediction_plan.prediction_timestamp if prediction_plan else None),
            observed_peak=pre_humidity,
            watering_window=dict(watering_window or {}),
            forced_exploration=bool(
                prediction_plan.forced_exploration if prediction_plan else False
            ),
            forced_exploration_reason=(
                str(prediction_plan.forced_exploration_reason or "")
                if prediction_plan else ""
            ),
        )

        # 任务1：将哨兵持久化到 system_state.json，断电重启后可恢复。
        # pump_end_time 在内存中是 monotonic 值，持久化时转为 wall-clock，
        # _restore_pending_soak 恢复时再映射回 monotonic 轴。
        pending_soak_state = {
            "water_sec":          soak.water_sec,
            "pre_humidity":       soak.pre_humidity,
            "pump_end_time_wall": time.time() - (time.monotonic() - soak.pump_end_time),
            "soak_duration":      soak.soak_duration,
            "is_emergency":       soak.is_emergency,
            "expected_delta_m":   soak.expected_delta_m,
            "plan_label":         soak.plan_label,
            "request_id":         soak.request_id,
            "device_code":        soak.device_code,
            "prediction_zone":    soak.prediction_zone,
            "predicted_peak":     soak.predicted_peak,
            "raw_model_peak":     soak.raw_model_peak,
            "predicted_h12":      soak.predicted_h12,
            "predicted_trajectory": list(soak.predicted_trajectory),
            "shadow_predicted_peak": soak.shadow_predicted_peak,
            "shadow_predicted_h12": soak.shadow_predicted_h12,
            "shadow_predicted_trajectory": list(soak.shadow_predicted_trajectory),
            "predicted_minutes_to_peak": soak.predicted_minutes_to_peak,
            "prediction_horizon_steps": soak.prediction_horizon_steps,
            "prediction_timestamp": soak.prediction_timestamp,
            "observed_peak":      soak.observed_peak,
            "watering_window":    dict(soak.watering_window or {}),
            "forced_exploration": soak.forced_exploration,
            "forced_exploration_reason": soak.forced_exploration_reason,
            "emergency_interrupts": soak.emergency_interrupts,
            "observation_marks": list(soak.observation_marks),
            "last_interrupt_time_wall": (
                time.time() - (time.monotonic() - soak.last_interrupt_time)
                if soak.last_interrupt_time is not None else None
            ),
        }
        def persist_pending(state):
            state["pending_soak"] = pending_soak_state
        _update_system_state(persist_pending)

        logger.info(
            f"[Layer6]水泵停止。渗透哨兵已挂载并持久化，"
            f"预计 {soak_duration}s 后（约 {soak_duration//60} 分钟）完成渗透。"
        )
        return soak

    def settle(self, soak: PendingSoak) -> "SensorReading":
        """
        渗透完成后调用：采样 → K_p EMA 更新 → pattern_memory 写入。

        任务1：返回 SensorReading 对象而非 float，
               供 run_cycle 直接复用，避免主循环再读一次硬件（双重写 CSV）。

        参数：
          soak : 之前挂载的 PendingSoak 哨兵

        返回：
          post_reading : 渗透稳定后的完整传感器快照（由主循环复用）
        """
        post_reading = self.sensor.read(require_fresh=True)
        delta_m      = post_reading.humidity - soak.pre_humidity
        soak.observed_peak = max(
            float(soak.observed_peak if soak.observed_peak is not None else soak.pre_humidity),
            float(post_reading.humidity),
        )
        actual_peak_window_minutes = round(
            max(0.0, time.monotonic() - soak.pump_end_time) / 60.0, 2
        )
        prediction_meta = {
            "request_id": soak.request_id,
            "device_code": soak.device_code,
            "prediction_zone": soak.prediction_zone,
            "predicted_peak": soak.predicted_peak,
            "raw_model_peak": soak.raw_model_peak,
            "predicted_h12": soak.predicted_h12,
            "predicted_trajectory": list(soak.predicted_trajectory),
            "shadow_predicted_peak": soak.shadow_predicted_peak,
            "shadow_predicted_h12": soak.shadow_predicted_h12,
            "shadow_predicted_trajectory": list(soak.shadow_predicted_trajectory),
            "predicted_minutes_to_peak": soak.predicted_minutes_to_peak,
            "prediction_horizon_steps": soak.prediction_horizon_steps,
            "prediction_timestamp": soak.prediction_timestamp,
            "actual_peak_observed": round(float(soak.observed_peak), 3),
            "peak_error_observed": (
                round(float(soak.observed_peak) - float(soak.predicted_peak), 3)
                if soak.predicted_peak is not None else None
            ),
            "shadow_peak_error_observed": (
                round(float(soak.observed_peak) - float(soak.shadow_predicted_peak), 3)
                if soak.shadow_predicted_peak is not None else None
            ),
            "actual_peak_window_minutes": actual_peak_window_minutes,
            "actual_h12": None,
            "h12_status": "pending" if soak.predicted_h12 is not None else "not_predicted",
            "h12_due_at": (
                float(soak.prediction_timestamp) + 12 * 3600
                if soak.predicted_h12 is not None and soak.prediction_timestamp is not None else None
            ),
            "forced_exploration": bool(soak.forced_exploration),
            "forced_exploration_reason": soak.forced_exploration_reason,
        }
        if soak.watering_window:
            prediction_meta["watering_window_level"] = soak.watering_window.get("level")
            prediction_meta["watering_window_reason"] = soak.watering_window.get("reason")
            prediction_meta["watering_window_sample_context"] = soak.watering_window.get("sample_context")
            prediction_meta["watering_window_local_hour"] = soak.watering_window.get("local_hour")
            prediction_meta["watering_window_allow_explore"] = soak.watering_window.get("allow_explore")
        logger.info(
            f"[Layer6] 渗透完成采样: 浇前={soak.pre_humidity:.1f}%  "
            f"浇后={post_reading.humidity:.1f}%  Δm={delta_m:+.2f}%"
        )

        if soak.plan_label == "reservoir_retest_probe":
            baseline = self._reservoir_retest_baseline(soak.water_sec)
            recovered_delta = float(baseline["threshold"])
            recovered = delta_m >= recovered_delta
            now = time.time()
            reason = "reservoir_retest_recovered" if recovered else "reservoir_retest_still_no_response"
            prediction_meta["reservoir_retest_baseline"] = baseline
            if recovered:
                def clear_reservoir_suspect(state):
                    for key in ("reservoir_empty_suspect", "low_wet_recovery_suspect"):
                        suspect = state.get(key)
                        if isinstance(suspect, dict):
                            suspect["active"] = False
                            suspect["cleared_at"] = now
                            suspect["clear_reason"] = reason
                            suspect["recovery_delta_m"] = round(float(delta_m), 3)
                            suspect["last_retest_result"] = "recovered"
                            suspect["last_retest_baseline"] = baseline
                            suspect["exclude_from_learning"] = False
                            suspect.pop("pause_until", None)
                    water_delivery = state.get("water_delivery_suspect")
                    if isinstance(water_delivery, dict) and water_delivery.get("active"):
                        water_delivery["repair_retest_pending"] = False
                        water_delivery["repair_retest_result"] = "reservoir_recovered"
                    state["water_path_recovered_at"] = now
                    state["post_repair_epoch"] = now
                    state["post_repair_epoch_reason"] = "reservoir_retest_recovered"
                    state["water_path_recovery_reason"] = reason
                    state["water_path_recovery_delta_m"] = round(float(delta_m), 3)
                _update_system_state(clear_reservoir_suspect)
                self._quarantine_pre_recovery_low_wet_stats(now, delta_m, baseline)
                logger.warning(
                    f"[ReservoirRetest] recovered: Δm={delta_m:+.3f}% >= "
                    f"dynamic_threshold={recovered_delta:.3f}% "
                    f"({baseline.get('decision_basis')}); reservoir suspect cleared."
                )
            else:
                interval = float(self.cfg.get_constant("RESERVOIR_RETEST_INTERVAL_SEC") or 21600)
                pause_until = now + interval
                def keep_reservoir_suspect(state):
                    for key in ("reservoir_empty_suspect", "low_wet_recovery_suspect"):
                        suspect = state.setdefault(key, {})
                        if isinstance(suspect, dict):
                            suspect["active"] = True
                            suspect["reason"] = reason
                            suspect["pause_until"] = pause_until
                            suspect["exclude_from_learning"] = True
                            suspect["last_retest_result"] = "still_no_response"
                            suspect["last_retest_delta_m"] = round(float(delta_m), 3)
                            suspect["last_retest_baseline"] = baseline
                            suspect["updated_at"] = now
                _update_system_state(keep_reservoir_suspect)
                logger.critical(
                    f"[ReservoirRetest] still no response: Δm={delta_m:+.3f}% < "
                    f"dynamic_threshold={recovered_delta:.3f}% "
                    f"({baseline.get('decision_basis')}); next retest after {interval/3600:.1f}h."
                )

            self._record_irrigation_trial(
                status="reservoir_retest",
                reason=reason,
                water_sec=soak.water_sec,
                pre_h=soak.pre_humidity,
                post_h=post_reading.humidity,
                delta_m=delta_m,
                expected_delta_m=soak.expected_delta_m,
                plan_label=soak.plan_label,
                quality_score=None,
                penalty=0.0,
                prediction_meta=prediction_meta,
            )
            logger.info("[Layer6] K_p 更新跳过：供水复测样本不参与 pattern_memory 或画像进化。")
            return post_reading

        # Fix-B：紧急满灌不写入 pattern_memory，但仍写入完整 trial log，
        # 避免第一阶段丢失"为什么这样浇水"的反馈经验。
        if soak.is_emergency:
            self._record_irrigation_trial(
                status="emergency",
                reason="emergency_pulse_not_reference",
                water_sec=soak.water_sec,
                pre_h=soak.pre_humidity,
                post_h=post_reading.humidity,
                delta_m=delta_m,
                expected_delta_m=soak.expected_delta_m,
                plan_label=soak.plan_label,
                quality_score=None,
                penalty=0.0,
                prediction_meta=prediction_meta,
            )
            self._update_irrigation_profile(
                status="emergency",
                reason="emergency_pulse_not_reference",
                pre_h=soak.pre_humidity,
                post_h=post_reading.humidity,
                water_sec=soak.water_sec,
                delta_m=delta_m,
                plan_label=soak.plan_label,
                quality_score=None,
            )
            logger.info("[Layer6] K_p 更新跳过：紧急补水样本不参与常规物理增益进化。")
        else:
            window_context = str((soak.watering_window or {}).get("sample_context") or "")
            if window_context in {"night_emergency", "evening_emergency"}:
                self._record_irrigation_trial(
                    status="timing_emergency",
                    reason="watering_window_emergency_context_not_reference",
                    water_sec=soak.water_sec,
                    pre_h=soak.pre_humidity,
                    post_h=post_reading.humidity,
                    delta_m=delta_m,
                    expected_delta_m=soak.expected_delta_m,
                    plan_label=soak.plan_label,
                    quality_score=None,
                    penalty=0.0,
                    prediction_meta=prediction_meta,
                )
                logger.info(
                    "[Layer6] K_p 更新跳过：浇水时机为夜间/傍晚救急上下文，"
                    "只保留安全响应记录，不作为普通策略经验。"
                )
                return post_reading
            accepted = self._update_pattern_memory(
                soak.water_sec, soak.pre_humidity,
                post_reading.humidity, delta_m,
                expected_delta_m=soak.expected_delta_m,
                plan_label=soak.plan_label,
                prediction_meta=prediction_meta,
            )
            if accepted:
                self._evolve_kp(
                    soak.water_sec,
                    delta_m,
                    soak.pre_humidity,
                    soak.plan_label,
                    raw_delta_m=delta_m,
                    expected_natural_loss=0.0,
                )
            else:
                logger.info(
                    "[Layer6] K_p 更新跳过：本次 trial 未通过 pattern_memory 质量门控，"
                    "仅保留在 irrigation_trials。"
                )

        return post_reading   # 任务1：返回完整快照，不仅仅是湿度值

    def _reservoir_retest_baseline(self, water_sec: float) -> dict[str, Any]:
        """用最近同秒数/近似同秒数 trial 生成供水恢复动态阈值。"""
        fallback = float(self.cfg.get_constant("RESERVOIR_RETEST_MIN_DELTA") or 0.5)
        limit = int(self.cfg.get_constant("RESERVOIR_RETEST_HISTORY_LIMIT") or 80)
        tolerance = float(self.cfg.get_constant("RESERVOIR_RETEST_SAME_SEC_TOLERANCE") or 0.6)
        min_samples = int(self.cfg.get_constant("RESERVOIR_RETEST_MIN_BASELINE_SAMPLES") or 3)
        min_improve = float(self.cfg.get_constant("RESERVOIR_RETEST_MIN_IMPROVEMENT") or 0.4)
        iqr_factor = float(self.cfg.get_constant("RESERVOIR_RETEST_IQR_FACTOR") or 1.5)

        def percentile(values: list[float], pct: float) -> float:
            if not values:
                return 0.0
            if len(values) == 1:
                return values[0]
            pos = (len(values) - 1) * pct
            lo = int(pos)
            hi = min(lo + 1, len(values) - 1)
            frac = pos - lo
            return values[lo] * (1.0 - frac) + values[hi] * frac

        trials = _load_irrigation_trials()
        deltas: list[float] = []
        accepted_status = {"accepted", "rejected", "reservoir_retest"}
        excluded_labels = {"reservoir_retest_probe", "emergency", "emergency_interrupt"}
        for trial in reversed(trials[-limit:]):
            try:
                sec = float(trial.get("water_sec"))
                delta = float(trial.get("delta_m"))
            except (TypeError, ValueError):
                continue
            if abs(sec - float(water_sec)) > tolerance:
                continue
            if str(trial.get("plan_label") or "") in excluded_labels:
                continue
            if str(trial.get("status") or "") not in accepted_status:
                continue
            deltas.append(delta)

        deltas = sorted(deltas)
        if len(deltas) < min_samples:
            return {
                "decision_basis": "absolute_fallback_insufficient_same_sec_history",
                "water_sec": round(float(water_sec), 3),
                "same_sec_tolerance": round(tolerance, 3),
                "sample_count": len(deltas),
                "threshold": round(fallback, 3),
                "fallback_min_delta": round(fallback, 3),
                "recent_deltas": [round(x, 3) for x in deltas[-10:]],
            }

        q1 = percentile(deltas, 0.25)
        med = percentile(deltas, 0.50)
        q3 = percentile(deltas, 0.75)
        iqr = max(0.0, q3 - q1)
        threshold = max(fallback, med + min_improve, med + iqr_factor * iqr)
        return {
            "decision_basis": "same_sec_dynamic_baseline",
            "water_sec": round(float(water_sec), 3),
            "same_sec_tolerance": round(tolerance, 3),
            "sample_count": len(deltas),
            "median_delta_m": round(med, 3),
            "q1_delta_m": round(q1, 3),
            "q3_delta_m": round(q3, 3),
            "iqr_delta_m": round(iqr, 3),
            "min_improvement": round(min_improve, 3),
            "fallback_min_delta": round(fallback, 3),
            "threshold": round(threshold, 3),
            "recent_deltas": [round(x, 3) for x in deltas[-10:]],
        }

    # ------------------------------------------------------------------
    # 硬件驱动：MQTT 开泵（对齐 phase1 send_mqtt_cmd）
    # ------------------------------------------------------------------

    def _activate_pump(self, water_sec: float) -> None:
        """对齐 phase1 send_mqtt_cmd：publish on → sleep → publish off。"""
        sec = max(0.0, float(water_sec))

        if sec <= 0:
            logger.info("  >>> [物理执行] 决议为 0s，水泵保持静默。")
            return

        logger.info(f"  >>> [物理执行] 准备下发灌溉指令，时长: {sec}s")
        pump_was_started = False
        try:
            self._publish_pump("on")
            pump_was_started = True
            def mark_active(state):
                state["pump_active"] = True
                state["pump_active_since"] = time.time()
                state["pump_last_command_sec"] = sec
            _update_system_state(mark_active)
            time.sleep(sec)
            self._force_pump_off()
            logger.info(f"  >>> [物理执行] {sec}s 灌溉顺利结束，等待水分渗透。")
        except Exception as e:
            if pump_was_started:
                try:
                    self._force_pump_off()
                except Exception:
                    def mark_off_failed(state):
                        state["pump_active"] = True
                        state["pump_off_failed_at"] = time.time()
                    _update_system_state(mark_off_failed)
                    logger.critical("[Layer6] 关泵兜底失败，请立即人工检查水泵状态。")
            logger.error(f"  >>> [MQTT 错误] 水泵执行失败: {e}")
            raise PumpExecutionError(str(e)) from e

    def _publish_pump(self, payload: str) -> None:
        info = self._mqtt_client.publish(self._mqtt_topic_pump, payload, qos=1)
        if hasattr(info, "wait_for_publish"):
            info.wait_for_publish(timeout=5)
        rc = getattr(info, "rc", 0)
        if rc != 0:
            raise PumpExecutionError(f"MQTT publish {payload!r} failed rc={rc}")

    def _force_pump_off(self) -> None:
        retry_count = int(self.cfg.get_constant("PUMP_OFF_RETRY_COUNT") or 3)
        interval = float(self.cfg.get_constant("PUMP_OFF_RETRY_INTERVAL_SEC") or 0.2)
        last_error: Optional[Exception] = None
        for _ in range(max(retry_count, 1)):
            try:
                self._publish_pump("off")
                def mark_off(state):
                    state["pump_active"] = False
                    state["pump_last_off_at"] = time.time()
                    state.pop("pump_off_failed_at", None)
                _update_system_state(mark_off)
                return
            except Exception as e:
                last_error = e
                time.sleep(interval)
        raise PumpExecutionError(f"failed to publish pump off after retries: {last_error}")

    # ------------------------------------------------------------------
    # K_p EMA 更新
    # ------------------------------------------------------------------

    def _evolve_kp(
        self,
        water_sec: float,
        delta_m: float,
        pre_humidity: float,
        plan_label: str = "",
        *,
        raw_delta_m: Optional[float] = None,
        expected_natural_loss: float = 0.0,
    ) -> None:
        """
        EMA 更新物理增益 K_p（短期物理进化）。

        K_p_measured = Δm / water_sec
        K_p_new      = (1 - ema_α) × K_p_old + ema_α × K_p_measured

        Fix-2：K_p 更新前必须过滤异常 delta_m，否则存在"反向污染"风险：
          · delta_m ≤ 0：K_p_measured < 0，EMA 会把 K_p 拉向负值，
            导致 Layer 1 天花板计算倒置（gap/K_p 变为负数），系统失控。
          · delta_m 超物理上限：传感器尖峰，K_p_measured 虚高，
            EMA 会过度压低未来的浇水量，植物持续缺水。
          · delta_m 对应的 K_p_measured 与当前 K_p 偏差超过 5 倍：
            单次异常读数不应剧烈改变增益，超阈值时跳过本轮更新并记录。

        Align-6：K_P_EMA_ALPHA 是可进化参数，用 cfg.get()；
                 K_p 下界用 0.001 而非 WATER_SEC_MIN（两者语义不同）。
        """
        if water_sec <= 0:
            return

        k_measured = delta_m / water_sec
        zone_name  = _humidity_zone_name(self.cfg, pre_humidity)
        k_old      = _zone_kp(self.cfg, zone_name)
        global_old = self.cfg.K_P

        # Fix-2 Gate-A：delta_m ≤ 0，K_p_measured 为负，直接跳过，不污染增益
        if delta_m <= 0:
            logger.warning(
                f"[Layer6] ⚠️ K_p 更新跳过：Δm={delta_m:+.3f}% ≤ 0，"
                f"负增益不参与 EMA（土壤读数异常或渗透未完成）。"
            )
            return

        # Fix-2 Gate-B：delta_m 超过物理上限（从当前湿度到 FC + 0.5% 容差）
        fc = self.cfg.FC
        # 注：pre_humidity 在此处不可直接访问，用 K_p×water_sec 的 3 倍作保守上界
        physical_ceiling = self.cfg.K_P * water_sec * 3.0
        if delta_m > physical_ceiling and delta_m > (fc * 0.1):
            logger.warning(
                f"[Layer6] ⚠️ K_p 更新跳过：Δm={delta_m:.3f}% 超过物理上界 "
                f"{physical_ceiling:.3f}%，疑似传感器尖峰。"
            )
            return

        # Fix-2 Gate-C：K_p_measured 与当前 K_p 偏差超过 5 倍，单次异常不更新
        if k_old > 0 and (k_measured > k_old * 5.0 or k_measured < k_old / 5.0):
            logger.warning(
                f"[Layer6] ⚠️ K_p 更新跳过：K_p_measured={k_measured:.5f} 与 "
                f"K_p_old={k_old:.5f} 偏差超过 5 倍，单次异常读数拒绝更新。"
            )
            return

        # 三关全部通过，执行 EMA 更新
        ema_alpha = self.cfg.get("K_P_EMA_ALPHA") or 0.2
        k_new     = max((1 - ema_alpha) * k_old + ema_alpha * k_measured, 0.001)

        global_new = max((1 - ema_alpha) * global_old + ema_alpha * k_measured, 0.001)
        # Fix-Echo: 防止 zone K_P 回音壁效应，限制偏离全局 K_P 不超过 2.5 倍
        kp_cap_lo = global_new * 0.3
        kp_cap_hi = global_new * 2.5
        if k_new < kp_cap_lo:
            logger.warning(f"[Layer6] K_p zone({zone_name}) {k_new:.5f} < cap_lo({kp_cap_lo:.5f}), 钳制到下限")
            k_new = kp_cap_lo
        elif k_new > kp_cap_hi:
            logger.warning(f"[Layer6] K_p zone({zone_name}) {k_new:.5f} > cap_hi({kp_cap_hi:.5f}), 钳制到上限")
            k_new = kp_cap_hi
        zone_key = _zone_kp_key(zone_name)
        global_new_rounded = round(global_new, 5)
        zone_new_rounded = round(k_new, 5)
        self.cfg.update({
            "K_P": global_new_rounded,
            zone_key: zone_new_rounded,
        })
        evidence = {
            "water_sec": round(float(water_sec), 3),
            "delta_m": round(float(delta_m), 3),
            "pre_humidity": round(float(pre_humidity), 3),
            "zone": zone_name,
            "zone_key": zone_key,
            "k_measured": round(float(k_measured), 5),
            "ema_alpha": round(float(ema_alpha), 5),
            "plan_label": plan_label,
            "source": "evolve_kp",
        }
        _record_parameter_change(
            "K_P",
            global_old,
            global_new_rounded,
            "auto_evolve_kp",
            "decision_brain",
            evidence,
        )
        _record_parameter_change(
            zone_key,
            k_old,
            zone_new_rounded,
            "auto_evolve_kp",
            "decision_brain",
            evidence,
        )
        self._update_zone_kp_profile(
            zone_name,
            k_new,
            raw_delta_m=raw_delta_m,
            net_delta_m=delta_m,
            water_sec=water_sec,
            expected_natural_loss=expected_natural_loss,
        )
        logger.info(
            f"[Layer6] K_p 短期进化: global {global_old:.5f} → {global_new:.5f}; "
            f"{zone_key} {k_old:.5f} → {k_new:.5f}  "
            f"(zone={zone_name} measured={k_measured:.5f}  ema_α={ema_alpha})"
        )

    def _update_zone_kp_profile(
        self,
        zone_name: str,
        zone_kp: float,
        *,
        raw_delta_m: Optional[float] = None,
        net_delta_m: Optional[float] = None,
        water_sec: float = 0.0,
        expected_natural_loss: float = 0.0,
    ) -> None:
        """Update learned watering gain and keep raw/net evidence side by side."""
        profile = _load_irrigation_profile()
        zone = profile["zones"][zone_name]
        alpha = float(self.cfg.get("K_P_PROFILE_EMA_ALPHA", 0.2) or 0.2)

        def ema_update(key: str, value: Optional[float]) -> None:
            if value is None:
                return
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                return
            if not math.isfinite(value_f):
                return
            old = zone.get(key)
            if isinstance(old, (int, float)) and math.isfinite(float(old)):
                value_f = (1 - alpha) * float(old) + alpha * value_f
            zone[key] = round(value_f, 5)

        zone["kp_ema"] = round(float(zone_kp), 5)
        zone["kp_net_ema"] = round(float(zone_kp), 5)
        raw_kp = (
            float(raw_delta_m) / float(water_sec)
            if raw_delta_m is not None and water_sec > 0 else None
        )
        net_kp = (
            float(net_delta_m) / float(water_sec)
            if net_delta_m is not None and water_sec > 0 else float(zone_kp)
        )
        ema_update("kp_raw_ema", raw_kp)
        ema_update("kp_net_ema", net_kp)
        ema_update("natural_loss_ema", expected_natural_loss)
        zone["kp_samples"] = int(zone.get("kp_samples") or 0) + 1
        zone["last_kp_raw"] = round(float(raw_kp), 5) if raw_kp is not None else None
        zone["last_kp_net"] = round(float(net_kp), 5) if net_kp is not None else round(float(zone_kp), 5)
        zone["last_expected_natural_loss"] = round(float(expected_natural_loss), 5)
        zone["last_updated"] = time.time()
        _save_irrigation_profile(profile)

    # ------------------------------------------------------------------
    # Fix-B：pattern_memory 三重质量门控
    # ------------------------------------------------------------------

    def _update_irrigation_profile(
        self,
        status: str,
        reason: str,
        pre_h: float,
        post_h: float,
        water_sec: float,
        delta_m: float,
        plan_label: str = "",
        quality_score: Optional[float] = None,
        zone_kp: Optional[float] = None,
    ) -> None:
        profile = _load_irrigation_profile()
        zone_name = _humidity_zone_name(self.cfg, pre_h)
        zone = profile["zones"][zone_name]
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        strategy_key = plan_label or f"{float(water_sec):.1f}s"
        strategies = zone.setdefault("strategy_stats", {})
        stat = strategies.setdefault(strategy_key, {
            "attempts": 0,
            "accepted": 0,
            "rejected": 0,
            "emergency": 0,
            "avg_delta_m": None,
            "last_reason": "",
            "last_water_sec": None,
            "last_updated": None,
        })
        stat["attempts"] = int(stat.get("attempts") or 0) + 1
        stat["last_reason"] = reason
        stat["last_water_sec"] = round(float(water_sec), 3)
        stat["last_updated"] = time.time()
        if delta_m is not None:
            previous = stat.get("avg_delta_m")
            if isinstance(previous, (int, float)):
                stat["avg_delta_m"] = round(previous * 0.8 + float(delta_m) * 0.2, 3)
            else:
                stat["avg_delta_m"] = round(float(delta_m), 3)

        if status == "accepted":
            stat["accepted"] = int(stat.get("accepted") or 0) + 1
            zone["stable_success"] = int(zone.get("stable_success") or 0) + 1
            zone["total_accepted"] = int(zone.get("total_accepted") or 0) + 1
            zone["failure_count"] = max(int(zone.get("failure_count") or 0) - 1, 0)
            if float(water_sec) > water_min + 0.1:
                zone["stuck_rounds_at_min"] = 0
            if zone_kp is not None:
                zone["kp_ema"] = round(float(zone_kp), 5)
            stable = int(zone["stable_success"])
            total_accepted = int(zone["total_accepted"])
            current_max = float(zone.get("max_allowed_sec") or water_min)
            fc_margin_after = float(self.cfg.FC) - float(post_h)
            if total_accepted >= 10 or stable >= int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_5S") or 5):
                zone["max_allowed_sec"] = max(current_max, 5.0)
            elif total_accepted >= 6 or stable >= int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_4S") or 3):
                zone["max_allowed_sec"] = max(current_max, 4.0)
            if (
                float(water_sec) >= float(self.cfg.get_constant("STYLE_PULSE_MEDIUM_SEC") or 6.0) - 0.05
                and float(delta_m) >= 5.0
                and float(pre_h) <= float(self.cfg.TARGET_LOW) + 0.6
                and fc_margin_after >= 1.0
            ):
                strong_sec = float(self.cfg.get_constant("STYLE_PULSE_STRONG_SEC") or 9.0)
                large_sec = float(self.cfg.get_constant("STYLE_PULSE_LARGE_SEC") or 10.0)
                hard_cap = float(self.cfg.get_constant("WATER_SEC_MAX_HARD") or large_sec)
                next_probe = min(max(strong_sec, float(water_sec)), large_sec, hard_cap)
                zone["max_allowed_sec"] = max(float(zone.get("max_allowed_sec") or water_min), next_probe)
                zone["last_strong_response_action"] = (
                    "expand_next_low_wet_probe_to_strong_pulse"
                )
                zone["last_strong_response_delta_m"] = round(float(delta_m), 3)
                zone["last_strong_response_water_sec"] = round(float(water_sec), 3)
                zone["last_strong_response_fc_margin_after"] = round(fc_margin_after, 3)
        elif status == "rejected":
            stat["rejected"] = int(stat.get("rejected") or 0) + 1
            zone["failure_count"] = int(zone.get("failure_count") or 0) + 1
            if float(water_sec) <= water_min + 0.1:
                zone["stuck_rounds_at_min"] = int(zone.get("stuck_rounds_at_min") or 0) + 1
            if reason in {"post_h_above_fc_tolerance", "delta_m_above_physical_max"}:
                zone["stable_success"] = 0
                zone["max_allowed_sec"] = max(
                    water_min,
                    min(float(zone.get("max_allowed_sec") or water_min), float(water_sec) - 1.0),
                )
            elif reason == "non_positive_delta_m":
                zone["stable_success"] = max(int(zone.get("stable_success") or 0) - 1, 0)
                zone["last_reason"] = "non_positive_delta_observe_longer"
        elif status == "emergency":
            stat["emergency"] = int(stat.get("emergency") or 0) + 1
            effective_delta = float(self.cfg.get_constant("EMERGENCY_INEFFECTIVE_DELTA") or 0.3)
            now = time.time()
            if delta_m >= effective_delta:
                def clear_suspect(state):
                    suspect = state.get("water_delivery_suspect", {})
                    repair_retest_active = (
                        suspect.get("repair_retest_started_at")
                        and not suspect.get("repair_retest_result")
                    )
                    if suspect.get("active") or repair_retest_active or suspect.get("repair_retest_pending"):
                        suspect["active"] = False
                        suspect["cleared_at"] = now
                        suspect["clear_reason"] = "emergency_response_recovered"
                        suspect["repair_retest_pending"] = False
                        suspect["repair_retest_result"] = "recovered"
                        suspect["repair_retest_delta_m"] = round(float(delta_m), 3)
                        suspect["repair_retest_completed_at"] = now
                        state["water_delivery_suspect"] = suspect
                _update_system_state(clear_suspect)
            else:
                def fail_repair_retest(state):
                    suspect = state.get("water_delivery_suspect", {})
                    repair_retest_active = (
                        suspect.get("repair_retest_started_at")
                        and not suspect.get("repair_retest_result")
                    )
                    if repair_retest_active:
                        pause_sec = float(
                            self.cfg.get_constant("EMERGENCY_INEFFECTIVE_PAUSE_SEC") or 21600
                        )
                        suspect["active"] = True
                        suspect["reason"] = "repair_retest_failed"
                        suspect["repair_retest_pending"] = False
                        suspect["repair_retest_result"] = "failed"
                        suspect["repair_retest_delta_m"] = round(float(delta_m), 3)
                        suspect["repair_retest_completed_at"] = now
                        suspect["last_blocked_at"] = now
                        suspect["pause_until"] = now + pause_sec
                        suspect["avg_recent_delta_m"] = round(float(delta_m), 3)
                        suspect["ineffective_count"] = 1
                        state["water_delivery_suspect"] = suspect
                _update_system_state(fail_repair_retest)
            if delta_m >= 5.0:
                zone["max_allowed_sec"] = min(float(zone.get("max_allowed_sec") or water_min), water_min)
                zone["last_reason"] = "emergency_large_response_limit"

        if zone.get("last_reason") not in {"emergency_large_response_limit", "non_positive_delta_observe_longer"}:
            zone["last_reason"] = reason
        zone["last_updated"] = time.time()
        _save_irrigation_profile(profile)

    def _forced_exploration_quarantine_reason(
        self,
        reason: str,
        prediction_meta: Optional[dict],
    ) -> Optional[str]:
        if not isinstance(prediction_meta, dict) or not prediction_meta.get("forced_exploration"):
            return None
        reason_l = str(reason or "").lower()
        if reason_l in {"post_h_above_fc_tolerance", "delta_m_above_physical_max"}:
            return f"forced_exploration_{reason_l}"
        if any(token in reason_l for token in ("sensor_spike", "sensor_fault", "sensor_stale")):
            return "forced_exploration_sensor_unreliable"

        level = str(prediction_meta.get("watering_window_level") or "").lower()
        context = str(prediction_meta.get("watering_window_sample_context") or "").lower()
        if level not in {"ideal", "allowed"} or context != "normal_window":
            return "forced_exploration_non_normal_watering_window"
        return None

    def _update_pattern_memory(
        self,
        water_sec: float,
        pre_h: float,
        post_h: float,
        delta_m: float,
        expected_delta_m: Optional[float] = None,
        plan_label: str = "",
        prediction_meta: Optional[dict] = None,
    ) -> bool:
        """
        将本次浇水结果写入 pattern_memory（含质量门控 + 惩罚系数）。

        Fix-B 三重质量门控，任意一条未通过则不写入 pattern_memory：
          Gate-1  delta_m > 0
          Gate-2  post_h <= FC
          Gate-3  delta_m <= FC - pre_h + 0.5

        惩罚系数（Fix-Penalty）：
          当 expected_delta_m 不为 None 时（Layer 5 有预测数据），
          计算预测误差比 = |delta_m - expected_delta_m| / expected_delta_m。
          · 误差 < 20%   → penalty = 0.0（预测准确，无惩罚）
          · 误差 20-60%  → penalty 线性从 0 → 0.5（预测偏差，轻度惩罚）
          · 误差 > 60%   → penalty = 0.5（预测严重失准，重度惩罚）
          · delta_m < expected_delta_m（Δm 不及预期，即"激进方案缩水"）时
            惩罚额外乘以 1.5，因为高估增益比低估更危险。

        返回值：
          True  = 可作为高质量经验，已写入 pattern_memory，可继续更新 K_p
          False = 只写入 irrigation_trials，不更新 K_p

        penalty 写入 JSON，_get_reference_sec 查询时叠加进综合代价：
          penalty_score = penalty × 5.0（换算到与时间衰减同量纲的惩罚强度）
          综合代价 = h_diff + time_penalty + quality_penalty + penalty_score
        """
        fc = self.cfg.FC
        zone_name_for_quality = _humidity_zone_name(self.cfg, pre_h)
        theoretical_delta = _zone_kp(self.cfg, zone_name_for_quality) * water_sec

        def forced_quarantine(reason: str) -> Optional[str]:
            return self._forced_exploration_quarantine_reason(reason, prediction_meta)

        def reject(reason: str, message: str) -> None:
            logger.warning(message)
            quarantine_reason = forced_quarantine(reason)
            trial_meta = dict(prediction_meta or {})
            if quarantine_reason:
                trial_meta["forced_exploration_quarantined"] = True
                trial_meta["forced_exploration_quarantine_reason"] = quarantine_reason
                logger.warning(
                    "[NightlyAdvice] forced exploration trial quarantined: "
                    f"{quarantine_reason}; profile scoring skipped."
                )
            else:
                self._update_irrigation_profile(
                    status="rejected",
                    reason=reason,
                    pre_h=pre_h,
                    post_h=post_h,
                    water_sec=water_sec,
                    delta_m=delta_m,
                    plan_label=plan_label,
                    quality_score=None,
                )
            self._record_irrigation_trial(
                status="rejected",
                reason=reason,
                water_sec=water_sec,
                pre_h=pre_h,
                post_h=post_h,
                delta_m=delta_m,
                expected_delta_m=expected_delta_m,
                plan_label=plan_label,
                quality_score=None,
                penalty=0.0,
                prediction_meta=trial_meta or None,
            )

        # Gate-1
        if delta_m <= 0:
            reject(
                "non_positive_delta_m",
                f"[Layer6] ⚠️ pattern_memory 丢弃：Δm={delta_m:+.3f}% ≤ 0，"
                f"湿度未上升（传感器噪声或渗透未完成）。"
            )
            return False

        fc_tolerance = 1.0

        # Gate-2
        if post_h > fc + fc_tolerance:
            reject(
                "post_h_above_fc_tolerance",
                f"[Layer6] pattern_memory 丢弃：post_h={post_h:.1f}% > FC+{fc_tolerance}={fc + fc_tolerance:.1f}%，"
                f"本次结果视为过冲或读数异常，不作为成功经验。"
            )
            return False

        # Gate-3
        physical_max_delta = max((fc - pre_h) + fc_tolerance, self.cfg.K_P * water_sec * 3.0)
        if delta_m > physical_max_delta:
            reject(
                "delta_m_above_physical_max",
                f"[Layer6] pattern_memory 丢弃：Δm={delta_m:.3f}% > "
                f"物理上限 {physical_max_delta:.3f}%，读数严重失真。"
            )
            return False

        # 三关全部通过，计算质量评分
        quality_score = round(
            1.0 - abs(delta_m - theoretical_delta) / max(theoretical_delta, 0.001),
            3,
        )
        quality_score = max(0.0, min(quality_score, 1.0))

        quarantine_reason = forced_quarantine("accepted_into_pattern_memory")
        if quarantine_reason:
            trial_meta = dict(prediction_meta or {})
            trial_meta["forced_exploration_quarantined"] = True
            trial_meta["forced_exploration_quarantine_reason"] = quarantine_reason
            logger.warning(
                "[NightlyAdvice] forced exploration passed physical gates but was "
                f"quarantined before pattern_memory: {quarantine_reason}."
            )
            self._record_irrigation_trial(
                status="rejected",
                reason="forced_exploration_quarantined",
                water_sec=water_sec,
                pre_h=pre_h,
                post_h=post_h,
                delta_m=delta_m,
                expected_delta_m=expected_delta_m,
                plan_label=plan_label,
                quality_score=None,
                penalty=0.0,
                prediction_meta=trial_meta,
            )
            return False

        # Fix-Penalty：根据预测误差计算惩罚系数
        penalty = 0.0
        if expected_delta_m is not None and expected_delta_m > 0:
            err_ratio = abs(delta_m - expected_delta_m) / expected_delta_m
            if err_ratio <= 0.20:
                penalty = 0.0
            elif err_ratio <= 0.60:
                penalty = round((err_ratio - 0.20) / 0.40 * 0.5, 3)
            else:
                penalty = 0.5
            # Δm 不及预期（激进方案高估增益）→ 额外放大惩罚
            if delta_m < expected_delta_m:
                penalty = round(min(penalty * 1.5, 0.75), 3)
            if penalty > 0:
                logger.info(
                    f"[Layer6] pattern_memory 惩罚系数: {penalty:.3f}  "
                    f"(actual_Δm={delta_m:.3f}  expected_Δm={expected_delta_m:.3f}  "
                    f"err={err_ratio:.1%})"
                )

        memory = _load_pattern_memory()
        zone_name = _humidity_zone_name(self.cfg, pre_h)
        memory.append({
            "timestamp":     time.time(),
            "humidity":      pre_h,
            "zone":          zone_name,
            "optimal_sec":   water_sec,
            "plan_label":    plan_label,
            "delta_m":       round(delta_m, 3),
            "kp_measured":   round(delta_m / max(water_sec, 0.001), 5),
            "quality_score": quality_score,
            "penalty":       penalty,   # Fix-Penalty：0.0=无惩罚；>0=预测高估，降低该记录被选概率
        })
        _save_pattern_memory(memory)
        self._record_irrigation_trial(
            status="accepted",
            reason="accepted_into_pattern_memory",
            water_sec=water_sec,
            pre_h=pre_h,
            post_h=post_h,
            delta_m=delta_m,
            expected_delta_m=expected_delta_m,
            plan_label=plan_label,
            quality_score=quality_score,
            penalty=penalty,
            prediction_meta=prediction_meta,
        )
        # Outcome statistics are independent from K_p acceptance gates. A trial
        # remains an accepted response even when its gain is too far from the
        # current EMA to update K_p safely.
        self._update_irrigation_profile(
            status="accepted",
            reason="accepted_into_pattern_memory",
            pre_h=pre_h,
            post_h=post_h,
            water_sec=water_sec,
            delta_m=delta_m,
            plan_label=plan_label,
            quality_score=quality_score,
            zone_kp=None,
        )
        logger.info(
            f"[Layer6] pattern_memory 写入: "
            f"humidity={pre_h:.1f}  sec={water_sec}  "
            f"Δm={delta_m:+.3f}  quality={quality_score:.3f}  penalty={penalty:.3f}"
        )
        return True

    def _trial_quality_tags(
        self,
        status: str,
        reason: str,
        plan_label: str,
        prediction_meta: Optional[dict] = None,
    ) -> dict[str, Any]:
        state = _load_system_state()
        recovered_at = _state_water_path_recovered_epoch(state)
        exclusions: list[str] = []
        suspect_keys = (
            "water_delivery_suspect",
            "reservoir_empty_suspect",
            "low_wet_recovery_suspect",
            "sensor_fault",
        )
        for key in suspect_keys:
            suspect = state.get(key) if isinstance(state, dict) else None
            if _suspect_blocks_learning_after_repair(suspect, recovered_at):
                exclusions.append(key)

        status_l = str(status or "").lower()
        reason_l = str(reason or "").lower()
        label_l = str(plan_label or "").lower()
        joined = "|".join((status_l, reason_l, label_l))
        if "observation_" in status_l:
            exclusions.append("intermediate_observation")
        if "emergency" in joined:
            exclusions.append("emergency_safety_sample")
        if "timing_emergency" in joined or "watering_window_emergency" in joined:
            exclusions.append("watering_window_emergency_context")
        if "reservoir" in joined:
            exclusions.append("reservoir_retest_sample")
        if reason_l in {"post_h_above_fc_tolerance", "delta_m_above_physical_max"}:
            exclusions.append("unsafe_or_unphysical_outcome")
        if "sensor_spike" in joined:
            exclusions.append("sensor_context_unreliable")
        if "sensor_stale" in joined or "sensor_fault" in joined:
            exclusions.append("sensor_context_unreliable")
        if "manual" in joined or "unknown_direct_mqtt" in joined:
            exclusions.append("manual_or_unknown_watering")
        if isinstance(prediction_meta, dict) and (
            prediction_meta.get("forced_exploration_quarantined")
            or self._forced_exploration_quarantine_reason(reason, prediction_meta)
        ):
            exclusions.append("forced_exploration_quarantined")

        seen: set[str] = set()
        exclusions = [x for x in exclusions if not (x in seen or seen.add(x))]
        learning_valid = status_l in {"accepted", "rejected"} and not exclusions
        if learning_valid:
            quality = "valid_strategy_feedback"
        elif "reservoir_retest_sample" in exclusions:
            quality = "excluded_water_delivery_retest"
        elif "emergency_safety_sample" in exclusions:
            quality = "excluded_safety_response"
        elif "watering_window_emergency_context" in exclusions:
            quality = "excluded_timing_emergency"
        elif "sensor_context_unreliable" in exclusions:
            quality = "excluded_sensor_context"
        elif "unsafe_or_unphysical_outcome" in exclusions:
            quality = "excluded_outcome_quality_gate"
        elif "intermediate_observation" in exclusions:
            quality = "observation_only"
        elif "forced_exploration_quarantined" in exclusions:
            quality = "excluded_forced_exploration_contaminated"
        elif exclusions:
            quality = "excluded_contaminated_context"
        else:
            quality = "non_learning_status"
        return {
            "learning_valid": learning_valid,
            "sample_quality": quality,
            "learning_exclusion_reasons": exclusions,
            "excluded_from_strategy_scoring": not learning_valid,
        }

    def _record_irrigation_trial(
        self,
        status: str,
        reason: str,
        water_sec: float,
        pre_h: float,
        post_h: float,
        delta_m: float,
        expected_delta_m: Optional[float],
        plan_label: str = "",
        quality_score: Optional[float] = None,
        penalty: float = 0.0,
        prediction_meta: Optional[dict] = None,
    ) -> None:
        """
        记录完整浇水试验流水。

        pattern_memory 只保存可作为 reference 的高质量样本；这里保存所有结算结果，
        包括过冲、无效、紧急补水等失败经验，服务第一阶段的反馈学习。
        """
        trials = _load_irrigation_trials()
        zone_name = _humidity_zone_name(self.cfg, pre_h)
        theoretical_delta = _zone_kp(self.cfg, zone_name) * water_sec
        quality_tags = self._trial_quality_tags(status, reason, plan_label, prediction_meta)
        record = {
            "timestamp": time.time(),
            "status": status,
            "reason": reason,
            "plan_label": plan_label,
            "zone": zone_name,
            "humidity_before": round(pre_h, 3),
            "humidity_after": round(post_h, 3),
            "water_sec": round(float(water_sec), 3),
            "delta_m": round(delta_m, 3),
            "expected_delta_m": (
                round(float(expected_delta_m), 3)
                if expected_delta_m is not None else None
            ),
            "theoretical_delta_m": round(theoretical_delta, 3),
            "quality_score": quality_score,
            "penalty": penalty,
            "FC": self.cfg.FC,
            "TARGET_LOW": self.cfg.TARGET_LOW,
            "K_P": self.cfg.K_P,
        }
        record.update(quality_tags)
        if prediction_meta:
            record.update(prediction_meta)
        evidence = classify_trial(record)
        record["evidence_tier"] = evidence.tier
        record["learning_weight"] = evidence.weight
        record["evidence_reason"] = evidence.reason
        _record_recent_response_guard(
            cfg=self.cfg,
            status=status,
            reason=reason,
            plan_label=plan_label,
            zone_name=zone_name,
            pre_h=pre_h,
            post_h=post_h,
            water_sec=water_sec,
            delta_m=delta_m,
        )
        if (
            record.get("watering_window_sample_context") == "poor_window"
            and record.get("learning_valid") is True
        ):
            record["sample_quality"] = "valid_strategy_feedback_poor_window"
            record["watering_window_weight"] = 0.5
        elif record.get("learning_valid") is True:
            record["watering_window_weight"] = 1.0
        trials.append(record)
        _save_irrigation_trials(trials)
        logger.info(
            f"[Layer6] irrigation_trials 记录: status={status} reason={reason} "
            f"sec={water_sec} H={pre_h:.1f}->{post_h:.1f} Δm={delta_m:+.3f} "
            f"learning_valid={record['learning_valid']} "
            f"sample_quality={record['sample_quality']} "
            f"evidence={record['evidence_tier']}({record['learning_weight']:.1f})"
        )


# ===========================================================================
# ⑧ DecisionBrain —— 统一调度六层逻辑的门面类
# ===========================================================================

class DecisionBrain:
    """
    决策大脑门面：协调 Layer 0-6 完成一次完整的 5 分钟决策循环。

    Fix-A：引入 _pending_soak 字段管理非阻塞渗透任务。
    每次 run_cycle() 开始时优先检查上一轮是否有待处理的渗透：
      · 未完成 → 本轮返回 SOAK_PENDING，跳过新的浇水决策
      · 已完成 → 调用 actuator.settle() 完成采样和进化，清除哨兵，继续正常决策

    任务1：
      · __init__ 启动时从 system_state.json 恢复 PendingSoak，防断电失忆
      · run_cycle 复用 settle() 返回的 SensorReading，跳过重复读硬件和双重 CSV 写入

    用法：
        brain = DecisionBrain(cfg)
        result = brain.run_cycle()   # 每 5 分钟调用一次
    """

    def __init__(self, cfg: ConfigManager):
        self.cfg           = cfg
        _bootstrap_high_zone_kp(cfg)
        self.sensor        = SensorLayer(cfg)
        self.physics       = PhysicsLayer(cfg)
        self.gate          = GateLayer(cfg)
        self.predictor     = Phase2Predictor(cfg)   # 任务2：重命名
        self.court         = CostCourt(cfg)
        self.actuator      = ActuatorLayer(cfg, self.sensor)
        self._pending_soak: Optional[PendingSoak] = None
        # The former cross-device experience bridge has deliberately been
        # removed from this soil3-baseline repository.  It was optional metadata,
        # never part of the pump authority, and the remaining calls below are
        # guarded by this None value.
        self._experience_validation = None

        # 任务1：启动时尝试从持久化状态恢复渗透哨兵
        self._restore_pending_soak()

    def _latest_settled_trial(self) -> Optional[dict]:
        """返回最近一次可用于策略反馈的完成样本，不使用中途/水路污染样本。"""
        trials = _load_irrigation_trials()
        for trial in reversed(trials):
            if _trial_learning_valid_legacy(trial):
                return trial
        return None

    def _recent_ineffective_emergency_trials(self) -> list[dict]:
        """返回最近连续的无效 emergency 结算样本。遇到有效样本或非 emergency 即停止。"""
        threshold = float(self.cfg.get_constant("EMERGENCY_INEFFECTIVE_DELTA") or 0.3)
        state = _load_system_state()
        suspect = state.get("water_delivery_suspect") or {}
        repair_epoch = float(suspect.get("repair_confirmed_at") or 0.0)
        ineffective = []
        for trial in reversed(_load_irrigation_trials()):
            trial_ts = float(trial.get("timestamp") or 0.0)
            if repair_epoch > 0 and trial_ts < repair_epoch:
                break
            status = trial.get("status")
            if status in {"observation_300s", "observation_900s"}:
                continue
            if status != "emergency":
                break
            try:
                delta_m = float(trial.get("delta_m"))
            except (TypeError, ValueError):
                break
            if delta_m >= threshold:
                break
            ineffective.append(trial)
        return ineffective

    def _auto_confirm_repair_if_sensor_recovers(
        self, suspect: dict, reading: SensorReading, now: float
    ) -> bool:
        """
        保护期间如果湿度显著回升，说明人工处理或水路恢复可能已经发生。
        不直接解除保护，只安排一次受控复试。
        """
        if not suspect.get("active"):
            return False
        if suspect.get("repair_retest_pending"):
            return False
        if suspect.get("repair_retest_started_at") and not suspect.get("repair_retest_result"):
            return False

        recovery_delta = float(
            self.cfg.get_constant("WATER_DELIVERY_AUTO_RECOVERY_DELTA") or 2.0
        )
        target_margin = float(
            self.cfg.get_constant("WATER_DELIVERY_AUTO_RECOVERY_TARGET_MARGIN") or 1.5
        )
        previous_h = suspect.get("current_humidity")
        try:
            previous_h = float(previous_h)
        except (TypeError, ValueError):
            previous_h = reading.humidity
        recovered_by_jump = reading.humidity - previous_h >= recovery_delta
        recovered_near_target = reading.humidity >= self.cfg.TARGET_LOW - target_margin
        if not (recovered_by_jump or recovered_near_target):
            return False

        def schedule_retest(state):
            latest = state.get("water_delivery_suspect") or {}
            latest["active"] = True
            latest["reason"] = "auto_sensor_recovery_retest_pending"
            latest["repair_confirmed_at"] = now
            latest["repair_confirm_source"] = "auto_sensor_recovery"
            latest["repair_retest_pending"] = True
            latest["repair_retest_result"] = None
            latest["repair_retest_started_at"] = None
            latest["repair_retest_completed_at"] = None
            latest["repair_retest_delta_m"] = None
            latest["repair_recovery_humidity"] = round(reading.humidity, 3)
            latest["repair_recovery_delta_m"] = round(reading.humidity - previous_h, 3)
            latest["pause_until"] = 0.0
            state["water_delivery_suspect"] = latest
        _update_system_state(schedule_retest)
        logger.warning(
            "[Brain] water_delivery_suspect 期间检测到湿度恢复，"
            f"H={reading.humidity:.1f}% previous={previous_h:.1f}%，安排一次受控复试。"
        )
        return True

    def _block_ineffective_emergency_if_needed(self, reading: SensorReading) -> Optional[str]:
        """
        连续紧急补水无效保护。

        如果 emergency 3s 已连续多次不能带来湿度上升，继续自动开泵只会消耗水泵
        或放大水路/出水/探头异常。此时进入 water_delivery_suspect，报警并暂停
        后续 emergency 自动补水一段时间，等待人工检查或下一次受控复试。
        """
        now = time.time()
        pause_sec = float(self.cfg.get_constant("EMERGENCY_INEFFECTIVE_PAUSE_SEC") or 21600)
        repeat_sec = float(self.cfg.get_constant("EMERGENCY_INEFFECTIVE_ALERT_REPEAT_SEC") or 21600)
        threshold = float(self.cfg.get_constant("EMERGENCY_INEFFECTIVE_DELTA") or 0.3)
        state = _load_system_state()
        suspect = state.get("water_delivery_suspect") or {}
        if self._auto_confirm_repair_if_sensor_recovers(suspect, reading, now):
            state = _load_system_state()
            suspect = state.get("water_delivery_suspect") or {}

        if suspect.get("repair_retest_pending"):
            def start_retest(state):
                latest = state.get("water_delivery_suspect") or {}
                latest["active"] = False
                latest["reason"] = "repair_confirmed_retest_in_progress"
                latest["repair_retest_pending"] = False
                latest["repair_retest_started_at"] = now
                latest["repair_retest_result"] = None
                latest["repair_retest_humidity_before"] = round(reading.humidity, 3)
                latest["retry_allowed_at"] = now
                state["water_delivery_suspect"] = latest
            _update_system_state(start_retest)
            logger.warning("[Brain] water_delivery_suspect 已确认修复，允许一次受控 emergency 复试。")
            return None

        ineffective = self._recent_ineffective_emergency_trials()
        max_count = int(self.cfg.get_constant("EMERGENCY_INEFFECTIVE_MAX_COUNT") or 3)
        if len(ineffective) < max_count:
            return None

        first_seen = float(suspect.get("first_seen_at") or now)
        last_alert = float(suspect.get("last_alert_sent_at") or 0.0)
        pause_until = float(suspect.get("pause_until") or 0.0)
        if suspect.get("active") and now >= pause_until > 0:
            def allow_retry(state):
                suspect = state.get("water_delivery_suspect") or {}
                suspect["active"] = False
                suspect["retry_allowed_at"] = now
                suspect["reason"] = "pause_elapsed_allow_one_retry"
                state["water_delivery_suspect"] = suspect
            _update_system_state(allow_retry)
            logger.warning("[Brain] water_delivery_suspect 暂停期已过，允许一次紧急补水复试。")
            return None
        pause_until_value = pause_until if suspect.get("active") and pause_until > now else now + pause_sec

        recent = list(reversed(ineffective[:max_count]))
        deltas = []
        for trial in recent:
            try:
                deltas.append(float(trial.get("delta_m")))
            except (TypeError, ValueError):
                pass
        avg_delta = sum(deltas) / len(deltas) if deltas else 0.0

        state["water_delivery_suspect"] = {
            "active": True,
            "reason": "consecutive_ineffective_emergency",
            "first_seen_at": first_seen,
            "last_blocked_at": now,
            "blocked_count": int(suspect.get("blocked_count") or 0) + 1,
            "ineffective_count": len(ineffective),
            "threshold_count": max_count,
            "delta_threshold": threshold,
            "avg_recent_delta_m": round(avg_delta, 3),
            "current_humidity": round(reading.humidity, 3),
            "target_low": self.cfg.TARGET_LOW,
            "pause_until": pause_until_value,
            "last_alert_sent_at": last_alert,
        }
        for key in (
            "repair_confirmed_at",
            "repair_confirm_source",
            "repair_retest_started_at",
            "repair_retest_completed_at",
            "repair_retest_result",
            "repair_retest_delta_m",
            "repair_retest_humidity_before",
            "repair_recovery_humidity",
            "repair_recovery_delta_m",
        ):
            if suspect.get(key) is not None:
                state["water_delivery_suspect"][key] = suspect.get(key)

        should_alert = now - last_alert >= repeat_sec
        if should_alert:
            subject = "soil3 报警：紧急补水连续无效"
            body = (
                "智养中心检测到 soil3 连续紧急补水无效。\n\n"
                f"当前湿度：{reading.humidity:.1f}%\n"
                f"Target Low：{self.cfg.TARGET_LOW:.1f}%\n"
                f"连续无效次数：{len(ineffective)} 次\n"
                f"最近 {max_count} 次平均 Δm：{avg_delta:+.3f}%\n"
                f"判定阈值：Δm < {threshold:.1f}%\n\n"
                "系统处置：暂停自动紧急补水，避免水泵空跑或无效循环。\n"
                "建议检查：水箱是否缺水、水管是否堵塞或脱落、水泵是否出水、"
                "出水口是否浇到探头附近、土壤探头是否松动或位置异常。"
            )
            if _send_alert_email(subject, body):
                state["water_delivery_suspect"]["last_alert_sent_at"] = now

        def update(state_current):
            state_current["water_delivery_suspect"] = state["water_delivery_suspect"]
        _update_system_state(update)
        return (
            f"连续 {len(ineffective)} 次紧急补水无效，最近平均 Δm={avg_delta:+.3f}%，"
            f"已暂停 automatic emergency {pause_sec/3600:.1f}h，等待检查水路/水箱/探头。"
        )

    def _dynamic_irrigation_cooldown_sec(self, reading: SensorReading) -> tuple[float, str]:
        """
        根据真实反馈调整普通战区冷却。

        固定 6 小时过于僵硬：学习期如果湿度仍靠近低线、下降明显或上次响应偏弱，
        应更早复查；若 3s 已带来强响应或湿度接近 FC，则保持或延长冷却。
        """
        base = float(self.cfg.get_constant("NORMAL_IRRIGATION_COOLDOWN_SEC") or 0.0)
        if base <= 0:
            return 0.0, "disabled"

        min_sec = float(self.cfg.get_constant("DYNAMIC_COOLDOWN_MIN_SEC") or base)
        max_sec = float(self.cfg.get_constant("DYNAMIC_COOLDOWN_MAX_SEC") or base)
        low_sec = float(self.cfg.get_constant("DYNAMIC_COOLDOWN_LOW_SEC") or base)
        mid_sec = float(self.cfg.get_constant("DYNAMIC_COOLDOWN_MID_SEC") or base)
        strong_delta = float(self.cfg.get_constant("DYNAMIC_COOLDOWN_STRONG_DELTA") or 4.0)
        weak_delta = float(self.cfg.get_constant("DYNAMIC_COOLDOWN_WEAK_DELTA") or 2.0)
        fast_drop = float(self.cfg.get_constant("DYNAMIC_COOLDOWN_FAST_DROP_STEP") or -0.12)

        cooldown = base
        reason = "base"
        latest = self._latest_settled_trial()
        last_delta = None
        if latest is not None:
            try:
                last_delta = float(latest.get("delta_m"))
            except (TypeError, ValueError):
                last_delta = None

        fc_gap = self.cfg.FC - reading.humidity
        low_margin = reading.humidity - self.cfg.TARGET_LOW
        slope = reading.recent_slope

        if fc_gap < 3.0:
            cooldown = max(base, max_sec)
            reason = "near_fc_extend"
        elif last_delta is not None and last_delta >= strong_delta and reading.humidity >= self.cfg.TARGET_LOW + 2.0:
            cooldown = base
            reason = "strong_response_keep_base"
        elif low_margin <= 1.5:
            cooldown = low_sec
            reason = "near_target_low"
        elif last_delta is not None and last_delta <= weak_delta:
            cooldown = low_sec
            reason = "weak_last_response"
        elif slope is not None and slope <= fast_drop and low_margin <= 4.0:
            cooldown = low_sec
            reason = "fast_drop"
        elif fc_gap >= 5.0 and low_margin <= 4.0:
            cooldown = mid_sec
            reason = "battle_mid_safe_gap"

        cooldown = max(min_sec, min(float(cooldown), max_sec))
        def update(state):
            state["dynamic_cooldown"] = {
            "cooldown_sec": round(cooldown, 1),
            "base_sec": round(base, 1),
            "reason": reason,
            "humidity": round(reading.humidity, 3),
            "fc_gap": round(fc_gap, 3),
            "target_low_margin": round(low_margin, 3),
            "recent_slope": round(slope, 4) if slope is not None else None,
            "last_delta_m": round(last_delta, 3) if last_delta is not None else None,
            "updated_at": time.time(),
        }
        _update_system_state(update)
        return cooldown, reason

    def _normal_irrigation_cooldown_remaining(self, reading: Optional[SensorReading] = None) -> float:
        """
        普通战区浇水冷却时间。

        缩短主循环只提高观察频率，不应导致普通浇水更频繁；紧急区补水在
        run_cycle 中更早处理，不受这个普通冷却限制。
        """
        if reading is None:
            cooldown = float(self.cfg.get_constant("NORMAL_IRRIGATION_COOLDOWN_SEC") or 0.0)
            reason = "base_no_reading"
        else:
            cooldown, reason = self._dynamic_irrigation_cooldown_sec(reading)
        if cooldown <= 0:
            return 0.0
        state = _load_system_state()
        last_ts = float(state.get("last_normal_irrigation_timestamp") or 0.0)
        if last_ts <= 0:
            return 0.0
        recovered_at = self._water_path_recovered_epoch(state)
        if recovered_at and last_ts <= recovered_at:
            logger.info(
                "[Brain] 普通浇水冷却忽略水路恢复前时间戳，"
                f"last_ts={last_ts:.0f}, recovered_at={recovered_at:.0f}"
            )
            return 0.0
        remaining = max(0.0, cooldown - (time.time() - last_ts))
        logger.info(
            f"[Brain] 普通浇水动态冷却: reason={reason} "
            f"cooldown={cooldown/3600:.2f}h remaining={remaining/3600:.2f}h"
        )
        return remaining

    def _emergency_pulse_sec(self, reading: Optional[SensorReading] = None) -> float:
        """
        紧急区分级补水。

        低于硬安全线越多，脉冲越长；仍受 WATER_SEC_MAX_HARD 约束，避免一次性大水。
        默认分级：
        - mild: 3s
        - moderate: 5s
        - severe: 8s
        - critical: 10s
        """
        base_raw = self.cfg.get_constant("EMERGENCY_WATER_SEC")
        try:
            base_sec = float(base_raw)
        except (TypeError, ValueError):
            base_sec = 3.0
        mild = float(self.cfg.get_constant("EMERGENCY_WATER_SEC_MILD") or base_sec)
        moderate = float(self.cfg.get_constant("EMERGENCY_WATER_SEC_MODERATE") or max(base_sec, 5.0))
        severe = float(self.cfg.get_constant("EMERGENCY_WATER_SEC_SEVERE") or max(moderate, 8.0))
        critical = float(self.cfg.get_constant("EMERGENCY_WATER_SEC_CRITICAL") or max(severe, 10.0))
        hard_cap = float(self.cfg.get_constant("WATER_SEC_MAX_HARD") or critical)
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 1.0)

        sec = mild
        severity = "mild"
        if reading is not None:
            hard_line, _, _ = self.physics._hard_safety_low()
            deficit = max(hard_line - float(reading.humidity), 0.0)
            if deficit >= float(self.cfg.get_constant("EMERGENCY_CRITICAL_DEFICIT") or 12.0):
                sec = critical
                severity = "critical"
            elif deficit >= float(self.cfg.get_constant("EMERGENCY_SEVERE_DEFICIT") or 6.0):
                sec = severe
                severity = "severe"
            elif deficit >= float(self.cfg.get_constant("EMERGENCY_MODERATE_DEFICIT") or 2.0):
                sec = moderate
                severity = "moderate"
            logger.critical(
                f"[Brain] emergency graded pulse: severity={severity}, "
                f"H={reading.humidity:.1f}%, hard_line={hard_line:.1f}%, "
                f"deficit={deficit:.1f}%, sec={sec:.1f}s"
            )

        return max(water_min, min(sec, hard_cap))

    def _reservoir_retest_plan(
        self,
        reading: SensorReading,
        trigger: str = "",
    ) -> Optional[ActionPlan]:
        """疑似水箱/水路无水后的低频恢复复测。"""
        enabled = self.cfg.get_constant("RESERVOIR_RETEST_ENABLED")
        if str(enabled).lower() not in {"1", "true", "yes"}:
            return None

        state = _load_system_state()
        reservoir = state.get("reservoir_empty_suspect") or {}
        low_wet = state.get("low_wet_recovery_suspect") or {}
        active = (
            isinstance(reservoir, dict) and reservoir.get("active")
        ) or (
            isinstance(low_wet, dict) and low_wet.get("active")
        )
        if not active:
            return None

        now = time.time()
        interval = float(self.cfg.get_constant("RESERVOIR_RETEST_INTERVAL_SEC") or 21600)
        sec = float(self.cfg.get_constant("RESERVOIR_RETEST_SEC") or 5.0)
        min_delta = float(self.cfg.get_constant("RESERVOIR_RETEST_MIN_DELTA") or 0.5)
        pause_until = max(
            float(reservoir.get("pause_until") or 0.0) if isinstance(reservoir, dict) else 0.0,
            float(low_wet.get("pause_until") or 0.0) if isinstance(low_wet, dict) else 0.0,
        )
        last_retest = max(
            float(reservoir.get("last_retest_at") or 0.0) if isinstance(reservoir, dict) else 0.0,
            float(low_wet.get("last_retest_at") or 0.0) if isinstance(low_wet, dict) else 0.0,
        )
        due = last_retest <= 0 or now - last_retest >= interval
        if not due:
            last_notice = max(
                float(reservoir.get("last_notice_at") or 0.0) if isinstance(reservoir, dict) else 0.0,
                float(low_wet.get("last_notice_at") or 0.0) if isinstance(low_wet, dict) else 0.0,
            )
            notice_interval = float(self.cfg.get_constant("LOW_WET_RECOVERY_PAUSE_LOG_INTERVAL_SEC") or 1800)
            if now - last_notice >= notice_interval:
                logger.warning(
                    f"[ReservoirRetest] paused: trigger={trigger}, H={reading.humidity:.1f}%, "
                    f"protect_pause_left={max(pause_until - now, 0.0)/60:.1f}min, "
                    f"timed_retest_left={max(interval - (now - last_retest), 0.0)/60:.1f}min"
                )
                def update_notice(current):
                    for key in ("reservoir_empty_suspect", "low_wet_recovery_suspect"):
                        suspect = current.get(key)
                        if isinstance(suspect, dict):
                            suspect["last_notice_at"] = now
                _update_system_state(update_notice)
            return ActionPlan("reservoir_empty_pause", 0.0)

        def mark_retest(current):
            for key in ("reservoir_empty_suspect", "low_wet_recovery_suspect"):
                suspect = current.setdefault(key, {})
                if isinstance(suspect, dict):
                    suspect["active"] = True
                    suspect["last_retest_at"] = now
                    suspect["last_retest_trigger"] = trigger
                    suspect["last_retest_sec"] = sec
                    suspect["retest_count"] = int(suspect.get("retest_count") or 0) + 1
                    suspect["exclude_from_learning"] = True
                    suspect["updated_at"] = now
        _update_system_state(mark_retest)

        plan = ActionPlan("reservoir_retest_probe", sec)
        plan.predicted_peak = round(float(reading.humidity) + min_delta, 3)
        plan.predicted_h12 = None
        logger.warning(
            f"[ReservoirRetest] due: trigger={trigger}, H={reading.humidity:.1f}%, "
            f"probe={sec:.1f}s, success_delta>={min_delta:.1f}%"
        )
        return plan

    def _execute_reservoir_retest(
        self,
        zone: ZoneStatus,
        reading: SensorReading,
        plan: ActionPlan,
    ) -> DecisionResult:
        if plan.water_sec <= 0:
            return self._finish_result(
                zone=zone,
                chosen_plan=plan,
                action_sec=0.0,
                reading=reading,
                notes="疑似水箱/水路无响应，复测间隔未到；暂停自动开泵，避免空泵干跑。",
            )

        expected_delta = max(0.0, (plan.predicted_peak or reading.humidity) - reading.humidity)
        self._pending_soak = self.actuator.execute_pump(
            plan.water_sec,
            reading.humidity,
            is_emergency=False,
            expected_delta_m=expected_delta,
            plan_label=plan.label,
            prediction_plan=plan,
        )
        return self._finish_result(
            zone=zone,
            chosen_plan=plan,
            action_sec=plan.water_sec,
            reading=reading,
            notes=f"执行供水恢复复测 {plan.water_sec:.1f}s；结果只用于解除/保持缺水保护，不进入 K_P 学习。",
        )

    def _water_path_recovered_epoch(self, state: Optional[dict] = None) -> float:
        if state is None:
            state = _load_system_state()
        return _state_water_path_recovered_epoch(state)

    def _quarantine_pre_recovery_low_wet_stats(
        self,
        recovered_at: float,
        delta_m: float,
        baseline: dict[str, Any],
    ) -> None:
        """Keep old low-wet failures visible but stop them blocking post-repair tests."""
        profile = _load_irrigation_profile()
        low = profile.get("zones", {}).get("low", {})
        stats = low.get("strategy_stats", {}).get("low_wet_recovery")
        if not isinstance(stats, dict):
            return
        last_updated = float(stats.get("last_updated") or 0.0)
        if last_updated and last_updated > recovered_at:
            return
        stats["quarantined_before"] = recovered_at
        stats["quarantine_reason"] = "water_path_recovered_after_old_failures"
        stats["post_repair_attempts"] = 0
        stats["post_repair_accepted"] = 0
        stats["post_repair_rejected"] = 0
        stats["last_recovery_delta_m"] = round(float(delta_m), 3)
        stats["last_recovery_baseline"] = baseline
        low["low_wet_recovery_quarantined_at"] = recovered_at
        low["low_wet_recovery_quarantine_reason"] = "water_path_recovered"
        _save_irrigation_profile(profile)
        logger.warning(
            "[LowWetRecovery] quarantined pre-repair low_wet_recovery stats; "
            f"recovered_at={recovered_at:.0f}, delta={delta_m:+.3f}%"
        )

    def _low_wet_recovery_plan(
        self,
        reading: SensorReading,
        style_exp: dict[str, Any],
    ) -> Optional[ActionPlan]:
        """
        Deep drydown 后的低湿恢复脉冲。

        这不是 emergency：硬安全线仍由 emergency 接管。这里处理的是已经完成
        drydown、进入 wet_hold 后仍处在低湿区的情况。目标是减少 3s/4s 小补水，
        用一轮一次的较大恢复脉冲制造可学习的大干大湿样本。
        """
        enabled = self.cfg.get_constant("LOW_WET_RECOVERY_ENABLED")
        if str(enabled).lower() not in {"1", "true", "yes"}:
            return None

        if style_exp.get("force_observe"):
            return None
        if str(style_exp.get("mode") or "") != "wet_hold":
            return None

        h = float(reading.humidity)
        hard_line, _, _ = self.physics._hard_safety_low()
        if h < hard_line:
            return None

        margin = float(self.cfg.get_constant("LOW_WET_RECOVERY_TRIGGER_MARGIN") or 0.5)
        drydown_target = float(
            style_exp.get("drydown_target")
            or self.cfg.get_constant("DEEP_DRYDOWN_TARGET")
            or self.cfg.TARGET_LOW
        )
        trigger_line = max(float(self.cfg.TARGET_LOW), drydown_target) + margin
        if h > trigger_line:
            return None

        now = time.time()
        state = _load_system_state()
        suspect = state.get("low_wet_recovery_suspect") or {}
        repaired_at = self._water_path_recovered_epoch(state)
        suspect_updated = (
            float(suspect.get("updated_at") or 0.0)
            if isinstance(suspect, dict) else 0.0
        )
        suspect_stale_after_repair = bool(repaired_at and suspect_updated and suspect_updated <= repaired_at)
        suspect_blocks_learning = bool(
            isinstance(suspect, dict)
            and (suspect.get("active") or suspect.get("exclude_from_learning"))
            and not suspect_stale_after_repair
        )
        pause_until = (
            float(suspect.get("pause_until") or 0.0)
            if suspect_blocks_learning and isinstance(suspect, dict) else 0.0
        )
        if pause_until > now:
            retest_plan = self._reservoir_retest_plan(reading, "low_wet_recovery_pause")
            return retest_plan or ActionPlan("low_wet_recovery_paused", 0.0)

        profile = _load_irrigation_profile()
        low_stats = (
            profile.get("zones", {})
            .get("low", {})
            .get("strategy_stats", {})
            .get("low_wet_recovery", {})
        )
        rejected = int(low_stats.get("rejected") or 0)
        avg_delta = float(low_stats.get("avg_delta_m") or 0.0)
        reject_limit = int(self.cfg.get_constant("LOW_WET_RECOVERY_INEFFECTIVE_REJECTS") or 3)
        delta_limit = float(self.cfg.get_constant("LOW_WET_RECOVERY_INEFFECTIVE_DELTA") or 0.3)
        stats_updated = float(low_stats.get("last_updated") or 0.0)
        stale_failure_stats = bool(repaired_at and stats_updated and stats_updated <= repaired_at)
        quarantined_before = float(low_stats.get("quarantined_before") or 0.0)
        if quarantined_before and stats_updated <= quarantined_before:
            stale_failure_stats = True
        if rejected >= reject_limit and avg_delta < delta_limit and not stale_failure_stats:
            pause_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_INEFFECTIVE_PAUSE_SEC") or 21600)
            pause_until = now + pause_sec
            def mark_suspect(current):
                suspect_state = {
                    "active": True,
                    "reason": "ineffective_low_wet_recovery",
                    "probable_causes": [
                        "reservoir_empty",
                        "pump_or_tube_no_flow",
                        "outlet_not_near_probe",
                        "probe_position_or_response_abnormal",
                    ],
                    "reservoir_empty_suspect": True,
                    "exclude_from_learning": True,
                    "pause_until": pause_until,
                    "rejected": rejected,
                    "avg_delta_m": round(avg_delta, 3),
                    "current_humidity": round(h, 3),
                    "trigger_line": round(trigger_line, 3),
                    "last_water_sec": low_stats.get("last_water_sec"),
                    "updated_at": now,
                    "last_notice_at": now,
                }
                current["low_wet_recovery_suspect"] = suspect_state
                current["reservoir_empty_suspect"] = {
                    "active": True,
                    "source": "low_wet_recovery",
                    "reason": "large_recovery_no_humidity_response",
                    "pause_until": pause_until,
                    "avg_delta_m": round(avg_delta, 3),
                    "last_water_sec": low_stats.get("last_water_sec"),
                    "updated_at": now,
                }
            _update_system_state(mark_suspect)
            logger.critical(
                f"[LowWetRecovery] ineffective recovery paused: rejected={rejected}, "
                f"avg_delta={avg_delta:.3f}% < {delta_limit:.3f}%, pause={pause_sec/3600:.1f}h"
            )
            return ActionPlan("low_wet_recovery_paused", 0.0)
        if stale_failure_stats:
            logger.warning(
                f"[LowWetRecovery] ignore stale ineffective stats after water path recovery: "
                f"rejected={rejected}, avg_delta={avg_delta:.3f}, "
                f"stats_updated={stats_updated:.0f}, repaired_at={repaired_at:.0f}"
            )

        min_interval = float(self.cfg.get_constant("LOW_WET_RECOVERY_MIN_INTERVAL_SEC") or 3600)
        last_ts = float(state.get("last_normal_irrigation_timestamp") or 0.0)
        if repaired_at and last_ts <= repaired_at:
            last_ts = 0.0
        if last_ts > 0 and (time.time() - last_ts) < min_interval:
            remaining = max(0.0, min_interval - (time.time() - last_ts))
            logger.info(
                f"[LowWetRecovery] interval guard: H={h:.1f}% <= trigger={trigger_line:.1f}% "
                f"but remaining={remaining/60:.1f}min"
            )
            return ActionPlan("low_wet_recovery_interval_observe", 0.0)

        fc_margin = float(self.cfg.get_constant("LOW_WET_RECOVERY_TARGET_FC_MARGIN") or 5.0)
        recovery_target = min(float(self.cfg.FC - fc_margin), float(self.cfg.M_SAFE_SLEEP - 1.0))
        recovery_target = min(recovery_target, self._style_hold_ceiling(style_exp))
        recovery_target = max(recovery_target, trigger_line)
        deficit = max(0.0, recovery_target - h)

        zone_name = _humidity_zone_name(self.cfg, h)
        zone_key = _zone_kp_key(zone_name)
        kp_value = self.cfg.get(zone_key, None)
        try:
            kp = float(kp_value)
        except (TypeError, ValueError):
            kp = _zone_kp(self.cfg, zone_name)
        if kp <= 0:
            kp = max(float(self.cfg.K_P), 0.1)

        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        min_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_MIN_SEC") or 6.0)
        max_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_MAX_SEC") or 10.0)
        deep_trigger = float(self.cfg.get_constant("LOW_WET_RECOVERY_DEEP_TRIGGER") or 30.0)
        deep_min_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_DEEP_MIN_SEC") or 8.0)
        hard_margin = float(self.cfg.get_constant("LOW_WET_RECOVERY_HARD_MARGIN") or 1.0)
        hard_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_HARD_SEC") or max(max_sec, 10.0))
        hard_cap = float(self.cfg.get_constant("WATER_SEC_MAX_HARD") or hard_sec)

        budget_sec = deficit / kp if kp > 0 else min_sec
        sec = max(budget_sec, water_min, min_sec)
        if h <= deep_trigger:
            sec = max(sec, deep_min_sec)
        if h <= hard_line + hard_margin:
            max_sec = max(max_sec, hard_sec)
            sec = max(sec, min(hard_sec, hard_cap))
        sec = round(min(sec, max_sec, hard_cap), 1)

        plan = ActionPlan("low_wet_recovery", sec)
        plan.predicted_peak = round(recovery_target, 3)
        plan.predicted_h12 = None
        logger.warning(
            f"[LowWetRecovery] H={h:.1f}% trigger={trigger_line:.1f}% "
            f"target={recovery_target:.1f}% kp={kp:.3f} deficit={deficit:.1f}% "
            f"budget={budget_sec:.1f}s -> recovery {sec:.1f}s"
        )
        return plan

    def _restore_pending_soak(self) -> None:
        """
        任务1：从 system_state.json 恢复上次断电前未完成的渗透哨兵。

        恢复逻辑：
          · 若 pending_soak 字段存在且渗透时间尚未到 → 恢复哨兵，继续等待
          · 若渗透时间已过（说明断电期间渗透已完成）→ 清除记录，标记为需补采样
            （补采样在下一轮 run_cycle 开头正常执行即可，哨兵 ready=True 会触发 settle）
          · 若字段不存在 → 正常首次启动，不做任何操作
        """
        state = _load_system_state()
        raw   = state.get("pending_soak")
        if not raw:
            return

        try:
            # pump_end_time_wall 是 wall-clock（time.time()），恢复时：
            # elapsed = now_wall - pump_end_time_wall（水泵停止至今已过去多少秒）
            # pump_end_time_mono = time.monotonic() - elapsed（映射到当前 monotonic 轴）
            pump_end_wall = float(raw.get("pump_end_time_wall") or raw.get("pump_end_time", 0))
            elapsed_since_pump = max(0.0, time.time() - pump_end_wall)
            pump_end_mono = time.monotonic() - elapsed_since_pump

            soak = PendingSoak(
                water_sec=float(raw["water_sec"]),
                pre_humidity=float(raw["pre_humidity"]),
                pump_end_time=pump_end_mono,
                soak_duration=float(raw["soak_duration"]),
                is_emergency=bool(raw.get("is_emergency", False)),
                expected_delta_m=raw.get("expected_delta_m"),  # 可为 None
                plan_label=str(raw.get("plan_label") or ""),
                request_id=raw.get("request_id"),
                device_code=raw.get("device_code"),
                prediction_zone=raw.get("prediction_zone"),
                predicted_peak=raw.get("predicted_peak"),
                raw_model_peak=raw.get("raw_model_peak"),
                predicted_h12=raw.get("predicted_h12"),
                predicted_trajectory=list(raw.get("predicted_trajectory") or []),
                shadow_predicted_peak=raw.get("shadow_predicted_peak"),
                shadow_predicted_h12=raw.get("shadow_predicted_h12"),
                shadow_predicted_trajectory=list(
                    raw.get("shadow_predicted_trajectory") or []
                ),
                predicted_minutes_to_peak=raw.get("predicted_minutes_to_peak"),
                prediction_horizon_steps=raw.get("prediction_horizon_steps"),
                prediction_timestamp=raw.get("prediction_timestamp"),
                observed_peak=raw.get("observed_peak", raw.get("pre_humidity")),
                watering_window=dict(raw.get("watering_window") or {}),
                forced_exploration=bool(raw.get("forced_exploration", False)),
                forced_exploration_reason=str(raw.get("forced_exploration_reason") or ""),
                emergency_interrupts=int(raw.get("emergency_interrupts", 0)),
                last_interrupt_time=(
                    time.monotonic() - max(0.0, time.time() - float(raw["last_interrupt_time_wall"]))
                    if raw.get("last_interrupt_time_wall") is not None else None
                ),
                observation_marks=[
                    int(x) for x in raw.get("observation_marks", [])
                    if isinstance(x, (int, float))
                ],
            )
            self._pending_soak = soak
            if soak.ready:
                logger.warning(
                    f"[Brain] 断电恢复：检测到未结算的渗透哨兵（渗透已完成），"
                    f"将在本轮 run_cycle 中执行补采样。"
                )
            else:
                logger.warning(
                    f"[Brain] 断电恢复：渗透哨兵已恢复，"
                    f"剩余等待 {soak.remaining_sec:.0f}s。"
                )
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"[Brain] 渗透哨兵恢复失败（数据损坏），忽略: {e}")
            # 清除损坏记录，防止下次重启重复报错
            def clear_pending(state):
                state.pop("pending_soak", None)
            _update_system_state(clear_pending)

    def _record_soak_observation(self, soak: PendingSoak, reading: SensorReading) -> None:
        """记录浇水后 5/15 分钟等中途反馈，不提前更新 K_P。"""
        marks = self.cfg.get_constant("SOAK_OBSERVATION_MARKS_SEC") or []
        elapsed = time.monotonic() - soak.pump_end_time
        previous_peak = float(soak.observed_peak if soak.observed_peak is not None else soak.pre_humidity)
        soak.observed_peak = max(previous_peak, float(reading.humidity))
        changed = soak.observed_peak != previous_peak
        for raw_mark in marks:
            try:
                mark = int(raw_mark)
            except (TypeError, ValueError):
                continue
            if mark <= 0 or elapsed < mark or mark in soak.observation_marks:
                continue
            delta_m = reading.humidity - soak.pre_humidity
            self.actuator._record_irrigation_trial(
                status=f"observation_{mark}s",
                reason="soak_intermediate_feedback",
                water_sec=soak.water_sec,
                pre_h=soak.pre_humidity,
                post_h=reading.humidity,
                delta_m=delta_m,
                expected_delta_m=soak.expected_delta_m,
                plan_label=soak.plan_label,
                quality_score=None,
                penalty=0.0,
                prediction_meta={
                    "watering_window_level": (soak.watering_window or {}).get("level"),
                    "watering_window_reason": (soak.watering_window or {}).get("reason"),
                    "watering_window_sample_context": (soak.watering_window or {}).get("sample_context"),
                    "watering_window_local_hour": (soak.watering_window or {}).get("local_hour"),
                    "watering_window_allow_explore": (soak.watering_window or {}).get("allow_explore"),
                } if soak.watering_window else None,
            )
            soak.observation_marks.append(mark)
            changed = True
            logger.info(
                f"[Brain] 渗透中途观测 {mark}s: "
                f"H={soak.pre_humidity:.1f}->{reading.humidity:.1f} Δm={delta_m:+.3f}%"
            )
        if changed:
            def update(state):
                if "pending_soak" in state:
                    state["pending_soak"]["observation_marks"] = list(soak.observation_marks)
                    state["pending_soak"]["observed_peak"] = soak.observed_peak
            _update_system_state(update)

    def _backfill_pending_h12(self, reading: SensorReading) -> None:
        """Fill real 12-hour observations near their due time; never fabricate late values."""
        now = time.time()
        completed = 0
        expired = 0
        snapshot = _load_irrigation_trials()
        if not any(
            item.get("h12_status") == "pending"
            and isinstance(item.get("h12_due_at"), (int, float))
            and now >= float(item["h12_due_at"])
            for item in snapshot
        ):
            return

        def update(records):
            nonlocal completed, expired
            if not isinstance(records, list):
                return []
            for item in records:
                if item.get("h12_status") != "pending":
                    continue
                due_at = item.get("h12_due_at")
                if not isinstance(due_at, (int, float)) or now < float(due_at):
                    continue
                offset = now - float(due_at)
                if offset <= 900 and not reading.sensor_stale_hard:
                    actual = round(float(reading.humidity), 3)
                    item["actual_h12"] = actual
                    item["actual_h12_time"] = now
                    item["actual_h12_offset_minutes"] = round(offset / 60.0, 2)
                    item["h12_error"] = (
                        round(actual - float(item["predicted_h12"]), 3)
                        if isinstance(item.get("predicted_h12"), (int, float)) else None
                    )
                    item["shadow_h12_error"] = (
                        round(actual - float(item["shadow_predicted_h12"]), 3)
                        if isinstance(item.get("shadow_predicted_h12"), (int, float)) else None
                    )
                    item["h12_status"] = "complete"
                    completed += 1
                elif offset > 900:
                    item["h12_status"] = "expired_no_timely_observation"
                    expired += 1
            return records[-500:]

        try:
            update_json_locked(_trial_log_path(), [], update)
        except OSError as exc:
            logger.error(f"[irrigation_trials] 12h 实测回填失败: {exc}")
            return
        if completed or expired:
            logger.info(f"[irrigation_trials] 12h 回填完成={completed} 过期={expired}")

    def _consume_exploration_budget(self, plan: ActionPlan) -> None:
        if not plan.label.startswith("explore_"):
            return
        profile = _load_irrigation_profile()
        daily = profile.setdefault("daily_exploration", {"date": time.strftime("%Y-%m-%d"), "used": 0})
        daily["used"] = int(daily.get("used") or 0) + 1
        _save_irrigation_profile(profile)
        logger.info(
            f"[Brain] 主动探索预算已消耗: {daily['used']}/"
            f"{self.cfg.get_constant('EXPLORATION_DAILY_BUDGET')}"
        )

    def _execution_arm_name(self, label: str) -> str:
        label = str(label or "").lower()
        if "style_pulse_10" in label or "large_pulse" in label or "budget_pulse" in label:
            return "large_pulse"
        if "style_pulse_9" in label or "strong" in label:
            return "strong_pulse"
        if "style_pulse_6" in label or "medium" in label:
            return "medium_pulse"
        if "style_pulse_3" in label or "micro" in label:
            return "micro_pulse"
        if "low_wet_recovery" in label or "drydown_recovery" in label:
            return "drydown_recovery"
        if "drydown" in label:
            return "drydown_cycle"
        if "wet_hold" in label:
            return "wet_hold"
        if "observe" in label:
            return "observe"
        return label or "unknown"

    @staticmethod
    def _best_scored_label(scores: dict[str, float], min_score: float = 0.2) -> str:
        if not scores:
            return "insufficient_evidence"
        label, score = max(scores.items(), key=lambda item: item[1])
        if score < min_score:
            return "insufficient_evidence"
        return label

    def _update_plant_learning_profile(self, reading: SensorReading, zone: ZoneStatus) -> None:
        """Summarize what the system has actually learned into two profile axes."""
        now = time.time()
        state = _load_system_state()
        repaired_at = self._water_path_recovered_epoch(state)
        trials = _load_irrigation_trials()
        valid_trials = []
        excluded_after_repair = 0
        for trial in trials:
            if not isinstance(trial, dict):
                continue
            ts = float(trial.get("timestamp") or 0.0)
            if repaired_at and ts < repaired_at:
                continue
            if _trial_learning_valid_legacy(trial):
                valid_trials.append(trial)
            elif ts >= repaired_at:
                excluded_after_repair += 1

        arms: dict[str, dict[str, Any]] = {}
        for trial in valid_trials:
            arm = self._execution_arm_name(str(trial.get("plan_label") or trial.get("status") or ""))
            item = arms.setdefault(
                arm,
                {
                    "attempts": 0,
                    "accepted": 0,
                    "rejected": 0,
                    "delta_sum": 0.0,
                    "water_sum": 0.0,
                    "zones": set(),
                },
            )
            item["attempts"] += 1
            status = str(trial.get("status") or "").lower()
            if status == "accepted":
                item["accepted"] += 1
            elif status == "rejected":
                item["rejected"] += 1
            try:
                item["delta_sum"] += float(trial.get("delta_m") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                item["water_sum"] += float(trial.get("water_sec") or 0.0)
            except (TypeError, ValueError):
                pass
            if trial.get("zone"):
                item["zones"].add(str(trial.get("zone")))

        arm_summary: dict[str, dict[str, Any]] = {}
        execution_scores = {
            "micro_pulse_fit": 0.0,
            "medium_pulse_fit": 0.0,
            "strong_pulse_fit": 0.0,
            "large_refill_needed": 0.0,
            "drydown_recovery_fit": 0.0,
            "wet_hold_fit": 0.0,
        }
        arm_to_profile = {
            "micro_pulse": "micro_pulse_fit",
            "medium_pulse": "medium_pulse_fit",
            "strong_pulse": "strong_pulse_fit",
            "large_pulse": "large_refill_needed",
            "drydown_recovery": "drydown_recovery_fit",
            "wet_hold": "wet_hold_fit",
        }
        for arm, item in arms.items():
            attempts = max(int(item["attempts"]), 0)
            accepted = int(item["accepted"])
            avg_delta = item["delta_sum"] / attempts if attempts else None
            avg_water = item["water_sum"] / attempts if attempts else None
            success_rate = accepted / attempts if attempts else None
            efficiency = (
                item["delta_sum"] / max(item["water_sum"], 0.001)
                if attempts and item["water_sum"] > 0 else None
            )
            arm_summary[arm] = {
                "attempts": attempts,
                "accepted": accepted,
                "rejected": int(item["rejected"]),
                "success_rate": round(success_rate, 3) if success_rate is not None else None,
                "avg_delta_m": round(avg_delta, 3) if avg_delta is not None else None,
                "avg_water_sec": round(avg_water, 3) if avg_water is not None else None,
                "efficiency_delta_per_sec": round(efficiency, 4) if efficiency is not None else None,
                "zones": sorted(item["zones"]),
            }
            profile_key = arm_to_profile.get(arm)
            if profile_key and attempts:
                sample_score = min(attempts / 3.0, 1.0)
                response_score = max(0.0, min((avg_delta or 0.0) / 2.0, 1.0))
                execution_scores[profile_key] = round(sample_score * (success_rate or 0.0) * response_score, 3)

        exp = state.get("irrigation_style_experiment") if isinstance(state, dict) else {}
        exp = exp if isinstance(exp, dict) else {}
        style_mode = str(exp.get("mode") or "unknown")
        observe_streak = int(state.get("battle_observe_streak") or 0)
        hard_line = float((state.get("hard_safety_low_guard") or {}).get("effective_low") or 0.0)
        low_margin = float(reading.humidity) - float(self.cfg.TARGET_LOW)
        hard_margin = float(reading.humidity) - hard_line if hard_line else None

        preference_scores = {
            "drought_tolerant": 0.0,
            "moisture_loving": 0.0,
            "overwet_sensitive": 0.0,
            "balanced_cycle": 0.0,
        }
        if low_margin < 0 and (hard_margin is None or hard_margin > 1.0):
            preference_scores["drought_tolerant"] += min(abs(low_margin) / 3.0, 0.5)
        if observe_streak >= 4 and low_margin <= 1.0 and zone != ZoneStatus.EMERGENCY:
            preference_scores["drought_tolerant"] += 0.25
        if arm_summary.get("drydown_recovery", {}).get("accepted", 0) >= 1:
            preference_scores["balanced_cycle"] += 0.35
        if execution_scores.get("large_refill_needed", 0.0) > 0.25:
            preference_scores["balanced_cycle"] += 0.2
        over_rejects = sum(
            1 for t in valid_trials
            if str(t.get("reason") or "") in {"post_h_above_fc_tolerance", "delta_m_above_physical_max"}
        )
        if over_rejects:
            preference_scores["overwet_sensitive"] += min(over_rejects / 3.0, 0.6)
        if low_margin < -1.5 and observe_streak == 0 and zone == ZoneStatus.EMERGENCY:
            preference_scores["moisture_loving"] += 0.3

        effective_samples = len(valid_trials)
        confidence = round(min(effective_samples / 12.0, 1.0), 3)
        missing = []
        for arm in ("micro_pulse", "medium_pulse", "strong_pulse", "large_pulse", "drydown_recovery"):
            if int(arm_summary.get(arm, {}).get("attempts") or 0) < 2:
                missing.append(f"{arm}:need_valid_samples")
        if confidence < 0.35:
            missing.append("plant_preference:insufficient_valid_episodes")

        plant_profile = {
            "schema_version": 1,
            "updated_at": now,
            "water_path_recovered_at": repaired_at or None,
            "effective_samples_after_repair": effective_samples,
            "excluded_after_repair": excluded_after_repair,
            "confidence": confidence,
            "water_preference": {
                "label": self._best_scored_label(preference_scores, 0.3 if confidence >= 0.35 else 0.8),
                "scores": {k: round(float(v), 3) for k, v in preference_scores.items()},
                "current_low_margin": round(low_margin, 3),
                "current_hard_margin": round(hard_margin, 3) if hard_margin is not None else None,
            },
            "watering_execution": {
                "label": self._best_scored_label(execution_scores, 0.25 if confidence >= 0.35 else 0.8),
                "scores": execution_scores,
                "arms": arm_summary,
            },
            "missing_evidence": missing[:12],
            "interpretation": (
                "only_water_path_recovered"
                if effective_samples < 3 and repaired_at else
                "learning_from_valid_post_repair_trials"
            ),
        }

        def update(current):
            exp_state = current.get("irrigation_style_experiment")
            if not isinstance(exp_state, dict):
                exp_state = {"enabled": True, "mode": style_mode}
            exp_state["plant_profile"] = plant_profile
            current["irrigation_style_experiment"] = exp_state

        _update_system_state(update)

    def _finish_result(
        self,
        zone: ZoneStatus,
        reading: SensorReading,
        action_sec: float = 0.0,
        chosen_plan: Optional[ActionPlan] = None,
        notes: str = "",
    ) -> DecisionResult:
        if chosen_plan is not None:
            _record_phase2_selected_prediction(chosen_plan, reading)
        _append_sensor_log(
            reading.humidity,
            reading.temperature,
            reading.ec_raw,
            reading.ec_norm,
            reading.vpd,
            action_sec,
        )
        def update(state):
            if zone == ZoneStatus.BATTLE_ZONE and action_sec <= 0:
                state["battle_observe_streak"] = int(state.get("battle_observe_streak", 0)) + 1
            elif zone != ZoneStatus.SOAK_PENDING:
                state["battle_observe_streak"] = 0
            if not reading.sensor_stale_hard and state.get("sensor_fault", {}).get("active"):
                state["sensor_fault"] = {"active": False, "cleared_at": time.time()}
        _update_system_state(update)
        try:
            self._update_plant_learning_profile(reading, zone)
        except Exception as e:
            logger.warning(f"[PlantProfile] update failed: {e}")
        if (
            action_sec <= 0
            and zone != ZoneStatus.SOAK_PENDING
            and not getattr(chosen_plan, "trigger_guard_blocked", False)
        ):
            plan_label = chosen_plan.label if chosen_plan is not None else "none"
            self._clear_watering_trigger_guard(
                reading,
                chosen_plan,
                f"observe_cycle:{plan_label}",
            )
        return DecisionResult(
            zone=zone,
            chosen_plan=chosen_plan,
            action_sec=action_sec,
            reading=reading,
            notes=notes,
        )

    def _maybe_force_observe_probe(
        self,
        best_plan: ActionPlan,
        reading: SensorReading,
        ceiling_sec: float,
        style_exp: dict[str, Any],
        trajectories: dict[str, list[float]],
    ) -> Optional[ActionPlan]:
        """
        战区长期 observe 诊断探针。

        长期 observe 只能降低诊断门槛，不能单独制造浇水需求。只有当 Layer 5
        选择 observe、连续观察超过阈值、距离 FC 足够远、物理天花板允许最小脉冲，
        且当前湿度/预测轨迹已经接近低湿目标或硬安全边界时，才把本轮改为一次
        安全探针。这样避免系统退化成“等够时间就浇一下”的定时逻辑。
        """
        if best_plan.water_sec > 0 or reading.sensor_stale_hard:
            return None

        state = _load_system_state()
        streak = int(state.get("battle_observe_streak", 0))
        threshold = int(self.cfg.get_constant("OBSERVE_STREAK_FORCE_THRESHOLD") or 0)
        if threshold <= 0 or streak < threshold:
            return None

        fc_gap = self.cfg.FC - reading.humidity
        min_gap = float(self.cfg.get_constant("OBSERVE_STREAK_FORCE_FC_GAP") or 5.0)
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        probe_sec = float(self.cfg.get_constant("OBSERVE_STREAK_FORCE_SEC") or water_min)
        probe_sec = max(water_min, probe_sec)
        h = float(reading.humidity)
        target_low = float(self.cfg.TARGET_LOW)
        hard_line, _, _ = self.physics._hard_safety_low()
        trigger_margin = float(self.cfg.get_constant("WATER_TRIGGER_TARGET_MARGIN") or 0.5)
        forecast_margin = float(self.cfg.get_constant("WATER_TRIGGER_FORECAST_MARGIN") or 0.3)

        if fc_gap < min_gap or ceiling_sec < water_min:
            logger.info(
                f"[Brain] 长期 observe 已达 {streak} 轮，但安全余量不足，"
                f"fc_gap={fc_gap:.2f}, ceiling={ceiling_sec:.2f}，继续观察。"
            )
            return None

        probe_reason = None
        if h <= target_low + trigger_margin:
            probe_reason = "target_low_recovery"
        elif h <= hard_line + trigger_margin:
            probe_reason = "hard_safety_margin_recovery"
        else:
            observe_traj = trajectories.get("observe") or []
            if observe_traj:
                observe_min = min(float(v) for v in observe_traj)
                if observe_min <= target_low + forecast_margin:
                    probe_reason = f"forecast_drydown_to_target:{observe_min:.2f}"
                elif observe_min <= hard_line + forecast_margin:
                    probe_reason = f"forecast_near_hard_safety:{observe_min:.2f}"

            if probe_reason is None:
                natural = style_exp.get("natural_prediction") if isinstance(style_exp, dict) else {}
                if isinstance(natural, dict):
                    confidence = float(natural.get("confidence") or 0.0)
                    min_conf = float(natural.get("min_confidence") or 0.35)
                    h12 = natural.get("predicted_h12")
                    if confidence >= min_conf and isinstance(h12, (int, float)):
                        if float(h12) <= target_low + forecast_margin:
                            probe_reason = f"natural_h12_drydown_to_target:{float(h12):.2f}"
                        elif float(h12) <= hard_line + forecast_margin:
                            probe_reason = f"natural_h12_near_hard_safety:{float(h12):.2f}"

        if probe_reason is None:
            logger.info(
                f"[Brain] 长期 observe 已达 {streak} 轮，但没有真实补水证据，"
                f"H={h:.2f}, TL={target_low:.2f}, hard={hard_line:.2f}，继续观察。"
            )
            return None

        action_sec = min(round(probe_sec, 1), round(ceiling_sec, 1))
        if action_sec < water_min:
            return None

        forced = ActionPlan(
            label="observe_guard_probe",
            water_sec=action_sec,
            cost_J=best_plan.cost_J,
            selected=True,
        )
        forced.predicted_trajectory = [
            reading.humidity + _zone_kp(self.cfg, _humidity_zone_name(self.cfg, reading.humidity)) * action_sec
        ]
        logger.warning(
            f"[Brain] 连续 observe={streak} 轮，触发长期观察保护探针："
            f"{action_sec}s，reason={probe_reason}，fc_gap={fc_gap:.2f}，"
            f"ceiling={ceiling_sec:.2f}。"
        )
        return forced

    def _plan_predicted_peak(self, plan: ActionPlan) -> Optional[float]:
        if isinstance(plan.predicted_peak, (int, float)):
            return float(plan.predicted_peak)
        trajectory = plan.predicted_trajectory or []
        if trajectory:
            return float(max(trajectory))
        return None

    def _physical_peak_for_plan(self, reading: SensorReading, plan: ActionPlan) -> float:
        zone_name = _humidity_zone_name(self.cfg, float(reading.humidity))
        kp, _guard = _guarded_zone_kp(self.cfg, zone_name)
        return float(reading.humidity) + max(0.0, kp) * max(0.0, float(plan.water_sec))

    def _watering_budget_sec(
        self,
        reading: SensorReading,
        style_exp: dict[str, Any],
    ) -> tuple[float, float, str]:
        """Return the largest useful single-shot seconds for a target refill budget."""
        h = float(reading.humidity)
        hold_ceiling = self._style_hold_ceiling(style_exp)
        target = min(
            hold_ceiling,
            float(self.cfg.M_SAFE_SLEEP) - 0.5,
            float(self.cfg.FC) - 0.3,
        )
        target = max(target, float(self.cfg.TARGET_LOW))
        deficit = max(0.0, target - h)

        zone_name = _humidity_zone_name(self.cfg, h)
        zone_key = _zone_kp_key(zone_name)
        kp_value = self.cfg.get(zone_key, None)
        try:
            kp = float(kp_value)
        except (TypeError, ValueError):
            kp = _zone_kp(self.cfg, zone_name)
        if kp <= 0:
            kp = max(float(self.cfg.K_P), 0.1)
        guard = _recent_response_guard(zone_name)
        guard_reason = ""
        if guard:
            guard_kp = float(guard["delta_per_sec"])
            if guard_kp > kp:
                kp = guard_kp
                guard_reason = (
                    f", response_guard={guard_kp:.3f}%/s "
                    f"from {guard.get('plan_label')}/{guard.get('water_sec')}s "
                    f"Δm={guard.get('delta_m')}"
                )

        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        hard_cap = float(self.cfg.get_constant("WATER_SEC_MAX_HARD") or 30.0)
        budget = min(max(deficit / kp if kp > 0 else 0.0, 0.0), hard_cap)
        if 0 < budget < water_min:
            budget = water_min
        return (
            round(budget, 1),
            round(target, 3),
            f"target={target:.1f}, deficit={deficit:.1f}, zone={zone_name}, {zone_key}={kp:.3f}{guard_reason}",
        )

    def _largest_safe_budget_plan(
        self,
        plans: list[ActionPlan],
        reading: SensorReading,
        style_exp: dict[str, Any],
    ) -> Optional[ActionPlan]:
        budget_sec, _target, budget_reason = self._watering_budget_sec(reading, style_exp)
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        if budget_sec < water_min:
            return None

        hard_overwet_line = min(
            float(self.cfg.FC) - 0.2,
            float(self.cfg.M_SAFE_SLEEP) - 0.2,
        )
        candidates: list[ActionPlan] = []
        for plan in plans:
            if plan.water_sec <= 0 or getattr(plan, "style_blocked", False):
                continue
            if not math.isfinite(plan.cost_J):
                continue
            if float(plan.water_sec) > budget_sec + 0.05:
                continue
            predicted_peak = self._plan_predicted_peak(plan)
            physical_peak = self._physical_peak_for_plan(reading, plan)
            if physical_peak > hard_overwet_line:
                logger.info(
                    f"[ResponseGuard] budget veto {plan.label}/{plan.water_sec:.1f}s: "
                    f"guarded_peak={physical_peak:.2f}% > limit={hard_overwet_line:.2f}%."
                )
                continue
            if (
                isinstance(predicted_peak, (int, float))
                and float(predicted_peak) > hard_overwet_line
                and not getattr(plan, "phase2_soft_risk", False)
            ):
                continue
            if getattr(plan, "phase2_soft_risk", False):
                if physical_peak > hard_overwet_line:
                    continue
            candidates.append(plan)

        if not candidates:
            return None
        chosen = max(candidates, key=lambda p: (float(p.water_sec), -float(p.cost_J)))
        logger.info(
            f"[TriggerGuard] refill budget largest safe plan: "
            f"{chosen.label}/{chosen.water_sec:.1f}s within {budget_sec:.1f}s "
            f"({budget_reason}, peak_limit={hard_overwet_line:.1f}%)."
        )
        return chosen

    def _maybe_apply_nightly_forced_exploration(
        self,
        best_plan: ActionPlan,
        plans: list[ActionPlan],
        reading: SensorReading,
        watering_window: dict[str, Any],
        trigger_reason: str,
    ) -> Optional[ActionPlan]:
        advice = _load_nightly_learning_advice()
        if not advice.get("active"):
            return None
        if best_plan.water_sec <= 0:
            return None
        if not self._is_real_watering_need_trigger(trigger_reason):
            logger.info(
                "[NightlyAdvice] skip forced exploration without real watering need: "
                f"plan={best_plan.label}/{best_plan.water_sec:.1f}s "
                f"trigger={trigger_reason}"
            )
            return None
        if str(watering_window.get("level") or "") == "forbidden" and not bool(
            watering_window.get("urgent_low")
        ):
            return None

        arms = advice.get("arms") if isinstance(advice.get("arms"), dict) else {}
        prefer = arms.get("prefer") if isinstance(arms.get("prefer"), dict) else {}
        notes = arms.get("notes") if isinstance(arms.get("notes"), list) else []
        shortage_arms = {
            str(note).split(":", 1)[0]
            for note in notes
            if str(note).endswith(":sample_shortage")
        }
        target_arms = {
            arm
            for arm, weight in prefer.items()
            if arm in {"medium_pulse", "strong_pulse", "large_pulse"}
            and float(weight or 0.0) > 0.0
            and (not shortage_arms or arm in shortage_arms)
        }
        if not target_arms:
            return None

        today = time.strftime("%Y-%m-%d")
        advice_id = str(advice.get("advice_id") or "")
        profile = _load_irrigation_profile()
        forced = profile.setdefault("nightly_forced_exploration", {})
        if forced.get("date") != today:
            forced.clear()
            forced.update({"date": today, "used": 0})
        if int(forced.get("used") or 0) >= 1:
            return None

        hard_overwet_line = min(
            float(self.cfg.FC) - 0.2,
            float(self.cfg.M_SAFE_SLEEP) - 0.2,
        )
        candidates: list[ActionPlan] = []
        for plan in plans:
            if plan.water_sec <= best_plan.water_sec + 0.05:
                continue
            if getattr(plan, "style_blocked", False) or not math.isfinite(plan.cost_J):
                continue
            arm = _learning_advice_arm_name(plan)
            if arm not in target_arms:
                continue
            predicted_peak = self._plan_predicted_peak(plan)
            if (
                isinstance(predicted_peak, (int, float))
                and float(predicted_peak) > hard_overwet_line
                and not getattr(plan, "phase2_soft_risk", False)
            ):
                continue
            if getattr(plan, "phase2_soft_risk", False):
                physical_peak = self._physical_peak_for_plan(reading, plan)
                if physical_peak > hard_overwet_line:
                    continue
            candidates.append(plan)

        if not candidates:
            return None

        arm_counts = profile.setdefault("style_learning_arm_counts", {})
        if not isinstance(arm_counts, dict):
            arm_counts = {}
            profile["style_learning_arm_counts"] = arm_counts
        chosen = min(
            candidates,
            key=lambda p: (
                int(arm_counts.get(_learning_advice_arm_name(p), 0) or 0),
                float(p.water_sec),
                float(p.cost_J),
            ),
        )
        chosen_arm = _learning_advice_arm_name(chosen)
        chosen.forced_exploration = True
        chosen.forced_exploration_reason = "nightly_advice_sample_shortage_rotate_real_watering"
        arm_counts[chosen_arm] = int(arm_counts.get(chosen_arm, 0) or 0) + 1
        forced.update({
            "date": today,
            "used": int(forced.get("used") or 0) + 1,
            "advice_id": advice_id,
            "chosen_label": chosen.label,
            "chosen_sec": round(float(chosen.water_sec), 3),
            "previous_label": best_plan.label,
            "previous_sec": round(float(best_plan.water_sec), 3),
            "target_arms": sorted(target_arms),
            "chosen_arm": chosen_arm,
            "trigger_reason": trigger_reason,
            "arm_counts": dict(arm_counts),
            "reason": "nightly_advice_sample_shortage_rotate_real_watering",
            "updated_at": time.time(),
        })
        profile["nightly_forced_exploration"] = forced
        _save_irrigation_profile(profile)

        def update(state):
            exp = state.get("irrigation_style_experiment")
            if not isinstance(exp, dict):
                exp = {"enabled": True, "mode": "unknown"}
            exp["nightly_forced_exploration"] = dict(forced)
            state["irrigation_style_experiment"] = exp

        _update_system_state(update)
        logger.warning(
            "[NightlyAdvice] rotate safe exploration on real watering need: "
            f"{best_plan.label}/{best_plan.water_sec:.1f}s -> "
            f"{chosen.label}/{chosen.water_sec:.1f}s "
            f"arm={chosen_arm} arms={sorted(target_arms)} "
            f"trigger={trigger_reason} advice={advice_id}"
        )
        return chosen

    def _is_real_watering_need_trigger(self, trigger_reason: str) -> bool:
        """Return True only when watering is needed, not merely useful for samples."""
        reason = str(trigger_reason or "")
        if (
            "target_low_recovery" in reason
            or "hard_safety_margin_recovery" in reason
            or reason.startswith("forecast_drydown_to_target")
            or reason.startswith("forecast_near_hard_safety")
            or reason.startswith("natural_h12_drydown_to_target")
            or reason.startswith("natural_h12_near_hard_safety")
        ):
            return True
        return False

    def _watering_trigger_reason(
        self,
        plan: ActionPlan,
        reading: SensorReading,
        style_exp: dict[str, Any],
        trajectories: dict[str, list[float]],
    ) -> tuple[bool, str]:
        """Cooldown only opens the gate; this decides whether watering has evidence."""
        if plan.water_sec <= 0:
            return True, "observe_selected"
        if reading.sensor_stale_hard:
            return False, "sensor_stale_no_auto_water"
        if getattr(plan, "style_blocked", False):
            return False, "plan_blocked_by_style_guard"

        h = float(reading.humidity)
        target_low = float(self.cfg.TARGET_LOW)
        hard_line, _, _ = self.physics._hard_safety_low()
        trigger_margin = float(self.cfg.get_constant("WATER_TRIGGER_TARGET_MARGIN") or 0.5)
        forecast_margin = float(self.cfg.get_constant("WATER_TRIGGER_FORECAST_MARGIN") or 0.3)

        if h <= target_low + trigger_margin:
            return True, "target_low_recovery"
        if h <= hard_line + trigger_margin:
            return True, "hard_safety_margin_recovery"

        observe_traj = trajectories.get("observe") or []
        if observe_traj:
            observe_min = min(float(v) for v in observe_traj)
            if observe_min <= target_low + forecast_margin:
                return True, f"forecast_drydown_to_target:{observe_min:.2f}"
            if observe_min <= hard_line + forecast_margin:
                return True, f"forecast_near_hard_safety:{observe_min:.2f}"

        natural = style_exp.get("natural_prediction") if isinstance(style_exp, dict) else {}
        if isinstance(natural, dict):
            confidence = float(natural.get("confidence") or 0.0)
            min_conf = float(natural.get("min_confidence") or 0.35)
            h12 = natural.get("predicted_h12")
            if confidence >= min_conf and isinstance(h12, (int, float)):
                if float(h12) <= target_low + forecast_margin:
                    return True, f"natural_h12_drydown_to_target:{float(h12):.2f}"
                if float(h12) <= hard_line + forecast_margin:
                    return True, f"natural_h12_near_hard_safety:{float(h12):.2f}"

        if plan.label == "observe_guard_probe":
            return False, "long_observe_probe_without_watering_need"

        if getattr(plan, "phase2_calibration_probe", False):
            state = _load_system_state()
            advice = _load_nightly_learning_advice(state)
            streak = int(state.get("battle_observe_streak", 0))
            min_streak = int(self.cfg.get_constant("PHASE2_CALIBRATION_MIN_OBSERVE_STREAK") or 6)
            exploration_bias = float((advice.get("exploration") or {}).get("bias_delta") or 0.0)
            if advice.get("active") and exploration_bias > 0:
                min_streak = max(1, min_streak - int(round(exploration_bias * 4)))
            hard_overwet_line = min(
                float(self.cfg.FC) - 0.2,
                float(self.cfg.M_SAFE_SLEEP) - 0.2,
            )
            physical_peak = self._physical_peak_for_plan(reading, plan)
            if streak >= min_streak and physical_peak <= hard_overwet_line:
                return True, f"phase2_calibration_probe:streak={streak},physical_peak={physical_peak:.2f}"
            return False, (
                "phase2_calibration_without_margin:"
                f"streak={streak}/{min_streak},physical_peak={physical_peak:.2f},"
                f"limit={hard_overwet_line:.2f}"
            )

        if plan.label.startswith("style_pulse_"):
            state = _load_system_state()
            advice = _load_nightly_learning_advice(state)
            if advice.get("active") and (advice.get("sample_quality") or {}).get("exclude_new_learning"):
                return False, "nightly_advice_blocks_learning_probe:sample_excluded"
            streak = int(state.get("battle_observe_streak", 0))
            min_streak = int(self.cfg.get_constant("STYLE_LEARNING_PROBE_MIN_OBSERVE_STREAK") or 2)
            fc_gap = float(self.cfg.FC - h)
            min_gap = float(self.cfg.get_constant("STYLE_LEARNING_PROBE_MIN_FC_GAP") or 2.0)
            near_target_margin = float(
                self.cfg.get_constant("STYLE_LEARNING_PROBE_TARGET_MARGIN") or 1.2
            )
            exploration_bias = float((advice.get("exploration") or {}).get("bias_delta") or 0.0)
            if advice.get("active") and exploration_bias > 0:
                min_streak = max(1, min_streak - int(round(exploration_bias * 4)))
                min_gap = max(0.5, min_gap - exploration_bias)
            hold_ceiling = self._style_hold_ceiling(style_exp)
            forecast_needs_water = False
            observe_traj = trajectories.get("observe") or []
            if observe_traj:
                observe_min = min(float(v) for v in observe_traj)
                forecast_needs_water = observe_min <= target_low + forecast_margin
            near_target = h <= target_low + near_target_margin
            if (
                streak >= min_streak
                and fc_gap >= min_gap
                and h <= hold_ceiling
                and (near_target or forecast_needs_water)
            ):
                if forecast_needs_water:
                    return True, f"forecast_drydown_to_target_style_probe:{streak}"
                return True, f"target_low_style_probe:{streak}"
            return False, (
                "learning_probe_without_evidence:"
                f"streak={streak}/{min_streak},fc_gap={fc_gap:.2f}/{min_gap:.2f},"
                f"H={h:.2f},target_margin={target_low + near_target_margin:.2f},"
                f"hold_ceiling={hold_ceiling:.2f}"
            )

        return False, "no_watering_trigger_evidence"

    def _force_observe_for_missing_trigger(
        self,
        plan: ActionPlan,
        reading: SensorReading,
        reason: str,
    ) -> ActionPlan:
        forced = ActionPlan(
            label="trigger_guard_observe",
            water_sec=0.0,
            cost_J=plan.cost_J,
            selected=True,
        )
        forced.predicted_trajectory = list(plan.predicted_trajectory)
        forced.predicted_peak = plan.predicted_peak
        forced.predicted_h12 = plan.predicted_h12
        setattr(forced, "trigger_guard_blocked", True)

        def update(state):
            state["watering_trigger_guard"] = {
                "active": True,
                "blocked": True,
                "reason": reason,
                "candidate_plan": plan.label,
                "candidate_water_sec": round(float(plan.water_sec), 3),
                "humidity": round(float(reading.humidity), 3),
                "target_low": round(float(self.cfg.TARGET_LOW), 3),
                "fc": round(float(self.cfg.FC), 3),
                "updated_at": time.time(),
            }
        _update_system_state(update)
        logger.info(
            f"[TriggerGuard] blocked watering without evidence: "
            f"{plan.label}/{plan.water_sec:.1f}s reason={reason}"
        )
        return forced

    def _record_watering_trigger(
        self,
        plan: ActionPlan,
        reading: SensorReading,
        reason: str,
    ) -> None:
        def update(state):
            state["watering_trigger_guard"] = {
                "active": True,
                "blocked": False,
                "reason": reason,
                "candidate_plan": plan.label,
                "candidate_water_sec": round(float(plan.water_sec), 3),
                "humidity": round(float(reading.humidity), 3),
                "target_low": round(float(self.cfg.TARGET_LOW), 3),
                "fc": round(float(self.cfg.FC), 3),
                "updated_at": time.time(),
            }
        _update_system_state(update)


    def _clear_watering_trigger_guard(
        self,
        reading: SensorReading,
        chosen_plan: Optional[ActionPlan],
        reason: str,
    ) -> None:
        plan_label = chosen_plan.label if chosen_plan is not None else None

        def update(state):
            state["watering_trigger_guard"] = {
                "active": False,
                "blocked": False,
                "reason": reason,
                "candidate_plan": plan_label,
                "candidate_water_sec": 0.0,
                "humidity": round(float(reading.humidity), 3),
                "target_low": round(float(self.cfg.TARGET_LOW), 3),
                "fc": round(float(self.cfg.FC), 3),
                "cleared_at": time.time(),
                "updated_at": time.time(),
            }
        _update_system_state(update)



    def _record_style_experiment_profile(
        self,
        exp: dict[str, Any],
        reading: SensorReading,
        previous: Optional[dict[str, Any]] = None,
    ) -> None:
        profile = _load_irrigation_profile()
        style = profile.setdefault("style_experiment", {})
        segments = style.setdefault("segments", [])
        now = time.time()

        prev_mode = previous.get("mode") if isinstance(previous, dict) else None
        new_mode = exp.get("mode")
        if prev_mode and new_mode and prev_mode != new_mode:
            segments.append(
                {
                    "mode": prev_mode,
                    "started_at": previous.get("started_at"),
                    "ended_at": now,
                    "end_humidity": round(float(reading.humidity), 3),
                    "reason": exp.get("reason"),
                    "drydown_target": previous.get("drydown_target"),
                    "hard_safety_low": previous.get("hard_safety_low"),
                }
            )
            del segments[:-20]

        current = style.setdefault("current", {})
        if current.get("mode") != new_mode or current.get("started_at") != exp.get("started_at"):
            current.clear()
            current.update(
                {
                    "mode": new_mode,
                    "started_at": exp.get("started_at"),
                    "start_humidity": round(float(reading.humidity), 3),
                    "min_humidity": round(float(reading.humidity), 3),
                    "max_humidity": round(float(reading.humidity), 3),
                    "observations": 0,
                }
            )

        current["last_humidity"] = round(float(reading.humidity), 3)
        current["last_seen_at"] = now
        current["drydown_target"] = exp.get("drydown_target")
        current["hard_safety_low"] = exp.get("hard_safety_low")
        current["force_observe"] = exp.get("force_observe")
        current["observations"] = int(current.get("observations") or 0) + 1
        current["min_humidity"] = round(
            min(float(current.get("min_humidity", reading.humidity)), float(reading.humidity)),
            3,
        )
        current["max_humidity"] = round(
            max(float(current.get("max_humidity", reading.humidity)), float(reading.humidity)),
            3,
        )
        style["updated_at"] = now
        _save_irrigation_profile(profile)

    def _deep_drydown_vpd_target(self, reading: SensorReading, base_target: float) -> tuple[float, str]:
        vpd = float(getattr(reading, "vpd", 0.0) or 0.0)
        low = float(self.cfg.get_constant("DEEP_DRYDOWN_VPD_LOW") or 1.0)
        high = float(self.cfg.get_constant("DEEP_DRYDOWN_VPD_HIGH") or 1.4)
        high_boost = float(self.cfg.get_constant("DEEP_DRYDOWN_VPD_HIGH_TARGET") or 32.0)
        mid_boost = float(self.cfg.get_constant("DEEP_DRYDOWN_VPD_MID_TARGET") or 31.0)
        if vpd >= high:
            return max(base_target, high_boost), "high_vpd"
        if vpd >= low:
            return max(base_target, mid_boost), "mid_vpd"
        return base_target, "normal_vpd"

    def _latest_natural_prediction(self) -> dict[str, Any]:
        response_path = self.cfg.get_constant("PHASE2_RESPONSE_PATH") or "/dev/shm/pred_response.json"
        try:
            response = load_json_locked(Path(response_path), {})
            metric = (response.get("candidate_metrics") or {}).get("observe") or {}
            if metric:
                return {
                    "source_time": response.get("timestamp"),
                    "confidence": float(metric.get("confidence") or 0.0),
                    "predicted_h12": float(metric.get("predicted_humidity_12h") or 0.0),
                    "predicted_peak": float(metric.get("predicted_peak") or 0.0),
                    "source": "phase2_response_observe",
                }
        except Exception as exc:
            logger.warning(f"[StyleExperiment] phase2 response read failed: {exc}")
        path = self.cfg.get_constant("NATURAL_PREDICTIONS_CSV") or "/root/water/wyc_training/natural_predictions.csv"
        try:
            rows = read_csv_dicts_locked(Path(path))
        except Exception as exc:
            logger.warning(f"[StyleExperiment] natural prediction read failed: {exc}")
            return {}
        if not rows:
            return {}
        row = rows[-1]
        try:
            confidence = float(row.get("confidence") or 0.0)
            h12 = float(row.get("predicted_humidity_12h") or row.get("predicted_humidity_h12") or 0.0)
            peak = float(row.get("predicted_peak") or row.get("humidity") or 0.0)
        except (TypeError, ValueError):
            return {}
        return {
            "source_time": row.get("source_time"),
            "confidence": confidence,
            "predicted_h12": h12,
            "predicted_peak": peak,
            "source": "natural_predictions_csv",
        }

    def _deep_drydown_phase2_guard(self, reading: SensorReading, exp: dict[str, Any]) -> dict[str, Any]:
        if str(exp.get("mode") or "") != "drydown_cycle" or not exp.get("force_observe"):
            return exp
        min_conf = float(self.cfg.get_constant("DEEP_DRYDOWN_MIN_NATURAL_CONFIDENCE") or 0.35)
        switch_margin = float(self.cfg.get_constant("DEEP_DRYDOWN_PREDICTED_SWITCH_MARGIN") or 0.8)
        hard_margin = float(self.cfg.get_constant("DEEP_DRYDOWN_PREDICTED_HARD_MARGIN") or 1.0)
        pred = self._latest_natural_prediction()
        if not pred:
            return exp
        hard_line, _, _ = self.physics._hard_safety_low()
        target = float(exp.get("drydown_target") or self.cfg.get_constant("DEEP_DRYDOWN_TARGET") or 30.0)
        predicted_h12 = float(pred.get("predicted_h12") or 0.0)
        confidence = float(pred.get("confidence") or 0.0)
        exp["natural_prediction"] = {
            "source_time": pred.get("source_time"),
            "source": pred.get("source"),
            "confidence": round(confidence, 4),
            "predicted_h12": round(predicted_h12, 3),
            "predicted_peak": round(float(pred.get("predicted_peak") or 0.0), 3),
            "min_confidence": min_conf,
        }
        if confidence < min_conf:
            exp["phase2_guard_reason"] = "low_confidence"
            return exp
        if predicted_h12 <= hard_line + hard_margin or predicted_h12 <= target + switch_margin:
            exp["mode"] = "wet_hold"
            exp["force_observe"] = False
            exp["reason"] = "phase2_predicted_drydown_floor"
            exp["last_drydown_min_humidity"] = round(float(reading.humidity), 3)
            exp["phase2_guard_reason"] = "predicted_h12_floor"
            exp["updated_at"] = time.time()
            logger.warning(
                f"[StyleExperiment] Phase2 natural guard switches to wet_hold: "
                f"H={reading.humidity:.1f}%, predicted_h12={predicted_h12:.2f}%, "
                f"target={target:.1f}%, hard={hard_line:.1f}%, confidence={confidence:.3f}"
            )
        else:
            exp["phase2_guard_reason"] = "continue_drydown"
        return exp

    def _style_experiment(self, reading: SensorReading) -> dict[str, Any]:
        """
        Alternate between wet-hold and drydown-cycle exploration without species assumptions.

        The drydown phase deliberately observes while humidity is still comfortably above
        TARGET_LOW, so the system can learn whether a wider dry/wet swing is acceptable.
        Emergency logic still has priority before this method is reached.
        """
        raw_enabled = self.cfg.get_constant("IRRIGATION_STYLE_EXPERIMENT_ENABLED")
        enabled = bool(raw_enabled) if raw_enabled is not None else True
        if not enabled:
            return {"enabled": False, "mode": "off", "force_observe": False}

        now = time.time()
        state = _load_system_state()
        exp = state.get("irrigation_style_experiment")
        previous_exp = dict(exp) if isinstance(exp, dict) else None
        dry_hours = float(self.cfg.get_constant("DRYDOWN_EXPERIMENT_HOURS") or 36.0)
        wet_hours = float(self.cfg.get_constant("WET_HOLD_EXPERIMENT_HOURS") or 24.0)
        margin = float(self.cfg.get_constant("DRYDOWN_TARGET_MARGIN") or 2.5)
        extension = float(self.cfg.get_constant("DRYDOWN_BELOW_TARGET_STEP") or 1.0)
        max_extension = float(self.cfg.get_constant("DRYDOWN_BELOW_TARGET_MAX") or 3.0)
        state_extension = float((exp or {}).get("below_target_extension") or extension) if isinstance(exp, dict) else extension
        below_target_extension = min(max(state_extension, 0.0), max_extension)
        deep_enabled_raw = self.cfg.get_constant("DEEP_DRYDOWN_EXPERIMENT_ENABLED")
        deep_enabled = bool(deep_enabled_raw) if deep_enabled_raw is not None else True
        if deep_enabled:
            base_target = float(self.cfg.get_constant("DEEP_DRYDOWN_TARGET") or 30.0)
            target, vpd_target_reason = self._deep_drydown_vpd_target(reading, base_target)
        else:
            vpd_target_reason = "disabled"
            target_gap = max(float(self.cfg.FC - self.cfg.TARGET_LOW), 0.0)
            narrow_gap = float(self.cfg.get_constant("TARGET_LOW_NARROW_GAP") or 8.0)
            if target_gap < narrow_gap:
                initial_gap = float(self.cfg.get_constant("DRYDOWN_NARROW_BAND_INITIAL_FC_GAP") or 8.0)
                max_gap = float(self.cfg.get_constant("DRYDOWN_NARROW_BAND_MAX_FC_GAP") or 14.0)
                target = self.cfg.FC - initial_gap
                if isinstance(exp, dict) and exp.get("mode") == "drydown_cycle" and float(reading.humidity) <= target:
                    target = max(self.cfg.FC - max_gap, target - below_target_extension)
            else:
                target = min(self.cfg.M_SAFE_SLEEP - 1.0, self.cfg.TARGET_LOW + margin)
                if isinstance(exp, dict) and exp.get("mode") == "drydown_cycle" and float(reading.humidity) <= target:
                    target = max(self.cfg.TARGET_LOW - below_target_extension, self.cfg.TARGET_LOW - max_extension)

        if not isinstance(exp, dict) or "mode" not in exp or "started_at" not in exp:
            exp = {
                "enabled": True,
                "mode": "drydown_cycle",
                "started_at": now,
                "drydown_target": round(target, 3),
                "reason": "initial_style_exploration",
            }
        else:
            started_at = float(exp.get("started_at") or now)
            mode = str(exp.get("mode") or "drydown_cycle")
            elapsed_h = (now - started_at) / 3600.0
            deep_drydown_time_complete = (elapsed_h >= dry_hours) and not deep_enabled
            target_low_crossed = mode == "drydown_cycle" and float(reading.humidity) <= float(self.cfg.TARGET_LOW)
            if mode == "drydown_cycle" and target_low_crossed and float(reading.humidity) > float(target):
                exp["target_low_crossed_at"] = now
                exp["target_low_crossed_humidity"] = round(float(reading.humidity), 3)
                exp["target_low_crossed_action"] = "continue_drydown_to_exploration_floor"
            if mode == "drydown_cycle" and (deep_drydown_time_complete or reading.humidity <= target):
                reason = "drydown_window_complete"
                exp = {
                    "enabled": True,
                    "mode": "wet_hold",
                    "started_at": now,
                    "drydown_target": round(target, 3),
                    "reason": reason,
                    "last_drydown_min_humidity": round(float(reading.humidity), 3),
                }
            elif mode == "wet_hold" and elapsed_h >= wet_hours:
                exp = {
                    "enabled": True,
                    "mode": "drydown_cycle",
                    "started_at": now,
                    "drydown_target": round(target, 3),
                    "reason": "wet_hold_window_complete",
                }

        mode = str(exp.get("mode"))
        force_observe = mode == "drydown_cycle" and reading.humidity > target
        exp["drydown_target"] = round(target, 3)
        exp["vpd_target_reason"] = vpd_target_reason
        exp["below_target_extension"] = round(float(below_target_extension), 3)
        hard_line, _, _ = self.physics._hard_safety_low()
        exp["hard_safety_low"] = round(hard_line, 3)
        exp["current_humidity"] = round(float(reading.humidity), 3)
        exp["force_observe"] = bool(force_observe)
        advice = _load_nightly_learning_advice(state)
        exp["nightly_learning_advice"] = {
            "active": bool(advice.get("active")),
            "reason": advice.get("reason"),
            "advice_id": advice.get("advice_id"),
            "phase2_trust_delta": (advice.get("phase2") or {}).get("trust_delta"),
            "exploration_bias_delta": (advice.get("exploration") or {}).get("bias_delta"),
            "prefer": (advice.get("arms") or {}).get("prefer"),
            "avoid": (advice.get("arms") or {}).get("avoid"),
            "sample_excluded": bool(
                advice.get("active")
                and (advice.get("sample_quality") or {}).get("exclude_new_learning")
            ),
        }
        exp["updated_at"] = now
        exp = self._deep_drydown_phase2_guard(reading, exp)
        force_observe = bool(exp.get("force_observe"))

        def update(state):
            state["irrigation_style_experiment"] = exp
        _update_system_state(update)
        self._record_style_experiment_profile(exp, reading, previous_exp)

        if force_observe:
            logger.info(
                f"[StyleExperiment] drydown_cycle active: H={reading.humidity:.1f}% "
                f"> target={target:.1f}%, force observe to learn dry/wet curve."
            )
        else:
            logger.info(
                f"[StyleExperiment] mode={mode} H={reading.humidity:.1f}% "
                f"target={target:.1f}% force_observe={force_observe}"
            )
        return exp

    def _style_hold_ceiling(self, style_exp: dict[str, Any]) -> float:
        """Upper moisture bound for style identification; FC is not the experiment target."""
        battle_width = max(float(self.cfg.FC - self.cfg.TARGET_LOW), 0.1)
        fraction = float(self.cfg.get_constant("STYLE_HOLD_CEILING_FRACTION") or 0.75)
        margin_below_safe = float(self.cfg.get_constant("STYLE_HOLD_SAFE_MARGIN") or 0.5)
        ceiling = float(self.cfg.TARGET_LOW) + battle_width * fraction
        ceiling = min(ceiling, float(self.cfg.M_SAFE_SLEEP) - margin_below_safe)
        drydown_target = style_exp.get("drydown_target")
        if isinstance(drydown_target, (int, float)):
            ceiling = max(ceiling, float(drydown_target) + 1.5)
        return round(ceiling, 3)

    def _augment_style_pulse_arms(
        self,
        style_exp: dict[str, Any],
        reading: SensorReading,
        ceiling_sec: float,
        plans: list[ActionPlan],
    ) -> list[ActionPlan]:
        """Expose 3s/6s/9s/10s as comparable strategy arms during wet_hold identification."""
        if str(style_exp.get("mode") or "") != "wet_hold" or style_exp.get("force_observe"):
            return plans

        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        medium_sec = float(
            self.cfg.get_constant("STYLE_PULSE_MEDIUM_SEC")
            or self.cfg.get_constant("LOW_WET_RECOVERY_MIN_SEC")
            or 6.0
        )
        strong_sec = float(
            self.cfg.get_constant("STYLE_PULSE_STRONG_SEC")
            or 9.0
        )
        large_sec = float(
            self.cfg.get_constant("STYLE_PULSE_LARGE_SEC")
            or self.cfg.get_constant("BUDGET_PULSE_LARGE_SEC")
            or 10.0
        )
        hard_cap = float(self.cfg.get_constant("WATER_SEC_MAX_HARD") or 30.0)
        labels = {plan.label for plan in plans}
        existing_secs = {round(float(plan.water_sec), 1) for plan in plans if plan.water_sec > 0}

        for sec, label in (
            (water_min, "style_pulse_3s"),
            (medium_sec, "style_pulse_6s"),
            (strong_sec, "style_pulse_9s"),
            (large_sec, "style_pulse_10s"),
        ):
            sec = round(min(max(sec, water_min), hard_cap), 1)
            if label in labels:
                continue
            duplicate = next(
                (
                    plan for plan in plans
                    if plan.water_sec > 0
                    and round(float(plan.water_sec), 1) == sec
                    and not str(plan.label).startswith("style_pulse_")
                ),
                None,
            )
            if duplicate is not None:
                original_label = duplicate.label
                duplicate.label = label
                setattr(duplicate, "style_learning_arm", True)
                setattr(duplicate, "style_original_label", original_label)
                labels.discard(original_label)
                labels.add(label)
                existing_secs.add(sec)
                continue
            if sec in existing_secs:
                continue
            plan = ActionPlan(label, sec)
            setattr(plan, "style_learning_arm", True)
            plans.append(plan)
            labels.add(label)
            existing_secs.add(sec)

        logger.info(
            "[StyleExperiment] wet_hold pulse arms exposed: "
            + " | ".join(f"{p.label}={p.water_sec}s" for p in plans if p.label.startswith("style_pulse_"))
        )
        return plans

    def _apply_style_identification_limits(
        self,
        style_exp: dict[str, Any],
        reading: SensorReading,
        plans: list[ActionPlan],
    ) -> None:
        """Prevent style identification from becoming an implicit refill-to-FC controller."""
        if str(style_exp.get("mode") or "") != "wet_hold":
            return

        hold_ceiling = self._style_hold_ceiling(style_exp)
        overrun_margin = float(self.cfg.get_constant("STYLE_HOLD_CEILING_OVERRUN_MARGIN") or 1.2)
        hard_overwet_line = min(
            float(self.cfg.FC) - 0.2,
            float(self.cfg.M_SAFE_SLEEP) - 0.2,
        )
        prediction_block_line = hard_overwet_line
        hard_prediction_margin = float(self.cfg.get_constant("PHASE2_HARD_VETO_FC_MARGIN") or 2.0)
        hard_prediction_line = max(hard_overwet_line, float(self.cfg.FC) + hard_prediction_margin)
        ratio_limit = float(self.cfg.get_constant("PHASE2_OVERDELTA_RATIO_LIMIT") or 2.5)
        soft_penalty = float(self.cfg.get_constant("PHASE2_SOFT_VETO_PENALTY") or 8.0)
        min_observe_streak = int(self.cfg.get_constant("PHASE2_SOFT_VETO_OBSERVE_STREAK") or 6)
        reliability = _phase2_peak_reliability_summary()
        state = _load_system_state()
        advice = _load_nightly_learning_advice(state)
        phase2_trust_delta = float((advice.get("phase2") or {}).get("trust_delta") or 0.0)
        advice_arms = advice.get("arms") if isinstance(advice.get("arms"), dict) else {}
        advice_prefer = advice_arms.get("prefer") if isinstance(advice_arms.get("prefer"), dict) else {}
        if advice.get("active") and phase2_trust_delta < 0:
            min_observe_streak = max(
                1,
                min_observe_streak - int(round(abs(phase2_trust_delta) * 8)),
            )
            soft_penalty = max(0.0, soft_penalty - abs(phase2_trust_delta) * 4.0)
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        observe_streak = int(state.get("battle_observe_streak", 0))
        blocked: list[dict[str, Any]] = []
        soft_veto: list[dict[str, Any]] = []
        for plan in plans:
            if plan.water_sec <= 0:
                continue
            predicted_peak = plan.predicted_peak
            if not isinstance(predicted_peak, (int, float)):
                trajectory = plan.predicted_trajectory or []
                predicted_peak = max(trajectory) if trajectory else None
            physical_peak = self._physical_peak_for_plan(reading, plan)
            plan.phase2_physical_peak = round(float(physical_peak), 3)
            if reading.humidity >= hard_overwet_line:
                setattr(plan, "style_blocked", True)
                plan.phase2_veto_class = "hard"
                blocked.append({
                    "label": plan.label,
                    "water_sec": round(float(plan.water_sec), 3),
                    "reason": "current_humidity_near_overwet_line",
                    "predicted_peak": predicted_peak,
                    "physical_peak": round(float(physical_peak), 3),
                })
                continue
            if isinstance(predicted_peak, (int, float)) and float(predicted_peak) > prediction_block_line:
                predicted_delta = max(0.0, float(predicted_peak) - float(reading.humidity))
                physical_delta = max(0.0, float(physical_peak) - float(reading.humidity))
                delta_ratio = predicted_delta / max(physical_delta, 0.1)
                physical_safe = physical_peak <= hard_overwet_line
                far_above_fc = float(predicted_peak) >= hard_prediction_line
                phase2_suspect = bool(reliability.get("overestimate_suspect")) or delta_ratio >= ratio_limit
                min_pulse_calibration = (
                    str(plan.label) in {"style_pulse_3s", "style_micro_pulse"}
                    or (
                        getattr(plan, "style_learning_arm", False)
                        and float(plan.water_sec) <= water_min + 0.25
                    )
                )
                medium_or_larger_style_probe = (
                    getattr(plan, "style_learning_arm", False)
                    and str(plan.label).startswith("style_pulse_")
                    and float(plan.water_sec) > water_min + 0.25
                    and (bool(phase2_trust_delta < 0) or bool(phase2_suspect))
                )
                arm = _learning_advice_arm_name(plan)
                advice_preferred_style_probe = (
                    bool(advice.get("active"))
                    and arm in {"medium_pulse", "strong_pulse", "large_pulse"}
                    and float(advice_prefer.get(arm) or 0.0) > 0.0
                    and getattr(plan, "style_learning_arm", False)
                    and physical_safe
                    and float(reading.humidity) <= float(self.cfg.TARGET_LOW) + 2.2
                )
                can_soft_veto = (
                    physical_safe
                    and (
                        (
                            phase2_suspect
                            and observe_streak >= min_observe_streak
                            and (
                                not far_above_fc
                                or min_pulse_calibration
                                or medium_or_larger_style_probe
                            )
                        )
                        or advice_preferred_style_probe
                    )
                )
                if can_soft_veto:
                    plan.phase2_soft_risk = True
                    plan.phase2_calibration_probe = True
                    plan.phase2_veto_class = "soft"
                    plan.phase2_soft_risk_reason = (
                        "phase2_far_above_soft_calibration_style_probe"
                        if far_above_fc and medium_or_larger_style_probe
                        else (
                            "nightly_advice_physical_safe_style_probe"
                            if advice_preferred_style_probe else
                            "phase2_far_above_soft_calibration_min_pulse"
                            if far_above_fc else "phase2_overestimate_soft_veto"
                        )
                    )
                    plan.phase2_soft_risk_penalty = soft_penalty
                    soft_veto.append({
                        "label": plan.label,
                        "water_sec": round(float(plan.water_sec), 3),
                        "reason": plan.phase2_soft_risk_reason,
                        "predicted_peak": round(float(predicted_peak), 3),
                        "physical_peak": round(float(physical_peak), 3),
                        "delta_ratio": round(float(delta_ratio), 3),
                        "soft_penalty": round(float(soft_penalty), 3),
                    })
                    continue
                setattr(plan, "style_blocked", True)
                plan.phase2_veto_class = "hard"
                blocked.append({
                    "label": plan.label,
                    "water_sec": round(float(plan.water_sec), 3),
                    "reason": (
                        "predicted_peak_far_above_fc"
                        if far_above_fc else "predicted_peak_near_overwet_line"
                    ),
                    "predicted_peak": round(float(predicted_peak), 3),
                    "physical_peak": round(float(physical_peak), 3),
                    "delta_ratio": round(float(delta_ratio), 3),
                    "phase2_overestimate_suspect": bool(phase2_suspect),
                    "observe_streak": observe_streak,
                })

        def update(state):
            exp = state.get("irrigation_style_experiment")
            if not isinstance(exp, dict):
                exp = {"enabled": True, "mode": "wet_hold"}
            exp["style_identification"] = {
                "objective": "compare_drydown_cycle_vs_hold_pulses",
                "hold_ceiling": hold_ceiling,
                "overrun_margin": round(overrun_margin, 3),
                "hard_overwet_line": round(hard_overwet_line, 3),
                "prediction_block_line": round(prediction_block_line, 3),
                "hard_prediction_line": round(hard_prediction_line, 3),
                "current_humidity": round(float(reading.humidity), 3),
                "blocked_plans": blocked,
                "soft_veto_plans": soft_veto,
                "phase2_reliability": reliability,
                "nightly_advice": {
                    "active": bool(advice.get("active")),
                    "advice_id": advice.get("advice_id"),
                    "phase2_trust_delta": phase2_trust_delta,
                    "soft_penalty": round(soft_penalty, 3),
                    "min_observe_streak": min_observe_streak,
                },
                "observe_streak": observe_streak,
                "updated_at": time.time(),
            }
            state["irrigation_style_experiment"] = exp

        _update_system_state(update)
        if blocked or soft_veto:
            logger.info(
                f"[StyleExperiment] hold ceiling={hold_ceiling:.1f}% "
                f"hard_blocks={len(blocked)} soft_veto={len(soft_veto)} "
                + " | ".join(
                    f"{item['label']}:{item['reason']}" for item in (blocked + soft_veto)
                )
            )

    def _evaluate_watering_window(
        self,
        reading: SensorReading,
        zone: ZoneStatus,
    ) -> dict[str, Any]:
        """Common horticulture timing prior: decide whether now is a good time to water."""
        local = time.localtime()
        local_hour = local.tm_hour + local.tm_min / 60.0
        temp = float(reading.temperature)
        vpd = float(reading.vpd or 0.0)
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        hard_line, _, _ = self.physics._hard_safety_low()
        urgent_low = (
            zone == ZoneStatus.EMERGENCY
            or reading.humidity <= hard_line + 0.5
            or reading.humidity <= float(self.cfg.TARGET_LOW) + 0.3
        )

        reasons: list[str] = []
        max_sec: Optional[float] = None
        allow_large = True
        allow_explore = True
        penalty = 0.0
        level = "allowed"
        sample_context = "normal_window"

        hot_or_dry = temp >= 30.0 or vpd >= 2.2
        cold = temp <= 12.0
        very_humid = 0.0 < vpd <= 0.35

        if 21.0 <= local_hour or local_hour < 6.0:
            level = "forbidden"
            reasons.append("night_no_regular_learning_water")
            max_sec = water_min if urgent_low else 0.0
            allow_large = False
            allow_explore = False
            penalty = 999.0 if not urgent_low else 18.0
            sample_context = "night_emergency" if urgent_low else "night_forbidden"
        elif 6.0 <= local_hour < 10.5:
            level = "ideal"
            reasons.append("morning_best_window")
            penalty = 0.0
        elif 10.5 <= local_hour < 15.0:
            if hot_or_dry:
                level = "avoid"
                reasons.append("midday_hot_or_high_vpd")
                max_sec = 6.0
                allow_large = False
                penalty = 10.0
                sample_context = "poor_window"
            elif cold:
                level = "allowed"
                reasons.append("cool_day_midday_allowed")
                penalty = 1.0
            else:
                level = "allowed"
                reasons.append("midday_moderate_allowed")
                penalty = 2.0
        elif 15.0 <= local_hour < 18.0:
            level = "allowed"
            reasons.append("afternoon_allowed")
            penalty = 2.0
        elif 18.0 <= local_hour < 19.0:
            level = "allowed"
            reasons.append("early_evening_small_or_medium_only")
            max_sec = 6.0
            allow_large = False
            penalty = 4.0
        else:
            level = "avoid"
            reasons.append("evening_avoid_regular_exploration")
            max_sec = water_min if urgent_low else 0.0
            allow_large = False
            allow_explore = False
            penalty = 999.0 if not urgent_low else 12.0
            sample_context = "evening_emergency" if urgent_low else "poor_window"

        if very_humid and level in {"ideal", "allowed"}:
            level = "allowed" if level == "ideal" else "avoid"
            reasons.append("very_low_vpd_slow_drying")
            allow_large = False
            max_sec = min(max_sec if max_sec is not None else 6.0, 6.0)
            penalty = max(penalty, 5.0)
            sample_context = "poor_window"

        if cold and not (10.5 <= local_hour < 15.0):
            level = "avoid" if level != "forbidden" else level
            reasons.append("cold_outside_midday_window")
            allow_large = False
            max_sec = min(max_sec if max_sec is not None else water_min, water_min)
            allow_explore = False
            penalty = max(penalty, 12.0)
            sample_context = "poor_window"

        if urgent_low and level in {"avoid", "forbidden"}:
            reasons.append("urgent_low_override_small_pulse_only")

        return {
            "level": level,
            "reason": ",".join(reasons),
            "local_hour": round(local_hour, 2),
            "temperature": round(temp, 3),
            "vpd": round(vpd, 3),
            "hard_safety_low": round(float(hard_line), 3),
            "urgent_low": bool(urgent_low),
            "max_sec": max_sec,
            "allow_large_pulse": bool(allow_large),
            "allow_explore": bool(allow_explore),
            "penalty": round(float(penalty), 3),
            "sample_context": sample_context,
        }

    def _apply_watering_window_limits(
        self,
        reading: SensorReading,
        zone: ZoneStatus,
        plans: list[ActionPlan],
    ) -> dict[str, Any]:
        """Apply the timing prior to normal candidates without turning it into a timer."""
        decision = self._evaluate_watering_window(reading, zone)
        blocked: list[dict[str, Any]] = []
        penalized: list[dict[str, Any]] = []
        level = str(decision.get("level") or "allowed")
        max_sec = decision.get("max_sec")
        max_sec_f = float(max_sec) if isinstance(max_sec, (int, float)) else None
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        penalty = float(decision.get("penalty") or 0.0)

        for plan in plans:
            plan.watering_window_level = level
            plan.watering_window_reason = str(decision.get("reason") or "")
            plan.watering_window_sample_context = str(decision.get("sample_context") or "normal_window")
            if plan.water_sec <= 0:
                continue

            reason = ""
            if level == "forbidden" and not decision.get("urgent_low"):
                reason = "watering_window_forbidden"
            elif max_sec_f is not None and float(plan.water_sec) > max_sec_f + 0.05:
                reason = f"watering_window_max_sec:{max_sec_f:.1f}"
            elif not decision.get("allow_large_pulse", True) and float(plan.water_sec) > water_min + 3.25:
                reason = "watering_window_large_pulse_not_allowed"

            if reason:
                setattr(plan, "style_blocked", True)
                setattr(plan, "style_blocked_reason", reason)
                blocked.append({
                    "label": plan.label,
                    "water_sec": round(float(plan.water_sec), 3),
                    "reason": reason,
                })
                continue

            if penalty > 0:
                plan.watering_window_penalty = penalty
                penalized.append({
                    "label": plan.label,
                    "water_sec": round(float(plan.water_sec), 3),
                    "penalty": round(float(penalty), 3),
                })

        def update(state):
            exp = state.get("irrigation_style_experiment")
            if not isinstance(exp, dict):
                exp = {"enabled": True, "mode": "unknown"}
            exp["watering_window_guard"] = {
                **decision,
                "blocked_plans": blocked,
                "penalized_plans": penalized,
                "updated_at": time.time(),
            }
            state["irrigation_style_experiment"] = exp

        _update_system_state(update)
        logger.info(
            f"[WateringWindow] level={decision.get('level')} "
            f"reason={decision.get('reason')} blocked={len(blocked)} "
            f"penalized={len(penalized)}"
        )
        return decision

    @staticmethod
    def _copy_plan_as_observe(plan: ActionPlan, label: str, reason: str) -> ActionPlan:
        forced = ActionPlan(label, 0.0)
        forced.cost_J = plan.cost_J
        forced.predicted_trajectory = list(plan.predicted_trajectory or [])
        forced.request_id = plan.request_id
        forced.device_code = plan.device_code
        forced.prediction_zone = plan.prediction_zone
        forced.watering_window_level = getattr(plan, "watering_window_level", None)
        forced.watering_window_reason = reason
        forced.watering_window_sample_context = getattr(
            plan, "watering_window_sample_context", None
        )
        return forced

    def _style_arm_for_plan(self, style_exp: dict[str, Any], plan: ActionPlan) -> tuple[str, str]:
        """Name the actual watering strategy arm, so trials are comparable."""
        mode = str(style_exp.get("mode") or "unknown")
        if getattr(plan, "trigger_guard_blocked", False) or plan.label == "trigger_guard_observe":
            return "trigger_guard_observe", "trigger_guard_observe"
        if plan.water_sec <= 0:
            if plan.label == "style_wet_hold_observe":
                return "style_wet_hold_observe", "wet_hold_observe"
            if mode == "drydown_cycle":
                return "style_drydown_observe", "drydown_observe"
            return "style_observe", "observe"

        if plan.label in ("low_wet_recovery", "reservoir_retest_probe"):
            return plan.label, "drydown_recovery"
        if plan.label == "style_pulse_3s":
            return plan.label, "micro_pulse"
        if plan.label == "style_pulse_6s":
            return plan.label, "medium_pulse"
        if plan.label == "style_pulse_9s":
            return plan.label, "strong_pulse"
        if plan.label == "style_pulse_10s":
            return plan.label, "large_pulse"

        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        if mode == "wet_hold":
            if plan.water_sec <= water_min + 0.25:
                return "style_micro_pulse", "micro_pulse"
            return "style_wet_hold_refill", "wet_hold_refill"
        if mode == "drydown_cycle":
            return "style_drydown_probe", "drydown_probe"
        return plan.label, plan.label or "unknown"

    def _wet_hold_observe_guard(
        self,
        style_exp: dict[str, Any],
        reading: SensorReading,
        plan: ActionPlan,
        original_plan: str,
    ) -> Optional[ActionPlan]:
        """Make wet_hold micro-pulses an explicit experiment, not a cooldown default."""
        mode = str(style_exp.get("mode") or "unknown")
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        if mode != "wet_hold" or plan.water_sec <= 0 or plan.water_sec > water_min + 0.25:
            return None

        state = _load_system_state()
        streak = int(state.get("battle_observe_streak", 0))
        exp = state.get("irrigation_style_experiment")
        if not isinstance(exp, dict):
            exp = {}

        target_gap = max(0.1, float(self.cfg.FC - self.cfg.TARGET_LOW))
        wet_fraction = float(
            self.cfg.get_constant("MICRO_PULSE_WET_HOLD_FRACTION") or 0.45
        )
        high_wet_line = float(self.cfg.TARGET_LOW) + target_gap * wet_fraction
        required_streak = int(
            self.cfg.get_constant("MICRO_PULSE_REQUIRE_OBSERVE_STREAK") or 2
        )
        near_fc_block_gap = float(
            self.cfg.get_constant("MICRO_PULSE_NEAR_FC_GAP_BLOCK") or 1.0
        )
        min_interval = float(
            self.cfg.get_constant("MICRO_PULSE_MIN_INTERVAL_SEC") or 2 * 3600
        )
        max_per_window = int(
            self.cfg.get_constant("MICRO_PULSE_MAX_PER_WET_HOLD") or 2
        )
        steep_loss_step = float(
            self.cfg.get_constant("MICRO_PULSE_STEEP_LOSS_STEP") or 0.35
        )

        now = time.time()
        fc_gap = float(self.cfg.FC - reading.humidity)
        last_at = float(exp.get("last_micro_pulse_at") or 0.0)
        started_at = float(style_exp.get("started_at") or 0.0)
        window_started_at = float(exp.get("micro_pulse_window_started_at") or 0.0)
        count = int(exp.get("micro_pulse_count") or 0)
        if started_at > 0 and abs(window_started_at - started_at) > 1.0:
            count = 0
            window_started_at = started_at

        guard = exp.get("wet_hold_observe_guard")
        if not isinstance(guard, dict):
            guard = {}
        window_guard = exp.get("watering_window_guard")
        if not isinstance(window_guard, dict):
            window_guard = {}
        last_seen_h = guard.get("last_seen_humidity")
        last_seen_at = float(guard.get("last_seen_at") or 0.0)
        recent_step = None
        if isinstance(last_seen_h, (int, float)) and now - last_seen_at <= 3 * 3600:
            recent_step = float(reading.humidity) - float(last_seen_h)
        drying_fast = recent_step is not None and recent_step <= -steep_loss_step

        block_reasons: list[str] = []
        if (
            str(window_guard.get("level") or "") == "forbidden"
            and not bool(window_guard.get("urgent_low"))
        ):
            block_reasons.append("watering_window_forbidden_micro_probe")
        if reading.humidity >= high_wet_line and streak < required_streak and not drying_fast:
            block_reasons.append("wet_hold_high_zone_needs_observe")
        if fc_gap <= near_fc_block_gap:
            block_reasons.append("too_close_to_fc")
        if last_at > 0 and now - last_at < min_interval:
            block_reasons.append("micro_pulse_interval_limit")
        if count >= max_per_window:
            block_reasons.append("micro_pulse_budget_limit")

        def update(state):
            exp_state = state.get("irrigation_style_experiment")
            if not isinstance(exp_state, dict):
                exp_state = {"enabled": True, "mode": mode}
            exp_state["micro_pulse_count"] = count
            exp_state["micro_pulse_limit"] = max_per_window
            exp_state["micro_pulse_window_started_at"] = window_started_at
            exp_state["wet_hold_observe_guard"] = {
                "blocked": bool(block_reasons),
                "reasons": block_reasons,
                "candidate_plan": original_plan,
                "candidate_water_sec": round(float(plan.water_sec), 3),
                "humidity": round(float(reading.humidity), 3),
                "high_wet_line": round(high_wet_line, 3),
                "fc_gap": round(fc_gap, 3),
                "observe_streak": streak,
                "required_observe_streak": required_streak,
                "recent_step": None if recent_step is None else round(recent_step, 4),
                "drying_fast": bool(drying_fast),
                "micro_pulse_count": count,
                "micro_pulse_limit": max_per_window,
                "watering_window_level": window_guard.get("level"),
                "watering_window_reason": window_guard.get("reason"),
                "watering_window_urgent_low": bool(window_guard.get("urgent_low")),
                "last_micro_pulse_at": last_at or None,
                "last_seen_humidity": round(float(reading.humidity), 3),
                "last_seen_at": now,
                "updated_at": now,
            }
            state["irrigation_style_experiment"] = exp_state

        _update_system_state(update)

        if not block_reasons:
            logger.info(
                "[StyleExperiment] wet_hold micro-pulse admitted: "
                f"H={reading.humidity:.1f}% high_line={high_wet_line:.1f}% "
                f"streak={streak}/{required_streak} count={count}/{max_per_window}"
            )
            return None

        forced = ActionPlan("style_wet_hold_observe", 0.0)
        forced.cost_J = plan.cost_J
        forced.predicted_trajectory = list(plan.predicted_trajectory or [])
        forced.request_id = plan.request_id
        forced.device_code = plan.device_code
        forced.prediction_zone = plan.prediction_zone
        logger.info(
            "[StyleExperiment] wet_hold micro-pulse blocked -> observe: "
            f"reasons={','.join(block_reasons)} H={reading.humidity:.1f}% "
            f"candidate={original_plan}/{plan.water_sec:.1f}s"
        )
        return forced

    def _record_micro_pulse_usage(
        self,
        style_exp: dict[str, Any],
        reading: SensorReading,
        action_sec: float,
        original_plan: str,
    ) -> None:
        """Persist the budget consumption for an admitted wet_hold micro-pulse."""
        now = time.time()
        started_at = float(style_exp.get("started_at") or 0.0)

        def update(state):
            exp = state.get("irrigation_style_experiment")
            if not isinstance(exp, dict):
                exp = {"enabled": True, "mode": "wet_hold"}
            window_started_at = float(exp.get("micro_pulse_window_started_at") or 0.0)
            count = int(exp.get("micro_pulse_count") or 0)
            if started_at > 0 and abs(window_started_at - started_at) > 1.0:
                count = 0
                window_started_at = started_at
            exp["micro_pulse_window_started_at"] = window_started_at
            exp["micro_pulse_count"] = count + 1
            exp["last_micro_pulse_at"] = now
            exp["last_micro_pulse_humidity"] = round(float(reading.humidity), 3)
            exp["last_micro_pulse_water_sec"] = round(float(action_sec), 3)
            exp["last_micro_pulse_original_plan"] = original_plan
            state["irrigation_style_experiment"] = exp

        _update_system_state(update)

    def _mark_style_arm(
        self,
        arm: str,
        original_plan: str,
        reading: SensorReading,
        water_sec: float,
        reason: str,
    ) -> None:
        """Persist the active strategy arm for the dashboard and later trial analysis."""
        now = time.time()

        def update(state):
            exp = state.get("irrigation_style_experiment")
            if not isinstance(exp, dict):
                exp = {"enabled": True, "mode": "unknown"}
            exp["current_arm"] = arm
            exp["current_arm_original_plan"] = original_plan
            exp["current_arm_water_sec"] = round(float(water_sec), 3)
            exp["current_arm_reason"] = reason
            exp["current_humidity"] = round(float(reading.humidity), 3)
            exp["current_arm_updated_at"] = now
            state["irrigation_style_experiment"] = exp

        _update_system_state(update)
        logger.info(
            f"[StyleExperiment] arm={arm} original_plan={original_plan} "
            f"water={water_sec:.1f}s reason={reason}"
        )

    def run_cycle(self) -> DecisionResult:
        """执行一次完整决策循环，返回 DecisionResult 供主进程记录与休眠。"""

        # 任务1：current_reading 用于复用 settle() 的传感器数据，跳过重复读取
        current_reading: Optional[SensorReading] = None

        # ── Fix-A / Fix-3：渗透等待期优先检查紧急状态 ───────────────
        if self._pending_soak is not None:
            soak = self._pending_soak

            # Fix-3：无论渗透是否完成，都先读传感器判断紧急态势
            reading = self.sensor.read()
            self._backfill_pending_h12(reading)
            self._record_soak_observation(soak, reading)
            zone    = self.physics.assess_zone(reading)

            if zone == ZoneStatus.EMERGENCY and not soak.ready:
                min_drop = float(self.cfg.get_constant("EMERGENCY_INTERRUPT_MIN_DROP") or 0.5)
                drop_since_pump = soak.pre_humidity - reading.humidity
                if not soak.ready and drop_since_pump < min_drop:
                    logger.critical(
                        f"[Brain] 渗透期间仍处紧急区，但未继续明显下跌 "
                        f"(before={soak.pre_humidity:.1f}% current={reading.humidity:.1f}% "
                        f"drop={drop_since_pump:.2f}% < {min_drop:.2f}%)，继续等待渗透，不二次补水。"
                    )
                    return self._finish_result(
                        zone=ZoneStatus.SOAK_PENDING,
                        chosen_plan=None,
                        action_sec=0.0,
                        reading=reading,
                        notes="渗透期间紧急读数未继续明显下跌，等待水分到达探头。",
                    )

                cooldown = float(self.cfg.get_constant("EMERGENCY_INTERRUPT_COOLDOWN_SEC") or 900)
                max_interrupts = int(self.cfg.get_constant("MAX_EMERGENCY_INTERRUPTS_PER_SOAK") or 1)
                since_last = (
                    time.monotonic() - soak.last_interrupt_time
                    if soak.last_interrupt_time is not None else float("inf")
                )
                if soak.emergency_interrupts >= max_interrupts or since_last < cooldown:
                    logger.critical(
                        f"[Brain] 渗透期间仍处紧急区，但已触发限流 "
                        f"(interrupts={soak.emergency_interrupts}/{max_interrupts}, "
                        f"cooldown_left={max(cooldown - since_last, 0):.0f}s)，本轮不再补水。"
                    )
                    return self._finish_result(
                        zone=ZoneStatus.SOAK_PENDING,
                        chosen_plan=None,
                        action_sec=0.0,
                        reading=reading,
                        notes="渗透期间紧急状态被限流，等待下一轮确认。",
                    )
                # ── 渗透期间紧急中断 ─────────────────────────────────
                block_reason = self._block_ineffective_emergency_if_needed(reading)
                if block_reason:
                    logger.critical(f"[Brain] 紧急补水被保护逻辑阻止：{block_reason}")
                    return self._finish_result(
                        zone=ZoneStatus.EMERGENCY,
                        chosen_plan=ActionPlan("water_delivery_suspect", 0.0),
                        action_sec=0.0,
                        reading=reading,
                        notes=block_reason,
                    )
                logger.critical(
                    f"[Brain]渗透期间检测到紧急状态！"
                    f"H={reading.humidity:.1f}% < TARGET_LOW={self.cfg.TARGET_LOW}%  "
                    f"放弃旧渗透哨兵，执行紧急补水。"
                )
                # 清除持久化哨兵记录
                def clear_pending(state):
                    state.pop("pending_soak", None)
                _update_system_state(clear_pending)
                self._pending_soak = None

                emerg_sec = self._emergency_pulse_sec(reading)
                self._pending_soak = self.actuator.execute_pump(
                    emerg_sec, reading.humidity, is_emergency=True,
                    plan_label="emergency_interrupt",
                    watering_window=self._evaluate_watering_window(reading, ZoneStatus.EMERGENCY),
                )
                self._pending_soak.emergency_interrupts = soak.emergency_interrupts + 1
                self._pending_soak.last_interrupt_time = time.monotonic()
                def update(state):
                    if "pending_soak" in state:
                        state["pending_soak"]["emergency_interrupts"] = self._pending_soak.emergency_interrupts
                        state["pending_soak"]["last_interrupt_time_wall"] = time.time()
                _update_system_state(update)
                return self._finish_result(
                    zone=ZoneStatus.EMERGENCY,
                    chosen_plan=ActionPlan("emergency_interrupt", emerg_sec),
                    action_sec=emerg_sec,
                    reading=reading,
                    notes=(
                        f"渗透期间紧急中断！旧哨兵已丢弃，"
                        f"紧急补水 {emerg_sec}s。"
                    ),
                )

            if not soak.ready:
                # 渗透未完成，且非紧急 → 本轮跳过浇水决策
                logger.info(
                    f"[Brain] 渗透等待中，剩余 {soak.remaining_sec:.0f}s "
                    f"（约 {soak.remaining_sec/60:.1f} 分钟）。"
                )
                return self._finish_result(
                    zone=ZoneStatus.SOAK_PENDING,
                    chosen_plan=None,
                    action_sec=0.0,
                    reading=reading,
                    notes=f"渗透等待中，剩余 {soak.remaining_sec:.0f}s。",
                )
            else:
                # 渗透完成 → settle() 采样 + K_p + pattern_memory，清除哨兵
                logger.info("[Brain] 渗透完成，执行采样与物理进化...")
                current_reading = self.actuator.settle(soak)  # 任务1：缓存返回的快照

                # 任务1：清除持久化哨兵记录
                def clear_pending(state):
                    state.pop("pending_soak", None)
                _update_system_state(clear_pending)
                self._pending_soak = None

        # ── Layer 0：读取传感器（任务1：渗透刚结算时直接复用，不重复读硬件）
        if current_reading is None:
            reading = self.sensor.read()
        else:
            reading = current_reading   # 任务1：复用 settle() 的数据，跳过重复读取和双重 CSV 写入
        self._backfill_pending_h12(reading)

        # ── Layer 1：计算物理天花板 ────────────────────────────────────
        ceiling = self.physics.calc_water_ceiling(reading.humidity)

        # ── Layer 2：态势评估 ─────────────────────────────────────────
        zone = self.physics.assess_zone(reading)

        if zone == ZoneStatus.SENSOR_STALE:
            return self._finish_result(
                zone=zone,
                chosen_plan=None,
                action_sec=0.0,
                reading=reading,
                notes="土壤传感器有效读数超过硬阈值，禁止自动浇水。",
            )

        # ── 安全区：直接放行 ──────────────────────────────────────────
        if zone == ZoneStatus.SAFE_SLEEP:
            return self._finish_result(
                zone=zone, chosen_plan=None,
                action_sec=0.0, reading=reading,
                notes="安全区，直接放行休眠。",
            )

        # ── 紧急区：跳过推理，短脉冲急救（EMERGENCY_WATER_SEC，非硬上限满灌）──
        if zone == ZoneStatus.EMERGENCY:
            reservoir_plan = self._reservoir_retest_plan(reading, "emergency")
            if reservoir_plan is not None and reservoir_plan.water_sec > 0:
                return self._execute_reservoir_retest(zone, reading, reservoir_plan)
            if reservoir_plan is not None:
                logger.critical(
                    "[Brain] 紧急区命中 reservoir_empty_pause；暂停连续 emergency 开泵，"
                    "等待下一次受控水路复测，避免空泵干跑。"
                )
                return self._finish_result(
                    zone=zone,
                    chosen_plan=reservoir_plan,
                    action_sec=0.0,
                    reading=reading,
                    notes=(
                        "紧急区但水路/水箱疑似无响应；本轮不继续 emergency 开泵，"
                        "只等待受控 reservoir_retest_probe。"
                    ),
                )

            block_reason = self._block_ineffective_emergency_if_needed(reading)
            if block_reason:
                logger.critical(f"[Brain] 紧急补水被保护逻辑阻止：{block_reason}")
                return self._finish_result(
                    zone=zone,
                    chosen_plan=ActionPlan("water_delivery_suspect", 0.0),
                    action_sec=0.0,
                    reading=reading,
                    notes=block_reason,
                )
            emerg_sec = self._emergency_pulse_sec(reading)
            logger.critical(f"[Brain]紧急补水 {emerg_sec}s（EMERGENCY_WATER_SEC）。")
            self._pending_soak = self.actuator.execute_pump(
                emerg_sec, reading.humidity, is_emergency=True,
                plan_label="emergency",
                watering_window=self._evaluate_watering_window(reading, zone),
            )
            return self._finish_result(
                zone=zone,
                chosen_plan=ActionPlan("emergency", emerg_sec),
                action_sec=emerg_sec,
                reading=reading,
                notes=f"紧急补水 {emerg_sec}s，渗透哨兵已挂载。",
            )

        # ── 战区：Layer 3-5 推理决策 ──────────────────────────────────
        reservoir_plan = self._reservoir_retest_plan(reading, "battle_zone")
        if reservoir_plan is not None:
            return self._execute_reservoir_retest(zone, reading, reservoir_plan)

        style_exp = self._style_experiment(reading)
        if style_exp.get("force_observe"):
            self._mark_style_arm(
                "drydown_observe",
                "style_drydown_observe",
                reading,
                0.0,
                "drydown_cycle_above_target",
            )
            return self._finish_result(
                zone=zone,
                chosen_plan=ActionPlan("style_drydown_observe", 0.0),
                action_sec=0.0,
                reading=reading,
                notes=(
                    f"drydown_cycle exploring; observe until H <= "
                    f"{style_exp.get('drydown_target')}%."
                ),
            )

        recovery_plan = self._low_wet_recovery_plan(reading, style_exp)
        if recovery_plan is not None and recovery_plan.water_sec <= 0:
            self._mark_style_arm(
                "drydown_recovery_blocked",
                recovery_plan.label,
                reading,
                0.0,
                "low_wet_recovery_blocked",
            )
            return self._finish_result(
                zone=zone,
                chosen_plan=recovery_plan,
                action_sec=0.0,
                reading=reading,
                notes=(
                    f"低湿恢复被 {recovery_plan.label} 阻止；"
                    "不回落到普通小脉冲，继续观察或等待 emergency 硬线。"
                ),
            )

        if recovery_plan is not None and recovery_plan.water_sec > 0:
            if recovery_plan.label == "reservoir_retest_probe":
                return self._execute_reservoir_retest(zone, reading, recovery_plan)
            recovery_window = self._evaluate_watering_window(reading, zone)
            recovery_max = recovery_window.get("max_sec")
            recovery_max_f = (
                float(recovery_max) if isinstance(recovery_max, (int, float)) else None
            )
            if (
                recovery_window.get("level") == "forbidden"
                and not recovery_window.get("urgent_low")
            ) or (
                recovery_max_f is not None
                and float(recovery_plan.water_sec) > recovery_max_f + 0.05
            ):
                forced = ActionPlan("watering_window_observe", 0.0)
                forced.watering_window_level = str(recovery_window.get("level") or "")
                forced.watering_window_reason = str(recovery_window.get("reason") or "")
                forced.watering_window_sample_context = str(
                    recovery_window.get("sample_context") or "poor_window"
                )
                self._mark_style_arm(
                    "watering_window_observe",
                    recovery_plan.label,
                    reading,
                    0.0,
                    f"watering_window:{recovery_window.get('reason')}",
                )
                return self._finish_result(
                    zone=zone,
                    chosen_plan=forced,
                    action_sec=0.0,
                    reading=reading,
                    notes=(
                        "低湿恢复被浇水时机裁决器延后；"
                        f"window={recovery_window.get('level')} "
                        f"reason={recovery_window.get('reason')}。"
                    ),
                )
            expected_delta = max(0.0, (recovery_plan.predicted_peak or reading.humidity) - reading.humidity)
            self._mark_style_arm(
                "drydown_recovery",
                recovery_plan.label,
                reading,
                recovery_plan.water_sec,
                "low_wet_recovery",
            )
            self._pending_soak = self.actuator.execute_pump(
                recovery_plan.water_sec,
                reading.humidity,
                is_emergency=False,
                expected_delta_m=expected_delta,
                plan_label=recovery_plan.label,
                prediction_plan=recovery_plan,
                watering_window=recovery_window,
            )
            return self._finish_result(
                zone=zone,
                chosen_plan=recovery_plan,
                action_sec=recovery_plan.water_sec,
                reading=reading,
                notes=(
                    f"低湿 wet_hold 恢复脉冲 {recovery_plan.water_sec:.1f}s，"
                    "减少小脉冲并记录大干大湿响应。"
                ),
            )

        cooldown_left = self._normal_irrigation_cooldown_remaining(reading)

        plans        = self.gate.generate_exam(ceiling, reading)
        plans        = self._augment_style_pulse_arms(style_exp, reading, ceiling, plans)
        trajectories = self.predictor.request_prediction(plans, reading)
        self._apply_style_identification_limits(style_exp, reading, plans)
        watering_window = self._apply_watering_window_limits(reading, zone, plans)
        best_plan    = self.court.evaluate_and_select(plans, trajectories)

        action_sec = max(best_plan.water_sec, 0.0)
        forced_plan = self._maybe_force_observe_probe(
            best_plan,
            reading,
            ceiling,
            style_exp,
            trajectories,
        )
        if forced_plan is not None:
            best_plan = forced_plan
            action_sec = max(best_plan.water_sec, 0.0)

        trigger_ok, trigger_reason = self._watering_trigger_reason(
            best_plan,
            reading,
            style_exp,
            trajectories,
        )
        if action_sec > 0 and trigger_ok and (
            "target" in trigger_reason
            or "hard_safety" in trigger_reason
            or trigger_reason.startswith("forecast_")
            or trigger_reason.startswith("natural_h12_")
        ):
            budget_plan = self._largest_safe_budget_plan(plans, reading, style_exp)
            if budget_plan is not None and budget_plan.water_sec > best_plan.water_sec:
                logger.info(
                    f"[TriggerGuard] upgrade watering plan by refill budget: "
                    f"{best_plan.label}/{best_plan.water_sec:.1f}s -> "
                    f"{budget_plan.label}/{budget_plan.water_sec:.1f}s "
                    f"reason={trigger_reason}"
                )
                best_plan = budget_plan
                action_sec = max(best_plan.water_sec, 0.0)

        if trigger_ok:
            forced_exploration_plan = self._maybe_apply_nightly_forced_exploration(
                best_plan,
                plans,
                reading,
                watering_window,
                trigger_reason,
            )
            if (
                forced_exploration_plan is not None
                and forced_exploration_plan.water_sec > best_plan.water_sec
            ):
                best_plan = forced_exploration_plan
                action_sec = max(best_plan.water_sec, 0.0)
                trigger_reason = (
                    f"{trigger_reason};nightly_forced_exploration"
                )

        if cooldown_left > 0:
            if action_sec > 0 and trigger_ok and getattr(best_plan, "forced_exploration", False):
                trigger_reason = f"{trigger_reason};cooldown_bypassed_for_forced_exploration"
                logger.warning(
                    "[NightlyAdvice] forced exploration bypasses normal cooldown: "
                    f"remaining={cooldown_left/3600:.2f}h plan={best_plan.label}/"
                    f"{best_plan.water_sec:.1f}s"
                )
            else:
                logger.info(
                    f"[Brain] 普通浇水冷却中，剩余 {cooldown_left/3600:.2f}h；"
                    f"本轮仅观察，不开泵。"
                )
                self._mark_style_arm(
                    "cooldown_observe",
                    "cooldown_observe",
                    reading,
                    0.0,
                    "normal_irrigation_cooldown",
                )
                return self._finish_result(
                    zone=zone,
                    chosen_plan=ActionPlan("cooldown_observe", 0.0),
                    action_sec=0.0,
                    reading=reading,
                    notes=f"普通浇水冷却中，剩余 {cooldown_left:.0f}s。",
                )

        if action_sec > 0 and not trigger_ok:
            best_plan = self._force_observe_for_missing_trigger(
                best_plan,
                reading,
                trigger_reason,
            )
            action_sec = 0.0

        original_plan_label = best_plan.label
        wet_hold_guard_plan = self._wet_hold_observe_guard(
            style_exp,
            reading,
            best_plan,
            original_plan_label,
        )
        if wet_hold_guard_plan is not None:
            best_plan = wet_hold_guard_plan
            action_sec = 0.0
            trigger_reason = "wet_hold_micro_pulse_guard"

        style_plan_label, style_arm = self._style_arm_for_plan(style_exp, best_plan)
        if style_plan_label != best_plan.label:
            best_plan.label = style_plan_label

        # 只比较同一时间窗口：模型峰值预计在渗透结算窗口内出现时，
        # 才把 peak-current 作为 expected_delta_m。绝不能再拿 12h 轨迹终点
        # 与约 30 分钟的实测增量比较。
        _soak_minutes = float(self.cfg.get_constant("SOAK_WAIT_SEC") or 1800) / 60.0
        _exp_delta = _settle_window_expected_delta(
            best_plan, reading.humidity, _soak_minutes
        )

        if action_sec <= 0:
            self._mark_style_arm(
                style_arm,
                original_plan_label,
                reading,
                0.0,
                "layer5_observe",
            )
            return self._finish_result(
                zone=zone,
                chosen_plan=best_plan,
                action_sec=0.0,
                reading=reading,
                notes=(
                    f"战区推理 → {best_plan.label} (0s, J={best_plan.cost_J:.4f})  "
                    f"按兵不动，继续高频观察失水曲线。"
                ),
            )

        self._consume_exploration_budget(best_plan)
        self._record_watering_trigger(best_plan, reading, trigger_reason)
        if style_arm == "micro_pulse":
            self._record_micro_pulse_usage(
                style_exp,
                reading,
                action_sec,
                original_plan_label,
            )
        self._mark_style_arm(
            style_arm,
            original_plan_label,
            reading,
            action_sec,
            "layer5_selected",
        )
        # This is deliberately after every native Phase3 guard and after its
        # own duration selection.  The bridge can only claim an already legal
        # action; it cannot change the duration, parameters, or MQTT command.
        experience_claim = None
        if self._experience_validation is not None:
            experience_claim = self._experience_validation.claim_if_eligible(
                reading,
                _load_system_state(),
                action_sec=action_sec,
                plan_label=best_plan.label,
            )
        try:
            self._pending_soak = self.actuator.execute_pump(
                action_sec, reading.humidity,
                is_emergency=False,
                expected_delta_m=_exp_delta,
                plan_label=best_plan.label,
                prediction_plan=best_plan,
                watering_window=watering_window,
            )
        except Exception:
            if self._experience_validation is not None:
                self._experience_validation.release_failed_execution(experience_claim)
            raise
        if self._experience_validation is not None:
            self._experience_validation.record_execution(
                experience_claim,
                reading,
                action_sec=action_sec,
                plan_label=best_plan.label,
            )

        return self._finish_result(
            zone=zone,
            chosen_plan=best_plan,
            action_sec=action_sec,
            reading=reading,
            notes=(
                f"战区推理 → {best_plan.label}  "
                f"({action_sec}s, J={best_plan.cost_J:.4f})  "
                f"trigger={trigger_reason}；渗透哨兵已挂载。"
            ),
        )
