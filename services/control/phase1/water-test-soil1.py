import time
import json
import os
import subprocess
from datetime import datetime
import paho.mqtt.client as mqtt

# ==========================================================
# 1. 物理与安全参数 (四大支柱：纯正零先验配置)
# ==========================================================
HARDWARE_FAULT_LOW = 5.0      # 物理故障底线：跌破此值判定为传感器脱落
HARDWARE_FLOOD_LIMIT = 85.0   # 绝对防涝极限

WATCHDOG_TIMEOUT_HRS = 2      # CSV 数据停更报警阈值
POLL_INTERVAL_SEC = 30        # 轮询 CSV 文件的频率

MAX_LEARNING_CYCLES = 3       # 完整学习周期数

# --- 零先验引擎动态参数 (探寻 FC 天花板) ---
INIT_PROBE_SEC = 5            
PROBE_STEP = 3                
VALID_JUMP_THRESHOLD = 1.5    
SATURATION_JUMP_THRESHOLD = 0.8 
MAX_PROBE_SEC = 20
NO_RESPONSE_RETRY_LIMIT = 3
NO_RESPONSE_RETRY_COOLDOWN_SEC = 6 * 3600
SENSOR_LOW_RETRY_COOLDOWN_SEC = 5 * 60
# [动态刹车卡钳] 漏水停止门槛：适用于快排基质
DRAINAGE_BRAKE_THRESHOLD = -0.02  

# --- [双层漏斗探测参数] 寻底引擎 ---
ACCELERATION_THRESHOLD = 0.001        # 加速度归零阈值
MIN_VALID_DROP_SLOPE = -0.005         # 必须经历过的最陡掉水速度
DAY_WINDOW = (10, 16)                 # 白天观测窗口
NIGHT_WINDOW = (20, 6)                # 夜晚观测窗口
DIURNAL_CONVERGENCE_THRESHOLD = 0.002 # 昼夜斜率趋同阈值
DRYING_MIN_DROP = 4.0                 # 最小有效下降幅度

# --- [新增] 信号处理与安全互锁参数 ---
SMA_WINDOW_SIZE = 3               # 滑动平均窗口大小
MASSIVE_DROP_THRESHOLD = 1.5      # 巨幅跳水阈值(%)：1小时跌幅超此值判定为异常
INTERLOCK_COOLDOWN_SEC = 2 * 3600 # 互锁冻结时间：2小时

# --- 环境保护与蓬松土壤参数 ---
TEMP_HOT_LIMIT = 32.0         
TEMP_COLD_LIMIT = 8.0         
SUMMER_NOON_WINDOW = (11, 16) 
WINTER_NIGHT_WINDOW = (18, 8) 
MAX_PULSES_PER_CYCLE = 8      
DIMINISHING_RATIO = 0.4       

# 路径设置
DATA_PATH = "openGauss:soil_data.soil_sensor_readings[soil1]"
STATE_FILE = "/root/water/phase1_test/phase1_data_soil1.json"
MQTT_BROKER = "localhost"               
TOPIC_WATER_CMD = "esp32/pump1/cmd"     

