"""
=============================================================================
 config_manager.py (严格三阶段框架版)
 ─────────────────────────────────────────────────────────────────────────
 【核心职责】：
  1. 零阶段破壳：强制读取 Phase 1 探索结果，注入绝对物理基因。
  2. 衍生联动：通过数学公式自动维持 FC 与安全线、Target_Low 与唤醒线的关系。
  3. 安全通信：提供夜间审计员与主控大脑之间的原子级 JSON 读写接口。

 【修复记录】：
  Fix-1  _inject_phase1_genes 中去除 .get() 默认值后门，改为强制字段校验。
         必须字段缺失 → 立即抛出 RuntimeError，不允许用猜测值继续。
  Fix-2  update() 中引入 _KNOWN_EVOLVABLE_KEYS 白名单，对未知 key 发出
         WARNING 并跳过，拒绝静默注入（防止 "alpha" 误写 "ALPHA" 之类的问题）。
  Fix-3  破壳时物理合理性校验：(FC - TARGET_LOW) / K_p 若超过 WATER_SEC_MAX_HARD，
         降级为 WARNING（不再崩溃），系统允许启动并采用多频次分步补水策略，
         闭环决策模块（Layer 5）将逐步填补剩余缺口。
  Fix-4  SYSTEM_CONSTANTS 注释明确区分"不参与自动进化"与"换硬件需人工修改"。
  Fix-5  新增 ensure_data_files_exist()，首次部署自动创建所有依赖的空文件，
         防止其他模块因文件缺失而崩溃。

 【升级记录】：
  Upgrade-1  清理陈旧 LLM 术语：
               · SYSTEM_CONSTANTS 中 LLM_PREDICT_HORIZON_H → PREDICT_HORIZON_STEPS
               · SHM 文件路径 llm_request/llm_response → pred_request/pred_response
               · 补充 brain 实际使用的缺失键：EC_NORM_REF_TEMP、EC_TEMP_COEFF、
                 LARGE_WATER_THRESHOLD_SEC、SHM_TIMEOUT_SEC
  Upgrade-2  新增 BIOLOGICAL_OFFSETS 全局常量块，将联动公式里的数学间距
             从硬编码魔法数字提升为具名常量，支持物种适配缩放。
  Upgrade-3  _KNOWN_EVOLVABLE_KEYS 新增 BUFFER_SCALE（物种缓冲系数）
             和 K_P_EMA_ALPHA（EMA 平滑速率），允许审计员合法写入。
  Upgrade-4  _inject_phase1_genes 从 phase1_data.json 提取 Buffer_Scale
             并注入为 BUFFER_SCALE（缺失时默认 1.0，允许无缩放启动）。
  Upgrade-5  _derive_linked_params 消除魔法数字，改用 BIOLOGICAL_OFFSETS
             乘以 BUFFER_SCALE 动态计算各安全边界。
  Compat-1   _inject_phase1_genes 新增兼容性字段提取逻辑，支持 Phase 1 实际
             输出的带前缀键名（如 learned_FC），并对 EC_norm 为 null 时通过
             ec_buffer 均值进行安全回退，确保旧版探索数据可直接使用。
  Upgrade-6  移除 SYSTEM_CONSTANTS 中的静态 LARGE_WATER_THRESHOLD_SEC（30.0s 固定值）。
             改为 BIOLOGICAL_OFFSETS["LARGE_WATER_RATIO"]（默认 0.7）乘以 T_full 的
             动态属性 ConfigManager.LARGE_WATER_THRESHOLD，由 _derive_linked_params
             实时推导：T_full = (FC - TARGET_LOW) / K_P，换盆/换泵后自动适配。
  Upgrade-7  _KNOWN_EVOLVABLE_KEYS 新增 PREDICTOR_CIRCUIT_OPEN（bool），
             供 main.py 的 PredictorCircuitBreaker 写入。
             True = Layer 4 跳过 SHM 预测请求，直接降级为物理外推；
             False = 正常调用预测模型。
             此字段不参与基因进化，审计员不会修改它。
=============================================================================
"""

import logging
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
from runtime_io import load_json_locked, save_json_locked, update_json_locked

logger = logging.getLogger("config_manager")

BASE_DIR = Path(__file__).parent

FILE_PATHS = {
    # Phase 1 留下的物理基因文件（系统初始化的唯一合法来源）
    "PHASE1_DATA":     Path("/root/water/phase1_test/phase1_data_soil2.json"),

    # 动态进化的参数文件（夜间审计员的输出，主控大脑的输入）
    "EVOLVING_PARAMS": BASE_DIR / "evolving_params.json",

    "SYSTEM_STATE":    BASE_DIR / "system_state.json",  #记录非进化类的系统和运行状态，以及未完成的任务
    "PATTERN_MEMORY":  BASE_DIR / "pattern_memory.json",#经验库
    "IRRIGATION_TRIALS": BASE_DIR / "irrigation_trials.json",  #完整浇水试验流水，含成功与失败样本
    "IRRIGATION_PROFILE": BASE_DIR / "irrigation_profile.json",  #分区学习画像，记录阶梯试探与失败限流
}

