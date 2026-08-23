"""
=============================================================================
 main.py
 ─────────────────────────────────────────────────────────────────────────
 【系统主入口 —— 心跳守护进程】

 职责：
   1. 系统环境初始化与零阶段破壳（Bootstrap）
      · ensure_data_files_exist() 建立所有依赖的数据文件
      · ConfigManager() 触发 Phase 0 物理基因注入（读取 phase1_data.json）
      · phase1_data.json 不存在时，系统在此处拒绝启动，而非在循环中崩溃

   2. 维持严格的 5 分钟（300 秒）心跳节律（防时间漂移）
      · 不使用 time.sleep(300)，而是 sleep_time = interval - elapsed
      · run_cycle() 本身会消耗数秒（读传感器、等预测模型），
        直接 sleep 300 会导致采样时间戳漂移：12:00 → 12:05:30 → 12:11:15

   3. 全局异常兜底，确保 7×24 不宕机
      · I²C 传感器偶发通讯失败、文件锁竞争，在边缘设备上是常态
      · run_cycle() 包裹在 try/except Exception 内
      · 异常发生时打印完整调用栈、等待下一个心跳周期重试，不退出进程

   4. 优雅退出（Graceful Shutdown）
      · 捕获 SIGINT（Ctrl+C）和 SIGTERM（systemd / supervisor stop）
      · 退出前强制关闭水泵 GPIO 引脚（见硬件保护注释，部署时必须取消注释）
      · 若退出时渗透哨兵尚存，打印剩余等待时间提醒人工确认植物状态

   5. 预测模型熔断器（PredictorCircuitBreaker）
      · 解决问题：Phase 2 模型宕机时，每轮心跳白等 SHM_TIMEOUT_SEC（30s）
      · 三态状态机：CLOSED → OPEN（连续 3 次超时）→ HALF_OPEN（1 小时后探测）
      · 熔断期间：写 PREDICTOR_CIRCUIT_OPEN=True 到 cfg，Layer 4 读到后直接
        跳过预测请求，改用物理外推，心跳不再阻塞
      · 恢复探测：半开状态放一次请求穿透，成功则关闭熔断，失败则重新计时
      · 状态持久化：写入 system_state.json，重启后自动恢复上次熔断状态

 【修复记录】：
   Fix-Timing  心跳节律时序漂移防御
               所有耗时计算（cycle_start、elapsed、sleep_time、熔断器计时）
               全部替换为 time.monotonic()，消除 NTP 时间同步跳变（树莓派
               运行中时钟可能突然跳几秒甚至几分钟）导致 elapsed 计算出负数
               或巨大值的风险。
               熔断器 _open_since 在内存中保持 monotonic 值，持久化时转换为
               wall-clock（time.time()）存储，_restore 恢复时再映射回 monotonic
               轴，保证重启后熔断剩余时间正确延续。

 【部署方式】
   systemd（推荐）：
     [Unit]
     Description=Adaptive Irrigation Control System
     After=network.target

     [Service]
     Type=simple
     ExecStart=/usr/bin/python3 /path/to/main.py
     Restart=on-failure
     RestartSec=30

     [Install]
     WantedBy=multi-user.target

   手动运行（调试）：
     python3 main.py

   夜间审计员（配合 crontab）：
     0 0 * * * /usr/bin/python3 /path/to/auditor.py >> /var/log/auditor.log 2>&1
=============================================================================
"""

import signal
import sys
import time
import logging
from pathlib import Path
from typing import Any

from config_manager import ConfigManager, ensure_data_files_exist, SYSTEM_CONSTANTS
from decision_brain import DecisionBrain, ZoneStatus

# ===========================================================================
# 日志配置（同时输出到控制台和日志文件）
# ===========================================================================
_LOG_FILE = Path(__file__).parent / "irrigation_system.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
    force=True,
)
logger = logging.getLogger("main_loop")

