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
import subprocess
import smtplib
import time
import logging
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

# 与 phase1_test/water-test-soil1.py 对齐；可用环境变量覆盖
MQTT_BROKER_DEFAULT = os.environ.get("IRRIGATION_MQTT_BROKER", "localhost")
MQTT_TOPIC_PUMP_CMD_DEFAULT = os.environ.get(
    "IRRIGATION_MQTT_TOPIC_PUMP", "esp32/pump1/cmd"
)
DEVICE_CODE_DEFAULT = "soil1"

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

from cloud_protection import (
    audit_view as cloud_protection_audit_view,
    cloud_watering_allowed,
    load_cloud_control,
    protection_mode as cloud_protection_mode,
)

logger = logging.getLogger("decision_brain")


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
    cmd = "gsql -d soil_data -p 7654 -c " + repr(sql)
    result = subprocess.run(
        ["su", "-", "opengauss", "-c", cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


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
    from_addr = os.environ.get("IRRIGATION_ALERT_FROM", username or f"soil1@{os.uname().nodename}")
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


def _mark_predictor_probe(called: bool, success: bool = False) -> None:
    def update(state):
        state["predictor_last_probe"] = {
            "called": called,
            "success": success,
            "timestamp": time.time(),
        }
    _update_system_state(update)


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
    return {"low": "K_P_LOW", "mid": "K_P_MID", "high": "K_P_HIGH"}.get(zone_name, "K_P_MID")


def _zone_kp(cfg: ConfigManager, zone_name: str) -> float:
    value = cfg.get(_zone_kp_key(zone_name))
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    profile = _load_irrigation_profile()
    kp = profile.get("zones", {}).get(zone_name, {}).get("kp_ema")
    if isinstance(kp, (int, float)) and kp > 0:
        return float(kp)
    return float(cfg.K_P)


# ===========================================================================
# ① 数据结构定义
# ===========================================================================

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
      · SOIL_CSV（与 phase1_test/water-test-soil1.py 的 DATA_PATH 一致，如 water-test-soil1_soil1.csv）
            每 ~5 分钟一行：土壤湿度(%)、温度(°C)、电导率 等
      · air_humidity.csv —— 空气相对湿度（%），采样可能较稀

    VPD：使用空气湿度 RH_air（来自 air CSV）与气温 T（与 soil1 脚本一致，取土壤 CSV 最新行「温度(°C)」，
         由本轮 _read_soil_raw 得到的土壤温度列传入，不依赖 air CSV 中的 temperature 列）。
    """

    # 两个数据文件的路径（固定，不参与进化）
    SOIL_CSV = Path("/root/data/soil1.csv")
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

        # ── 空气湿度来自 air CSV；VPD 用气温 = 与 water-test-soil1 同源（土壤 CSV「温度(°C)」）
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
        """从 openGauss 统一传感器表读取 soil1 最新一条有效土壤数据。"""
        sql = (
            "SELECT id, recv_time, temp, humidity, ec FROM soil_sensor_readings "
            "WHERE device_code='soil1' "
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
                raise RuntimeError("openGauss soil_sensor_readings[soil1] 表中没有有效土壤记录")
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
                    f"openGauss soil_sensor_readings[soil1] 最新有效记录超出物理范围: "
                    f"H={humidity_f}, T={temp_f}, EC={ec_f}"
                )
            soil_ts = self._parse_soil_timestamp(recv_time)
            return humidity_f, temp_f, ec_f, soil_ts, 0
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"openGauss soil_sensor_readings[soil1] 读取失败: {e}") from e

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
        subject = "soil1 报警：土壤传感器数据超时"
        body = (
            f"智养中心检测到 soil1 土壤传感器数据异常。\n\n"
            f"报警类型：土壤传感器有效数据超时\n"
            f"超时时长：{stale_age/3600:.2f} 小时\n"
            f"最后有效湿度：{humidity:.1f}%\n"
            f"最后有效时间戳：{soil_ts}\n"
            f"连续异常行数：{skipped}\n\n"
            f"系统处置：已进入传感器故障保护，禁止使用旧读数自动浇水。\n"
            f"建议操作：检查 soil1 传感器供电、接线、ESP 上报和 MQTT/CSV 数据链路。"
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
          3. 历史最后有效空气湿度；
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
            "WHERE device_code='soil1' AND air_humidity IS NOT NULL "
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
        deep_enabled_raw = self.cfg.get_constant("DEEP_DRYDOWN_EXPERIMENT_ENABLED")
        deep_enabled = bool(deep_enabled_raw) if deep_enabled_raw is not None else True
        if deep_enabled:
            emergency_line = float(self.cfg.get_constant("DEEP_DRYDOWN_HARD_SAFETY_LOW") or 28.0)
            return emergency_line, emergency_line, target_gap
        hard_fc_gap = float(self.cfg.get_constant("HARD_SAFETY_FC_GAP") or 25.0)
        phase1_floor = float(self.cfg.FC) - hard_fc_gap
        hard_below_target = float(self.cfg.get_constant("HARD_SAFETY_BELOW_TARGET") or 5.0)
        min_target_gap = float(self.cfg.get_constant("TARGET_LOW_MIN_GAP_FOR_HARD_LINE") or 8.0)
        if target_gap < min_target_gap:
            emergency_line = phase1_floor
        else:
            emergency_line = max(phase1_floor, target_low - hard_below_target)
        return emergency_line, phase1_floor, target_gap

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
                f"(TARGET_LOW={target_low}%, target_gap={target_gap:.1f}%, phase1_floor={phase1_floor:.1f}%)"
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
            plans.append(ActionPlan(label=f"explore_{int(allowed_sec)}s_{zone_name}", water_sec=allowed_sec))
        for plan in plans:
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
        total_accept = int(zone.get("total_accepted") or 0)
        stuck_rounds = int(zone.get("stuck_rounds_at_min") or 0)
        if stable >= int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_5S") or 5):
            max_allowed = max(max_allowed, 5.0)
        elif stable >= int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_4S") or 3):
            max_allowed = max(max_allowed, 4.0)
        # Fix-Explore: 累计接受次数也解锁（不要求连续）
        elif total_accept >= (int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_5S") or 5) * 2):
            max_allowed = max(max_allowed, 5.0)
        elif total_accept >= (int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_4S") or 3) * 2):
            max_allowed = max(max_allowed, 4.0)
        # Fix-Stuck: 长期卡在最小剂量 → 强制探针
        if stuck_rounds >= int(self.cfg.get_constant("EXPLORATION_STUCK_FORCE_ROUNDS") or 8):
            max_allowed = max(max_allowed, water_min + 1.0)
            reason += f", stuck_probe({stuck_rounds}r)"
        daily_used = int(profile.get("daily_exploration", {}).get("used") or 0)
        fc_gap = self.cfg.FC - reading.humidity
        reason = f"stable={stable}, failures={zone.get('failure_count', 0)}"
        if daily_used >= daily_budget:
            max_allowed = min(max_allowed, water_min)
            reason += ", daily_budget_exhausted"
        if fc_gap < min_fc_gap:
            max_allowed = min(max_allowed, water_min)
            reason += f", fc_gap={fc_gap:.1f}<{min_fc_gap:.1f}"
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
            h_diff          = abs(record.get("humidity", 50.0) - current_humidity)
            days_old        = max(0.0, now - record.get("timestamp", now)) / 86400.0
            time_penalty    = days_old * 0.1
            quality_penalty = (1.0 - record.get("quality_score", 1.0)) * 2.0
            # Fix-Penalty：惩罚系数叠加，penalty=0.5 对应额外惩罚 2.5（与 quality_penalty 同量纲）
            # 让"预测曾经严重高估 Δm"的记录在综合排名中自然靠后，而不是简单丢弃。
            penalty_score   = record.get("penalty", 0.0) * 5.0
            return h_diff + time_penalty + quality_penalty + penalty_score

        sorted_records = sorted(candidates, key=_score)
        top3    = sorted_records[:3]
        avg_sec = sum(r.get("optimal_sec", water_min) for r in top3) / len(top3)
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
        if not bool(self.cfg.get_constant("PHASE2_ENABLED")):
            logger.info("[Layer4] Phase2 未启用，使用公共物理外推。")
            return self._fallback_trajectories(plans, reading)

        if self.cfg.get("PREDICTOR_CIRCUIT_OPEN", False):
            logger.warning("[Layer4] 预测熔断已打开，跳过 Phase2 等待，直接使用物理外推。")
            _mark_predictor_probe(called=False, success=False)
            return self._fallback_trajectories(plans, reading)

        payload = {
            "timestamp":    time.time(),
            "candidates":   [{"label": p.label, "water_sec": p.water_sec} for p in plans],
            "horizon_steps": self.horizon,
        }

        self.resp_path.unlink(missing_ok=True)
        _mark_predictor_probe(called=True, success=False)
        try:
            with open(self.req_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
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
                    trajectories = resp.get("trajectories", {})
                    if not isinstance(trajectories, dict) or not all(p.label in trajectories for p in plans):
                        logger.warning("[Layer4] 预测响应格式或候选标签不匹配，忽略本次响应。")
                        time.sleep(0.5)
                        continue
                    logger.info("[Layer4] 成功获取预测轨迹。")
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
        for plan in plans:
            traj = trajectories.get(plan.label, [])
            if not traj:
                plan.cost_J = math.inf
                continue
            plan.predicted_trajectory = traj
            plan.cost_J = self._calc_cost_J(traj, plan)
            logger.info(f"[Layer5] {plan.label:12s}: J={plan.cost_J:.4f}  ({plan.water_sec}s)")

        best = min(plans, key=lambda p: p.cost_J)
        best.selected = True
        logger.info(
            f"[Layer5] 最高法庭判决 → {best.label}  "
            f"J={best.cost_J:.4f}  浇水={best.water_sec}s"
        )
        return best

    def _calc_cost_J(self, trajectory: list[float], plan: ActionPlan) -> float:
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

        for y in trajectory:
            if y < wake_line - tolerance:
                loss_survival    += (wake_line - tolerance - y) ** 2
            if y > resp_limit + tolerance:
                loss_respiration += (y - resp_limit - tolerance) ** 2

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

        return alpha * loss_survival + beta * loss_respiration + gamma * cost_mechanical + observe_penalty + ceiling_penalty


# ===========================================================================
# ⑦ Layer 6 —— 执行器 + 短期物理进化（EMA 更新 K_p）
# ===========================================================================

class ActuatorLayer:
    """
    Layer 6: 执行浇水 → 创建渗透哨兵（非阻塞）→ 采集增量 → EMA 更新 K_p。

    Fix-A：渗透等待由 PendingSoak 管理，不再用 1800s 阻塞主线程。
           MQTT 浇水段与 water-test-soil1.py 一致：on → sleep(sec) → off，
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

        返回：
          PendingSoak  : 挂载到 DecisionBrain._pending_soak 的哨兵对象
        """
        self._activate_pump(water_sec, is_emergency=is_emergency)

        # 更新系统状态（水泵启停计数）
        large_thr = self.cfg.LARGE_WATER_THRESHOLD
        def update(state):
            state["pump_total_cycles"] = int(state.get("pump_total_cycles", 0)) + 1
            state["total_water_sec_dispensed"] = (
                float(state.get("total_water_sec_dispensed", 0.0)) + water_sec
            )
            if not is_emergency:
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

    def _reservoir_retest_baseline(self, water_sec: float) -> dict[str, Any]:
        fallback = float(self.cfg.get_constant("RESERVOIR_RETEST_MIN_DELTA") or 0.5)
        limit = int(self.cfg.get_constant("RESERVOIR_RETEST_HISTORY_LIMIT") or 80)
        tolerance = float(self.cfg.get_constant("RESERVOIR_RETEST_SAME_SEC_TOLERANCE") or 0.6)
        min_samples = int(self.cfg.get_constant("RESERVOIR_RETEST_MIN_BASELINE_SAMPLES") or 3)
        min_improve = float(self.cfg.get_constant("RESERVOIR_RETEST_MIN_IMPROVEMENT") or 0.4)
        iqr_factor = float(self.cfg.get_constant("RESERVOIR_RETEST_IQR_FACTOR") or 1.5)
        deltas = []
        for trial in reversed(_load_irrigation_trials()):
            if len(deltas) >= limit:
                break
            try:
                sec = float(trial.get("water_sec"))
                delta = float(trial.get("delta_m"))
            except (TypeError, ValueError):
                continue
            if abs(sec - float(water_sec)) > tolerance:
                continue
            if str(trial.get("plan_label") or "") in {"reservoir_retest_probe", "emergency", "emergency_interrupt"}:
                continue
            if str(trial.get("status") or "") not in {"accepted", "rejected"}:
                continue
            deltas.append(delta)

        deltas = sorted(deltas)
        if len(deltas) < min_samples:
            return {
                "decision_basis": "fallback_min_delta",
                "samples": len(deltas),
                "threshold": round(fallback, 3),
                "recent_deltas": [round(x, 3) for x in deltas[-10:]],
            }

        median = deltas[len(deltas) // 2]
        q1 = deltas[len(deltas) // 4]
        q3 = deltas[(len(deltas) * 3) // 4]
        iqr = max(0.0, q3 - q1)
        robust_floor = max(fallback, median - iqr_factor * iqr, min_improve)
        threshold = min(max(robust_floor, fallback), max(median, fallback))
        return {
            "decision_basis": "historical_same_sec_robust_delta",
            "samples": len(deltas),
            "median_delta_m": round(median, 3),
            "q1_delta_m": round(q1, 3),
            "q3_delta_m": round(q3, 3),
            "iqr_delta_m": round(iqr, 3),
            "threshold": round(threshold, 3),
            "recent_deltas": [round(x, 3) for x in deltas[-10:]],
        }

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
                _update_system_state(clear_reservoir_suspect)
                logger.warning(
                    f"[ReservoirRetest] recovered: Δm={delta_m:+.3f}% >= "
                    f"dynamic_threshold={recovered_delta:.3f}% ({baseline.get('decision_basis')})."
                )
            else:
                interval = float(self.cfg.get_constant("RESERVOIR_RETEST_INTERVAL_SEC") or 3600)
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
                    f"dynamic_threshold={recovered_delta:.3f}% ({baseline.get('decision_basis')}); "
                    f"next retest after {interval/3600:.1f}h."
                )

            self._record_irrigation_trial(
                status="reservoir_retest",
                reason=reason,
                water_sec=soak.water_sec,
                pre_h=soak.pre_humidity,
                post_h=post_reading.humidity,
                delta_m=delta_m,
                expected_delta_m=soak.expected_delta_m,
                quality_score=None,
                penalty=0.0,
                plan_label=soak.plan_label,
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
                quality_score=None,
                penalty=0.0,
                plan_label=soak.plan_label,
            )
            self._update_irrigation_profile(
                status="emergency",
                reason="emergency_pulse_not_reference",
                pre_h=soak.pre_humidity,
                post_h=post_reading.humidity,
                water_sec=soak.water_sec,
                delta_m=delta_m,
                quality_score=None,
                plan_label=soak.plan_label,
            )
            logger.info("[Layer6] K_p 更新跳过：紧急补水样本不参与常规物理增益进化。")
        else:
            accepted = self._update_pattern_memory(
                soak.water_sec, soak.pre_humidity,
                post_reading.humidity, delta_m,
                expected_delta_m=soak.expected_delta_m,
                plan_label=soak.plan_label,
            )
            if accepted:
                self._evolve_kp(
                    soak.water_sec, delta_m, soak.pre_humidity, soak.plan_label
                )
            else:
                logger.info(
                    "[Layer6] K_p 更新跳过：本次 trial 未通过 pattern_memory 质量门控，"
                    "仅保留在 irrigation_trials。"
                )

        return post_reading   # 任务1：返回完整快照，不仅仅是湿度值

    # ------------------------------------------------------------------
    # 硬件驱动：MQTT 开泵（对齐 phase1 send_mqtt_cmd）
    # ------------------------------------------------------------------

    def _activate_pump(self, water_sec: float, is_emergency: bool = False) -> None:
        """对齐 phase1 send_mqtt_cmd：publish on → sleep → publish off。"""
        sec = max(0.0, float(water_sec))

        if sec <= 0:
            logger.info("  >>> [物理执行] 决议为 0s，水泵保持静默。")
            return

        # Authoritative second check immediately before MQTT pump-on.
        control = load_cloud_control()
        allowed, reason = cloud_watering_allowed(
            control,
            emergency=is_emergency,
            sensor_trusted=True,
        )
        if not allowed:
            logger.critical(
                "[CloudProtection] pump-on blocked at final gate: "
                "mode=%s risk=%s reason=%s command_id=%s",
                cloud_protection_mode(control),
                control.get("risk_level"),
                reason,
                control.get("command_id") or control.get("event_id"),
            )
            raise PumpExecutionError("cloud protection blocked pump-on: %s" % reason)

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

    def _phase1_low_confidence(self) -> bool:
        state = _load_system_state()
        if str(state.get("phase1_confidence") or "").lower() == "low":
            return True
        target_gap = max(float(self.cfg.FC - self.cfg.TARGET_LOW), 0.0)
        return target_gap < float(self.cfg.get_constant("TARGET_LOW_NARROW_GAP") or 8.0)

    def _evolve_kp(self, water_sec: float, delta_m: float, pre_humidity: float, plan_label: str = "") -> None:
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
        low_confidence = self._phase1_low_confidence()

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
        physical_kp = max(self.cfg.K_P, 0.3) if low_confidence else self.cfg.K_P
        physical_ceiling = physical_kp * water_sec * 3.0
        if delta_m > physical_ceiling and delta_m > (fc * 0.1):
            logger.warning(
                f"[Layer6] ⚠️ K_p 更新跳过：Δm={delta_m:.3f}% 超过物理上界 "
                f"{physical_ceiling:.3f}%，疑似传感器尖峰。"
            )
            return

        # Fix-2 Gate-C：低可信 phase1 画像允许 K_P 重新 bootstrap，避免坏 K_P=0.017 锁死学习。
        if low_confidence:
            kp_bootstrap_min = float(self.cfg.get_constant("LOW_CONF_KP_BOOTSTRAP_MIN") or 0.002)
            kp_bootstrap_max = float(self.cfg.get_constant("LOW_CONF_KP_BOOTSTRAP_MAX") or 2.0)
            if k_measured > kp_bootstrap_max or k_measured < kp_bootstrap_min:
                logger.warning(
                    f"[Layer6] low-confidence K_p bootstrap skipped: measured={k_measured:.5f} "
                    f"outside [{kp_bootstrap_min:.5f}, {kp_bootstrap_max:.5f}]."
                )
                return
        elif k_old > 0 and (k_measured > k_old * 5.0 or k_measured < k_old / 5.0):
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
        self._update_zone_kp_profile(zone_name, k_new)
        logger.info(
            f"[Layer6] K_p 短期进化: global {global_old:.5f} → {global_new:.5f}; "
            f"{zone_key} {k_old:.5f} → {k_new:.5f}  "
            f"(zone={zone_name} measured={k_measured:.5f}  ema_α={ema_alpha})"
        )

    # ------------------------------------------------------------------
    # Fix-B：pattern_memory 三重质量门控
    # ------------------------------------------------------------------

    def _update_zone_kp_profile(self, zone_name: str, zone_kp: float) -> None:
        """Update only the learned gain; never increment outcome counters here."""
        profile = _load_irrigation_profile()
        zone = profile["zones"][zone_name]
        zone["kp_ema"] = round(float(zone_kp), 5)
        zone["last_updated"] = time.time()
        _save_irrigation_profile(profile)

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
            # Fix-Stuck: 接受时重置卡死计数器
            if water_sec > water_min + 0.5:
                zone["stuck_rounds_at_min"] = 0
            if zone_kp is not None:
                zone["kp_ema"] = round(float(zone_kp), 5)
            stable = int(zone["stable_success"])
            if stable >= int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_5S") or 5):
                zone["max_allowed_sec"] = max(float(zone.get("max_allowed_sec") or water_min), 5.0)
            elif stable >= int(self.cfg.get_constant("EXPLORATION_STEP_SUCCESS_4S") or 3):
                zone["max_allowed_sec"] = max(float(zone.get("max_allowed_sec") or water_min), 4.0)
        elif status == "rejected":
            stat["rejected"] = int(stat.get("rejected") or 0) + 1
            zone["failure_count"] = int(zone.get("failure_count") or 0) + 1
            # Fix-Stuck: 如果浇水剂量=最小值又被拒绝，卡死+1
            if water_sec <= water_min + 0.5:
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

        def reject(reason: str, message: str) -> None:
            logger.warning(message)
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
                prediction_meta=prediction_meta,
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
        if prediction_meta:
            record.update(prediction_meta)
        trials.append(record)
        _save_irrigation_trials(trials)
        logger.info(
            f"[Layer6] irrigation_trials 记录: status={status} reason={reason} "
            f"sec={water_sec} H={pre_h:.1f}->{post_h:.1f} Δm={delta_m:+.3f}"
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

        # 任务1：启动时尝试从持久化状态恢复渗透哨兵
        self._restore_pending_soak()

    def _latest_settled_trial(self) -> Optional[dict]:
        """返回最近一次完成结算的浇水样本，不使用中途 observation。"""
        trials = _load_irrigation_trials()
        for trial in reversed(trials):
            if trial.get("status") in {"accepted", "rejected", "emergency"}:
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
            subject = f"{DEVICE_CODE_DEFAULT} 报警：紧急补水连续无效"
            body = (
                f"智养中心检测到 {DEVICE_CODE_DEFAULT} 连续紧急补水无效。\n\n"
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

    def _phase1_low_confidence(self) -> bool:
        state = _load_system_state()
        if str(state.get("phase1_confidence") or "").lower() == "low":
            return True
        target_gap = max(float(self.cfg.FC - self.cfg.TARGET_LOW), 0.0)
        return target_gap < float(self.cfg.get_constant("TARGET_LOW_NARROW_GAP") or 8.0)

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
            if self._phase1_low_confidence() and reading.humidity < self.cfg.TARGET_LOW:
                rebuild_cap = float(self.cfg.get_constant("LOW_CONF_REBUILD_COOLDOWN_SEC") or 1800)
                if cooldown > rebuild_cap:
                    cooldown = rebuild_cap
                    reason = f"{reason}, low_confidence_rebuild_cap"
        if cooldown <= 0:
            return 0.0
        state = _load_system_state()
        last_ts = float(state.get("last_normal_irrigation_timestamp") or 0.0)
        if last_ts <= 0:
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

    def _reservoir_retest_baseline(self, water_sec: float) -> dict[str, Any]:
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
        for trial in reversed(trials[-limit:]):
            try:
                sec = float(trial.get("water_sec"))
                delta = float(trial.get("delta_m"))
            except (TypeError, ValueError):
                continue
            if abs(sec - float(water_sec)) > tolerance:
                continue
            if str(trial.get("plan_label") or "") in {"reservoir_retest_probe", "emergency", "emergency_interrupt"}:
                continue
            if str(trial.get("status") or "") not in {"accepted", "rejected"}:
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
            "threshold": round(threshold, 3),
            "recent_deltas": [round(x, 3) for x in deltas[-10:]],
        }

    def _reservoir_retest_plan(self, reading: SensorReading, trigger: str = "") -> Optional[ActionPlan]:
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
        interval = float(self.cfg.get_constant("RESERVOIR_RETEST_INTERVAL_SEC") or 3600)
        sec = float(self.cfg.get_constant("RESERVOIR_RETEST_SEC") or 5.0)
        min_delta = float(self.cfg.get_constant("RESERVOIR_RETEST_MIN_DELTA") or 0.5)
        last_retest = max(
            float(reservoir.get("last_retest_at") or 0.0) if isinstance(reservoir, dict) else 0.0,
            float(low_wet.get("last_retest_at") or 0.0) if isinstance(low_wet, dict) else 0.0,
        )
        due = last_retest <= 0 or now - last_retest >= interval
        if not due:
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
        plan.predicted_trajectory = []
        plan.predicted_peak = round(float(reading.humidity) + min_delta, 3)
        logger.warning(
            f"[ReservoirRetest] due: trigger={trigger}, H={reading.humidity:.1f}%, probe={sec:.1f}s."
        )
        return plan

    def _execute_reservoir_retest(self, zone: ZoneStatus, reading: SensorReading, plan: ActionPlan) -> DecisionResult:
        if plan.water_sec <= 0:
            return self._finish_result(
                zone=zone,
                chosen_plan=plan,
                action_sec=0.0,
                reading=reading,
                notes="疑似水箱/水路无响应，复测间隔未到；暂停自动开泵。",
            )
        expected_delta = max(0.0, (plan.predicted_peak or reading.humidity) - reading.humidity)
        self._pending_soak = self.actuator.execute_pump(
            plan.water_sec,
            reading.humidity,
            is_emergency=False,
            expected_delta_m=expected_delta,
            plan_label=plan.label,
        )
        return self._finish_result(
            zone=zone,
            chosen_plan=plan,
            action_sec=plan.water_sec,
            reading=reading,
            notes=f"执行供水恢复复测 {plan.water_sec:.1f}s；结果不进入 K_P 学习。",
        )

    def _low_wet_recovery_plan(self, reading: SensorReading, style_exp: dict[str, Any]) -> Optional[ActionPlan]:
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

        retest_plan = self._reservoir_retest_plan(reading, "low_wet_recovery")
        if retest_plan is not None:
            return retest_plan

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
        if rejected >= reject_limit and avg_delta < delta_limit:
            now = time.time()
            pause_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_INEFFECTIVE_PAUSE_SEC") or 21600)
            pause_until = now + pause_sec
            def mark_suspect(current):
                suspect_state = {
                    "active": True,
                    "reason": "ineffective_low_wet_recovery",
                    "probable_causes": ["reservoir_empty", "pump_or_tube_no_flow", "outlet_not_near_probe"],
                    "reservoir_empty_suspect": True,
                    "exclude_from_learning": True,
                    "pause_until": pause_until,
                    "rejected": rejected,
                    "avg_delta_m": round(avg_delta, 3),
                    "current_humidity": round(h, 3),
                    "trigger_line": round(trigger_line, 3),
                    "last_water_sec": low_stats.get("last_water_sec"),
                    "updated_at": now,
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
                f"[LowWetRecovery] ineffective recovery paused: rejected={rejected}, avg_delta={avg_delta:.3f}%"
            )
            return ActionPlan("low_wet_recovery_paused", 0.0)

        state = _load_system_state()
        min_interval = float(self.cfg.get_constant("LOW_WET_RECOVERY_MIN_INTERVAL_SEC") or 21600)
        last_ts = float(state.get("last_normal_irrigation_timestamp") or 0.0)
        if last_ts > 0 and (time.time() - last_ts) < min_interval:
            return ActionPlan("low_wet_recovery_interval_observe", 0.0)

        fc_margin = float(self.cfg.get_constant("LOW_WET_RECOVERY_TARGET_FC_MARGIN") or 5.0)
        recovery_target = min(float(self.cfg.FC - fc_margin), float(self.cfg.M_SAFE_SLEEP - 1.0))
        recovery_target = max(recovery_target, trigger_line)
        deficit = max(0.0, recovery_target - h)
        try:
            kp = float(self.cfg.get("K_P_LOW", None))
        except (TypeError, ValueError):
            kp = float(self.cfg.K_P or 0.1)
        if kp <= 0:
            kp = max(float(self.cfg.K_P or 0.1), 0.1)
        water_min = float(self.cfg.get_constant("WATER_SEC_MIN") or 3.0)
        min_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_MIN_SEC") or 6.0)
        max_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_MAX_SEC") or 8.0)
        deep_trigger = float(self.cfg.get_constant("LOW_WET_RECOVERY_DEEP_TRIGGER") or 30.0)
        deep_min_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_DEEP_MIN_SEC") or 8.0)
        hard_margin = float(self.cfg.get_constant("LOW_WET_RECOVERY_HARD_MARGIN") or 1.0)
        hard_sec = float(self.cfg.get_constant("LOW_WET_RECOVERY_HARD_SEC") or max_sec)
        hard_cap = float(self.cfg.get_constant("WATER_SEC_MAX_HARD") or hard_sec)
        sec = max(deficit / kp if kp > 0 else min_sec, water_min, min_sec)
        if h <= deep_trigger:
            sec = max(sec, deep_min_sec)
        if h <= hard_line + hard_margin:
            max_sec = max(max_sec, hard_sec)
            sec = max(sec, min(hard_sec, hard_cap))
        sec = round(min(sec, max_sec, hard_cap), 1)
        plan = ActionPlan("low_wet_recovery", sec)
        plan.predicted_trajectory = [round(recovery_target, 3)]
        plan.predicted_peak = round(recovery_target, 3)
        logger.warning(
            f"[LowWetRecovery] H={h:.1f}% trigger={trigger_line:.1f}% target={recovery_target:.1f}% -> recovery {sec:.1f}s"
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
        marks = self.cfg.get_constant("SOAK_OBSERVATION_MARKS_SEC") or []
        elapsed = time.monotonic() - soak.pump_end_time
        changed = False
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
                quality_score=None,
                penalty=0.0,
                plan_label=soak.plan_label,
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
            _update_system_state(update)

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

    def _finish_result(
        self,
        zone: ZoneStatus,
        reading: SensorReading,
        action_sec: float = 0.0,
        chosen_plan: Optional[ActionPlan] = None,
        notes: str = "",
    ) -> DecisionResult:
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
    ) -> Optional[ActionPlan]:
        """
        战区长期 observe 保护。

        第一阶段需要通过反馈学习，不能在安全余量充足时无限观察。只有当 Layer 5
        选择 observe、连续观察超过阈值、距离 FC 足够远、且物理天花板允许最小脉冲时，
        才把本轮改为一次 3s 安全探针。
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

        if fc_gap < min_gap or ceiling_sec < water_min:
            logger.info(
                f"[Brain] 长期 observe 已达 {streak} 轮，但安全余量不足，"
                f"fc_gap={fc_gap:.2f}, ceiling={ceiling_sec:.2f}，继续观察。"
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
            f"{action_sec}s，fc_gap={fc_gap:.2f}，ceiling={ceiling_sec:.2f}。"
        )
        return forced

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
            if mode == "drydown_cycle" and (deep_drydown_time_complete or reading.humidity <= target):
                exp = {
                    "enabled": True,
                    "mode": "wet_hold",
                    "started_at": now,
                    "drydown_target": round(target, 3),
                    "reason": "drydown_window_complete",
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
        exp["updated_at"] = now

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

    def run_cycle(self) -> DecisionResult:
        """执行一次完整决策循环，返回 DecisionResult 供主进程记录与休眠。"""

        # First cloud-protection check: snapshot policy before any decision work.
        cloud_control = load_cloud_control()
        cloud_audit = cloud_protection_audit_view(cloud_control)
        def record_cloud_protection(state):
            state["cloud_protection"] = cloud_audit
        _update_system_state(record_cloud_protection)
        logger.info(
            "[CloudProtection] cycle gate: mode=%s risk=%s reason=%s "
            "command_id=%s expires_at=%s",
            cloud_audit["mode"],
            cloud_audit["risk_level"],
            cloud_audit["reason"],
            cloud_audit["command_id"],
            cloud_audit["expires_at"],
        )

        # 任务1：current_reading 用于复用 settle() 的传感器数据，跳过重复读取
        current_reading: Optional[SensorReading] = None

        # ── Fix-A / Fix-3：渗透等待期优先检查紧急状态 ───────────────
        if self._pending_soak is not None:
            soak = self._pending_soak

            # Fix-3：无论渗透是否完成，都先读传感器判断紧急态势
            reading = self.sensor.read()
            self._record_soak_observation(soak, reading)
            zone    = self.physics.assess_zone(reading)

            if zone == ZoneStatus.EMERGENCY and not soak.ready:
                allowed, cloud_reason = cloud_watering_allowed(
                    cloud_control,
                    emergency=True,
                    sensor_trusted=not reading.sensor_stale_hard,
                )
                if not allowed:
                    logger.critical(
                        "[CloudProtection] emergency interrupt blocked: %s",
                        cloud_reason,
                    )
                    return self._finish_result(
                        zone=zone,
                        chosen_plan=ActionPlan("cloud_protection_block", 0.0),
                        action_sec=0.0,
                        reading=reading,
                        notes="Cloud protection blocked emergency interrupt: %s" % cloud_reason,
                    )
                min_drop = float(self.cfg.get_constant("EMERGENCY_INTERRUPT_MIN_DROP") or 0.5)
                drop_since_pump = soak.pre_humidity - reading.humidity
                if drop_since_pump < min_drop:
                    logger.critical(
                        f"[Brain] 渗透期间仍处紧急区，但未继续明显下跌 "
                        f"(before={soak.pre_humidity:.1f}% current={reading.humidity:.1f}% "
                        f"drop={drop_since_pump:.2f}% < {min_drop:.2f}%)，继续等待渗透。"
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

        # ── Layer 1：计算物理天花板 ────────────────────────────────────
        ceiling = self.physics.calc_water_ceiling(reading.humidity)

        # ── Layer 2：态势评估 ─────────────────────────────────────────
        zone = self.physics.assess_zone(reading)

        allowed, cloud_reason = cloud_watering_allowed(
            cloud_control,
            emergency=(zone == ZoneStatus.EMERGENCY),
            sensor_trusted=not reading.sensor_stale_hard,
        )
        if not allowed:
            logger.critical(
                "[CloudProtection] automatic watering blocked: mode=%s reason=%s",
                cloud_protection_mode(cloud_control),
                cloud_reason,
            )
            return self._finish_result(
                zone=zone,
                chosen_plan=ActionPlan("cloud_protection_block", 0.0),
                action_sec=0.0,
                reading=reading,
                notes="Cloud protection blocked automatic watering: %s" % cloud_reason,
            )

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
                    "[Brain] 紧急区忽略 reservoir_empty_pause，继续执行紧急补水安全链路。"
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
            return self._finish_result(
                zone=zone,
                chosen_plan=recovery_plan,
                action_sec=0.0,
                reading=reading,
                notes=f"低湿恢复被 {recovery_plan.label} 阻止，继续观察。",
            )

        if recovery_plan is not None and recovery_plan.water_sec > 0:
            if recovery_plan.label == "reservoir_retest_probe":
                return self._execute_reservoir_retest(zone, reading, recovery_plan)
            expected_delta = max(0.0, (recovery_plan.predicted_peak or reading.humidity) - reading.humidity)
            self._pending_soak = self.actuator.execute_pump(
                recovery_plan.water_sec,
                reading.humidity,
                is_emergency=False,
                expected_delta_m=expected_delta,
                plan_label=recovery_plan.label,
            )
            return self._finish_result(
                zone=zone,
                chosen_plan=recovery_plan,
                action_sec=recovery_plan.water_sec,
                reading=reading,
                notes=f"低湿 wet_hold 恢复脉冲 {recovery_plan.water_sec:.1f}s。",
            )

        cooldown_left = self._normal_irrigation_cooldown_remaining(reading)
        if cooldown_left > 0:
            logger.info(
                f"[Brain] 普通浇水冷却中，剩余 {cooldown_left/3600:.2f}h；"
                f"本轮仅观察，不开泵。"
            )
            return self._finish_result(
                zone=zone,
                chosen_plan=ActionPlan("cooldown_observe", 0.0),
                action_sec=0.0,
                reading=reading,
                notes=f"普通浇水冷却中，剩余 {cooldown_left:.0f}s。",
            )

        plans        = self.gate.generate_exam(ceiling, reading)
        trajectories = self.predictor.request_prediction(plans, reading)
        best_plan    = self.court.evaluate_and_select(plans, trajectories)

        action_sec = max(best_plan.water_sec, 0.0)
        forced_plan = self._maybe_force_observe_probe(best_plan, reading, ceiling)
        if forced_plan is not None:
            best_plan = forced_plan
            action_sec = max(best_plan.water_sec, 0.0)

        # 计算 expected_delta_m：预测轨迹终点湿度 - 浇水前湿度
        # 供 _update_pattern_memory 在渗透结算时评估预测准确性，写入惩罚系数。
        _traj = best_plan.predicted_trajectory
        _exp_delta = (_traj[-1] - reading.humidity) if _traj else None

        if action_sec <= 0:
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
        self._pending_soak = self.actuator.execute_pump(
            action_sec, reading.humidity,
            is_emergency=False,
            expected_delta_m=_exp_delta,
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
                f"渗透哨兵已挂载。"
            ),
        )