# ===========================================================================
# 系统工程常量
# ─────────────────────────────────────────────────────────────────────────
# 注意：这里的常量分两类，含义不同，不可混淆：
#
#   A 类：架构级控制常量
#         与具体硬件无关，正常情况下永远不需要修改。
#
#   B 类：硬件物理约束常量  ⚠️ 换硬件时必须人工重新标定 ⚠️
#         WATER_SEC_MIN, WATER_SEC_MAX_HARD, EMERGENCY_WATER_SEC
#         这些值【不参与自动进化】，但与接入的水泵型号、急救策略强绑定。
#         例：换用大流量水泵后，30 秒的上限可能直接导致沤根，
#             必须重新测定并手动修改这里，不能依赖进化系统自动处理。
#         EMERGENCY_WATER_SEC 与 WATER_SEC_MAX_HARD 脱钩：紧急区只做短脉冲补水，
#             战区/Layer1 天花板仍受 WATER_SEC_MAX_HARD 约束。
# ===========================================================================
SYSTEM_CONSTANTS = {
    # A 类：架构级
    "MAIN_LOOP_INTERVAL_SEC":    180,       #主循环心跳间隔；缩短观察周期，但不等于更频繁开泵
    "PHASE2_ENABLED":            True,      # soil2 独立 Phase2 影子链路
    "PHASE2_SHADOW_ONLY":        True,      # 只记录预测，不参与控泵评分
    "PREDICT_HORIZON_STEPS":     12,        # Upgrade-1：Phase2 预测步长（步）
    "SHM_REQUEST_FILE":          "/dev/shm/soil2_pred_request.json",
    "SHM_RESPONSE_FILE":         "/dev/shm/soil2_pred_response.json",
    "SHM_TIMEOUT_SEC":           30,        # 等待预测模型传过来的时间，超过30，会等待或者崩溃，#######需要优化####
    "SOAK_WAIT_SEC":             3600,      # 浇水后等待渗透稳定（秒，60 分钟）；20 分钟易把扩散延迟误判为无响应
    "SOAK_OBSERVATION_MARKS_SEC": [300, 900, 1800],  # 5/15/30 分钟仅记录中途反馈，60 分钟后才最终结算
    "NORMAL_IRRIGATION_COOLDOWN_SEC": 21600,  # 普通战区浇水冷却（6小时），紧急补水不受此限制
    "DYNAMIC_COOLDOWN_MIN_SEC": 7200,
    "DYNAMIC_COOLDOWN_MAX_SEC": 28800,
    "DYNAMIC_COOLDOWN_LOW_SEC": 10800,
    "DYNAMIC_COOLDOWN_MID_SEC": 14400,
    "DYNAMIC_COOLDOWN_STRONG_DELTA": 4.0,
    "DYNAMIC_COOLDOWN_WEAK_DELTA": 2.0,
    "DYNAMIC_COOLDOWN_FAST_DROP_STEP": -0.12,
    "EXPLORATION_DAILY_BUDGET":  5,         # 每日最多主动阶梯探索次数
    "EXPLORATION_MIN_FC_GAP":    5.0,       # 距 FC 至少 5% 才允许 4s/5s 探索
    "EXPLORATION_STEP_SUCCESS_4S": 3,       # 同湿度区连续稳定 3 次后开放 4s
    "EXPLORATION_STEP_SUCCESS_5S": 5,       # 同湿度区连续稳定 5 次后开放 5s
    "EXPLORATION_MAX_SEC":       5.0,       # 第一阶段安全阶梯探索硬上限
    "EXPLORATION_STUCK_FORCE_ROUNDS": 8,    # Fix-Stuck: 卡在最小剂量多少轮后强制探针
    "LOW_WET_RECOVERY_ENABLED":  True,
    "LOW_WET_RECOVERY_TRIGGER_MARGIN": 0.5,
    "LOW_WET_RECOVERY_TARGET_FC_MARGIN": 5.0,
    "LOW_WET_RECOVERY_MIN_SEC":  6.0,
    "LOW_WET_RECOVERY_MAX_SEC":  8.0,
    "LOW_WET_RECOVERY_DEEP_TRIGGER": 30.0,
    "LOW_WET_RECOVERY_DEEP_MIN_SEC": 8.0,
    "LOW_WET_RECOVERY_HARD_MARGIN": 1.0,
    "LOW_WET_RECOVERY_HARD_SEC": 10.0,
    "LOW_WET_RECOVERY_MIN_INTERVAL_SEC": 21600,
    "LOW_WET_RECOVERY_INEFFECTIVE_REJECTS": 3,
    "LOW_WET_RECOVERY_INEFFECTIVE_DELTA": 0.3,
    "LOW_WET_RECOVERY_INEFFECTIVE_PAUSE_SEC": 21600,
    "LOW_WET_RECOVERY_PAUSE_LOG_INTERVAL_SEC": 1800,
    "RESERVOIR_RETEST_ENABLED": True,
    "RESERVOIR_RETEST_INTERVAL_SEC": 3600,
    "RESERVOIR_RETEST_SEC": 12.0,       # 5 秒仅作探测，12 秒作为服务端参考剂量复测
    "RESERVOIR_RETEST_MIN_DELTA": 0.3,  # 与传感器 0.1% 分辨率匹配，避免把微小有效响应误判为无响应
    "RESERVOIR_RETEST_HISTORY_LIMIT": 80,
    "RESERVOIR_RETEST_SAME_SEC_TOLERANCE": 0.6,
    "RESERVOIR_RETEST_MIN_BASELINE_SAMPLES": 3,
    "RESERVOIR_RETEST_MIN_IMPROVEMENT": 0.4,
    "RESERVOIR_RETEST_IQR_FACTOR": 1.5,
    "OBSERVE_STREAK_FORCE_THRESHOLD": 24,
    "OBSERVE_STREAK_FORCE_FC_GAP": 5.0,
    "OBSERVE_STREAK_FORCE_SEC": 3.0,
    "IRRIGATION_STYLE_EXPERIMENT_ENABLED": True,
    "IRRIGATION_STYLE_MODE": "see_dry_wet",
    "DEEP_DRYDOWN_EXPERIMENT_ENABLED": False,
    "DRYDOWN_EXPERIMENT_HOURS": 36.0,
    "WET_HOLD_EXPERIMENT_HOURS": 24.0,
    "DRYDOWN_TARGET_MARGIN": 0.0,
    "DRYDOWN_BELOW_TARGET_STEP": 0.0,
    "DRYDOWN_BELOW_TARGET_MAX": 0.0,
    "HARD_SAFETY_LOW_OVERRIDE": 27.0,
    "HARD_SAFETY_GAP_RATIO": 0.35,
    "HARD_SAFETY_DOWN_MAX_PER_DAY": 1.0,
    "SOIL_STALE_WARN_SEC":       900,       # 土壤有效读数超过 15 分钟未更新时写 WARNING
    "SOIL_STALE_HARD_SEC":       18000,     # 土壤有效读数超过 5 小时后进入传感器故障保护
    "SOIL_STALE_ALERT_SEC":      18000,     # 土壤有效读数超过 5 小时后发送邮件告警
    "SOIL_STALE_ALERT_REPEAT_SEC": 21600,   # 同一故障邮件最短重复间隔（6 小时）
    "AIR_HUMIDITY_STALE_WARN_SEC": 21600,
    "AIR_HUMIDITY_FALLBACK_DEFAULT": 63.5,
    "ALERT_EMAIL_TO":            "3278326875@qq.com",
    "EMERGENCY_INEFFECTIVE_MAX_COUNT": 5,  # 延长观察后再触发保护，避免短脉冲累计误报
    "EMERGENCY_INEFFECTIVE_DELTA": 0.3,
    "EMERGENCY_INEFFECTIVE_PAUSE_SEC": 21600,
    "EMERGENCY_INEFFECTIVE_ALERT_REPEAT_SEC": 21600,
    "WATER_DELIVERY_AUTO_RECOVERY_DELTA": 2.0,
    "WATER_DELIVERY_AUTO_RECOVERY_TARGET_MARGIN": 1.5,
    "EMERGENCY_INTERRUPT_COOLDOWN_SEC": 900,  # 渗透期紧急补水最短间隔（15 分钟）
    "EMERGENCY_INTERRUPT_MIN_DROP": 0.5,
    "MAX_EMERGENCY_INTERRUPTS_PER_SOAK": 1,   # 同一渗透窗口最多允许一次紧急打断补水
    "PUMP_OFF_RETRY_COUNT":      3,         # MQTT 关泵指令重复发送次数
    "PUMP_OFF_RETRY_INTERVAL_SEC": 0.2,
    # LARGE_WATER_THRESHOLD_SEC 已移除：
    # 原固定值 30.0s 对不同盆径/水泵不通用。
    # 现改为动态属性 ConfigManager.LARGE_WATER_THRESHOLD，
    # 由 _derive_linked_params 按 T_full × LARGE_WATER_RATIO 实时计算。

    # A 类：EC 温度补偿物理常数（由电化学原理决定，不随硬件或植物变化）
    "EC_NORM_REF_TEMP":          25.0,      # 把ec都换成25℃时的等效值，EC 归一化参考温度（°C）
    "EC_TEMP_COEFF":             0.02,      # 每 °C 偏差的 EC 修正系数

    # B 类：硬件物理约束（换硬件时必须人工重新标定）
    "WATER_SEC_MIN":             3.0,       # 水泵最短单次运行秒数（防空转损耗）
    "WATER_SEC_MAX_HARD":        30.0,      # 水泵单次最长运行秒数（当前泵型硬上限）
    "EMERGENCY_WATER_SEC":       5.0,       # 紧急区最小脉冲；低于原 5 秒的剂量不再用于响应判断
    "EMERGENCY_WATER_SEC_MILD":  5.0,
    "EMERGENCY_WATER_SEC_MODERATE": 8.0,
    "EMERGENCY_WATER_SEC_SEVERE": 12.0,
    "EMERGENCY_WATER_SEC_CRITICAL": 15.0,
}

