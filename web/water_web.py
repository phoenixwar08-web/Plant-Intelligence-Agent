"""
水系统 Web 面板 — 支持 soil1/2/3/test 四套系统切换
数据源优先级: GaussDB > CSV
"""
from flask import Flask, render_template, jsonify, request
import pandas as pd
import os, time, json, subprocess
from datetime import datetime, timedelta

# ── 四系统配置 ────────────────────────────────────────
DEVICES = {
    "soil1": {"name": "soil1", "csv": "/root/data/soil1.csv", "dir": "/root/water/phase3/soil1", "pump_topic": "esp32/pump1/cmd"},
    "soil2": {"name": "soil2", "csv": "/root/data/soil2.csv", "dir": "/root/water/phase3/soil2", "pump_topic": "esp32/pump2/cmd"},
    "soil3": {"name": "soil3", "csv": "/root/data/soil3.csv", "dir": "/root/water/phase3/soil3", "pump_topic": "esp32/pump3/cmd"},
    "soil_test": {"name": "soil_test", "csv": "/root/data/soil_test.csv", "dir": "/root/water/phase3/soil_test", "pump_topic": "esp32/pump_test/cmd"},
}

OBSERVE_FORCE_THRESHOLD = 24
OBSERVE_FORCE_FC_GAP = 5.0
HARD_SAFETY_GAP_RATIO_DEFAULT = 0.35
CHART_HISTORY_DAYS = 5
CHART_HISTORY_LIMIT = 3000
CHART_EXPECTED_INTERVAL_SEC = 180
CHART_GAP_THRESHOLD_SEC = 600

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
last_cmd_time = 0

