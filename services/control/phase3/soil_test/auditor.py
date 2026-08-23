"""
=============================================================================
 auditor.py  ——  夜间审计员（Nightly Auditor）
 ─────────────────────────────────────────────────────────────────────────
 【运行机制】
  独立 Python 脚本，通过 crontab 每天凌晨 04:10 触发。
   不负责浇水，只负责"翻阅历史账本"，寻找物理反馈信号，
   调用 ConfigManager.update() 悄悄修改基因。
   主控大脑每天一睁眼，拿到的就是最懂这盆植物当下状态的"新鲜基因"。

  crontab 写法：
    10 4 * * * /usr/bin/python3 /path/to/auditor.py

 【四大进化模块】
   模块 A  物理结构进化  ——  重测真实 FC，试探 TARGET_LOW 底线
   模块 B  气候权重进化  ——  跟随 VPD 调整 α/β，自适应 SLOPE_STEEP
   模块 C  化学基准进化  ——  大水冲洗后重建 EC_BASE，监控换土时机
   模块 D  硬件健康进化  ——  传感器方差自检，水泵老化递增 GAMMA

 【与 config_manager.py 的约定】
   · 审计员只提交锚点参数（FC、TARGET_LOW、EC_BASE 等）。
   · M_SAFE_SLEEP、RESPIRATION_LIMIT、M_WAKE_UP、EC_SALT_STRESS
     均由 ConfigManager._derive_linked_params() 自动联动算出，
     审计员无需手动计算，更不能直接写入这些派生字段。
   · 所有 update() 调用统一在 run_full_audit() 末尾一次性提交，
     避免多次写盘，也避免中间状态被主控大脑读到。

 【数据来源（列名占位符，部署时对照真实表头修改 _COL_* 常量）】
   · sensor_log.csv  系统内部日志（brain 每 5 分钟追加一行）
       列：timestamp, humidity, temperature, ec_raw, ec_norm, vpd, action_sec

 【边缘场景修复记录】：
   Fix-E1  跨夜截断漏洞（灰姑娘 Bug）
           数据窗口从 24h 扩大到 26h，为深夜 23:00 的大灌溉保留足够的观察期。
           模块 A/C 在抓取大灌溉事件时，同时过滤掉距当前不足 2h 的事件，
           防止观察期越界到"未来"。
   Fix-E2  夜间偏置导致 VPD 判断失效
           模块 B 计算 VPD 均值时，从过滤 >0 改为过滤 >0.5kPa，
           只保留白天产生真实蒸发拉力的读数，剔除夜间长达 12h 的近零值。
   Fix-E3  方差统计学谬误（对斜率直线误报）
           模块 D 改为计算一阶差分的方差，剥离自然失水的趋势项；
           匀速失水的差分是常数，方差为 0，不会误报传感器损坏。
   Fix-E4  单一极值摧毁 SLOPE_STEEP
           模块 B 收集所有窗口负斜率后，取第 5 百分位数而非 min()，
           抛弃前 5% 最极端的离谱尖峰，防止断联瞬间被永久固化。
   Fix-E5  两点法计算趋势的盲区
           _check_ec_trend 改用最小二乘时间序列回归（_linear_regression_time），
           取代首尾两点相减，利用全部历史样本估算全局趋势，大幅降低误报率。
   Fix-E6  CSV 并发读写竞争（午夜冲突）
           _load_sensor_log 读取 sensor_log.csv 前加 fcntl.LOCK_SH 共享读锁，
           先把全部行读入内存再释放锁，彻底消除与主循环 LOCK_EX 写锁的竞争窗口。
   Fix-E7  LARGE_WATER_THRESHOLD_SEC 静态常量键名失效
           模块 A/C 中对已删除的 SYSTEM_CONSTANTS["LARGE_WATER_THRESHOLD_SEC"] 的
           引用替换为 self.cfg.LARGE_WATER_THRESHOLD（动态属性，Upgrade-6 引入），
           确保大水阈值始终与当前系统的盆径和水泵参数自适应匹配。
=============================================================================
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, variance
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 导入 ConfigManager 及其常量
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from config_manager import ConfigManager, FILE_PATHS

DEVICE_CODE = "soil_test"
PARAMETER_CHANGE_SYSTEM = "phase3_soil_test"
PARAMETER_CHANGE_SOURCE = "nightly_auditor"

# ===========================================================================
# 日志配置
# ===========================================================================
_LOG_FILE = Path(__file__).parent / "auditor.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auditor: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
)
from runtime_io import (
    load_json_locked,
    read_csv_dicts_locked,
    save_json_locked,
    update_json_locked,
)

logger = logging.getLogger("nightly_auditor")


# ---------------------------------------------------------------------------
# GaussDB parameter change log
# ---------------------------------------------------------------------------
def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(float(value))
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _as_float_or_null(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gsql_exec(sql: str) -> None:
    result = subprocess.run(
        ["su", "-", "opengauss", "-c", "gsql -d soil_data -p 7654"],
        input=sql,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def _insert_parameter_change_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    values = []
    for row in rows:
        values.append(
            "("
            + ", ".join(
                [
                    _sql_literal(row["change_time"]),
                    _sql_literal(row["device_code"]),
                    _sql_literal(row["system_name"]),
                    _sql_literal(row["parameter_name"]),
                    _sql_literal(row.get("old_value")),
                    _sql_literal(row.get("new_value")),
                    _sql_literal(row.get("delta_value")),
                    _sql_literal(row["change_reason"]),
                    _sql_literal(row["change_source"]),
                    _sql_literal(row.get("audit_date")),
                    _sql_literal(row.get("evidence")),
                ]
            )
            + ")"
        )
    sql = (
        "INSERT INTO parameter_change_log "
        "(change_time, device_code, system_name, parameter_name, old_value, new_value, "
        "delta_value, change_reason, change_source, audit_date, evidence) VALUES "
        + ", ".join(values)
        + ";"
    )
    _gsql_exec(sql)

# ===========================================================================
# 数据列名占位符（部署时对照真实 sensor_log.csv 表头修改这里）
# ===========================================================================
_LOG_COL_TS          = "timestamp"
_LOG_COL_HUMIDITY    = "humidity"
_LOG_COL_TEMPERATURE = "temperature"
_LOG_COL_EC_NORM     = "ec_norm"
_LOG_COL_VPD         = "vpd"
_LOG_COL_ACTION_SEC  = "action_sec"

# ===========================================================================
# 审计员调优阈值（不参与进化，在此集中声明，避免魔法数字散落各处）
# ===========================================================================

# ── 模块 A：物理结构 ──────────────────────────────────────────────────────
_FC_EMA_ALPHA          = 0.2    # FC 更新的 EMA 系数（新测量值权重）
_FC_MIN_CHANGE         = 0.05   # FC 变化小于此值时不更新（过滤传感器抖动，%）
_FC_OBSERVE_WINDOW_H   = 2.0    # 大灌溉后观察期（小时），等待重力下渗完毕
_FC_MIN_OBSERVE_ROWS   = 3      # 观察期至少需要的有效数据行数
_TARGET_LOW_SAFE_GAP   = 10.0   # TARGET_LOW 不能低于 FC 的此数值（%），防战区崩溃
_TARGET_LOW_MAX_DAILY_DROP = 1.0  # TARGET_LOW 单次最多下探 1 个百分点，保留激进但防过冲
_SLOPE_FLAT_THRESHOLD  = 0.3    # 失水斜率"已变平缓"的判定阈值（%/5min）

# ── 模块 B：气候权重 ──────────────────────────────────────────────────────
_VPD_HIGH_THRESHOLD    = 1.5    # 盛夏判定阈值（kPa）
_VPD_LOW_THRESHOLD     = 0.8    # 寒冬判定阈值（kPa）
_ALPHA_STEP            = 0.1    # α 每次调整步长
_BETA_STEP             = 0.1    # β 每次调整步长
_ALPHA_MAX             = 2.0
_ALPHA_MIN             = 0.5
_BETA_MAX              = 2.0
_BETA_MIN              = 0.5
_SLOPE_STEEP_WINDOW    = 6      # 极速滑落检测滑动窗口（步，每步 5min）
_SLOPE_STEEP_MARGIN    = 1.2    # 实测最陡斜率 × 此系数 = 新 SLOPE_STEEP（留余量）
_SLOPE_STEEP_FLOOR     = -3.0   # SLOPE_STEEP 下限（防止过度宽松）
_SLOPE_MIN_CHANGE      = 0.05   # 变化小于此值时不更新

# ── 模块 C：化学基准 ──────────────────────────────────────────────────────
_EC_BASE_EMA_ALPHA     = 0.5    # EC_BASE 更新的 EMA 系数
_EC_MIN_CHANGE         = 5.0    # EC_BASE 变化小于此值时不更新（当前 EC_norm 为数百级归一化值）
_EC_STABLE_WINDOW_MIN  = 30     # 大水后等待 EC 稳定的分钟数
_EC_STABLE_FC_MARGIN   = 0.5    # 湿度重回 FC - 此值 时认为稳定（%）
_EC_STABLE_SAMPLE_ROWS = 6      # 取稳定期前几行做均值
_EC_HISTORY_DAYS       = 180    # system_state 中保留 EC 历史的最大天数
_EC_MONTHLY_RISE_LIMIT = 0.10   # 月增长率超过此值触发换土预警

# ── 模块 D：硬件健康 ──────────────────────────────────────────────────────
_QUIET_START_H          = 2     # 传感器静默期开始（02:00）
_QUIET_END_H            = 4     # 传感器静默期结束（04:00）
_QUIET_MIN_ROWS         = 4     # 静默期最少数据行数
_SENSOR_VARIANCE_THRESH = 0.15  # 方差超过此值判定传感器老化
_TRAJ_TOL_AGED          = 0.4   # 老化后放宽的容忍度
_TRAJ_TOL_NORMAL        = 0.2   # 正常容忍度
_SENSOR_HEALTH_CONFIRM_DAYS = 2  # 连续确认天数，避免 0.2/0.4 来回抖动
_DYNAMIC_MAX_PAIR_GAP_MIN = 12    # 动态可信度相邻点最大间隔
_DYNAMIC_WATER_EXCLUDE_MIN = 30   # 浇水前后此窗口内不做跳变判定
_DYNAMIC_JUMP_THRESH = 4.0        # 无浇水短时跳变超过此值计为异常
_DYNAMIC_HARD_JUMP_THRESH = 6.0   # 单次硬跳变阈值
_DYNAMIC_JUMP_COUNT_THRESH = 3    # 全天异常跳变累计阈值
_DYNAMIC_FLATLINE_MIN_MIN = 240   # 高蒸发下卡死至少 4h 才判异常
_PUMP_WARN_COUNT        = 2000  # 启停次数超过此值开始线性上调 GAMMA
_PUMP_LIFE_COUNT        = 5000  # 设计寿命（对应 GAMMA 上限）
_GAMMA_BASE             = 10.0  # GAMMA 初始值（与 _inject_phase1_genes 保持一致）
_GAMMA_MAX              = 20.0  # GAMMA 老化终态上限
_PUMP_ALERT_RATIO       = 0.9   # 达到寿命此比例时触发预警

# ── 输入数据质量门：异常传感器读数不得进入审计学习 ─────────────────────────
_TEMP_MIN_C             = 0.0
_TEMP_MAX_C             = 60.0
_VPD_MIN_KPA            = 0.0
_VPD_MAX_KPA            = 8.0
_HUMIDITY_MIN           = 0.0
_HUMIDITY_MAX           = 100.0
_EC_NORM_MIN            = 0.0
_EC_NORM_MAX            = 5000.0
_AUDITOR_STATE_KEY      = "nightly_auditor"
_LEARNING_ADVICE_TTL_SEC = 36 * 3600
_LEARNING_ADVICE_MIN_STRATEGY_SAMPLES = 3
_LEARNING_ADVICE_MAX_BIAS = 0.35


# ===========================================================================
# 辅助函数
# ===========================================================================

def _load_json(path: Path, default: Any) -> Any:
    return load_json_locked(path, default)


def _profile_path() -> Path:
    return FILE_PATHS.get(
        "IRRIGATION_PROFILE",
        FILE_PATHS["PATTERN_MEMORY"].with_name("irrigation_profile.json"),
    )


def _save_json(path: Path, data: Any) -> None:
    try:
        save_json_locked(path, data)
    except OSError as e:
        logger.error(f"[IO] 写入 {path.name} 失败: {e}")


_SYSTEM_STATE_DEFAULT: dict[str, Any] = {
    "pump_total_cycles": 0,
    "total_water_sec_dispensed": 0.0,
    "last_large_water_timestamp": None,
    "ec_base_history": [],
    "pending_soak": None,
}


def _merge_system_state(raw: Optional[dict]) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    return {**_SYSTEM_STATE_DEFAULT, **raw}


def _load_system_state() -> dict:
    return _merge_system_state(_load_json(FILE_PATHS["SYSTEM_STATE"], {}))


def _save_system_state(state: dict) -> None:
    def merge(current):
        merged = _merge_system_state(current)
        merged.update(state)
        return merged
    try:
        update_json_locked(FILE_PATHS["SYSTEM_STATE"], {}, merge)
    except OSError as e:
        logger.error(f"[system_state] 审计状态写入失败: {e}")


def _load_sensor_log(hours: int = 24) -> list[dict]:
    """从 sensor_log.csv 读取最近 hours 小时的有效记录。"""
    csv_path = FILE_PATHS["SENSOR_LOG"]
    if not csv_path.exists():
        logger.warning(f"[IO] sensor_log.csv 不存在: {csv_path}")
        return []

    cutoff = datetime.now() - timedelta(hours=hours)
    records: list[dict] = []
    skipped_bad_quality = 0

    try:
        reader_rows = read_csv_dicts_locked(csv_path)
    except OSError as e:
        logger.error(f"[IO] 读取 sensor_log.csv 失败: {e}")
        return records

    for row in reader_rows:
        try:
            ts = datetime.strptime(row[_LOG_COL_TS], "%Y-%m-%d %H:%M:%S")
            if ts < cutoff:
                continue
            humidity = float(row[_LOG_COL_HUMIDITY])
            temperature = float(row[_LOG_COL_TEMPERATURE])
            ec_norm = float(row[_LOG_COL_EC_NORM])
            vpd = float(row[_LOG_COL_VPD])
            action_sec = float(row[_LOG_COL_ACTION_SEC])

            if not (
                _HUMIDITY_MIN <= humidity <= _HUMIDITY_MAX
                and _TEMP_MIN_C <= temperature <= _TEMP_MAX_C
                and _EC_NORM_MIN <= ec_norm <= _EC_NORM_MAX
                and _VPD_MIN_KPA <= vpd <= _VPD_MAX_KPA
                and action_sec >= 0
            ):
                skipped_bad_quality += 1
                continue

            records.append({
                "timestamp": ts,
                "humidity": humidity,
                "temperature": temperature,
                "ec_norm": ec_norm,
                "vpd": vpd,
                "action_sec": action_sec,
            })
        except (KeyError, ValueError, TypeError):
            continue

    records.sort(key=lambda r: r["timestamp"])
    logger.info(
        f"[IO] 加载过去 {hours}h 日志: {len(records)} 条"
        f"（过滤异常读数 {skipped_bad_quality} 条）"
    )
    return records


def _linear_slope(h_series: list[float]) -> float:
    """
    最小二乘法计算湿度序列的线性斜率（%/步，每步 5 分钟）。
    比相邻差分更抗传感器噪声。
    """
    n = len(h_series)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = mean(h_series)
    numer  = sum((i - x_mean) * (h_series[i] - y_mean) for i in range(n))
    denom  = sum((i - x_mean) ** 2 for i in range(n))
    return numer / denom if denom != 0 else 0.0


def _linear_regression_time(points: list[tuple[float, float]]) -> float:
    """
    Fix-E5：基于时间戳的最小二乘线性回归，返回每秒的变化率。

    参数：
      points : [(Unix时间戳, 数值), ...] 列表，至少需要 2 个点

    返回：
      slope : 每秒变化量（正值 = 上升趋势，负值 = 下降趋势）

    用途：EC_BASE 月增长率趋势估算。
    相比首尾两点相减，利用全部历史样本，大幅降低端点离群值导致的误报率。
    """
    if len(points) < 2:
        return 0.0
    x_mean = mean(p[0] for p in points)
    y_mean = mean(p[1] for p in points)
    numer  = sum((p[0] - x_mean) * (p[1] - y_mean) for p in points)
    denom  = sum((p[0] - x_mean) ** 2 for p in points)
    return numer / denom if denom != 0 else 0.0


def _send_alert(message: str) -> None:
    """
    【占位】向用户发送预警通知。
    部署时替换为真实的邮件/微信/Telegram 接口。
    """
    logger.critical(f"用户预警: {message}")
    # TODO: requests.post(WEBHOOK_URL, json={"text": message})


# ===========================================================================
# NightlyAuditor
# ===========================================================================

class NightlyAuditor:
    """
    夜间审计员主类。

    每天凌晨 00:00 由 crontab 触发，读取历史数据，计算参数漂移，
    一次性提交给 ConfigManager，主控大脑次日读取生效。
    """

    def __init__(self):
        self.cfg   = ConfigManager()
        self.state = _load_system_state()
        logger.info(
            f"[Auditor] 初始化完成。"
            f"FC={self.cfg.FC}  TL={self.cfg.TARGET_LOW}  "
            f"战区={(self.cfg.FC - self.cfg.TARGET_LOW):.2f}%  |  "
            f"α={self.cfg.ALPHA}  β={self.cfg.BETA}  γ={self.cfg.GAMMA}  |  "
            f"EC_BASE={self.cfg.EC_BASE}"
        )

    def _audit_date(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    # ──────────────────────────────────────────────────────────────────────
    # 主入口
    # ──────────────────────────────────────────────────────────────────────



    def _parameter_change_rows(self, updates: dict[str, Any], old_values: dict[str, Any]) -> list[dict[str, Any]]:
        change_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        audit_date = datetime.now().strftime("%Y-%m-%d")
        evidence = json.dumps(
            {
                "source": "nightly_auditor",
                "update_keys": sorted(updates.keys()),
                "note": "logged after ConfigManager.update succeeded",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        rows: list[dict[str, Any]] = []
        for parameter_name in sorted(updates.keys()):
            old_value = _as_float_or_null(old_values.get(parameter_name))
            new_value = _as_float_or_null(self.cfg.get(parameter_name, updates.get(parameter_name)))
            delta_value = (
                round(new_value - old_value, 6)
                if old_value is not None and new_value is not None
                else None
            )
            rows.append(
                {
                    "change_time": change_time,
                    "device_code": DEVICE_CODE,
                    "system_name": PARAMETER_CHANGE_SYSTEM,
                    "parameter_name": parameter_name,
                    "old_value": old_value,
                    "new_value": new_value,
                    "delta_value": delta_value,
                    "change_reason": "nightly_audit",
                    "change_source": PARAMETER_CHANGE_SOURCE,
                    "audit_date": audit_date,
                    "evidence": evidence,
                }
            )
        return rows

    @staticmethod
    def _strategy_arm_name(label: str) -> str:
        label = str(label or "").lower()
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

    def _summarize_strategy_feedback(self) -> dict[str, Any]:
        profile = _load_json(_profile_path(), {})
        arms: dict[str, dict[str, Any]] = {}
        for zone_name, zone_data in (profile.get("zones") or {}).items():
            for label, stat in (zone_data.get("strategy_stats") or {}).items():
                arm = self._strategy_arm_name(label)
                item = arms.setdefault(arm, {
                    "attempts": 0, "accepted": 0, "rejected": 0,
                    "emergency": 0, "delta_samples": [], "zones": set(),
                })
                item["attempts"] += int(stat.get("attempts") or 0)
                item["accepted"] += int(stat.get("accepted") or 0)
                item["rejected"] += int(stat.get("rejected") or 0)
                item["emergency"] += int(stat.get("emergency") or 0)
                item["zones"].add(zone_name)
                avg_delta = stat.get("avg_delta_m")
                if isinstance(avg_delta, (int, float)):
                    item["delta_samples"].append(float(avg_delta))
        summary: dict[str, Any] = {}
        for arm, item in arms.items():
            attempts = int(item["attempts"])
            deltas = item["delta_samples"]
            summary[arm] = {
                "attempts": attempts,
                "accepted": int(item["accepted"]),
                "rejected": int(item["rejected"]),
                "emergency": int(item["emergency"]),
                "success_rate": round(item["accepted"] / attempts, 3) if attempts else None,
                "avg_delta_m": round(sum(deltas) / len(deltas), 3) if deltas else None,
                "zones": sorted(item["zones"]),
            }
        return summary

    def _build_nightly_learning_advice(
        self,
        df_26h: list[dict],
        df_7d: list[dict],
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        now_ts = time.time()
        exclusions: list[dict[str, Any]] = []
        if self.state.get("pending_soak"):
            exclusions.append({"type": "pending_soak", "reason": "pump_response_not_settled"})
        for key in ("water_delivery_suspect", "reservoir_empty_suspect", "low_wet_recovery_suspect", "sensor_fault"):
            suspect = self.state.get(key)
            if isinstance(suspect, dict) and (suspect.get("active") or suspect.get("exclude_from_learning")):
                exclusions.append({"type": key, "reason": str(suspect.get("reason") or "active_or_learning_excluded")})
        audit_state = self.state.get(_AUDITOR_STATE_KEY, {})
        sensor_health = audit_state.get("sensor_health", {}) if isinstance(audit_state, dict) else {}
        if isinstance(sensor_health, dict):
            dynamic = sensor_health.get("last_dynamic_integrity") or {}
            static = sensor_health.get("last_static_noise") or {}
            if dynamic.get("status") == "bad" or static.get("status") == "high":
                exclusions.append({"type": "sensor_health", "reason": f"static={static.get('status')},dynamic={dynamic.get('status')}"})

        strategy_stats = self._summarize_strategy_feedback()
        prefer: dict[str, float] = {}
        avoid: dict[str, float] = {}
        if not exclusions:
            for arm, bias in (("medium_pulse", 0.18), ("strong_pulse", 0.12)):
                if int((strategy_stats.get(arm) or {}).get("attempts") or 0) < _LEARNING_ADVICE_MIN_STRATEGY_SAMPLES:
                    prefer[arm] = bias
        micro = strategy_stats.get("micro_pulse", {})
        if int(micro.get("attempts") or 0) >= _LEARNING_ADVICE_MIN_STRATEGY_SAMPLES:
            rate = micro.get("success_rate")
            delta = micro.get("avg_delta_m")
            if (isinstance(rate, (int, float)) and rate < 0.45) or (isinstance(delta, (int, float)) and delta < 0.5):
                avoid["micro_pulse"] = 0.22

        return {
            "schema_version": 1,
            "advice_id": f"{DEVICE_CODE}-{self._audit_date()}-{int(now_ts)}",
            "source": "nightly_auditor",
            "device_code": DEVICE_CODE,
            "audit_date": self._audit_date(),
            "created_at": now_ts,
            "expires_at": now_ts + _LEARNING_ADVICE_TTL_SEC,
            "scope": "advisory_only",
            "control_boundary": {
                "may_directly_pump": False,
                "may_override_phase3": False,
                "may_change_style_mode": False,
                "may_cross_hard_safety": False,
            },
            "sample_quality": {
                "exclude_new_learning": bool(exclusions),
                "exclusions": exclusions,
                "watering_rows_26h": sum(1 for row in df_26h if float(row.get("action_sec") or 0.0) > 0),
                "rows_26h": len(df_26h),
                "rows_7d": len(df_7d),
            },
            "phase2": {"trust_delta": 0.0, "reason": "no_phase2_reliability_evidence"},
            "exploration": {
                "bias_delta": -0.25 if exclusions else 0.05,
                "reason": "sample_excluded" if exclusions else "keep_small_exploration_prior",
                "max_extra_daily_probes": 0 if exclusions else 1,
            },
            "arms": {"prefer": prefer, "avoid": avoid, "stats": strategy_stats, "notes": []},
            "parameter_updates": {
                "keys": sorted(updates.keys()),
                "commit_policy": "phase3_auto_approval",
                "will_auto_commit": bool(updates),
            },
        }

    def _store_nightly_learning_advice(self, advice: dict[str, Any]) -> None:
        self.state["nightly_learning_advice"] = advice
        audit_state = self.state.get(_AUDITOR_STATE_KEY, {})
        if not isinstance(audit_state, dict):
            audit_state = {}
        audit_state["last_learning_advice"] = advice
        self.state[_AUDITOR_STATE_KEY] = audit_state
        _save_system_state(self.state)
        logger.info(
            "[Advice] 已生成夜间学习建议: "
            f"prefer={advice.get('arms', {}).get('prefer')} "
            f"avoid={advice.get('arms', {}).get('avoid')}"
        )

    def _phase3_auto_approval(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not updates:
            return {"approved": True, "decision": "no_change", "reason": "no parameter updates"}

        reasons: list[str] = []
        if self.state.get("pending_soak"):
            reasons.append("pending_soak_active")
        water_delivery = self.state.get("water_delivery_suspect")
        if isinstance(water_delivery, dict) and water_delivery.get("active"):
            reasons.append("water_delivery_suspect_active")

        new_fc = float(updates.get("FC", self.cfg.get("FC", self.cfg.FC)))
        new_tl = float(updates.get("TARGET_LOW", self.cfg.get("TARGET_LOW", self.cfg.TARGET_LOW)))
        old_tl = float(self.cfg.get("TARGET_LOW", self.cfg.TARGET_LOW))
        if not 5.0 <= new_tl <= 95.0:
            reasons.append(f"TARGET_LOW out of plausible range: {new_tl:.3f}")
        if not 10.0 <= new_fc <= 98.0:
            reasons.append(f"FC out of plausible range: {new_fc:.3f}")
        if new_tl >= new_fc - 5.0:
            reasons.append(f"TARGET_LOW {new_tl:.3f}% too close to FC {new_fc:.3f}%")
        if abs(new_tl - old_tl) > _TARGET_LOW_MAX_DAILY_DROP:
            reasons.append(
                f"TARGET_LOW daily move {new_tl - old_tl:+.3f}% exceeds {_TARGET_LOW_MAX_DAILY_DROP}%"
            )

        for key in ("ALPHA", "BETA"):
            if key in updates and not 0.3 <= float(updates[key]) <= 2.5:
                reasons.append(f"{key} out of adaptive range: {updates[key]}")
        if "GAMMA" in updates and not _GAMMA_BASE <= float(updates["GAMMA"]) <= _GAMMA_MAX:
            reasons.append(f"GAMMA out of hardware range: {updates['GAMMA']}")
        if "TRAJ_TOLERANCE" in updates and not 0.1 <= float(updates["TRAJ_TOLERANCE"]) <= 0.6:
            reasons.append(f"TRAJ_TOLERANCE out of sensor range: {updates['TRAJ_TOLERANCE']}")
        if "SLOPE_STEEP" in updates and float(updates["SLOPE_STEEP"]) >= 0:
            reasons.append(f"SLOPE_STEEP must remain negative: {updates['SLOPE_STEEP']}")
        if "EC_BASE" in updates and float(updates["EC_BASE"]) <= 0:
            reasons.append(f"EC_BASE must remain positive: {updates['EC_BASE']}")

        return {
            "approved": not reasons,
            "decision": "auto_accept" if not reasons else "auto_reject",
            "reason": "; ".join(reasons) if reasons else "phase3 auto approval passed",
            "commit_policy": "phase3_auto_approval",
            "updates": updates,
        }

    def _record_phase3_parameter_decision(
        self,
        updates: dict[str, Any],
        decision: dict[str, Any],
        before: dict[str, Any],
        after: Optional[dict[str, Any]] = None,
    ) -> None:
        if not updates:
            return
        audit_state = self.state.get(_AUDITOR_STATE_KEY, {})
        if not isinstance(audit_state, dict):
            audit_state = {}
        record = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "audit_date": self._audit_date(),
            "status": "auto_accepted" if decision.get("approved") else "auto_rejected",
            "commit_policy": "phase3_auto_approval",
            "phase3_decision": decision,
            "updates": updates,
            "before": before,
            "after": after or before,
        }
        audit_state["pending_param_change"] = record
        audit_state["last_parameter_decision"] = record
        self.state[_AUDITOR_STATE_KEY] = audit_state
        _save_system_state(self.state)

    def run_full_audit(self) -> None:
        """
        完整夜间审计流程：
          1. 加载历史数据
          2. 四模块独立计算（任一模块异常不影响其他模块）
          3. 统一一次性提交 → ConfigManager 触发联动推导 → 写盘
        """
        logger.info("=" * 65)
        logger.info(f"  夜间审计启动  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 65)

        df_26h = _load_sensor_log(hours=26)   # Fix-E1：扩大到 26h，覆盖深夜大灌溉的观察期
        df_7d  = _load_sensor_log(hours=24 * 7)

        if not df_26h:
            logger.warning("[Auditor] 过去 26h 无有效传感器数据，跳过本轮审计。")
            return

        updates: dict[str, Any] = {}

        for module_name, method, data in [
            ("模块A 物理结构", self._evolve_physical_structure, df_26h),  # Fix-E1
            ("模块B 气候权重", self._evolve_climate_weights,    df_7d),
            ("模块C 化学基准", self._evolve_chemical_baseline,  df_26h),  # Fix-E1
            ("模块D 硬件健康", self._evolve_hardware_health,    df_26h),  # Fix-E1
        ]:
            try:
                result = method(data)
                updates.update(result)
            except Exception as e:
                logger.error(f"[{module_name}] 异常: {e}", exc_info=True)

        learning_advice = self._build_nightly_learning_advice(df_26h, df_7d, updates)
        self._store_nightly_learning_advice(learning_advice)

        if updates:
            logger.info(f"\n{'─'*55}")
            logger.info("  本轮参数建议汇总（Phase3 自动审批）：")
            for k, v in updates.items():
                old = self.cfg.get(k, "?")
                logger.info(f"    {k:25s}: {old!r:>10}  →  {v!r}")
            logger.info(f"{'─'*55}\n")
            before = {k: self.cfg.get(k, None) for k in updates.keys()}
            decision = self._phase3_auto_approval(updates)
            if decision.get("approved"):
                logger.info(f"  Phase3 自动审批通过，写入控制参数: {decision.get('reason')}")
                self.cfg.update(updates, persist=True)
                after = {k: self.cfg.get(k, updates.get(k)) for k in updates.keys()}
                self._record_phase3_parameter_decision(updates, decision, before, after)
                try:
                    change_rows = self._parameter_change_rows(updates, before)
                    _insert_parameter_change_rows(change_rows)
                    if change_rows:
                        logger.info(f"  参数变化已写入 GaussDB parameter_change_log: {len(change_rows)} 行")
                except Exception as e:
                    logger.error(f"  参数变化写入 GaussDB 失败，JSON 审计历史仍保留: {e}")
            else:
                logger.warning(f"  Phase3 自动审批拒绝，不写入参数: {decision.get('reason')}")
                self._record_phase3_parameter_decision(updates, decision, before)
        else:
            logger.info("  今夜无参数漂移，无需写盘。")

        logger.info("\n  审计完成。参数建议由 Phase3 自动审批，硬边界内允许主动进化。")
        logger.info("=" * 65)

    # ──────────────────────────────────────────────────────────────────────
    # 模块 A：物理结构进化
    # ──────────────────────────────────────────────────────────────────────

    def _evolve_physical_structure(self, df: list[dict]) -> dict[str, Any]:
        """
        模块 A：重测 FC，试探 TARGET_LOW。

        FC 测量方法：
          · 找当天大灌溉事件（action_sec ≥ LARGE_WATER_THRESHOLD_SEC）
          · 截取其后 2 小时观察期（等待重力下渗完毕）
          · 取观察期内的湿度极大值作为 FC_measured
          · EMA 平滑：FC_new = (1-α)×FC_old + α×FC_measured

        TARGET_LOW 试探方法：
          · 扫描是否有湿度跌穿 TARGET_LOW 的时段
          · 计算该时段的失水斜率
          · 若斜率未变平缓（植物气孔未关闭，未感到干渴）→ TARGET_LOW 有上限地下探
          · 若已变平缓（植物已自我保护）→ 不下调，说明当前 TARGET_LOW 合适

        注意：绝对不写入 M_SAFE_SLEEP / M_WAKE_UP，由 ConfigManager 联动计算。
        """
        updates: dict[str, Any] = {}
        # Upgrade-6：LARGE_WATER_THRESHOLD_SEC 已从 SYSTEM_CONSTANTS 迁移为动态属性，
        # 按 (FC - TARGET_LOW) / K_P × LARGE_WATER_RATIO 实时推导，换盆/换泵自动适配。
        large_thr = self.cfg.LARGE_WATER_THRESHOLD

        # ── A1：重测 FC ───────────────────────────────────────────────────
        # Fix-E1：safe_cutoff 过滤掉距当前不足 2h 的事件，防止观察期越界到"未来"。
        # 例：23:58 发生大灌溉，到 00:00 审计时观察期只有 2 分钟，数据严重不足；
        #     若不过滤，系统会误判或静默跳过，且下一天 24h 窗口又找不到它（灰姑娘 Bug）。
        safe_cutoff  = datetime.now() - timedelta(hours=_FC_OBSERVE_WINDOW_H)
        large_events = [
            r for r in df
            if r["action_sec"] >= large_thr and r["timestamp"] <= safe_cutoff
        ]
        logger.info(f"[ModA] 大灌溉事件（含观察期安全过滤）: {len(large_events)} 次 "
                    f"（阈值 ≥ {large_thr}s，截止 {safe_cutoff.strftime('%H:%M')}）")

        for event in sorted(large_events, key=lambda r: r["timestamp"]):
            event_ts   = event["timestamp"]
            window_end = event_ts + timedelta(hours=_FC_OBSERVE_WINDOW_H)

            # 截取观察期：事件发生后 2 小时内的记录
            observe = [r for r in df if event_ts < r["timestamp"] <= window_end]

            if len(observe) < _FC_MIN_OBSERVE_ROWS:
                logger.info(
                    f"[ModA] {event_ts.strftime('%H:%M')} 大灌溉后观察期数据不足 "
                    f"({len(observe)}/{_FC_MIN_OBSERVE_ROWS})，跳过。"
                )
                continue

            fc_measured = max(r["humidity"] for r in observe)
            fc_old      = self.cfg.FC
            fc_new      = round((1 - _FC_EMA_ALPHA) * fc_old + _FC_EMA_ALPHA * fc_measured, 3)

            if abs(fc_new - fc_old) >= _FC_MIN_CHANGE:
                updates["FC"] = fc_new
                logger.info(
                    f"[ModA] FC 重测 @ {event_ts.strftime('%H:%M')}: "
                    f"观察极大值={fc_measured:.2f}%  "
                    f"EMA({_FC_EMA_ALPHA}): {fc_old:.3f} → {fc_new:.3f}%"
                )
            else:
                logger.info(
                    f"[ModA] FC 变化 {abs(fc_new-fc_old):.3f}% < {_FC_MIN_CHANGE}%，"
                    f"在传感器噪声范围内，无需更新。"
                )
            break  # 每天只取第一次大灌溉作为 FC 样本

        if not large_events:
            logger.info("[ModA] 过去 24h 无大灌溉事件，FC 无法重测。")

        # ── A2：气孔拐点猎手 (大胆试探真正的 TARGET_LOW) ───────────────────────────
        old_tl = self.cfg.TARGET_LOW
        
        # 只提取没有浇水动作的纯自然失水记录
        natural = sorted(
            [r for r in df if r["action_sec"] == 0],
            key=lambda r: r["timestamp"]
        )

        if len(natural) >= _SLOPE_STEEP_WINDOW:
            h_nat = [r["humidity"] for r in natural]
            steep_humidities = []
            
            # 滑动窗口扫描：寻找失水斜率仍然陡峭的片段
            for i in range(len(h_nat) - _SLOPE_STEEP_WINDOW + 1):
                window = h_nat[i : i + _SLOPE_STEEP_WINDOW]
                slope = _linear_slope(window)
                
                # slope 是负值代表失水中。如果比 -_SLOPE_FLAT_THRESHOLD 还小（比如 -0.4 < -0.3）
                # 说明这段时间植物正在畅快喝水，气孔完全大开
                if slope < -_SLOPE_FLAT_THRESHOLD:
                    steep_humidities.append(min(window))
            
            if steep_humidities:
                # 仍在畅快喝水的最低湿度，就是植物今天展现出的真实抗旱底线！
                true_bottom = round(min(steep_humidities), 3)
                safe_floor  = round(self.cfg.FC - _TARGET_LOW_SAFE_GAP, 3)
                
                # 如果今天测出的底线比现在的 TARGET_LOW 还要低，就主动下探。
                # 但单日最多下探 _TARGET_LOW_MAX_DAILY_DROP，保留激进，同时避免一次坏样本过冲。
                if true_bottom < old_tl:
                    observed_target = max(true_bottom, safe_floor)
                    daily_floor = round(old_tl - _TARGET_LOW_MAX_DAILY_DROP, 3)
                    new_tl = round(max(observed_target, daily_floor), 3)
                    updates["TARGET_LOW"] = new_tl
                    
                    if new_tl == safe_floor and true_bottom < safe_floor:
                        logger.warning(
                            f"[ModA] 气孔拐点 ({true_bottom}%) 已触及物理安全底线 ({safe_floor}%)，"
                            f"TARGET_LOW 被强制拦截在 {safe_floor}%，不再下探。"
                        )
                    else:
                        logger.info(
                            f"[ModA] 气孔拐点猎手触发，受单日下探上限保护: "
                            f"{old_tl} → {new_tl}% "
                            f"（观测底线 {true_bottom}%，单日上限 {_TARGET_LOW_MAX_DAILY_DROP}%）"
                        )
                else:
                    logger.info(
                        f"[ModA] 试探中，今日陡峭失水极小值 ({true_bottom}%) 未突破当前 TARGET_LOW ({old_tl}%)，保持观望。"
                    )
            else:
                logger.info(f"[ModA] 今日未检测到斜率陡峭（<{ -_SLOPE_FLAT_THRESHOLD}）的有效失水段。")
        else:
            logger.info("[ModA] 过去 24h 连续自然失水数据不足，无法寻找气孔拐点。")

        return updates

    # ──────────────────────────────────────────────────────────────────────
    # 模块 B：气候权重进化
    # ──────────────────────────────────────────────────────────────────────

    def _evolve_climate_weights(self, df: list[dict]) -> dict[str, Any]:
        """
        模块 B：跟随 7 日 VPD 均值调整 α/β，自适应 SLOPE_STEEP。

        α/β 调整逻辑：
          · 盛夏（VPD > 1.5kPa）：α↑ β↓（生存优先，减少沤根惩罚）
          · 寒冬（VPD < 0.8kPa）：β↑ α↓（防沤根优先，减少干旱惩罚）
          · 温和区间：不调整（避免参数抖动）

        SLOPE_STEEP 自适应：
          · 提取 7 日内自然失水（无浇水）的滑动窗口斜率
          · 取最陡值 × 1.2 余量 = 新的 SLOPE_STEEP
          · 防止夏天蒸腾加速导致系统天天误报"极速滑落"
        """
        updates: dict[str, Any] = {}

        if not df:
            logger.warning("[ModB] 7 日数据为空，跳过气候权重进化。")
            return updates

        # ── B1：7 日有效平均 VPD（只取白天产生真实蒸发拉力的读数）────────
        # Fix-E2：夜间 VPD 长达 12h 接近 0，若全部纳入会严重拉低均值，
        #         导致系统永远达不到盛夏阈值 1.5kPa。只保留 >0.5kPa 的白昼峰值段。
        daytime_vpds = [r["vpd"] for r in df if r["vpd"] > 0.5]

        if not daytime_vpds:
            logger.warning("[ModB] 7 日内未检测到有效日间蒸发（VPD 均 ≤ 0.5kPa），"
                           "可能为寒冬或空气传感器离线，使用 avg_vpd=0.0。")
            avg_vpd = 0.0
        else:
            avg_vpd = mean(daytime_vpds)

        logger.info(f"[ModB] 7 日有效平均 VPD = {avg_vpd:.4f} kPa  "
                    f"（{len(daytime_vpds)} 条白昼强蒸发读数）")

        # ── B2：α/β 博弈推演 ─────────────────────────────────────────────
        alpha, beta = self.cfg.ALPHA, self.cfg.BETA

        if avg_vpd > _VPD_HIGH_THRESHOLD:
            new_alpha = round(min(alpha + _ALPHA_STEP, _ALPHA_MAX), 3)
            new_beta  = round(max(beta  - _BETA_STEP,  _BETA_MIN),  3)
            logger.info(
                f"[ModB]盛夏（VPD={avg_vpd:.3f} > {_VPD_HIGH_THRESHOLD}）"
                f"→ 生存优先: α {alpha} → {new_alpha}，β {beta} → {new_beta}"
            )
            if new_alpha != alpha: updates["ALPHA"] = new_alpha
            if new_beta  != beta:  updates["BETA"]  = new_beta

        elif avg_vpd < _VPD_LOW_THRESHOLD:
            new_alpha = round(max(alpha - _ALPHA_STEP, _ALPHA_MIN), 3)
            new_beta  = round(min(beta  + _BETA_STEP,  _BETA_MAX),  3)
            logger.info(
                f"[ModB]寒冬（VPD={avg_vpd:.3f} < {_VPD_LOW_THRESHOLD}）"
                f"→ 防沤根优先: α {alpha} → {new_alpha}，β {beta} → {new_beta}"
            )
            if new_alpha != alpha: updates["ALPHA"] = new_alpha
            if new_beta  != beta:  updates["BETA"]  = new_beta

        else:
            logger.info(
                f"[ModB]温和季节（VPD ∈ [{_VPD_LOW_THRESHOLD}, {_VPD_HIGH_THRESHOLD}]），"
                f"α/β 无需调整。"
            )

        # ── B3：SLOPE_STEEP 自适应 ────────────────────────────────────────
        # 只取无浇水的自然失水段（排除浇水后的湿度回升干扰）
        natural = sorted(
            [r for r in df if r["action_sec"] == 0],
            key=lambda r: r["timestamp"]
        )
        h_nat        = [r["humidity"] for r in natural]
        valid_slopes: list[float] = []

        w = _SLOPE_STEEP_WINDOW
        if len(h_nat) >= w:
            for i in range(len(h_nat) - w + 1):
                s = _linear_slope(h_nat[i: i + w])
                if s < 0:   # 只收集负斜率（失水段）
                    valid_slopes.append(s)

        if valid_slopes:
            valid_slopes.sort()   # 从小到大（最陡负值在最前）

            # Fix-E4：取第 5 百分位数，抛弃前 5% 最极端的离谱尖峰。
            # 原来用 min() 只要传感器断联产生一次 45%→0% 的瞬间跳变，
            # 算出的斜率就是天文数字，且会被永久固化。
            # 第 5 百分位兼顾了"取最陡真实失水速率"与"抛弃异常尖峰"的平衡。
            idx      = max(int(len(valid_slopes) * 0.05), 0)
            steepest = valid_slopes[idx]

            new_slope = round(max(steepest * _SLOPE_STEEP_MARGIN, _SLOPE_STEEP_FLOOR), 3)
            old_slope = self.cfg.SLOPE_STEEP
            if abs(new_slope - old_slope) >= _SLOPE_MIN_CHANGE:
                updates["SLOPE_STEEP"] = new_slope
                logger.info(
                    f"[ModB] SLOPE_STEEP 自适应: "
                    f"7日自然失水 P5={steepest:.4f}%/5min（{len(valid_slopes)} 个样本）  "
                    f"更新: {old_slope} → {new_slope}"
                )
            else:
                logger.info(f"[ModB] SLOPE_STEEP 变化 < {_SLOPE_MIN_CHANGE}，无需更新。")
        else:
            logger.info("[ModB] 7 日内未检测到自然失水斜率，SLOPE_STEEP 保持不变。")

        return updates

    # ──────────────────────────────────────────────────────────────────────
    # 模块 C：化学基准进化
    # ──────────────────────────────────────────────────────────────────────

    def _evolve_chemical_baseline(self, df: list[dict]) -> dict[str, Any]:
        """
        模块 C：大水冲洗后重建 EC_BASE，监控盐分积累趋势。

        EC_BASE 测量方法：
          · 找大灌溉事件
          · 等待 30 分钟后，当湿度重回 FC 附近时认为稳定
          · 取稳定期前几行 EC_norm 的均值作为测量值
          · EMA 平滑（系数 0.5，对新数据更敏感）

        换土预警：
          · 记录每次 EC_BASE 到 system_state.json 历史列表
          · 若月增长率 > 10%，触发用户通知

        注意：EC_SALT_STRESS = EC_BASE × 1.5 由 ConfigManager 联动计算，无需手动提交。
        """
        updates: dict[str, Any] = {}
        # Upgrade-6：同模块 A，使用动态属性替代已删除的静态常量。
        large_thr = self.cfg.LARGE_WATER_THRESHOLD

        # ── C1：重建 EC_BASE ──────────────────────────────────────────────
        # Fix-E1：同模块 A，过滤掉距当前不足稳定等待时间的事件，防止稳定期越界。
        safe_cutoff  = datetime.now() - timedelta(minutes=_EC_STABLE_WINDOW_MIN)
        large_events = sorted(
            [r for r in df if r["action_sec"] >= large_thr and r["timestamp"] <= safe_cutoff],
            key=lambda r: r["timestamp"]
        )

        ec_base_new_val = self.cfg.EC_BASE   # 默认不变
        ec_trend_sample_obtained = False

        if not large_events:
            logger.info("[ModC] 过去 24h 无大水冲洗事件，EC_BASE 无法重建。")
        else:
            for event in large_events:
                event_ts     = event["timestamp"]
                stable_start = event_ts + timedelta(minutes=_EC_STABLE_WINDOW_MIN)
                fc_floor     = self.cfg.FC - _EC_STABLE_FC_MARGIN

                # 稳定期：时间戳在等待期后 且 湿度已回到 FC 附近
                stable = [
                    r for r in df
                    if r["timestamp"] >= stable_start and r["humidity"] >= fc_floor
                ]

                if not stable:
                    logger.info(
                        f"[ModC] {event_ts.strftime('%H:%M')} 大水后，"
                        f"湿度未回到 FC 附近（≥{fc_floor:.1f}%），跳过。"
                    )
                    continue

                ec_measured = mean(r["ec_norm"] for r in stable[:_EC_STABLE_SAMPLE_ROWS])
                ec_old      = self.cfg.EC_BASE
                ec_new      = round(
                    (1 - _EC_BASE_EMA_ALPHA) * ec_old + _EC_BASE_EMA_ALPHA * ec_measured, 4
                )
                ec_base_new_val = ec_new
                ec_trend_sample_obtained = True

                if abs(ec_new - ec_old) >= _EC_MIN_CHANGE:
                    updates["EC_BASE"] = ec_new
                    logger.info(
                        f"[ModC] EC_BASE 重建 @ {event_ts.strftime('%H:%M')}: "
                        f"稳定期均值={ec_measured:.4f}  "
                        f"EMA({_EC_BASE_EMA_ALPHA}): {ec_old:.4f} → {ec_new:.4f} mS/cm"
                    )
                else:
                    logger.info(
                        f"[ModC] EC_BASE 变化 {abs(ec_new-ec_old):.4f} < {_EC_MIN_CHANGE}，"
                        f"无需更新。"
                    )
                break   # 只取第一次大灌溉

        # ── C2：盐分积累趋势监控 + 换土预警 ─────────────────────────────
        if ec_trend_sample_obtained:
            self._check_ec_trend(ec_base_new_val)
        else:
            logger.info("[ModC] 无有效 EC 稳定样本，本轮不追加 EC_BASE 趋势历史。")

        return updates

    def _check_ec_trend(self, current_ec: float) -> None:
        """
        将本次 EC_BASE 记录追加到历史列表，分析月增长率，超阈值则预警。

        Fix-E5：使用最小二乘时间序列回归（_linear_regression_time）替代首尾两点相减。
        原来的两点法：月增长率 = (当前 - 最旧) / 最旧 / 月数。
        问题：若首尾任意一次采样是离群值（传感器读数偏高/偏低），
              会算出荒谬的增长率触发误报。
        新方法：用所有历史点做线性回归，估算每秒变化率，换算为月增长率，
               需要至少 3 个点，对单点偏差的容忍度大幅提升。
        """
        now_ts  = time.time()
        history: list[dict] = self.state.get("ec_base_history", [])

        history.append({"timestamp": now_ts, "ec_base": current_ec})

        # 只保留最近 N 天
        cutoff_ts = now_ts - _EC_HISTORY_DAYS * 86400
        history   = [h for h in history if h["timestamp"] >= cutoff_ts]
        self.state["ec_base_history"] = history
        _save_system_state(self.state)

        # Fix-E5：至少需要 3 个点才能做有意义的回归
        if len(history) < 3:
            return

        oldest    = min(history, key=lambda h: h["timestamp"])
        days_span = (now_ts - oldest["timestamp"]) / 86400.0
        if days_span < 7:
            return   # 数据跨度太短，趋势判断不可靠

        # Fix-E5：最小二乘全局趋势，而非首尾两点相减
        pts      = [(h["timestamp"], h["ec_base"]) for h in history]
        sec_rate = _linear_regression_time(pts)          # 每秒变化量

        # 换算为月增长率：每秒增量 × 30天 × 86400秒/天 / 初始基准值
        monthly_rise = sec_rate * 86400 * 30
        monthly_rate = monthly_rise / max(oldest["ec_base"], 1e-6)

        logger.info(
            f"[ModC] EC_BASE 月增长率（回归）: {monthly_rate * 100:.1f}%/月  "
            f"（跨度 {days_span:.0f} 天，样本数 {len(history)}，"
            f"sec_rate={sec_rate:.2e} mS/cm/s）"
        )

        if monthly_rate > _EC_MONTHLY_RISE_LIMIT:
            _send_alert(
                f"土壤盐分预警：EC_BASE 月增长率 {monthly_rate*100:.1f}% "
                f"> 阈值 {_EC_MONTHLY_RISE_LIMIT*100:.0f}%。\n"
                f"当前 EC_BASE={current_ec:.4f} mS/cm，盐分正在不可逆积累。\n"
                f"建议考虑换土，防止根系盐害。"
            )

    # ──────────────────────────────────────────────────────────────────────
    # 模块 D：硬件健康进化
    # ──────────────────────────────────────────────────────────────────────

    def _sensor_health_state(self) -> dict[str, Any]:
        audit_state = self.state.get(_AUDITOR_STATE_KEY, {})
        if not isinstance(audit_state, dict):
            audit_state = {}
        sensor_health = audit_state.get("sensor_health", {})
        if not isinstance(sensor_health, dict):
            sensor_health = {}
        return sensor_health

    def _store_sensor_health_state(self, sensor_health: dict[str, Any]) -> None:
        audit_state = self.state.get(_AUDITOR_STATE_KEY, {})
        if not isinstance(audit_state, dict):
            audit_state = {}
        audit_state["sensor_health"] = sensor_health
        self.state[_AUDITOR_STATE_KEY] = audit_state

    def _watering_context_reason(self, rows: list[dict]) -> Optional[str]:
        if self.state.get("pending_soak"):
            return "pending_soak_active"
        if any(float(r.get("action_sec", 0.0) or 0.0) > 0 for r in rows):
            return "action_sec_in_window"
        return None

    def _near_watering(self, ts: datetime, water_times: list[datetime]) -> bool:
        window = _DYNAMIC_WATER_EXCLUDE_MIN * 60
        return any(abs((ts - wt).total_seconds()) <= window for wt in water_times)

    def _audit_static_noise(self, df: list[dict]) -> dict[str, Any]:
        audit_day = datetime.strptime(self._audit_date(), "%Y-%m-%d").date()
        quiet = sorted(
            [
                r for r in df
                if r["timestamp"].date() == audit_day
                and _QUIET_START_H <= r["timestamp"].hour < _QUIET_END_H
            ],
            key=lambda r: r["timestamp"],
        )
        logger.info(
            f"[ModD] 当日凌晨静默期 "
            f"（{_QUIET_START_H:02d}:00–{_QUIET_END_H:02d}:00）: {len(quiet)} 条"
        )

        reason = self._watering_context_reason(quiet)
        if reason:
            logger.info(f"[ModD] 静默期被浇水/渗透上下文污染，跳过静态噪声判断: {reason}")
            return {"status": "skipped", "reason": reason, "rows": len(quiet)}
        if len(quiet) < _QUIET_MIN_ROWS:
            logger.info(f"[ModD] 静默期数据不足（{len(quiet)} < {_QUIET_MIN_ROWS}），跳过静态噪声判断。")
            return {"status": "skipped", "reason": "insufficient_rows", "rows": len(quiet)}

        diffs: list[float] = []
        for i in range(1, len(quiet)):
            gap_min = (quiet[i]["timestamp"] - quiet[i - 1]["timestamp"]).total_seconds() / 60
            if 0 < gap_min <= _DYNAMIC_MAX_PAIR_GAP_MIN:
                diffs.append(quiet[i]["humidity"] - quiet[i - 1]["humidity"])
        if len(diffs) < 2:
            logger.info("[ModD] 静默期连续样本不足，跳过静态噪声判断。")
            return {"status": "skipped", "reason": "insufficient_pairs", "rows": len(quiet)}

        try:
            var = variance(diffs)
        except Exception:
            var = 0.0
        status = "high" if var > _SENSOR_VARIANCE_THRESH else "normal"
        logger.info(
            f"[ModD] 当日静态噪声方差: {var:.5f} "
            f"（阈值 {_SENSOR_VARIANCE_THRESH}, status={status}）"
        )
        return {"status": status, "variance": round(var, 6), "rows": len(quiet), "pairs": len(diffs)}

    def _audit_dynamic_integrity(self, df: list[dict]) -> dict[str, Any]:
        rows = sorted(df, key=lambda r: r["timestamp"])
        water_times = [
            r["timestamp"] for r in rows
            if float(r.get("action_sec", 0.0) or 0.0) > 0
        ]
        valid_pairs = 0
        jump_count = 0
        max_jump = 0.0
        max_jump_at: Optional[str] = None
        flatline_max_min = 0.0
        flatline_start: Optional[datetime] = None
        flatline_vpds: list[float] = []
        flatline_bad = False

        for i in range(1, len(rows)):
            prev = rows[i - 1]
            cur = rows[i]
            gap_min = (cur["timestamp"] - prev["timestamp"]).total_seconds() / 60
            if gap_min <= 0 or gap_min > _DYNAMIC_MAX_PAIR_GAP_MIN:
                flatline_start = None
                flatline_vpds = []
                continue
            if self._near_watering(prev["timestamp"], water_times) or self._near_watering(cur["timestamp"], water_times):
                flatline_start = None
                flatline_vpds = []
                continue

            valid_pairs += 1
            diff = cur["humidity"] - prev["humidity"]
            abs_diff = abs(diff)
            if abs_diff > max_jump:
                max_jump = abs_diff
                max_jump_at = f"{prev['timestamp']}->{cur['timestamp']}"
            if abs_diff >= _DYNAMIC_JUMP_THRESH:
                jump_count += 1

            if abs_diff <= 0.001:
                if flatline_start is None:
                    flatline_start = prev["timestamp"]
                    flatline_vpds = []
                flatline_vpds.append(float(prev.get("vpd", 0.0) or 0.0))
                flatline_vpds.append(float(cur.get("vpd", 0.0) or 0.0))
                span_min = (cur["timestamp"] - flatline_start).total_seconds() / 60
                flatline_max_min = max(flatline_max_min, span_min)
                if span_min >= _DYNAMIC_FLATLINE_MIN_MIN and flatline_vpds and mean(flatline_vpds) > _VPD_LOW_THRESHOLD:
                    flatline_bad = True
            else:
                flatline_start = None
                flatline_vpds = []

        if valid_pairs < _QUIET_MIN_ROWS:
            logger.info(f"[ModD] 全天动态可信度连续样本不足（pairs={valid_pairs}），跳过。")
            return {"status": "skipped", "reason": "insufficient_pairs", "pairs": valid_pairs}

        bad = (
            jump_count >= _DYNAMIC_JUMP_COUNT_THRESH
            or max_jump >= _DYNAMIC_HARD_JUMP_THRESH
            or flatline_bad
        )
        status = "bad" if bad else "normal"
        logger.info(
            f"[ModD] 全天动态可信度: pairs={valid_pairs}, jumps={jump_count}, "
            f"max_jump={max_jump:.2f}, flatline_max={flatline_max_min:.0f}min, status={status}"
        )
        return {
            "status": status,
            "pairs": valid_pairs,
            "jump_count": jump_count,
            "max_jump": round(max_jump, 3),
            "max_jump_at": max_jump_at,
            "flatline_max_min": round(flatline_max_min, 1),
            "flatline_bad": flatline_bad,
        }

    def _apply_sensor_health_decision(
        self,
        static_diag: dict[str, Any],
        dynamic_diag: dict[str, Any],
        updates: dict[str, Any],
    ) -> None:
        health = self._sensor_health_state()
        static_status = static_diag.get("status")
        dynamic_status = dynamic_diag.get("status")

        if static_status == "high":
            health["static_bad_streak"] = int(health.get("static_bad_streak", 0)) + 1
            health["static_ok_streak"] = 0
        elif static_status == "normal":
            health["static_ok_streak"] = int(health.get("static_ok_streak", 0)) + 1
            health["static_bad_streak"] = 0

        if dynamic_status == "bad":
            health["dynamic_bad_streak"] = int(health.get("dynamic_bad_streak", 0)) + 1
            health["dynamic_ok_streak"] = 0
        elif dynamic_status == "normal":
            health["dynamic_ok_streak"] = int(health.get("dynamic_ok_streak", 0)) + 1
            health["dynamic_bad_streak"] = 0

        static_bad = int(health.get("static_bad_streak", 0))
        static_ok = int(health.get("static_ok_streak", 0))
        dynamic_bad = int(health.get("dynamic_bad_streak", 0))
        dynamic_ok = int(health.get("dynamic_ok_streak", 0))
        should_age = (
            static_bad >= _SENSOR_HEALTH_CONFIRM_DAYS
            or dynamic_bad >= _SENSOR_HEALTH_CONFIRM_DAYS
        )
        should_normal = (
            static_ok >= _SENSOR_HEALTH_CONFIRM_DAYS
            and dynamic_ok >= _SENSOR_HEALTH_CONFIRM_DAYS
        )

        health["last_static_noise"] = static_diag
        health["last_dynamic_integrity"] = dynamic_diag
        health["last_checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        health["confirm_days"] = _SENSOR_HEALTH_CONFIRM_DAYS

        if should_age:
            if self.cfg.TRAJ_TOLERANCE < _TRAJ_TOL_AGED:
                updates["TRAJ_TOLERANCE"] = _TRAJ_TOL_AGED
                logger.warning(
                    f"[ModD] 传感器可信度连续异常，放宽追踪容忍度: "
                    f"{self.cfg.TRAJ_TOLERANCE} → {_TRAJ_TOL_AGED}"
                )
            else:
                logger.info("[ModD] 传感器可信度连续异常，但容忍度已处于放宽状态。")
        elif should_normal:
            if self.cfg.TRAJ_TOLERANCE > _TRAJ_TOL_NORMAL:
                updates["TRAJ_TOLERANCE"] = _TRAJ_TOL_NORMAL
                logger.info(
                    f"[ModD] 静态噪声与全天动态连续正常，容忍度收回: "
                    f"{self.cfg.TRAJ_TOLERANCE} → {_TRAJ_TOL_NORMAL}"
                )
            else:
                logger.info("[ModD] 传感器可信度连续正常，容忍度保持正常。")
        else:
            logger.info(
                "[ModD] 传感器可信度证据未达连续确认门槛，"
                f"static_bad/ok={static_bad}/{static_ok}, dynamic_bad/ok={dynamic_bad}/{dynamic_ok}。"
            )

        self._store_sensor_health_state(health)

    def _evolve_hardware_health(self, df: list[dict]) -> dict[str, Any]:
        """
        模块 D：传感器方差自检放宽容忍度，水泵老化线性递增 GAMMA。

        传感器方差检测：
          · 提取凌晨 02:00~04:00 静默期数据（无光照、无剧烈蒸发）
          · 计算湿度方差，正常应接近 0（理想直线）
          · 方差 > 0.15 → 电极可能氧化 → 放宽 TRAJ_TOLERANCE 至 0.4
          · 方差恢复正常 → TRAJ_TOLERANCE 收回至 0.2

        水泵老化递增 GAMMA：
          · 从 system_state.json 读取 pump_total_cycles
          · 超过 2000 次后在 [2000, 5000] 范围内线性插值
          · GAMMA: 10 → 20，迫使 Layer 5 倾向低频次深浇策略
          · 接近寿命上限（90%）时触发预警
        """
        updates: dict[str, Any] = {}

        # ── D1：传感器可信度自检 ──────────────────────────────────────────
        static_diag = self._audit_static_noise(df)
        dynamic_diag = self._audit_dynamic_integrity(df)
        self._apply_sensor_health_decision(static_diag, dynamic_diag, updates)

        # ── D2：水泵老化递增 GAMMA ────────────────────────────────────────
        pump_cycles = self.state.get("pump_total_cycles", 0)
        logger.info(f"[ModD] 水泵累计启停: {pump_cycles} 次 "
                    f"（寿命警戒线: {_PUMP_WARN_COUNT}，设计寿命: {_PUMP_LIFE_COUNT}）")

        if pump_cycles >= _PUMP_WARN_COUNT:
            # [WARN, LIFE] 范围内线性插值
            ratio     = min(
                (pump_cycles - _PUMP_WARN_COUNT) / (_PUMP_LIFE_COUNT - _PUMP_WARN_COUNT),
                1.0
            )
            gamma_new = round(_GAMMA_BASE + ratio * (_GAMMA_MAX - _GAMMA_BASE), 2)
            old_gamma = self.cfg.GAMMA

            if gamma_new > old_gamma:
                updates["GAMMA"] = gamma_new
                logger.info(
                    f"[ModD] 水泵老化，GAMMA 上调: {old_gamma} → {gamma_new}  "
                    f"（ratio={ratio:.2f}，迫使 Layer 5 走向少次深浇）"
                )
            else:
                logger.info(f"[ModD] GAMMA 已达当前老化阈值 {gamma_new}，无需更新。")

            # 接近寿命极限时触发预警
            if pump_cycles >= _PUMP_LIFE_COUNT * _PUMP_ALERT_RATIO:
                _send_alert(
                    f"水泵寿命预警：累计启停 {pump_cycles} 次，"
                    f"已达设计寿命 {_PUMP_LIFE_COUNT} 次的 "
                    f"{pump_cycles/_PUMP_LIFE_COUNT*100:.0f}%。\n"
                    f"建议尽快安排更换，避免突发故障导致植物缺水。"
                )
        else:
            logger.info(
                f"[ModD] 水泵启停 {pump_cycles} < {_PUMP_WARN_COUNT}，"
                f"GAMMA 无需调整。"
            )

        return updates


# ===========================================================================
# 脚本入口
# ===========================================================================

if __name__ == "__main__":
    try:
        auditor = NightlyAuditor()
        auditor.run_full_audit()
    except Exception as e:
        logger.critical(f"[Auditor] 未捕获异常: {e}", exc_info=True)
        sys.exit(1)