# ===========================================================================
# 生物学基准偏移常量（Upgrade-2）
# ─────────────────────────────────────────────────────────────────────────
# 这些值是联动公式中的数学间距，从函数体内的魔法数字提升为具名常量。
# _derive_linked_params 在计算衍生边界时，会用这些值乘以 BUFFER_SCALE，
# 实现不同植物物种的安全边界弹性缩放：
#   · BUFFER_SCALE = 1.0（默认）：使用标准偏移，适合大多数常见植物
#   · BUFFER_SCALE > 1.0（如多肉 1.5）：安全缓冲带更宽，系统更保守
#   · BUFFER_SCALE < 1.0（如蕨类 0.6）：安全缓冲带更窄，系统更激进
#
# 注意：SALT_MULTIPLIER 是化学阈值倍率，与水分缓冲无关，不参与 BUFFER_SCALE 缩放。
# ===========================================================================
BIOLOGICAL_OFFSETS = {
    "SAFE_SLEEP_MARGIN":  0.2,   # FC 距安全休眠下限的间距（%）
    "RESPIRATION_MARGIN": 0.3,   # FC 距沤根触发线的间距（%）
    "WAKE_UP_MARGIN":     0.5,   # TARGET_LOW 距大模型唤醒线的缓冲（%）
    "SALT_MULTIPLIER":    1.5,   # EC_BASE 容忍倍率（不参与 BUFFER_SCALE 缩放）
    "LARGE_WATER_RATIO":  0.7,   # 大水阈值占 T_full（从干渴到全饱所需秒数）的比例
                                  # T_full = (FC - TARGET_LOW) / K_P
                                  # LARGE_WATER_THRESHOLD = T_full × LARGE_WATER_RATIO
}