# ===========================================================================
# 全局状态
# ===========================================================================
_RUNNING = True                          # 主循环开关，由信号处理器置 False
_brain: "DecisionBrain | None" = None   # 持有 brain 引用，供信号处理器访问渗透哨兵


# ===========================================================================
# 区域状态标签（格式化日志用）
# ===========================================================================
_ZONE_LABELS = {
    ZoneStatus.SAFE_SLEEP:   "安全区",
    ZoneStatus.BATTLE_ZONE:  "战区",
    ZoneStatus.EMERGENCY:    "紧急区",
    ZoneStatus.SOAK_PENDING: "渗透等待",
    ZoneStatus.SENSOR_STALE: "传感器故障",
}


# ===========================================================================
# 预测模型熔断器（Circuit Breaker for Layer 4 Predictor）
# ─────────────────────────────────────────────────────────────────────────
# 解决的问题：
#   Phase 2 预测模型若因 OOM 或死机彻底宕机，主循环每 5 分钟都会死等
#   SHM_TIMEOUT_SEC（30s）。300 秒心跳中有 30 秒是纯粹的 IO 阻塞浪费。
#
# 工作状态机（三态）：
#   CLOSED（正常）  →  连续 FAIL_THRESHOLD 次心跳耗时超出阈值
#                  →  OPEN（熔断）：写 PREDICTOR_CIRCUIT_OPEN=True 到 cfg，
#                                   Layer 4 读到后直接跳过预测，用物理外推
#                  →  OPEN 持续 RESET_SEC 秒后进入 HALF_OPEN（半开探测）
#   HALF_OPEN（探测）→  清除 PREDICTOR_CIRCUIT_OPEN，放一次心跳穿透到 Layer 4
#                     →  耗时正常：→ CLOSED（恢复）
#                     →  耗时超标：→ OPEN（重新熔断，重置计时器）
#
# 超时判定：
#   run_cycle() 总耗时 > SHM_TIMEOUT_SEC + _CB_ELAPSED_GRACE（宽限量）
#   之所以用总耗时而非直接 hook Layer 4，是因为 decision_brain.py 不在
#   main.py 的直接控制内；宽限量容纳传感器读取、CSV 追加等正常开销。
#
# 持久化：
#   熔断状态写入 system_state.json，重启后自动恢复，避免重启即触发探测。
# ===========================================================================

_CB_FAIL_THRESHOLD = 3       # 连续超时次数触发熔断
_CB_RESET_SEC      = 3600    # 熔断持续时长（秒，1 小时后进入半开探测）
_CB_ELAPSED_GRACE  = 0       # Phase2 超时时 run_cycle 约等于 SHM_TIMEOUT，不能再额外放宽


_MIN_VALID_WALL_CLOCK_TS = 1704067200.0  # 2024-01-01 00:00:00 UTC
_WALL_CLOCK_WAIT_TIMEOUT_SEC = 180.0
_WALL_CLOCK_WAIT_INTERVAL_SEC = 3.0


def _wait_for_valid_wall_clock() -> None:
    """
    启动期等待系统 wall-clock 进入可信年份。

    openEuler 设备重启后可能先以 1970 时间启动 systemd 服务，随后 NTP 才同步。
    冷却时间、熔断恢复和试验记录都依赖 time.time() 的绝对时间戳；若在 1970
    先跑一轮，会把冷却剩余时间算成几十万小时。这里在进入任何决策前拦住。
    """
    deadline = time.monotonic() + _WALL_CLOCK_WAIT_TIMEOUT_SEC
    warned = False

    while _RUNNING:
        now = time.time()
        if now >= _MIN_VALID_WALL_CLOCK_TS:
            if warned:
                logger.info("[Main] 系统时间已同步，继续启动。")
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.critical(
                "[Main] 系统时间仍未同步，拒绝进入浇水决策；"
                "等待 systemd 重启或人工检查 NTP。"
            )
            sys.exit(1)

        if not warned:
            logger.warning(
                "[Main] 检测到系统时间不可信，等待 NTP 同步后再启动决策..."
            )
            warned = True
        time.sleep(min(_WALL_CLOCK_WAIT_INTERVAL_SEC, remaining))