def gsql(query: str) -> list[list[str]]:
    cmd = ["su", "-", "opengauss", "-c", f"gsql -d soil_data -p 7654 -t -A -F'|' -c {repr(query)}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return [l.strip().split("|") for l in r.stdout.split("\n") if l.strip()]
    except Exception:
        pass
    return []

def read_gauss_latest(device: str):
    """从 GaussDB 读取最新传感器数据"""
    rows = gsql(
        f"SELECT recv_time, humidity, temp, ec, air_humidity, lux "
        f"FROM soil_sensor_readings WHERE device_code='{device}' "
        f"ORDER BY recv_time DESC LIMIT 1"
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "humidity": float(r[1]) if r[1] else None,
        "temperature": float(r[2]) if r[2] else None,
        "ec": float(r[3]) if r[3] else None,
        "air_humidity": float(r[4]) if r[4] else None,
        "lux": float(r[5]) if r[5] else None,
        "time": r[0],
    }


def read_latest_irrigation_event(device: str):
    """Read the latest physical pump event, including external user/OpenClaw source."""
    rows = gsql(
        "SELECT command_time, water_sec, reason, source, status, operator_id, operator_name, request_id, command_payload "
        "FROM irrigation_events "
        f"WHERE device_code='{device}' AND water_sec IS NOT NULL AND water_sec > 0 "
        "ORDER BY command_time DESC LIMIT 1"
    )
    if not rows:
        return None
    r = rows[0]
    source = r[3] if len(r) > 3 else None
    reason = r[2] if len(r) > 2 else None
    operator_name = r[6] if len(r) > 6 else None
    is_user = source in ("openclaw_qqbot", "openclaw", "qqbot") or reason in (
        "user_approved_irrigation",
        "user_manual_watering",
    )
    return {
        "time": r[0],
        "water_sec": float(r[1]) if len(r) > 1 and r[1] else None,
        "reason": reason,
        "source": source,
        "status": r[4] if len(r) > 4 else None,
        "operator_id": r[5] if len(r) > 5 else None,
        "operator_name": operator_name,
        "request_id": r[7] if len(r) > 7 else None,
        "command_payload": r[8] if len(r) > 8 else None,
        "kind": "user_openclaw" if is_user else "autonomous_or_mqtt",
        "label": (
            f"用户通过机器人浇水 {float(r[1]):g}s"
            if is_user and len(r) > 1 and r[1]
            else f"系统浇水 {float(r[1]):g}s" if len(r) > 1 and r[1] else "浇水记录"
        ),
    }

def read_gauss_history(device: str, days: int = CHART_HISTORY_DAYS, limit: int = CHART_HISTORY_LIMIT) -> list[dict]:
    """Read a rolling wall-clock window; missing sensor spans stay as null gaps."""
    rows = gsql(
        f"SELECT recv_time, humidity, temp, ec, lux, air_humidity "
        f"FROM soil_sensor_readings WHERE device_code='{device}' "
        f"AND recv_time >= CURRENT_TIMESTAMP - INTERVAL '{int(days)} day' "
        f"ORDER BY recv_time DESC LIMIT {limit}"
    )
    result = []
    for r in reversed(rows):
        result.append({
            "time": r[0],
            "humi": float(r[1]) if r[1] else None,
            "temp": float(r[2]) if r[2] else None,
            "ec": float(r[3]) if r[3] else None,
            "lux": float(r[4]) if r[4] else None,
            "rh": float(r[5]) if len(r) > 5 and r[5] else None,
        })
    return insert_chart_gap_placeholders(result, window_end=datetime.now())

def insert_chart_gap_placeholders(rows: list[dict], window_end=None) -> list[dict]:
    """Keep sensor outages visible by inserting null-valued chart points."""
    if not rows:
        return rows
    if window_end is not None:
        try:
            last_t = datetime.strptime(str(rows[-1].get("time", ""))[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            last_t = None
        if last_t is not None and window_end - last_t > timedelta(seconds=CHART_EXPECTED_INTERVAL_SEC):
            rows = list(rows)
            rows.append({
                "time": window_end.strftime("%Y-%m-%d %H:%M:%S"),
                "humi": None,
                "temp": None,
                "ec": None,
                "lux": None,
                "rh": None,
                "gap": True,
            })
    if len(rows) < 2:
        return rows
    expanded = [rows[0]]
    expected = timedelta(seconds=CHART_EXPECTED_INTERVAL_SEC)
    threshold = timedelta(seconds=CHART_GAP_THRESHOLD_SEC)
    for prev, cur in zip(rows, rows[1:]):
        try:
            prev_t = datetime.strptime(str(prev.get("time", ""))[:19], "%Y-%m-%d %H:%M:%S")
            cur_t = datetime.strptime(str(cur.get("time", ""))[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            expanded.append(cur)
            continue
        gap = cur_t - prev_t
        if gap > threshold:
            t = prev_t + expected
            while t < cur_t:
                expanded.append({
                    "time": t.strftime("%Y-%m-%d %H:%M:%S"),
                    "humi": None,
                    "temp": None,
                    "ec": None,
                    "lux": None,
                    "rh": None,
                    "gap": True,
                })
                t += expected
        expanded.append(cur)
    return expanded

def read_csv_fallback(path: str) -> pd.DataFrame:
    """CSV 兜底读取"""
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        # 先读 raw 行，忽略字段数不一致的问题
        with open(path) as f:
            raw = [l.strip() for l in f.readlines() if l.strip()]
        if len(raw) < 2:
            return pd.DataFrame()
        header = raw[0].split(",")
        # 取最后 300 行有效数据
        data_rows = []
        for line in raw[-310:]:
            cols = line.split(",")
            if len(cols) == len(header):
                data_rows.append(cols)
            elif len(cols) > len(header):
                # 多出来的列截掉
                data_rows.append(cols[:len(header)])
        if not data_rows:
            return pd.DataFrame()
        df = pd.DataFrame(data_rows, columns=header)
        for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"]:
            try:
                df["timestamp"] = pd.to_datetime(df["接收时间"], format=fmt, errors="coerce")
                if df["timestamp"].notna().sum() > 0:
                    break
            except Exception:
                continue
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
        # 列名映射：CSV 的湿度在第四列
        col_map = {"湿度(%)": "humi", "温度(°C)": "temp", "电导率": "ec", "光照(lux)": "lux"}
        for c, v in col_map.items():
            if c not in df.columns:
                df[v] = None
            else:
                df[v] = pd.to_numeric(df[c], errors="coerce")
        return df.tail(limit)
    except Exception:
        return pd.DataFrame()

def read_json(path: str):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def build_anomaly_story(state: dict, trials: list[dict]) -> dict:
    """Build a display-only fault loop summary without changing control logic."""
    state = state if isinstance(state, dict) else {}
    trials = [t for t in trials if isinstance(t, dict)]

    def joined_text(t: dict) -> str:
        return "|".join([
            str(t.get("status") or "").lower(),
            str(t.get("reason") or "").lower(),
            str(t.get("plan_label") or "").lower(),
            str(t.get("sample_quality") or "").lower(),
        ])

    fault_tokens = (
        "water_delivery",
        "reservoir",
        "ineffective",
        "no_response",
        "no_positive",
        "low_wet_recovery_suspect",
        "pump",
        "esp",
    )
    fault_trials = []
    reservoir_trials = []
    no_gain_trials = []
    isolated_trials = []

    for t in trials:
        text = joined_text(t)
        status = str(t.get("status") or "").lower()
        if any(token in text for token in fault_tokens):
            fault_trials.append(t)
        if "reservoir" in text:
            reservoir_trials.append(t)
        try:
            water_sec = float(t.get("water_sec")) if t.get("water_sec") is not None else 0.0
        except (TypeError, ValueError):
            water_sec = 0.0
        try:
            delta_m = float(t.get("delta_m")) if t.get("delta_m") is not None else None
        except (TypeError, ValueError):
            delta_m = None
        if water_sec > 0 and not status.startswith("observation_") and delta_m is not None and delta_m <= 0.3:
            no_gain_trials.append(t)
        if (
            any(token in text for token in fault_tokens)
            or status in ("emergency", "reservoir_retest")
            or "manual" in text
            or "unknown_direct_mqtt" in text
        ):
            isolated_trials.append(t)

    suspects = []
    for key in ("water_delivery_suspect", "reservoir_empty_suspect", "low_wet_recovery_suspect"):
        item = state.get(key) or {}
        if not isinstance(item, dict):
            continue
        cleared = bool(item.get("cleared_at") or item.get("repair_confirmed_at") or item.get("repair_retest_completed_at"))
        if item.get("active") or cleared:
            suspects.append({
                "key": key,
                "active": bool(item.get("active")),
                "cleared": cleared,
                "reason": item.get("reason") or item.get("status") or "--",
            })

    latest_fault = fault_trials[-1] if fault_trials else (no_gain_trials[-1] if no_gain_trials else None)
    protected = any(s.get("active") for s in suspects)
    repaired = any(s.get("cleared") for s in suspects)

    if protected:
        headline = "水路/执行端异常保护中"
        conclusion = "系统已暂停把无效开泵结果写入正常学习，等待人工或复测确认。"
    elif repaired:
        headline = "异常已修复，垃圾数据已隔离"
        conclusion = "历史无效开泵、水箱复测或执行端异常只作为故障证据，不参与植物习性学习。"
    elif fault_trials or no_gain_trials:
        headline = "发现过无效增湿样本"
        conclusion = "系统已把低质量样本与正常策略反馈分开，避免学习被带偏。"
    else:
        headline = "暂无水路异常记录"
        conclusion = "当前没有检测到供水链路保护事件；若出现开泵无增湿，会进入异常闭环。"

    return {
        "headline": headline,
        "conclusion": conclusion,
        "protected": protected,
        "repaired": repaired,
        "fault_trial_count": len(fault_trials),
        "reservoir_retest_count": len(reservoir_trials),
        "no_gain_count": len(no_gain_trials),
        "isolated_count": len(isolated_trials),
        "suspects": suspects,
        "latest_fault": {
            "status": latest_fault.get("status") if latest_fault else None,
            "reason": latest_fault.get("reason") if latest_fault else None,
            "plan_label": latest_fault.get("plan_label") if latest_fault else None,
            "water_sec": latest_fault.get("water_sec") if latest_fault else None,
            "delta_m": latest_fault.get("delta_m") if latest_fault else None,
            "timestamp": latest_fault.get("timestamp") if latest_fault else None,
        } if latest_fault else None,
        "steps": [
            {"name": "开泵无效", "text": "连续开泵或复测后湿度没有有效上升。"},
            {"name": "进入保护", "text": "Phase3 标记水箱/水路/执行端风险，限制重复补水。"},
            {"name": "OpenClaw解释", "text": "读取异常标记和浇水记录，提示检查水箱、水管、泵控或 ESP。"},
            {"name": "人类修复", "text": "人工处理硬件链路后，通过后续增湿反馈确认恢复。"},
            {"name": "样本隔离", "text": "故障期 trial 不参与 K_P 和浇水策略学习。"},
        ],
    }

def derive_hard_safety_low(params: dict, state: dict) -> float:
    """按当前盆的 FC/TARGET_LOW/BUFFER_SCALE 推导硬安全低线。"""
    guard = state.get("hard_safety_low_guard") or {}
    if isinstance(guard, dict) and guard.get("effective_low") is not None:
        try:
            return float(guard.get("effective_low"))
        except (TypeError, ValueError):
            pass
    exp = state.get("irrigation_style_experiment") or {}
    raw = exp.get("hard_safety_low")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    fc = float(params.get("FC") or 0)
    target_low = float(params.get("TARGET_LOW") or 0)
    if fc <= target_low or target_low <= 0:
        return 0.0
    ratio = float(params.get("HARD_SAFETY_GAP_RATIO") or HARD_SAFETY_GAP_RATIO_DEFAULT)
    buffer_scale = max(float(params.get("BUFFER_SCALE") or 1.0), 0.1)
    return round(target_low - (fc - target_low) * ratio / buffer_scale, 3)

def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def strategy_display_name(label: str) -> str:
    names = {
        "style_pulse_3s": "3s 小脉冲",
        "style_pulse_6s": "6s 中脉冲",
        "style_pulse_9s": "9s 强脉冲",
        "style_micro_pulse": "小脉冲维持",
        "micro_pulse": "小脉冲维持",
        "medium_pulse": "6s 中脉冲",
        "strong_pulse": "9s 强脉冲",
        "style_wet_hold_refill": "湿区补水",
        "wet_hold_refill": "湿区补水",
        "style_drydown_probe": "干湿试探补水",
        "drydown_probe": "干湿试探补水",
        "style_drydown_observe": "干湿下降观察",
        "drydown_observe": "干湿下降观察",
        "low_wet_recovery": "低湿恢复",
        "drydown_recovery": "低湿恢复",
        "reservoir_retest_probe": "水路复测",
        "cooldown_observe": "冷却观察",
        "aggressive": "历史探索脉冲",
        "conservative": "保守补水",
        "reference": "参考补水",
        "observe_guard_probe": "观察保护试探",
        "emergency": "硬安全补水",
    }
    return names.get(label or "", label or "未知策略")

def strategy_group_label(label: str) -> str:
    """Group legacy plan labels into comparable strategy arms."""
    if label in {"style_pulse_3s", "style_micro_pulse", "micro_pulse"}:
        return "micro_pulse"
    if label in {"style_pulse_6s", "medium_pulse"}:
        return "medium_pulse"
    if label in {"style_pulse_9s", "strong_pulse"}:
        return "strong_pulse"
    if label in {"low_wet_recovery", "drydown_recovery"}:
        return "drydown_recovery"
    if label in {"style_drydown_observe", "drydown_observe"}:
        return "drydown_observe"
    if label in {"cooldown_observe", "style_wet_hold_observe", "style_observe", "observe"}:
        return "observe"
    return label or "unknown"

def score_strategy_arm(item: dict) -> tuple[float, dict, str]:
    """Score a strategy arm using only existing soil/water feedback evidence."""
    attempts = max(int(item.get("attempts") or 0), 0)
    accepted = max(int(item.get("accepted") or 0), 0)
    rejected = max(int(item.get("rejected") or 0), 0)
    emergency = max(int(item.get("emergency") or 0), 0)
    recent = max(int(item.get("recent") or 0), 0)
    success_rate = accepted / attempts if attempts else 0.0
    avg_delta = safe_float(item.get("avg_delta_m"), 0.0) or 0.0
    avg_error = safe_float(item.get("avg_peak_error"), 0.0) or 0.0
    avg_efficiency = safe_float(item.get("avg_efficiency"), 0.0) or 0.0
    overwet_count = max(int(item.get("overwet_count") or 0), 0)

    sample_score = min(attempts, 8) / 8.0 * 15.0
    success_score = success_rate * 30.0
    efficiency_score = max(min(avg_efficiency, 0.75), -0.25) / 0.75 * 20.0
    predict_score = max(0.0, 15.0 - min(abs(avg_error), 5.0) * 3.0)
    safety_penalty = rejected * 3.0 + emergency * 8.0 + overwet_count * 5.0
    recency_score = min(recent, 12) / 12.0 * 10.0
    score = sample_score + success_score + efficiency_score + predict_score + recency_score - safety_penalty
    score = max(0.0, min(100.0, score))

    if attempts < 3:
        verdict = "样本不足"
    elif emergency or overwet_count:
        verdict = "谨慎使用"
    elif score >= 70:
        verdict = "优先策略"
    elif score >= 50:
        verdict = "可继续试探"
    else:
        verdict = "证据偏弱"

    return round(score, 1), {
        "sample": round(sample_score, 1),
        "success": round(success_score, 1),
        "efficiency": round(efficiency_score, 1),
        "predictability": round(predict_score, 1),
        "recency": round(recency_score, 1),
        "safety_penalty": round(safety_penalty, 1),
    }, verdict


def summarize_learning_advice(advice: dict) -> dict:
    if not isinstance(advice, dict) or not advice:
        return {"active": False, "reason": "missing"}
    if not advice.get("advice_id") and not advice.get("source"):
        return {"active": False, "reason": "missing"}
    expires_at = safe_float(advice.get("expires_at"))
    if expires_at is None:
        return {"active": False, "reason": "missing_expiry"}
    active = bool(expires_at >= time.time())
    return {
        "active": active,
        "reason": "active" if active else "expired",
        "advice_id": advice.get("advice_id"),
        "audit_date": advice.get("audit_date"),
        "scope": advice.get("scope"),
        "sample_quality": advice.get("sample_quality") or {},
        "phase2": advice.get("phase2") or {},
        "exploration": advice.get("exploration") or {},
        "prefer": (advice.get("arms") or {}).get("prefer") or {},
        "avoid": (advice.get("arms") or {}).get("avoid") or {},
        "notes": (advice.get("arms") or {}).get("notes") or [],
        "control_boundary": advice.get("control_boundary") or {},
    }


def summarize_strategy_arms(profile: dict, trials: list[dict], style_exp: dict, params: dict = None) -> dict:
    """Build dashboard evidence for watering strategy arms."""
    params = params or {}
    arms = {}

    def ensure(label: str) -> dict:
        key = strategy_group_label(label)
        item = arms.setdefault(key, {
            "label": key,
            "display": strategy_display_name(key),
            "raw_labels": set(),
            "zones": set(),
            "attempts": 0,
            "accepted": 0,
            "rejected": 0,
            "emergency": 0,
            "avg_delta_m": None,
            "delta_samples": [],
            "efficiency_samples": [],
            "avg_efficiency": None,
            "last_water_sec": None,
            "last_reason": None,
            "last_updated": None,
            "recent": 0,
            "avg_peak_error": None,
            "peak_error_samples": [],
            "overwet_count": 0,
        })
        item["raw_labels"].add(label or "unknown")
        return item

    for zone_name, zone_data in (profile.get("zones") or {}).items():
        for label, stat in (zone_data.get("strategy_stats") or {}).items():
            item = ensure(label)
            item["zones"].add(zone_name)
            item["attempts"] += int(stat.get("attempts") or 0)
            item["accepted"] += int(stat.get("accepted") or 0)
            item["rejected"] += int(stat.get("rejected") or 0)
            item["emergency"] += int(stat.get("emergency") or 0)
            avg_delta = safe_float(stat.get("avg_delta_m"))
            if avg_delta is not None:
                item["delta_samples"].append(avg_delta)
            updated = safe_float(stat.get("last_updated"), 0)
            if updated and (not item["last_updated"] or updated > item["last_updated"]):
                item["last_updated"] = updated
                item["last_water_sec"] = stat.get("last_water_sec")
                item["last_reason"] = stat.get("last_reason")

    fc = safe_float(params.get("FC"), 0.0) or 0.0
    hold_ceiling = safe_float((style_exp.get("style_identification") or {}).get("hold_ceiling"))
    overwet_line = hold_ceiling or (fc - 1.0 if fc else None)

    for trial in trials[-120:]:
        label = trial.get("plan_label") or "unknown"
        item = ensure(label)
        item["recent"] += 1
        err = safe_float(trial.get("peak_error_observed"))
        if err is not None:
            item["peak_error_samples"].append(err)
        delta = safe_float(trial.get("delta_m"))
        water_sec = safe_float(trial.get("water_sec"))
        if delta is not None:
            item["delta_samples"].append(delta)
        if delta is not None and water_sec and water_sec > 0:
            item["efficiency_samples"].append(delta / water_sec)
        actual_peak = safe_float(trial.get("actual_peak_observed"))
        predicted_peak = safe_float(trial.get("predicted_peak"))
        peak = actual_peak if actual_peak is not None else predicted_peak
        if overwet_line is not None and peak is not None and peak > overwet_line:
            item["overwet_count"] += 1

    current_arm = style_exp.get("current_arm")
    if current_arm:
        ensure(current_arm)

    rows = []
    for item in arms.values():
        if item["delta_samples"]:
            item["avg_delta_m"] = sum(item["delta_samples"]) / len(item["delta_samples"])
        if item["efficiency_samples"]:
            item["avg_efficiency"] = sum(item["efficiency_samples"]) / len(item["efficiency_samples"])
        if item["peak_error_samples"]:
            item["avg_peak_error"] = sum(item["peak_error_samples"]) / len(item["peak_error_samples"])
        attempts = max(int(item["attempts"] or 0), 0)
        accepted = max(int(item["accepted"] or 0), 0)
        success_rate = (accepted / attempts) if attempts else None
        avg_delta = safe_float(item["avg_delta_m"], 0.0) or 0.0
        avg_error = safe_float(item["avg_peak_error"])
        score, breakdown, verdict = score_strategy_arm(item)
        rows.append({
            "label": item["label"],
            "display": item["display"],
            "raw_labels": sorted(item["raw_labels"]),
            "zones": sorted(item["zones"]),
            "attempts": attempts,
            "accepted": accepted,
            "rejected": int(item["rejected"] or 0),
            "emergency": int(item["emergency"] or 0),
            "success_rate": round(success_rate * 100, 1) if success_rate is not None else None,
            "avg_delta_m": round(avg_delta, 3) if item["avg_delta_m"] is not None else None,
            "avg_efficiency": round(item["avg_efficiency"], 3) if item["avg_efficiency"] is not None else None,
            "avg_peak_error": round(avg_error, 3) if avg_error is not None else None,
            "overwet_count": int(item["overwet_count"] or 0),
            "last_water_sec": item["last_water_sec"],
            "last_reason": item["last_reason"],
            "last_updated": item["last_updated"],
            "recent": int(item["recent"] or 0),
            "score": round(score, 2),
            "score_breakdown": breakdown,
            "verdict": verdict,
        })

    rows.sort(key=lambda r: (-r["score"], -r["attempts"]))
    eligible = [r for r in rows if r["attempts"] >= 3]
    best = eligible[0] if eligible else (rows[0] if rows else None)
    if best is None:
        recommendation = {
            "label": None,
            "display": "等待样本",
            "score": None,
            "verdict": "样本不足",
            "reason": "还没有足够的浇水策略样本，系统继续记录反馈。",
        }
    elif best["attempts"] < 3:
        recommendation = {
            "label": best["label"],
            "display": best["display"],
            "score": best["score"],
            "verdict": "样本不足",
            "reason": f"{best['display']} 暂时分数最高，但样本数只有 {best['attempts']}，不能定为偏好。",
        }
    else:
        parts = []
        if best["success_rate"] is not None:
            parts.append(f"成功率 {best['success_rate']}%")
        if best["avg_efficiency"] is not None:
            parts.append(f"效率 {best['avg_efficiency']}%/s")
        if best["avg_peak_error"] is not None:
            parts.append(f"预测误差 {best['avg_peak_error']}%")
        if best["overwet_count"]:
            parts.append(f"过湿 {best['overwet_count']} 次")
        recommendation = {
            "label": best["label"],
            "display": best["display"],
            "score": best["score"],
            "verdict": best["verdict"],
            "reason": "，".join(parts) if parts else "基于现有土壤响应样本评分最高。",
        }

    current_group = strategy_group_label(current_arm)
    current_rows_first = sorted(rows[:8], key=lambda r: (r["label"] != current_group, -r["score"]))
    return {
        "current_arm": current_arm,
        "current_arm_display": strategy_display_name(current_arm),
        "current_arm_original_plan": style_exp.get("current_arm_original_plan"),
        "current_arm_water_sec": style_exp.get("current_arm_water_sec"),
        "current_arm_reason": style_exp.get("current_arm_reason"),
        "mode": style_exp.get("mode"),
        "drydown_target": style_exp.get("drydown_target"),
        "hard_safety_low": style_exp.get("hard_safety_low"),
        "nightly_learning_advice": style_exp.get("nightly_learning_advice") or {},
        "style_identification": style_exp.get("style_identification") or {},
        "watering_window_guard": style_exp.get("watering_window_guard") or {},
        "recommendation": recommendation,
        "arms": current_rows_first,
    }

def build_status(device: str):
    cfg = DEVICES.get(device)
    if not cfg:
        return {"error": f"unknown device: {device}"}

    # 优先 GaussDB
    latest = read_gauss_latest(device)
    data_source = "gaussdb"

    # GaussDB 无数据时回退到 CSV
    if not latest or latest.get("humidity") is None:
        df = read_csv_fallback(cfg["csv"])
        data_source = "csv"
        if not df.empty:
            row = df.iloc[-1]
            latest = {
                "humidity": float(row.get("humi", 0)) if row.get("humi") else None,
                "temperature": float(row.get("temp", 0)) if row.get("temp") else None,
                "ec": float(row.get("ec", 0)) if row.get("ec") else None,
                "lux": float(row.get("lux", 0)) if row.get("lux") else None,
                "air_humidity": None,
                "time": row["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if pd.notna(row.get("timestamp")) else None,
            }

    state = read_json(os.path.join(cfg["dir"], "system_state.json"))
    params = read_json(os.path.join(cfg["dir"], "evolving_params.json"))

    freshness = {"ok": False, "age_min": None, "text": "无数据"}
    if latest and latest.get("time"):
        try:
            t = datetime.strptime(latest["time"][:19], "%Y-%m-%d %H:%M:%S")
            age = (datetime.now() - t).total_seconds() / 60
            freshness = {
                "ok": age <= 20,
                "age_min": round(age, 1),
                "text": f"来自{data_source} · 最新 {age:.0f} 分钟前" if age <= 30 else f"数据已 {age:.0f} 分钟未更新",
            }
        except ValueError:
            pass

    fc = float(params.get("FC") or 0)
    tl = float(params.get("TARGET_LOW") or 0)
    safe = float(params.get("M_SAFE_SLEEP") or fc)
    hard_low = derive_hard_safety_low(params, state)
    humi = latest.get("humidity") if latest else None
    observe_streak = int(state.get("battle_observe_streak") or 0)
    pending = state.get("pending_soak")
    wds = state.get("water_delivery_suspect") or {}

    if pending:
        mode = "渗透等待"
        action = f"已浇 {pending.get('water_sec')}s，等待渗透中"
    elif wds.get("active"):
        mode = "水路异常保护"
        action = "连续补水无效，已暂停自动灌溉"
    elif humi is None:
        mode = "无数据"
        action = "等待传感器"
    elif hard_low and humi is not None and humi < hard_low:
        mode = "硬安全补水区"
        action = f"低于硬安全线({hard_low}%)，Phase3 优先保命，并隔离异常样本"
    elif tl and humi is not None and humi < tl:
        mode = "低湿目标区"
        action = f"低于低湿目标线({tl}%)，进入恢复/观察决策，不等同硬危险"
    elif safe and humi is not None and humi >= safe:
        mode = "🟢 安全区"
        action = "湿度充足，观察不浇水"
    else:
        mode = "⚔️ 战区"
        fc_gap = fc - humi if fc and humi else None
        if observe_streak >= OBSERVE_FORCE_THRESHOLD and fc_gap and fc_gap >= OBSERVE_FORCE_FC_GAP:
            action = "连续观察过阈值，即将执行安全探针"
        else:
            action = "比较候选策略中"

    history = read_gauss_history(device)
    chart = {
        "time": [h["time"][5:16] if h["time"] else "" for h in history],
        "humi": [h["humi"] for h in history],
        "temp": [h["temp"] for h in history],
    }

    return {
        "device": device,
        "name": cfg["name"],
        "data_source": data_source,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest": latest or {},
        "freshness": freshness,
        "mode": mode,
        "action_text": action,
        "params": {
            "FC": fc,
            "TARGET_LOW": tl,
            "HARD_SAFETY_LOW": hard_low,
            "HARD_SAFETY_GAP_RATIO": float(params.get("HARD_SAFETY_GAP_RATIO") or HARD_SAFETY_GAP_RATIO_DEFAULT),
            "BUFFER_SCALE": float(params.get("BUFFER_SCALE") or 1.0),
            "M_SAFE_SLEEP": safe,
            "K_P": params.get("K_P"),
        },
        "state": {
            "observe_streak": observe_streak,
            "pending_soak": pending,
            "water_delivery_suspect": wds,
            "pump_total_cycles": state.get("pump_total_cycles"),
            "total_water_sec_dispensed": state.get("total_water_sec_dispensed"),
        },
        "chart": chart,
    }


# ── 辅助 ──────────────────────────────────────────────

def read_json_list(path: str):
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def read_phase1_data(device: str) -> dict:
    path_map = {
        "soil1": "/root/water/phase1_test/phase1_data_soil1.json",
        "soil2": "/root/water/phase1_test/phase1_data_soil2.json",
        "soil3": "/root/water/phase1_test/phase1_data_soil3.json",
    }
    path = path_map.get(device, "")
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

# ── 路由 ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("water.html")

@app.route("/api/status")
def api_status():
    device = request.args.get("device", "soil3")
    return jsonify({"ok": True, "data": build_status(device)})

@app.route("/api/dashboard")
def api_dashboard():
    """一站式仪表盘数据，包含传感器、参数、图表、代价法庭"""
    device = request.args.get("device", "soil3")
    cfg = DEVICES.get(device)
    if not cfg:
        return jsonify({"ok": False, "msg": f"unknown device: {device}"})

    latest = read_gauss_latest(device)
    state = read_json(os.path.join(cfg["dir"], "system_state.json"))
    params = read_json(os.path.join(cfg["dir"], "evolving_params.json"))
    phase1 = read_phase1_data(device)
    profile = read_json(os.path.join(cfg["dir"], "irrigation_profile.json"))
    trials = read_json_list(os.path.join(cfg["dir"], "irrigation_trials.json"))
    sensor_log_path = os.path.join(cfg["dir"], "sensor_log.csv")

    # 传感器数据
    humi = latest.get("humidity") if latest else None
    temp = latest.get("temperature") if latest else None
    ec_raw = latest.get("ec") if latest else None
    lux_val = latest.get("lux") if latest else None
    air_humi = latest.get("air_humidity") if latest else None

    # 参数
    fc = float(params.get("FC") or 0)
    tl = float(params.get("TARGET_LOW") or 0)
    kp = float(params.get("K_P") or 0)
    safe_line = float(params.get("M_SAFE_SLEEP") or fc)
    hard_low = derive_hard_safety_low(params, state)
    resp_limit = float(params.get("RESPIRATION_LIMIT") or fc)
    wake_line = float(params.get("M_WAKE_UP") or tl)
    kp_low = params.get("K_P_LOW")
    kp_mid = params.get("K_P_MID")
    kp_high = params.get("K_P_HIGH")
    ec_base = params.get("EC_BASE")
    ec_norm_25c = phase1.get("learned_ec_norm")
    phase1_kp = phase1.get("irrigation_gain_kp")
    phase1_fc = phase1.get("learned_FC")
    phase1_tl = phase1.get("learned_target_low")

    # 系统状态
    pending = state.get("pending_soak")
    wds = state.get("water_delivery_suspect") or {}
    observe_streak = int(state.get("battle_observe_streak") or 0)
    pump_cycles = state.get("pump_total_cycles", 0)
    total_water = state.get("total_water_sec_dispensed", 0)
    predictor_circuit = state.get("predictor_circuit", {})
    is_circuit_open = predictor_circuit.get("state") == "OPEN"
    last_irrigation_ts = state.get("last_normal_irrigation_timestamp")
    style_exp = state.get("irrigation_style_experiment") or {}
    nightly_advice = summarize_learning_advice(state.get("nightly_learning_advice") or {})

    # 数据新鲜度
    freshness_ok, age_min = False, None
    if latest and latest.get("time"):
        try:
            t = datetime.strptime(latest["time"][:19], "%Y-%m-%d %H:%M:%S")
            age_min = round((datetime.now() - t).total_seconds() / 60, 1)
            freshness_ok = age_min <= 20
        except ValueError:
            pass

    # 态势判断
    if pending:
        zone = "SOAK_PENDING"
        zone_label = "渗透等待"
    elif wds.get("active"):
        zone = "WATER_DELIVERY_SUSPECT"
        zone_label = "水路异常保护"
    elif humi is None:
        zone = "NO_DATA"
        zone_label = "无数据"
    elif hard_low and humi is not None and humi < hard_low:
        zone = "EMERGENCY"
        zone_label = "硬安全补水"
    elif tl and humi is not None and humi < tl:
        zone = "LOW_TARGET"
        zone_label = "低湿目标恢复"
    elif safe_line and humi is not None and humi >= safe_line:
        zone = "SAFE_SLEEP"
        zone_label = "安全休眠"
    else:
        zone = "BATTLE_ZONE"
        zone_label = "战区推理"

    # 最近一次 completed trial
    latest_trial = None
    for t in reversed(trials):
        if t.get("status") in ("accepted", "rejected", "emergency"):
            latest_trial = t
            break
    trial_summary = {
        "total": len(trials),
        "accepted": sum(1 for t in trials if t.get("status") == "accepted"),
        "rejected": sum(1 for t in trials if t.get("status") == "rejected"),
        "emergency": sum(1 for t in trials if "emergency" in str(t.get("status") or "").lower()),
    }
    style_summary = summarize_strategy_arms(profile, trials, style_exp, params)

    # 最近传感器日志中的斜率（从 sensor_log.csv 最后几行推算）
    recent_slope = None
    try:
        with open(sensor_log_path) as f:
            lines = f.readlines()
        if len(lines) >= 3:
            vals = []
            for line in lines[-6:]:
                parts = line.strip().split(",")
                if len(parts) >= 2:
                    try:
                        vals.append(float(parts[1]))
                    except ValueError:
                        pass
            if len(vals) >= 2:
                recent_slope = round((vals[-1] - vals[0]) / (len(vals) - 1), 4)
    except Exception:
        pass

    # 图表数据
    history = read_gauss_history(device)
    latest_irrigation_event = read_latest_irrigation_event(device)

    return jsonify({
        "ok": True,
        "device": device,
        "name": cfg["name"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sensors": {
            "humidity": humi,
            "temperature": temp,
            "ec_raw": ec_raw,
            "lux": lux_val,
            "air_humidity": air_humi,
            "freshness_ok": freshness_ok,
            "age_min": age_min,
        },
        "params": {
            "FC": fc,
            "TARGET_LOW": tl,
            "HARD_SAFETY_LOW": hard_low,
            "HARD_SAFETY_GAP_RATIO": float(params.get("HARD_SAFETY_GAP_RATIO") or HARD_SAFETY_GAP_RATIO_DEFAULT),
            "BUFFER_SCALE": float(params.get("BUFFER_SCALE") or 1.0),
            "K_P": kp,
            "M_SAFE_SLEEP": safe_line,
            "M_WAKE_UP": wake_line,
            "RESPIRATION_LIMIT": resp_limit,
            "K_P_LOW": kp_low,
            "K_P_MID": kp_mid,
            "K_P_HIGH": kp_high,
            "EC_BASE": ec_base,
            "EC_NORM_25C": ec_norm_25c,
        },
        "phase1": {
            "learned_FC": phase1_fc,
            "learned_target_low": phase1_tl,
            "irrigation_gain_kp": phase1_kp,
            "learned_ec_norm": ec_norm_25c,
            "handover_complete": phase1.get("handover_complete", False),
        },
        "zone": {
            "code": zone,
            "label": zone_label,
            "observe_streak": observe_streak,
            "pending_soak": {
                "active": bool(pending),
                "water_sec": pending.get("water_sec") if pending else None,
                "remaining_sec": pending.get("remaining_sec") if pending else None,
            } if pending else None,
            "water_delivery_suspect": wds.get("active", False),
        },
        "stats": {
            "pump_total_cycles": pump_cycles,
            "total_water_sec": total_water,
            "predictor_circuit_open": is_circuit_open,
            "last_irrigation_ts": last_irrigation_ts,
        },
        "latest_trial": {
            "status": latest_trial.get("status") if latest_trial else None,
            "water_sec": latest_trial.get("water_sec") if latest_trial else None,
            "delta_m": latest_trial.get("delta_m") if latest_trial else None,
            "zone": latest_trial.get("zone") if latest_trial else None,
            "plan_label": latest_trial.get("plan_label") if latest_trial else None,
            "predicted_peak": latest_trial.get("predicted_peak") if latest_trial else None,
            "actual_peak_observed": latest_trial.get("actual_peak_observed") if latest_trial else None,
            "peak_error_observed": latest_trial.get("peak_error_observed") if latest_trial else None,
            "timestamp": latest_trial.get("timestamp") if latest_trial else None,
        } if latest_trial else None,
        "latest_irrigation_event": latest_irrigation_event,
        "trial_summary": trial_summary,
        "anomaly_story": build_anomaly_story(state, trials),
        "style_experiment": style_summary,
        "nightly_learning_advice": nightly_advice,
        "diag": {
            "recent_slope": recent_slope,
            "slope_steep": params.get("SLOPE_STEEP"),
            "observe_streak": observe_streak,
            "observe_threshold": OBSERVE_FORCE_THRESHOLD,
            "kp_net_enabled": params.get("KP_NET_GAIN_ENABLED", False),
        },
        "chart": {
            "time": [h["time"][5:16] if h["time"] else "" for h in history],
            "humi": [h["humi"] for h in history],
            "temp": [h["temp"] for h in history],
            "ec": [h["ec"] for h in history],
            "lux": [h["lux"] for h in history],
            "rh": [h["rh"] for h in history],
        },
        "profile": {
            "zones": profile.get("zones", {}),
        },
    })

@app.route("/api/devices")
def api_devices():
    return jsonify({"ok": True, "devices": {k: {"name": v["name"]} for k, v in DEVICES.items()}})

@app.route("/pump_control", methods=["POST"])
def pump_control():
    return jsonify({
        "ok": False,
        "msg": "Web 直控泵已禁用。比赛演示只展示 Phase3 状态，人工浇水需走留痕网关。",
    }), 403

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