# phase1_data.json 中必须存在的字段（缺一不可，不允许任何默认值兜底）
_PHASE1_REQUIRED_KEYS = ["FC", "Target_Low", "K_p", "EC_norm"]
                                                               
# evolving_params.json 中合法的可进化 key 白名单。
# update() 遇到不在此集合内的 key 时发出 WARNING 并跳过，拒绝静默注入。
# 若需新增可进化参数，先在这里登记，再在 _inject_phase1_genes 中给出初始值。
_KNOWN_EVOLVABLE_KEYS = {
    # 物理锚点
    "FC", "TARGET_LOW", "K_P",
    # 物种缓冲系数（Upgrade-3：允许审计员根据植物表现调整安全边界宽度）
    "BUFFER_SCALE",
    # 控制回路（Upgrade-3：EMA 平滑速率，审计员可调整 K_p 的学习速度）
    "K_P_EMA_ALPHA", "K_P_LOW", "K_P_MID", "K_P_HIGH",
    # 气候权重
    "ALPHA", "BETA", "SLOPE_STEEP",
    # 化学基准
    "EC_BASE","EC_STRESS_MULTIPLIER",
    # 硬件与控制
    "GAMMA", "TRAJ_TOLERANCE", "PUMP_LIFE_WARNING_COUNT",
    "COST_STARTUP_FACTOR", "OBSERVE_STREAK_PENALTY_STEP", "OBSERVE_STREAK_PENALTY_MAX",
    "VPD_SAFE_LINE_START", "VPD_SAFE_LINE_MAX_BOOST",
    # 联动派生参数（正常不由外部直接写入，但保留以支持审计员紧急覆写）
    "M_SAFE_SLEEP", "RESPIRATION_LIMIT", "M_WAKE_UP", "EC_SALT_STRESS",
    # 动态大水阈值（由 _derive_linked_params 计算，审计员可紧急覆写）
    "LARGE_WATER_THRESHOLD",
    # 预测模型熔断标志（由 main.py 的 PredictorCircuitBreaker 写入，不参与进化）
    # True = Layer 4 跳过 SHM 预测请求直接物理外推；False = 正常调用预测模型
    "PREDICTOR_CIRCUIT_OPEN", "PHASE2_SHADOW_ONLY",
}