class PredictorCircuitBreaker:
    """
    三态熔断器：保护主循环不被宕机的预测模型无限拖累。

    状态持久化在 system_state.json 的 "predictor_circuit" 键下，
    重启后自动恢复上次状态，避免刚启动就触发"半开探测"导致一轮超时。
    """

    _STATE_CLOSED    = "CLOSED"
    _STATE_OPEN      = "OPEN"
    _STATE_HALF_OPEN = "HALF_OPEN"

    def __init__(self, cfg: "ConfigManager") -> None:
        import json, os, shutil
        self._cfg       = cfg
        self._fail_count = 0
        self._state     = self._STATE_CLOSED
        self._open_since: float = 0.0
        self._json_ops  = (json, os, shutil)      # 存储供 _persist 使用
        self._restore()

    # ------------------------------------------------------------------

    def _state_path(self) -> Path:
        from config_manager import FILE_PATHS
        return FILE_PATHS["SYSTEM_STATE"]

    def _restore(self) -> None:
        """从 system_state.json 恢复上次熔断状态。
        
        注意：_open_since 存储的是持久化时的 wall-clock 时间戳（time.time()），
        恢复时转换为当前 monotonic 时间轴，保证重启后熔断剩余时间正确延续。
        """
        import json
        path = self._state_path()
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            cb = state.get("predictor_circuit", {})
            self._state      = cb.get("state",      self._STATE_CLOSED)
            self._fail_count = cb.get("fail_count",  0)
            # open_since 持久化为 wall-clock（time.time()），恢复时：
            # 计算"距上次熔断已过多少秒"，再映射到当前 monotonic 轴。
            wall_open_since  = cb.get("open_since", 0.0)
            elapsed_since    = max(0.0, time.time() - wall_open_since)
            self._open_since = time.monotonic() - elapsed_since
            if self._state != self._STATE_CLOSED:
                logger.info(
                    f"[CB] 恢复熔断状态: {self._state}  "
                    f"fail_count={self._fail_count}  "
                    f"已熔断 {elapsed_since/60:.1f} 分钟"
                )
        except Exception as e:
            logger.warning(f"[CB] 恢复熔断状态失败（忽略，从 CLOSED 重启）: {e}")

    def _persist(self) -> None:
        """将熔断状态合并写回 system_state.json（原子操作）。"""
        import json, os, shutil
        path = self._state_path()
        try:
            state: dict = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    state = json.load(f)
        except Exception:
            state = {}
        state["predictor_circuit"] = {
            "state":      self._state,
            "fail_count": self._fail_count,
            # open_since 存 wall-clock（time.time()），重启后恢复时可计算已熔断时长。
            # 运行时内存中保持 monotonic 值，_restore 负责两者之间的转换。
            "open_since": time.time() - (time.monotonic() - self._open_since),
        }
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            shutil.move(str(tmp), str(path))
        except OSError as e:
            logger.warning(f"[CB] 持久化熔断状态失败（非致命）: {e}")

    # ------------------------------------------------------------------

    def _timeout_threshold(self) -> float:
        """超时判定门槛 = SHM_TIMEOUT_SEC + 宽限量。"""
        shm_timeout = self._cfg.get_constant("SHM_TIMEOUT_SEC") or 30
        return shm_timeout + _CB_ELAPSED_GRACE

    def _last_predictor_probe(self) -> dict:
        import json
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state.get("predictor_last_probe", {})
        except Exception:
            return {}

    def before_cycle(self) -> None:
        """
        每次心跳前调用。
        · OPEN 且已超过 RESET_SEC → 切换到 HALF_OPEN，清除熔断标志，允许一次探测
        · OPEN 未超时 → 保持熔断，确保 PREDICTOR_CIRCUIT_OPEN=True 写入 cfg
        · CLOSED / HALF_OPEN → 不干预
        """
        now = time.monotonic()
        if self._state == self._STATE_OPEN:
            if now - self._open_since >= _CB_RESET_SEC:
                self._state = self._STATE_HALF_OPEN
                logger.info(
                    f"[CB] 熔断器进入半开探测（距上次熔断 "
                    f"{(now - self._open_since)/60:.0f} 分钟），"
                    f"本轮放行预测请求，观察耗时..."
                )
                # 清除熔断标志，让 Layer 4 能发出一次探测请求
                self._cfg.update({"PREDICTOR_CIRCUIT_OPEN": False}, persist=False)
                self._persist()
            else:
                # 仍在熔断中，确保 cfg 标志正确（可能被重载覆盖）
                self._cfg.update({"PREDICTOR_CIRCUIT_OPEN": True}, persist=False)

    def after_cycle(self, elapsed: float, cycle_no: int) -> None:
        """
        每次心跳后调用，根据实际耗时更新熔断状态机。

        · elapsed > 阈值 → 记录失败（CLOSED 累积；HALF_OPEN 立即重新熔断）
        · elapsed 正常：
          - HALF_OPEN → 确认恢复，关闭熔断器
          - CLOSED    → 重置连续失败计数
        """
        threshold = self._timeout_threshold()
        probe = self._last_predictor_probe()
        predictor_called = bool(probe.get("called"))
        predictor_success = bool(probe.get("success"))
        timed_out = predictor_called and elapsed >= threshold

        if timed_out:
            self._fail_count += 1
            if self._state == self._STATE_HALF_OPEN:
                logger.warning(
                    f"[CB] 半开探测超时（{elapsed:.1f}s > {threshold:.0f}s），"
                    f"重新熔断，{_CB_RESET_SEC//60} 分钟后再次探测。"
                )
                self._trip(cycle_no)
            elif self._state == self._STATE_CLOSED:
                logger.warning(
                    f"[CB] 预测请求超时 #{self._fail_count}/{_CB_FAIL_THRESHOLD} "
                    f"（{elapsed:.1f}s > {threshold:.0f}s）[Cycle #{cycle_no:04d}]"
                )
                if self._fail_count >= _CB_FAIL_THRESHOLD:
                    self._trip(cycle_no)
                else:
                    self._persist()
        else:
            if self._state == self._STATE_HALF_OPEN:
                if not predictor_called:
                    logger.info("[CB] 半开探测本轮未进入 Phase2（安全区/故障保护等），保持 HALF_OPEN 等待下一次真实探测。")
                    self._persist()
                    return
                if not predictor_success:
                    logger.warning("[CB] 半开探测未获得有效 Phase2 响应，重新熔断。")
                    self._trip(cycle_no)
                    return
                logger.info(
                    f"[CB] 半开探测成功（{elapsed:.1f}s ≤ {threshold:.0f}s），"
                    f"预测模型已恢复，熔断器关闭。"
                )
                self._state      = self._STATE_CLOSED
                self._fail_count = 0
                self._cfg.update({"PREDICTOR_CIRCUIT_OPEN": False}, persist=False)
                self._persist()
            elif self._state == self._STATE_CLOSED and self._fail_count > 0:
                logger.debug(f"[CB] 心跳耗时恢复正常，连续失败计数清零。")
                self._fail_count = 0
                self._persist()

    def _trip(self, cycle_no: int) -> None:
        """触发熔断：进入 OPEN 状态，写标志到 cfg。"""
        self._state      = self._STATE_OPEN
        self._open_since = time.monotonic()   # monotonic：不受 NTP 影响，熔断计时准确
        self._cfg.update({"PREDICTOR_CIRCUIT_OPEN": True}, persist=False)
        self._persist()
        logger.error(
            f"[CB] ⚡ 熔断器触发 [Cycle #{cycle_no:04d}]！"
            f"连续 {self._fail_count} 次超时，预测模型标记为 DOWN。\n"
            f"       Layer 4 将在接下来 {_CB_RESET_SEC//60} 分钟内跳过预测，"
            f"改用物理外推，不再白白等待 SHM 超时。\n"
            f"       {_CB_RESET_SEC//60} 分钟后自动发起半开探测。"
        )

    @property
    def is_open(self) -> bool:
        return self._state == self._STATE_OPEN