# ==========================================================
# 2. 状态管理与辅助工具
# ==========================================================
def load_state():
    default_state = {
        "phase": "UNINITIALIZED",
        "probing_seconds": INIT_PROBE_SEC, 
        "last_water_ts": 0,
        "history_buffer": [],
        "learned_FC": None,
        "learned_target_low": None,
        
        # ================= 新增：系统辨识参数 =================
        "irrigation_gain_kp": None,      # 当前生效的增益 Kp
        "kp_history": [],                # 历史有效增益池
        "moisture_before_probe": None,   # 探测前湿度
        "peak_moisture_after_probe": None, # 探测后峰值湿度
        # ======================================================

        "pulse_count": 0,        
        "last_delta_x": 0.0,
        "completed_cycles": 0,      
        "fc_history": [],            
        "target_low_history": [],   
        "drying_buffer": [],        
        "fastest_drying_slope": 0.0,
        "previous_slope": 0.0,      
        "last_night_slope": None,   
        "handover_complete": False,

        # ===== 修改: 采用 FC 锚定法记录 EC 快照 =====
        "ec_fc_history": [],
        "learned_ec_norm": None,

        "low_candidate_buffer": [],

        # === 以下为需要插入的新增字段 ===
        "phase1_start_ts": time.time(),     # 记录第一阶段启动时间
        "phase1_duration_hrs": 0.0,         # 统计总耗时
        "raw_sma_buffer": [],               # SMA滤波缓冲池
        "interlock_until_ts": 0.0,          # 互斥锁解冻时间戳
        "probe_no_response_count": 0,
        "hardware_fault": None,
        # ==============================
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[系统异常] 状态文件已损坏，正在重置: {e}")
    else:
        print(f"[系统初始化] 未找到 {STATE_FILE}，建立全新植物档案...")
    return default_state

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[系统异常] 无法保存状态到磁盘: {e}")

def calculate_derivative(history):
    if len(history) < 2: return 0.0
    dt_mins = (history[-1]["ts"] - history[0]["ts"]) / 60.0
    dx = history[-1]["val"] - history[0]["val"]
    return dx / dt_mins if dt_mins > 0 else 0.0

# [新增] 滑动平均滤波器 (削峰填谷)
def apply_sma_filter(state, raw_moisture):
    sma_buffer = state.get("raw_sma_buffer", [])
    sma_buffer.append(raw_moisture)
    if len(sma_buffer) > SMA_WINDOW_SIZE:
        sma_buffer.pop(0)
    state["raw_sma_buffer"] = sma_buffer
    return sum(sma_buffer) / len(sma_buffer)

# [新增] 最小二乘法线性回归求导 (取代脆弱的两点求导)
def calculate_derivative_lr(history):
    n = len(history)
    if n < 2: return 0.0
    
    t0 = history[0]["ts"]
    sum_x = 0.0; sum_y = 0.0; sum_xy = 0.0; sum_xx = 0.0
    
    for p in history:
        x = (p["ts"] - t0) / 60.0  # 转为相对分钟数
        y = p["val"]
        sum_x += x; sum_y += y
        sum_xy += x * y; sum_xx += x * x
        
    denominator = (n * sum_xx) - (sum_x * sum_x)
    if denominator == 0: return 0.0
    return ((n * sum_xy) - (sum_x * sum_y)) / denominator # 单位: %/min

def is_environment_safe(current_temp):
    curr_hour = time.localtime().tm_hour
    if current_temp > TEMP_HOT_LIMIT and SUMMER_NOON_WINDOW[0] <= curr_hour <= SUMMER_NOON_WINDOW[1]:
        print(f"[环境护栏] 当前 {current_temp}°C 且为夏日午间，为防热激，动作挂起。")
        return False
    if current_temp < TEMP_COLD_LIMIT and (curr_hour >= WINTER_NIGHT_WINDOW[0] or curr_hour <= WINTER_NIGHT_WINDOW[1]):
        print(f"[环境护栏] 当前 {current_temp}°C 且为冬日寒夜，为防冷害，动作挂起。")
        return False
    return True

# ==========================================================
# 新增：灌溉增益 (Kp) 提取与更新逻辑 (已修复两步走逻辑)
# ==========================================================
def calculate_and_update_kp(state, current_moisture):
    m_before = state.get("moisture_before_probe")
    m_peak = current_moisture
    seconds = state.get("probing_seconds")
    # 注意：这里不再设置默认值，直接读取真实状态
    fc = state.get("learned_FC") 

    # 基础防线：参数不全时不计算
    if m_before is None or seconds is None or seconds <= 0:
        return

    # 【安全防线 1（两步走策略）】：只有在已经学到了 FC 的情况下，才执行高水位拦截
    if fc is not None:
        if m_before > (fc - 2.0):
            print(f"  -> [Kp 忽略] 浇水前湿度已接近饱和 ({m_before:.1f}% > FC-2.0)，本次增益计算易失真，丢弃。")
            return
    else:
        print(f"  -> [Kp 宽容模式] 尚未定标持水量 FC，跳过饱和防线，记录原始增益。")

    # 【安全防线 2】：如果没有产生明显的上升，说明水流走了或传感器异常
    if (m_peak - m_before) < 0.5:
        print(f"  -> [Kp 忽略] 湿度上升不明显 ({m_peak - m_before:.1f}%)，忽略本次增益。")
        return

    # 核心计算：本次的实际增益
    current_kp = (m_peak - m_before) / seconds
    print(f"  => [物理辨识] 本次试探浇水 {seconds}s，湿度 {m_before:.1f}->{m_peak:.1f}%，测得增益 Kp = {current_kp:.3f} %/s")

    # 存入历史池，最多保留最近 5 次的经验
    history = state.get("kp_history", [])
    history.append(current_kp)
    if len(history) > 5:
        history.pop(0)

    # 滑动平均，对抗非线性干扰
    avg_kp = sum(history) / len(history)

    # 写入 JSON 状态
    state["kp_history"] = history
    state["irrigation_gain_kp"] = round(avg_kp, 3)
    state["peak_moisture_after_probe"] = m_peak

def enter_recoverable_hardware_pause(state, reason, current_moisture, current_ts):
    no_response_count = int(state.get("probe_no_response_count") or 0)
    retry_after = current_ts + NO_RESPONSE_RETRY_COOLDOWN_SEC
    state["phase"] = "HARDWARE_ERROR"
    state["probing_seconds"] = INIT_PROBE_SEC
    state["pulse_count"] = 0
    state["hardware_fault"] = {
        "kind": "water_delivery_no_response",
        "reason": reason,
        "recoverable": True,
        "retry_after_ts": retry_after,
        "no_response_count": no_response_count,
        "last_moisture": current_moisture,
        "last_seen_ts": current_ts,
        "next_probe_sec": INIT_PROBE_SEC,
        "cooldown_sec": NO_RESPONSE_RETRY_COOLDOWN_SEC,
    }
    print(
        "  !!! [水路保护] 连续试探未带来有效湿度上升，暂停自动加大水量。"
        f" reason={reason}，{NO_RESPONSE_RETRY_COOLDOWN_SEC/3600:.1f} 小时后只允许 "
        f"{INIT_PROBE_SEC}s 小脉冲复测。"
    )

def recover_hardware_error_if_allowed(state, current_moisture, current_ts):
    fault = state.get("hardware_fault") or {}
    if current_moisture <= HARDWARE_FAULT_LOW:
        return False

    if not fault:
        fault = {
            "kind": "legacy_hardware_error",
            "recoverable": True,
            "retry_after_ts": current_ts,
            "reason": "legacy_state_without_fault_detail",
        }
        state["hardware_fault"] = fault

    if fault.get("kind") == "sensor_low" and not fault.get("recoverable", False):
        fault["recoverable"] = True
        fault["retry_after_ts"] = current_ts
        fault["reason"] = fault.get("reason") or "sensor_low_recovered"
        state["hardware_fault"] = fault

    if not fault.get("recoverable", False):
        return False

    retry_after = float(fault.get("retry_after_ts") or 0.0)
    if current_ts < retry_after:
        wait_min = (retry_after - current_ts) / 60.0
        print(f"  -> [水路保护] HARDWARE_ERROR 可恢复暂停中，还需 {wait_min:.1f} 分钟再复测。")
        save_state(state)
        return False

    print("  -> [水路复测] 可恢复硬件暂停到期，重置为小脉冲重新验证水路。")
    state["phase"] = "UNINITIALIZED"
    state["probing_seconds"] = INIT_PROBE_SEC
    state["pulse_count"] = 0
    state["last_delta_x"] = 0.0
    state["moisture_before_probe"] = None
    state["peak_moisture_after_probe"] = None
    state["hardware_fault"] = {
        **fault,
        "recoverable": True,
        "retest_started_ts": current_ts,
        "next_probe_sec": INIT_PROBE_SEC,
    }
    save_state(state)
    return True

# ==========================================================
# 3. 核心执行件
# ==========================================================
def send_mqtt_cmd(client, seconds):
    print(f"  >>> [物理执行] 准备下发灌溉指令，时长: {seconds}s")
    try:
        client.publish(TOPIC_WATER_CMD, "on", qos=1)
        time.sleep(seconds) 
        client.publish(TOPIC_WATER_CMD, "off", qos=1)
        print(f"  >>> [物理执行] {seconds}s 灌溉顺利结束，等待水分渗透。")
    except Exception as e:
        print(f"  >>> [MQTT 错误] 无法连接到水泵: {e}")

# ==========================================================
# 4. 核心自适应逻辑
# ==========================================================
def run_control_cycle(client, current_moisture, current_temp, current_ec):
    state = load_state()
    current_ts = time.time()

    if state.get("handover_complete", False): return
        
    if current_moisture <= HARDWARE_FAULT_LOW:
        print(
            f"!!! [传感器保护] 湿度跌破 {HARDWARE_FAULT_LOW}%，"
            "判定为无效低读数；暂停自动浇水，等待有效读数恢复。"
        )
        state["phase"] = "HARDWARE_ERROR"
        state["probing_seconds"] = INIT_PROBE_SEC
        state["pulse_count"] = 0
        state["hardware_fault"] = {
            "kind": "sensor_low",
            "reason": "humidity_below_physical_fault_low",
            "recoverable": True,
            "retry_after_ts": current_ts + SENSOR_LOW_RETRY_COOLDOWN_SEC,
            "last_moisture": current_moisture,
            "last_seen_ts": current_ts,
            "cooldown_sec": SENSOR_LOW_RETRY_COOLDOWN_SEC,
        }
        save_state(state)
        return
        
    if state.get("phase") == "HARDWARE_ERROR":
        if not recover_hardware_error_if_allowed(state, current_moisture, current_ts):
            return

    safety_flag = is_environment_safe(current_temp)
    curr_hour = time.localtime().tm_hour

    dynamic_ceiling = (state["learned_FC"] + 5.0) if state.get("learned_FC") else HARDWARE_FLOOD_LIMIT

    # --- 阶段 A1: 寻找有效脉冲 ---
    if state["phase"] == "UNINITIALIZED":
        if current_moisture >= dynamic_ceiling: 
            print(f"  -> [动态保护] 当前水分已达上限({dynamic_ceiling:.1f}%)，取消本次试探。")
            return
        if not safety_flag: return 

        print(f"  -> [决策下达] 盲测阶段：准备发出一记 {state['probing_seconds']}s 的试探脉冲。")
        state["moisture_before_probe"] = current_moisture
        send_mqtt_cmd(client, state["probing_seconds"])
        state["phase"] = "PROBE_SENT"
        state["last_water_ts"] = current_ts
        state["pulse_count"] = 1       
        state["last_delta_x"] = 0.0
        save_state(state)

    elif state["phase"] == "PROBE_SENT":
        mins_since = (current_ts - state["last_water_ts"]) / 60.0
        print(f"  -> [耐心等待] 脉冲发毕，等待水分下渗... (已过 {mins_since:.1f} 分钟，需满 15 分钟)")
        
        if mins_since >= 15.0:
            # 【新增逻辑】：等待 15 分钟后水分已稳定，此时计算灌溉增益 Kp
            calculate_and_update_kp(state, current_moisture)

            delta_x = current_moisture - state.get("moisture_before_probe", 0)
            if delta_x < VALID_JUMP_THRESHOLD: 
                state["probe_no_response_count"] = int(state.get("probe_no_response_count") or 0) + 1
                if current_moisture >= dynamic_ceiling:
                    enter_recoverable_hardware_pause(state, "near_dynamic_ceiling_without_valid_jump", current_moisture, current_ts)
                elif (
                    state["probing_seconds"] >= MAX_PROBE_SEC
                    or state["probe_no_response_count"] >= NO_RESPONSE_RETRY_LIMIT
                ):
                    enter_recoverable_hardware_pause(state, "probe_no_response_limit_reached", current_moisture, current_ts)
                else:
                    print(f"  -> [算法思考] {state['probing_seconds']}s 的水量被吞没了(仅涨 {delta_x:.1f}%)，水量太小。")
                    state["probing_seconds"] = min(state["probing_seconds"] + PROBE_STEP, MAX_PROBE_SEC)
                    state["phase"] = "UNINITIALIZED" 
                    print(f"  -> [决策下达] 下次脉冲增加至 {state['probing_seconds']}s。")
            else:
                print(f"  => [特征捕获] 成功！{state['probing_seconds']}s 带来了 {delta_x:.1f}% 的明显涨幅。")
                state["phase"] = "CLIMBING_PULSE"
                state["last_delta_x"] = delta_x  
                state["probe_no_response_count"] = 0
                state["hardware_fault"] = None
            save_state(state)

    # --- 阶段 A2: 阶梯爬升至饱和 ---
    elif state["phase"] == "CLIMBING_PULSE":
        current_pulses = state.get("pulse_count", 0)
        if current_pulses >= MAX_PULSES_PER_CYCLE or current_moisture >= dynamic_ceiling:
            print(f"  -> [安全拦截] 达到最大爬升次数或触及天花板，防止发大水，强行切入排水观测！")
            state["phase"] = "OBSERVING_DRAINAGE"
            state["history_buffer"] = []
            state["pulse_count"] = 0
            save_state(state)
            return

        if not safety_flag: return

        print(f"  -> [决策下达] 喂水阶段：执行第 {current_pulses + 1} 次饱和爬升灌溉。")
        state["moisture_before_probe"] = current_moisture
        send_mqtt_cmd(client, state["probing_seconds"])
        state["phase"] = "CLIMBING_WAIT"
        state["last_water_ts"] = current_ts
        state["pulse_count"] = current_pulses + 1
        save_state(state)

    elif state["phase"] == "CLIMBING_WAIT":
        mins_since = (current_ts - state["last_water_ts"]) / 60.0
        print(f"  -> [耐心等待] 正在吸收水分... (已过 {mins_since:.1f} 分钟)")
        
        if mins_since >= 15.0:
            # 【新增逻辑】：等待 15 分钟后水分已稳定，此时计算灌溉增益 Kp
            calculate_and_update_kp(state, current_moisture)

            delta_x = current_moisture - state.get("moisture_before_probe", 0)
            last_dx = state.get("last_delta_x", 0.0)
            is_diminishing = (last_dx > 0 and delta_x < (last_dx * DIMINISHING_RATIO))

            if delta_x >= SATURATION_JUMP_THRESHOLD and not is_diminishing:
                print(f"  -> [算法思考] 涨幅不错({delta_x:.1f}%)，土壤还能喝。继续喂水！")
                state["phase"] = "CLIMBING_PULSE"
                state["last_delta_x"] = delta_x 
            else:
                if is_diminishing:
                    print(f"  -> [算法思考] 边际效应出现了！这次只涨了 {delta_x:.1f}%，不到上次的一半，水漏出去了。")
                else:
                    print(f"  -> [算法思考] 涨幅极小({delta_x:.1f}%)，土壤已经彻底喝饱了。")
                
                print("  => [决策下达] 停止喂水，切入自然排水与观测阶段。")
                state["phase"] = "OBSERVING_DRAINAGE"
                state["history_buffer"] = []
                state["pulse_count"] = 0 
            save_state(state)

    # --- 阶段 B1: 【高频快排卡钳】锁定 FC ---
    elif state["phase"] == "OBSERVING_DRAINAGE":
        buffer = state["history_buffer"]
        buffer.append({"ts": current_ts, "val": current_moisture})
        if len(buffer) > 4: buffer.pop(0) 
        state["history_buffer"] = buffer
        save_state(state) 

        mins_since = (current_ts - state["last_water_ts"]) / 60.0
        print(f"  -> [排水平衡] 观察重力水下渗中... (停水已过 {mins_since:.1f} 分钟，当前取样点: {len(buffer)}/3)")

        if len(buffer) >= 3:
            dx_dt = calculate_derivative(buffer)
            print(f"  -> [算力输出] 过去10分钟瞬时斜率: {dx_dt:.4f} %/min (门槛: 大于 {DRAINAGE_BRAKE_THRESHOLD})")
            
            if dx_dt > DRAINAGE_BRAKE_THRESHOLD:
                state["learned_FC"] = current_moisture

                # ===== 【修改核心：FC 锚定法记录基准 EC】 =====
                # 此时重力水刚排完，土壤水气比例最完美，EC 读数最具代表性
                ec_fc_history = state.get("ec_fc_history", [])
                ec_fc_history.append(current_ec)
                state["ec_fc_history"] = ec_fc_history
                # ==============================================

                state["phase"] = "OBSERVING_DRYING"
                state["low_candidate_buffer"] = []
                state["drying_buffer"] = []
                state["last_night_slope"] = None
                state["fastest_drying_slope"] = 0.0
                state["previous_slope"] = 0.0
                print(f"  => [决策下达] 瞬时斜率已极度平缓！踩下刹车，物理漏水结束。")
                print(f"  => [基准确认] 成功定标真实持水量 (FC) = {current_moisture}% ！！！")
                save_state(state)
            else:
                print("  -> [算法思考] 斜率依然较陡，说明物理下渗还在继续，保持观察。")

    # --- 阶段 B2: 【高精雷达寻底】日常折点 vs 死亡线 ---
    elif state["phase"] == "OBSERVING_DRYING":
        # 1. 强制数据过 SMA 滤波器
        smoothed_moisture = apply_sma_filter(state, current_moisture)
        
        buffer = state.get("drying_buffer", [])
        buffer.append({"ts": current_ts, "val": smoothed_moisture}) # 存入平滑后的值
        if len(buffer) > 13: buffer.pop(0) 
        state["drying_buffer"] = buffer
        save_state(state) 

        print(f"  -> [雷达充能] 1小时滑动窗口: {len(buffer)}/13 点 (滤波值: {smoothed_moisture:.2f}%)")

        if len(buffer) >= 13:
            # 2. 互斥锁：检查物理异常跳水
            window_drop = buffer[0]["val"] - buffer[-1]["val"]
            if window_drop >= MASSIVE_DROP_THRESHOLD:
                state["interlock_until_ts"] = current_ts + INTERLOCK_COOLDOWN_SEC
                print(f"  !!! [系统互锁] 侦测到巨幅跳水 ({window_drop:.2f}%)，判定为传感器物理异动！")
                print(f"  !!! [系统互锁] 引擎冻结 2 小时，清空污染数据...")
                state["drying_buffer"] = []
                save_state(state)
                return

            if current_ts < state.get("interlock_until_ts", 0):
                print(f"  -> [互锁冷却中] 引擎挂起，解冻还需 {(state['interlock_until_ts'] - current_ts)/60.0:.1f} 分钟。")
                return

            # 3. 使用线性回归求取真实斜率
            current_slope = calculate_derivative_lr(buffer)
            drop_from_fc = state["learned_FC"] - smoothed_moisture
            
            fastest_slope = state.get("fastest_drying_slope", 0.0)
            if current_slope < fastest_slope:
                fastest_slope = current_slope
                state["fastest_drying_slope"] = fastest_slope
                save_state(state)
            
            if curr_hour >= NIGHT_WINDOW[0] or curr_hour < NIGHT_WINDOW[1]:
                state["last_night_slope"] = current_slope
                save_state(state)
            
            previous_slope = state.get("previous_slope", current_slope)
            acceleration = abs(current_slope - previous_slope)
            
            # 纯文本数据面板
            print("  +-- [探底运算核心面板] --------------------------------+")
            print(f"  | 距FC已蒸发 : {drop_from_fc:.1f}% (目标需 > {DRYING_MIN_DROP}%)")
            print(f"  | 当时动态斜率 : {current_slope:.4f} %/min")
            print(f"  | 历史最快失水 : {fastest_slope:.4f} %/min")
            print(f"  | 曲线加速度   : {acceleration:.4f} (需 < {ACCELERATION_THRESHOLD})")
            print("  +------------------------------------------------------+")
            
            if drop_from_fc >= DRYING_MIN_DROP:
                cycle_ended = False
                trigger_reason = ""

                candidate_buffer = state.get("low_candidate_buffer", [])
                candidate_buffer.append(smoothed_moisture)
                if len(candidate_buffer) > 10: candidate_buffer.pop(0)
                state["low_candidate_buffer"] = candidate_buffer
                save_state(state)

                if len(candidate_buffer) >= 8:
                    fluctuation = max(candidate_buffer) - min(candidate_buffer)
                    slope_is_slow = abs(current_slope) < 0.0015
                    slope_far_from_fast = abs(current_slope - fastest_slope) > 0.002
                    
                    if fluctuation <= 0.5 and slope_is_slow and slope_far_from_fast:
                        target_low = sum(candidate_buffer) / len(candidate_buffer)
                        cycle_ended = True
                        trigger_reason = "检测到稳定干区平台（波动<0.5%）"

                if not cycle_ended and fastest_slope < MIN_VALID_DROP_SLOPE:
                    if acceleration <= ACCELERATION_THRESHOLD and current_slope > fastest_slope + 0.002:
                        target_low = smoothed_moisture
                        trigger_reason = "失水加速度归零，自由水耗尽，物理曲线平缓过渡完成！"
                        cycle_ended = True

                if cycle_ended:
                    state["learned_target_low"] = target_low
                    state.get("fc_history", []).append(state["learned_FC"])
                    state.get("target_low_history", []).append(target_low)
                    state["low_candidate_buffer"] = []  
                    
                    completed = state.get("completed_cycles", 0) + 1
                    state["completed_cycles"] = completed
                    state["phase"] = "STEADY_STATE"
                    
                    print(f"\n  !!! [折点警报] 漏斗触发！原因: {trigger_reason}")
                    print(f"  => [基准确认] 第 {completed}/{MAX_LEARNING_CYCLES} 周期竣工。锁定生物学下限: {target_low}%\n")
                    save_state(state)
                    return 
            
            state["previous_slope"] = current_slope
            save_state(state)

    # --- 阶段 C: 多周期调度与终极交接 ---
    elif state["phase"] == "STEADY_STATE":
        if state.get("completed_cycles", 0) >= MAX_LEARNING_CYCLES:
            avg_fc = sum(state["fc_history"]) / len(state["fc_history"])
            avg_low = sum(state["target_low_history"]) / len(state["target_low_history"])

            ec_history = state.get("ec_fc_history", [])
            if len(ec_history) > 0:
                ec_norm = sum(ec_history) / len(ec_history)
                state["learned_ec_norm"] = round(ec_norm, 3)
                print(f"  -> [生化锚点] 成功计算 FC 状态下的基准 EC_norm = {state['learned_ec_norm']}")
            else:
                state["learned_ec_norm"] = None
                print("  -> [警告] 未能捕获到有效的基准 EC")

            # === [新增] 统计 Phase 1 总耗时 ===
            start_ts = state.get("phase1_start_ts", current_ts)
            duration_hrs = (current_ts - start_ts) / 3600.0
            state["phase1_duration_hrs"] = round(duration_hrs, 2)

            phase1_output = {
                "FC": round(avg_fc, 1),
                "Target_Low": round(avg_low, 1),
                "K_p": state.get("irrigation_gain_kp"),
                "EC_norm": state.get("learned_ec_norm"),
                "Phase1_Duration_Hrs": state["phase1_duration_hrs"]  # 新增输出项
            }

            output_path = "/root/water/phase1_clean_soil1.json"
            with open(output_path, "w") as f:
                json.dump(phase1_output, f, indent=4)

            print(f"  -> [输出] 标准Phase1文件已生成: {output_path}")
            print("\n" + "="*55)
            print("[使命必达] 零先验引擎已完成 3 个完美探测周期！")
            print(f"  -> 总耗时: {state['phase1_duration_hrs']} 小时")
            print("[最终植物生理报告]:")
            print(f"  -> 平均真实持水量 (FC)  : {round(avg_fc, 1)}%")
            print(f"  -> 综合天然唤醒下限     : {round(avg_low, 1)}%")
            print(f"  -> 最优解渴脉冲时长     : {state.get('probing_seconds')}s")
            print(f"  -> 盆栽灌溉增益 (Kp)    : {state.get('irrigation_gain_kp', '暂无')} %/s")
            print("[全权移交] 特征数据已封存。水泵电源逻辑锁死，请由主干网络接管日常灌溉。")
            print("="*55 + "\n")
            
            state["handover_complete"] = True
            save_state(state)
            return

        if current_moisture <= state['learned_target_low']:
            if safety_flag:
                next_cycle = state.get('completed_cycles', 0) + 1
                print(f"  -> [决策下达] 生理底线探明，植物需要解渴。立即启动第 {next_cycle} 周期！")
                state["phase"] = "CLIMBING_PULSE" 
                state["pulse_count"] = 0
                state["last_delta_x"] = 0.0
                save_state(state)

# ==========================================================
# 5. 主循环
# ==========================================================
def _gsql_query(sql):
    cmd = "gsql -d soil_data -p 7654 -t -A -F \",\" -c " + repr(sql)
    result = subprocess.run(["su", "-", "opengauss", "-c", cmd], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()

def _latest_soil1_row():
    sql = (
        "SELECT id, recv_time, temp, humidity, ec FROM soil_sensor_readings "
        "WHERE device_code='soil1' "
        "AND temp IS NOT NULL AND humidity IS NOT NULL AND ec IS NOT NULL "
        "ORDER BY recv_time DESC, id DESC LIMIT 1;"
    )
    out = _gsql_query(sql)
    if not out:
        return None
    parts = out.split(",")
    if len(parts) < 5:
        return None
    row_id = int(parts[0])
    recv_time = parts[1].strip()
    temp = float(parts[2])
    humidity = float(parts[3])
    ec = float(parts[4])
    return row_id, recv_time, temp, humidity, ec

def check_csv_watchdog():
    try:
        row = _latest_soil1_row()
        if row is None:
            print("[硬件警报] openGauss soil_sensor_readings[soil1] 暂无有效数据，请检查 MQTT→openGauss 写入链路！")
            return False
        _, recv_time, _, _, _ = row
        dt = datetime.strptime(recv_time, "%Y-%m-%d %H:%M:%S")
        idle_time_hrs = (time.time() - dt.timestamp()) / 3600.0
        if idle_time_hrs > WATCHDOG_TIMEOUT_HRS:
            print(f"[硬件警报] openGauss soil_sensor_readings[soil1] 停更 {idle_time_hrs:.1f} 小时，请检查 MQTT→openGauss 写入链路！")
            return False
        return True
    except Exception as e:
        print(f"[硬件警报] openGauss soil_sensor_readings[soil1] 查询失败: {e}")
        return False

def read_latest_csv_data():
    try:
        row = _latest_soil1_row()
        if row is None:
            return None, None, None, None
        row_id, _recv_time, temp, humidity, ec = row
        return humidity, temp, ec, row_id
    except Exception as e:
        print(f"[读取错误] openGauss soil_sensor_readings[soil1] 查询失败: {e}")
        return None, None, None, None

if __name__ == "__main__":
    client = mqtt.Client()
    try:
        client.connect(MQTT_BROKER, 1883, 60)
        client.loop_start()  
        print("\n" + "="*60)
        print("[系统启动] 纯正零先验侦察引擎")
        print("="*60 + "\n")
        
        last_processed_mtime = 0
        while True:
            check_csv_watchdog()
            moisture_val, temp_val, ec_val, current_mtime = read_latest_csv_data()
            
            if moisture_val is not None and current_mtime != last_processed_mtime:
                print(f"\n------------------------------------------------------------")
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [环境流] 实时水分: {moisture_val}% | 环境温度: {temp_val}°C")
                print(f"------------------------------------------------------------")
                run_control_cycle(client, moisture_val, temp_val, ec_val)
                last_processed_mtime = current_mtime
                
            time.sleep(POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\n[系统关闭] 检测到中断信号，引擎已安全退出。")
        client.loop_stop()