# ===========================================================================
# Fix-5：首次部署时自动创建所有依赖文件
# ===========================================================================

def ensure_data_files_exist() -> None:
    """
    首次部署或迁移环境时调用，确保所有依赖的数据文件都存在。

    · system_state.json   → 创建空 {}
    · pattern_memory.json → 创建空 []
    · irrigation_trials.json → 创建空 []，记录所有浇水试验，包括被经验库拒绝的样本
    · evolving_params.json 不在这里创建，由 ConfigManager._bootstrap() 负责。
    · phase1_data.json    不在这里创建，必须由 Phase 1 探索程序写入，
                          不存在时 ConfigManager 会主动抛异常提醒用户。

    建议在 main.py 最顶部、ConfigManager 实例化之前调用一次。
    """
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    # JSON 文件
    json_init: dict[str, Any] = {
        "SYSTEM_STATE":   {},
        "PATTERN_MEMORY": [],
        "IRRIGATION_TRIALS": [],
        "IRRIGATION_PROFILE": {},
    }
    for key, empty_val in json_init.items():
        path = FILE_PATHS[key]
        if not path.exists():
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(empty_val, f, ensure_ascii=False, indent=4)
                logger.info(f"[ensure] 创建空文件: {path}")
            except OSError as e:
                logger.error(f"[ensure] 创建 {path} 失败: {e}")

# ===========================================================================
# ConfigManager
# ===========================================================================