# ===========================================================================
# 优雅退出处理
# ===========================================================================

def graceful_shutdown(signum: int, frame: Any) -> None:
    """
    捕获 SIGINT（Ctrl+C）和 SIGTERM（kill / systemd stop），安全退出。

    执行顺序：
      1. 置 _RUNNING = False，通知主循环在本次 _interruptible_sleep 内立即醒来
      2. ⚠️ 强制关闭水泵 GPIO（硬件保护，部署时必须取消注释）
      3. 若渗透哨兵尚存，打印剩余等待时间（已持久化，下次启动自动恢复）
      4. sys.exit(0) 清洁退出

    为什么必须关 GPIO：
      程序被 kill 时，若水泵正在通电（run_cycle 刚调用 execute_pump），
      Python 进程死掉但 GPIO 引脚仍维持高电平，水泵会永远开下去直到水漫金山。
    """
    global _RUNNING, _brain

    logger.warning(f"[Main] 收到终止信号 ({signum})，执行优雅退出...")
    _RUNNING = False

    # ── ⚠️ 硬件保护：强制关闭水泵 GPIO ────────────────────────────────
    # 部署时取消注释，将 PUMP_PIN 替换为实际 BCM 引脚编号
    # try:
    #     import RPi.GPIO as GPIO
    #     GPIO.setmode(GPIO.BCM)
    #     GPIO.setup(PUMP_PIN, GPIO.OUT)
    #     GPIO.output(PUMP_PIN, GPIO.LOW)   # 强制拉低，关闭水泵
    #     GPIO.cleanup()
    #     logger.info("[Main] 水泵 GPIO 已强制拉低，硬件安全复位完成。")
    # except Exception as e:
    #     logger.error(f"[Main] GPIO 复位失败（非致命，请人工检查水泵状态）: {e}")
    # ──────────────────────────────────────────────────────────────────

    # 若渗透哨兵尚存，提醒人工确认植物是否得到足够水量
    if _brain is not None and _brain._pending_soak is not None:
        soak      = _brain._pending_soak
        remaining = soak.remaining_sec
        logger.warning(
            f"[Main] ⚠️ 退出时存在未完成的渗透哨兵！\n"
            f"         本次浇水: {soak.water_sec:.1f}s  "
            f"浇前湿度: {soak.pre_humidity:.1f}%  "
            f"剩余渗透: {remaining:.0f}s（约 {remaining/60:.1f} 分钟）\n"
            f"         哨兵状态已持久化至 system_state.json，"
            f"下次启动将自动恢复并完成采样。"
        )

    logger.info("[Main] 系统已安全退出。")
    sys.exit(0)