class ConfigManager:

    def __init__(self, override_path: Path | None = None):
        self._path = override_path or FILE_PATHS["EVOLVING_PARAMS"]
        self._params: dict[str, Any] = {}
        self._bootstrap()

    # ------------------------------------------------------------------
    # 破壳逻辑
    # ------------------------------------------------------------------

    def _bootstrap(self) -> None:
        """
        零阶段初始化：
        · evolving_params.json 已存在 → 直接加载（进化接管模式）
        · 不存在 → 强制从 phase1_data.json 注入物理基因
        """
        if self._path.exists():
            self._load_from_disk()
            logger.info(
                f"[ConfigManager] 从进化记录醒来: "
                f"FC={self.FC}%  TARGET_LOW={self.TARGET_LOW}%  "
                f"战区宽度={(self.FC - self.TARGET_LOW):.2f}%"
            )
        else:
            logger.warning("[ConfigManager] 未发现进化记录，执行零阶段物理基因注入...")
            self._inject_phase1_genes()

    def _inject_phase1_genes(self) -> None:
        """
        从 phase1_data.json 强制提取物理锚点，构建第一版可进化参数表。

        Fix-1: 所有字段必须显式存在，不允许任何 .get(key, default) 的默认值兜底。
        Fix-3: 注入后立即做物理合理性校验，矛盾在启动阶段暴露。
        Compat-1: 新增兼容性字段提取，支持 Phase 1 实际输出的带前缀键名
                  （如 learned_FC），并对 EC_norm 为 null 时通过 ec_buffer
                  均值进行安全回退，确保旧版探索数据可直接使用。
        """
        phase1_path = FILE_PATHS["PHASE1_DATA"]

        # 文件存在性检查
        if not phase1_path.exists():
            raise RuntimeError(
                f"\n{'='*60}\n"
                f"  致命错误：未找到 Phase 1 探索数据文件\n"
                f"  路径: {phase1_path}\n\n"
                f"  系统拒绝使用任何拍脑门的默认值启动。\n"
                f"  请先运行 phase1_explorer.py 完成探索后再启动主程序。\n"
                f"{'='*60}"
            )

        # 解析 JSON
        try:
            with open(phase1_path, "r", encoding="utf-8") as f:
                p1_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(f"读取 Phase 1 数据失败，文件可能损坏: {e}") from e

        # ---------------------------------------------------------------
        # Compat-1：兼容性字段提取
        # 优先读取标准键名，若无则回退到 Phase 1 实际输出的带前缀键名。
        # 这里不使用 _PHASE1_REQUIRED_KEYS 的硬校验，改为最终有效性统一校验。
        # ---------------------------------------------------------------
        fc         = p1_data.get("FC",         p1_data.get("learned_FC"))
        target_low = p1_data.get("Target_Low", p1_data.get("learned_target_low"))
        kp         = p1_data.get("K_p",        p1_data.get("irrigation_gain_kp"))
        ec_norm    = p1_data.get("EC_norm",     p1_data.get("learned_ec_norm"))

        # Compat-1：EC_norm 安全回退——learned_ec_norm 为 null 时用历史 EC 均值
        if ec_norm is None:
            ec_buffer = p1_data.get("ec_buffer", [])
            ec_source = "ec_buffer"
            if not ec_buffer:
                ec_buffer = p1_data.get("ec_fc_history", [])
                ec_source = "ec_fc_history"
            if ec_buffer and len(ec_buffer) > 0:
                ec_norm = sum(ec_buffer) / len(ec_buffer)
                logger.info(
                    f"[ConfigManager] EC_norm 由 {ec_source} 均值推算: "
                    f"{ec_norm:.4f}（样本数={len(ec_buffer)}）"
                )

        # 最终有效性校验：所有必要参数都必须是有效数值
        if None in (fc, target_low, kp, ec_norm):
            missing_desc = {
                "FC / learned_FC":                          fc,
                "Target_Low / learned_target_low":          target_low,
                "K_p / irrigation_gain_kp":                 kp,
                "EC_norm / learned_ec_norm 或非空 ec_buffer": ec_norm,
            }
            missing_fields = [k for k, v in missing_desc.items() if v is None]
            raise RuntimeError(
                f"Phase 1 数据解析失败：缺少必须的物理基因参数或参数值为 null。\n"
                f"缺失字段：{missing_fields}\n"
                f"请检查 JSON 中是否包含有效的上述字段。\n"
                f"请重新运行 Phase 1 探索程序补全测量后再启动。"
            )

        fc_initial         = float(fc)
        target_low_initial = float(target_low)
        kp_initial         = float(kp)
        ec_norm_initial    = float(ec_norm)

        # 物理合理性校验（Fix-3 升级版）
        battle_zone = fc_initial - target_low_initial
        if battle_zone < 0.5:
            raise RuntimeError(
                f"物理数据异常：FC={fc_initial}% 与 Target_Low={target_low_initial}% "
                f"之差仅 {battle_zone:.2f}%，小于最小安全间距 0.5%。\n"
                f"请检查 Phase 1 探索数据是否正确。"
            )

        max_sec         = SYSTEM_CONSTANTS["WATER_SEC_MAX_HARD"]
        theoretical_sec = battle_zone / kp_initial

        # Upgrade-1（Fix-3 降级）：超出硬上限时改为 WARNING，不再崩溃。
        # 系统允许启动，Layer 5 闭环决策将自动采用多频次分步补水策略，
        # 每轮最多浇 WATER_SEC_MAX_HARD 秒，逐步将湿度填至目标区间。
        if theoretical_sec > max_sec:
            logger.warning(
                f"⚠️ 物理参数提示：战区宽度 {battle_zone:.2f}% ÷ K_p={kp_initial} "
                f"= 理论最大浇水 {theoretical_sec:.1f}s，超过硬件单次上限 ({max_sec}s)。\n"
                f"⚙️ 系统将采取【多频次分步补水】策略，剩余缺口由闭环决策模块逐步填补。"
            )

        # Upgrade-4：从 phase1_data.json 提取物种缓冲系数。
        # Buffer_Scale 不在 _PHASE1_REQUIRED_KEYS 中（旧探索数据可能没有此字段），
        # 缺失时默认 1.0（标准偏移，无缩放），系统仍可正常启动。
        buffer_scale = float(p1_data.get("Buffer_Scale", 1.0))
        if buffer_scale != 1.0:
            logger.info(f"[ConfigManager] 物种缓冲系数 BUFFER_SCALE={buffer_scale} 已从 Phase 1 注入。")

        # 注入初始参数集
        self._params = {
            # 物理锚点（Phase 1 测量的绝对真值，下面这些是唯一的合法起点）
            "FC":           fc_initial,
            "TARGET_LOW":   target_low_initial,
            "K_P":          kp_initial,

            # 物种缓冲系数（Upgrade-4：控制安全边界的弹性宽窄）
            "BUFFER_SCALE": buffer_scale,

            # 控制回路（K_P_EMA_ALPHA：K_p 短期进化的平滑系数，审计员可调整）
            "K_P_EMA_ALPHA": 0.2,

            # 气候权重（初始中性值；不预设季节，待审计员根据真实 VPD 进化）
            "ALPHA":        1.0,
            "BETA":         1.0,
            "SLOPE_STEEP":  -0.8,

            # 化学基准（Phase 1 清水基线）
            "EC_BASE":      ec_norm_initial,
            "EC_STRESS_MULTIPLIER": BIOLOGICAL_OFFSETS["SALT_MULTIPLIER"],

            # 硬件与控制
            "GAMMA":                   10.0,
            "TRAJ_TOLERANCE":          0.2,
            "COST_STARTUP_FACTOR":      0.25,
            "OBSERVE_STREAK_PENALTY_STEP": 0.75,
            "OBSERVE_STREAK_PENALTY_MAX":  45.0,
            "VPD_SAFE_LINE_START":      1.2,
            "VPD_SAFE_LINE_MAX_BOOST":  0.8,
            "PUMP_LIFE_WARNING_COUNT": 5000,
        }

        # 触发联动推导，生成所有衍生参数
        self._derive_linked_params()

        # 原子写盘，生成第一版进化基因文件
        self.save()

        logger.info("=" * 60)
        logger.info("  零阶段物理基因注入完成")
        logger.info(f"  FC={fc_initial}%  Target_Low={target_low_initial}%  战区={battle_zone:.2f}%")
        logger.info(f"  BUFFER_SCALE={buffer_scale}  理论最大浇水={theoretical_sec:.1f}s")
        logger.info(f"  M_SAFE_SLEEP={self.M_SAFE_SLEEP}%  M_WAKE_UP={self.M_WAKE_UP}%")
        logger.info(f"  EC_BASE={ec_norm_initial}  EC_SALT_STRESS={self.EC_SALT_STRESS}")
        logger.info("=" * 60)

    def _derive_linked_params(self) -> None:
        """
        核心联动法则（Upgrade-5：消除魔法数字，支持物种缩放）。
        任意参数被 update() 修改后都必须调用此方法。

        各安全边界的计算公式：
          safe_margin = BIOLOGICAL_OFFSETS["SAFE_SLEEP_MARGIN"] × BUFFER_SCALE
          resp_margin = BIOLOGICAL_OFFSETS["RESPIRATION_MARGIN"] × BUFFER_SCALE
          wake_margin = BIOLOGICAL_OFFSETS["WAKE_UP_MARGIN"] × BUFFER_SCALE

          M_SAFE_SLEEP      = FC - safe_margin    （安全休眠区下限）
          RESPIRATION_LIMIT = FC - resp_margin    （沤根触发线）
          M_WAKE_UP         = TARGET_LOW + wake_margin  （大模型强制唤醒线）
          EC_SALT_STRESS    = EC_BASE × SALT_MULTIPLIER （盐分超标红线，不参与缩放）

        BUFFER_SCALE 进化示例：
          · 多肉植物 BUFFER_SCALE=1.5 → safe_margin=0.3，缓冲带更宽，系统更保守
          · 蕨类植物 BUFFER_SCALE=0.6 → safe_margin=0.12，缓冲带更窄，系统更激进
        """
        fc         = self._params["FC"]
        target_low = self._params["TARGET_LOW"]
        ec_base    = self._params["EC_BASE"]
        scale      = self._params.get("BUFFER_SCALE", 1.0)

        # 从全局常量中读取各间距基准值，乘以物种缩放系数
        safe_margin = BIOLOGICAL_OFFSETS["SAFE_SLEEP_MARGIN"]  * scale
        resp_margin = BIOLOGICAL_OFFSETS["RESPIRATION_MARGIN"] * scale
        wake_margin = BIOLOGICAL_OFFSETS["WAKE_UP_MARGIN"]     * scale

        self._params["M_SAFE_SLEEP"]      = round(fc - safe_margin,         3)
        self._params["RESPIRATION_LIMIT"] = round(fc - resp_margin,         3)
        self._params["M_WAKE_UP"]         = round(target_low + wake_margin, 3)
        # 盐分超标倍率与水分缓冲无关，不参与 BUFFER_SCALE 缩放
        multiplier = self._params.get(
            "EC_STRESS_MULTIPLIER", 
            BIOLOGICAL_OFFSETS["SALT_MULTIPLIER"]
        )
        self._params["EC_SALT_STRESS"]    = round(ec_base * multiplier, 3)

        # 动态大水阈值：T_full × LARGE_WATER_RATIO
        # T_full = (FC - TARGET_LOW) / K_P —— 系统从干渴到全饱理论所需秒数
        # 乘以比例系数后，无论换盆还是换泵，"大灌溉"定义随之自适应。
        kp = self._params["K_P"]
        if kp > 0:
            t_full = (fc - target_low) / kp
            ratio  = BIOLOGICAL_OFFSETS["LARGE_WATER_RATIO"]
            self._params["LARGE_WATER_THRESHOLD"] = round(t_full * ratio, 2)
        else:
            logger.warning("[ConfigManager] K_P <= 0，跳过 LARGE_WATER_THRESHOLD 推导。")

    # ------------------------------------------------------------------
    # I/O 接口
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> None:
        """通过统一 sidecar 锁安全读取 evolving_params.json。"""
        params = load_json_locked(self._path, None)
        if isinstance(params, dict):
            self._params = params
        else:
            logger.error(f"[ConfigManager] 读取进化参数失败或格式异常: {self._path}")

    def load(self) -> None:
        """主控大脑每轮循环调用，重载审计员可能已写入的最新基因。"""
        self._load_from_disk()

    def save(self) -> None:
        """通过统一 sidecar 锁原子写回 evolving_params.json。"""
        try:
            save_json_locked(self._path, self._params)
        except OSError as e:
            logger.error(f"[ConfigManager] 保存失败: {e}")

    def update(self, updates: dict[str, Any], persist: bool = True) -> None:
        """
        供主控大脑（K_p 短期微调）或夜间审计员（FC/EC_BASE 深度进化）调用。
        更新后自动触发联动推导。

        Fix-2: 白名单校验。key 不在 _KNOWN_EVOLVABLE_KEYS 中时，
               发出 WARNING 并跳过，拒绝静默注入未知字段。
        """
        valid_updates: dict[str, Any] = {}
        for k, v in updates.items():
            # 白名单过滤
            if k not in _KNOWN_EVOLVABLE_KEYS:
                logger.warning(
                    f"[ConfigManager] 拒绝未知 key: '{k}'（值={v!r}）。"
                    f"若需新增可进化参数，请先将其加入 _KNOWN_EVOLVABLE_KEYS 白名单。"
                )
                continue
            valid_updates[k] = v

        if not valid_updates:
            return

        def apply(current):
            if isinstance(current, dict):
                self._params = current
            for k, v in valid_updates.items():
                old = self._params.get(k, "<新增>")
                self._params[k] = v
                logger.info(f"[ConfigManager] 参数进化: {k}  {old!r} → {v!r}")

            # 强制刷新全部联动公式，保证派生参数永远不脱节
            self._derive_linked_params()
            return self._params

        if persist:
            try:
                self._params = update_json_locked(self._path, self._params, apply)
            except OSError as e:
                logger.error(f"[ConfigManager] 参数原子更新失败: {e}")
        else:
            apply(self._params)

    # ------------------------------------------------------------------
    # 属性访问器
    # ------------------------------------------------------------------

    @property
    def FC(self) -> float:
        return float(self._params["FC"])

    @property
    def TARGET_LOW(self) -> float:
        return float(self._params["TARGET_LOW"])

    @property
    def M_SAFE_SLEEP(self) -> float:
        return float(self._params["M_SAFE_SLEEP"])

    @property
    def RESPIRATION_LIMIT(self) -> float:
        return float(self._params["RESPIRATION_LIMIT"])

    @property
    def M_WAKE_UP(self) -> float:
        return float(self._params["M_WAKE_UP"])

    @property
    def ALPHA(self) -> float:
        return float(self._params["ALPHA"])

    @property
    def BETA(self) -> float:
        return float(self._params["BETA"])

    @property
    def GAMMA(self) -> float:
        return float(self._params["GAMMA"])

    @property
    def K_P(self) -> float:
        return float(self._params["K_P"])

    @property
    def EC_BASE(self) -> float:
        return float(self._params["EC_BASE"])

    @property
    def EC_SALT_STRESS(self) -> float:
        return float(self._params["EC_SALT_STRESS"])

    @property
    def LARGE_WATER_THRESHOLD(self) -> float:
        """大灌溉判断阈值（秒）。动态推导：T_full × LARGE_WATER_RATIO。"""
        return float(self._params["LARGE_WATER_THRESHOLD"])

    @property
    def TRAJ_TOLERANCE(self) -> float:
        return float(self._params["TRAJ_TOLERANCE"])

    @property
    def SLOPE_STEEP(self) -> float:
        return float(self._params["SLOPE_STEEP"])

    def get_constant(self, key: str) -> Any:
        """读取系统工程常量。"""
        return SYSTEM_CONSTANTS.get(key)

    def get(self, key: str, fallback: Any = None) -> Any:
        """通用参数读取，供审计员按名访问不在属性列表里的字段。"""
        return self._params.get(key, fallback)

    def __repr__(self) -> str:
        return (
            f"<ConfigManager FC={self.FC} TL={self.TARGET_LOW} "
            f"战区={(self.FC - self.TARGET_LOW):.2f}% "
            f"SAFE={self.M_SAFE_SLEEP} WAKE={self.M_WAKE_UP}>"
        )


# ===========================================================================
# 自测入口
# ===========================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # 不要删文件！直接实例化！
    cfg = ConfigManager()
    
    print("\n" + "="*50)
    print("当前系统真实基因图谱巡检")
    print("="*50)
    print(f"  FC (容器持水量):      {cfg.FC}%")
    print(f"  TARGET_LOW (枯萎线):  {cfg.TARGET_LOW}%")
    print(f"  战区宽度:             {cfg.FC - cfg.TARGET_LOW:.2f}%")
    print(f"  K_P (物理增益):       {cfg.K_P}")
    print("-" * 50)
    print(f"  BUFFER_SCALE (缩放):  {cfg.get('BUFFER_SCALE', 1.0)}")
    print(f"  EC_BASE (基准盐分):   {cfg.EC_BASE}")
    print(f"  M_SAFE_SLEEP (安全):  {cfg.M_SAFE_SLEEP}%")
    print(f"  M_WAKE_UP (唤醒):     {cfg.M_WAKE_UP}%")
    print(f"  EC_SALT_STRESS(盐害): {cfg.EC_SALT_STRESS}")
    print("="*50)