# 注册信号回调
signal.signal(signal.SIGINT,  graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


# ===========================================================================
# 辅助：可中断的分段睡眠
# ===========================================================================

def _interruptible_sleep(seconds: float) -> None:
    """
    可被信号中断的分段睡眠，每 1 秒检查一次 _RUNNING 标志位。

    为什么不直接 time.sleep(300)：
      信号处理器置 _RUNNING=False 后，主循环仍被卡在 sleep 里，
      最多需要等待 5 分钟才能响应退出请求。
      分段睡眠确保收到信号后最多 1 秒内响应。
    """
    # monotonic：不受 NTP 跳变影响，睡眠窗口计算永远正确。
    end = time.monotonic() + seconds
    while _RUNNING and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


# ===========================================================================
# 辅助：循环摘要日志
# ===========================================================================

def _log_cycle_summary(result: Any, cycle_no: int, elapsed: float) -> None:
    """
    格式化打印一次决策循环的摘要。

    日志级别策略（方便告警系统接入）：
      EMERGENCY    → logger.critical（最高优先级，告警系统应捕获）
      BATTLE_ZONE  → logger.warning（正在主动干预，可能需要关注）
      SOAK_PENDING → logger.info（渗透等待，正常运行状态）
      SAFE_SLEEP   → logger.info（安全休眠，正常运行状态）
    """
    zone       = result.zone
    zone_label = _ZONE_LABELS.get(zone, zone.name)
    action_str = f"浇水 {result.action_sec:.1f}s" if result.action_sec > 0 else "不浇水"
    humidity   = f"{result.reading.humidity:.1f}%"
    vpd        = f"{result.reading.vpd:.3f}kPa"

    plan_str = ""
    if result.chosen_plan and result.chosen_plan.cost_J < float("inf"):
        plan_str = (
            f"  方案: {result.chosen_plan.label}"
            f" J={result.chosen_plan.cost_J:.4f}"
        )

    msg = (
        f"[Cycle #{cycle_no:04d}] {zone_label} | "
        f"H={humidity} VPD={vpd} | {action_str}{plan_str} | "
        f"耗时 {elapsed:.1f}s"
    )

    if zone == ZoneStatus.EMERGENCY:
        logger.critical(msg)
    elif zone == ZoneStatus.SENSOR_STALE:
        logger.critical(msg)
    elif zone == ZoneStatus.BATTLE_ZONE:
        logger.warning(msg)
    else:
        logger.info(msg)

    # notes 包含方案标签、J 值、渗透哨兵状态等详情，降为 DEBUG 减少日志噪音
    if result.notes:
        logger.debug(f"         {result.notes}")


# ===========================================================================
# 主心跳循环
# ===========================================================================

def main() -> None:
    global _brain

    logger.info("=" * 65)
    logger.info("自适应闭环灌溉系统 (Adaptive Irrigation Control) 启动")
    logger.info("=" * 65)

    _wait_for_valid_wall_clock()

    # ── Step 1：初始化依赖文件 ─────────────────────────────────────────
    # 创建 system_state.json / pattern_memory.json 等运行状态文件（若不存在）
    # phase1_data.json 和 evolving_params.json 不在此创建（前者由 Phase 1 写入，
    # 后者由 ConfigManager 负责），缺失时系统会在 Step 2 给出明确错误。
    logger.info("[Main] Step 1 — 检查并初始化系统数据文件...")
    ensure_data_files_exist()

    # ── Step 2：实例化配置管家（触发 Phase 0 物理基因注入）────────────
    # 若 phase1_data.json 不存在：ConfigManager 抛出 RuntimeError，在此处
    # 清晰报错并退出，而非在循环中引发难以定位的运行时崩溃。
    logger.info("[Main] Step 2 — 初始化配置管家（Phase 0 破壳）...")
    try:
        cfg = ConfigManager()
    except RuntimeError as e:
        logger.critical(f"[Main]启动失败，配置管家初始化错误:\n{e}")
        sys.exit(1)

    logger.info(
        f"[Main] ConfigManager 就绪 | "
        f"FC={cfg.FC}%  TL={cfg.TARGET_LOW}%  "
        f"战区={(cfg.FC - cfg.TARGET_LOW):.2f}%  |  "
        f"K_p={cfg.K_P}  α={cfg.ALPHA}  β={cfg.BETA}  γ={cfg.GAMMA}"
    )

    # ── Step 3：实例化决策大脑 ────────────────────────────────────────
    # DecisionBrain.__init__ 内部调用 _restore_pending_soak()，
    # 从 system_state.json 恢复断电前可能未完成的渗透哨兵。
    logger.info("[Main] Step 3 — 初始化决策大脑（检查断电渗透哨兵）...")
    _brain = DecisionBrain(cfg)
    logger.info("[Main] DecisionBrain 就绪。")

    # ── Step 4：读取心跳周期 ──────────────────────────────────────────
    interval_sec: float = cfg.get_constant("MAIN_LOOP_INTERVAL_SEC") or 300.0
    logger.info(
        f"[Main] 心跳周期: {interval_sec:.0f}s（{interval_sec/60:.1f} 分钟）"
    )

    # ── Step 4b：初始化预测模型熔断器 ─────────────────────────────────
    circuit_breaker = PredictorCircuitBreaker(cfg)
    logger.info(
        f"[Main] 熔断器就绪 "
        f"（触发阈值: 连续 {_CB_FAIL_THRESHOLD} 次超时 "
        f">{(cfg.get_constant('SHM_TIMEOUT_SEC') or 30) + _CB_ELAPSED_GRACE}s，"
        f"熔断时长: {_CB_RESET_SEC//60} 分钟）"
    )

    logger.info("[Main] 系统就绪，进入主循环。")
    logger.info("=" * 65)

    cycle_no = 0

    # ── Step 5：永不停止的心跳循环 ────────────────────────────────────
    while _RUNNING:
        cycle_start = time.monotonic()   # monotonic：NTP 跳变不影响心跳节律
        cycle_no   += 1

        try:
            # 每轮开始前重载配置管家：读取审计员在夜间可能已更新的参数基因。
            # load() 只读盘，不重建 brain；cfg 和 brain 共享同一对象引用，
            # 参数变更对 brain 内部立即生效（下一个属性访问即返回新值）。
            cfg.load()

            # 熔断器前置检查：OPEN 且未到复位时间 → 写 PREDICTOR_CIRCUIT_OPEN=True，
            # Layer 4 将直接跳过预测；OPEN 且到期 → 切换 HALF_OPEN，清除标志探测一次。
            circuit_breaker.before_cycle()

            # 执行核心六层决策逻辑（Layer 0 → Layer 6）
            result = _brain.run_cycle()

            # 记录本轮耗时并打印摘要
            elapsed = time.monotonic() - cycle_start
            _log_cycle_summary(result, cycle_no, elapsed)

        except Exception as e:
            # ── 全局异常兜底 ──────────────────────────────────────────
            # I²C 传感器偶发失败、/dev/shm 文件锁竞争、CSV 解析异常等，
            # 在嵌入式设备上是常态，绝对不能让 while 循环因此崩溃退出。
            # 打印完整调用栈（exc_info=True）便于事后排查，然后等待下一轮重试。
            elapsed = time.monotonic() - cycle_start
            logger.error(
                f"[Main] ⚠️ Cycle #{cycle_no:04d} 发生未捕获异常（进程保持运行）: {e}",
                exc_info=True,
            )
            logger.info(
                f"[Main] 本轮耗时 {elapsed:.1f}s，"
                f"等待 {max(interval_sec - elapsed, 0):.0f}s 后重试..."
            )

        # 熔断器后置更新：根据本轮实际耗时更新状态机
        circuit_breaker.after_cycle(elapsed, cycle_no)

        # ── 防时间漂移的精准睡眠 ──────────────────────────────────────
        # 减去本轮已消耗的时间，确保下一轮严格在 interval_sec 后触发。
        # 若本轮耗时已超过 interval_sec（预测模型超时等极端情况），
        # sleep_time 为 0，跳过本次睡眠，立即进入下一轮，并打印 WARNING。
        sleep_time = interval_sec - elapsed

        if sleep_time > 0:
            logger.debug(
                f"[Main] 耗时 {elapsed:.2f}s，"
                f"休眠 {sleep_time:.2f}s 对齐下一心跳..."
            )
            _interruptible_sleep(sleep_time)
        else:
            logger.warning(
                f"[Main] ⚠️ Cycle #{cycle_no:04d} 耗时 {elapsed:.2f}s "
                f"> 心跳周期 {interval_sec:.0f}s，直接进入下一轮。"
                f"（可能原因：预测模型响应超时 / 传感器读取阻塞）"
            )

    logger.info("[Main] 主循环已正常退出。")


# ===========================================================================
# 脚本入口
# ===========================================================================

if __name__ == "__main__":
    main()
