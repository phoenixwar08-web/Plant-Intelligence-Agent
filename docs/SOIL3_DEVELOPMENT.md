# soil3 开发说明

`soil3` 是当前项目迭代最先进、功能最完整的 **reference implementation**。新能力的设计讨论通常先以 soil3 为参考，但它不是可以直接复制到 soil1、soil2 或其他设备的配置模板。

## 当前覆盖能力

| 能力 | 主要位置 | 当前含义 |
| --- | --- | --- |
| Phase1 水分档案探索 | `services/control/phase1/water-test-soil3.py` | 初始响应学习和交接前流程的来源实现 |
| Phase2 预测与影子训练 | `experiments/phase2/` | 预测候选、校准、评估与 shadow training；不成为执行权威 |
| Phase3 自动控制 | `services/control/phase3/soil3/` | 当前最完整的自动安全决策、执行、审计与反馈实现 |
| 植物状态理解 | `agents/plant_agent/plant_state_builder.py` | 面向 soil3 等设备构建传感、控制、健康和学习摘要 |
| OpenClaw 交互 | `agents/openclaw/tools/openclaw_soil3_water.py` 等 | 受限人工交互与审计辅助；不是新的自动泵权威 |

## 核心文件与修改入口

| 目标 | 文件 | 修改前必须确认 |
| --- | --- | --- |
| 控制周期与信号处理 | `services/control/phase3/soil3/main.py` | 是否影响服务生命周期、周期、退出或恢复行为 |
| 决策、安全门与执行计划 | `services/control/phase3/soil3/decision_brain.py` | 是否改变触发条件、时长、MQTT 发布或人工旁路 |
| 配置与参数读取 | `services/control/phase3/soil3/config_manager.py` | 参数是否为设备私有、是否需要来源/实验记录 |
| 数据/状态读取适配 | `services/control/phase3/soil3/runtime_io.py` | 是否改变规范数据来源、状态路径或错误处理 |
| 审计与安全记录 | `services/control/phase3/soil3/auditor.py` | 是否保留完整可审计证据 |
| 自适应证据与经验验证 | `adaptive_evidence.py`、`experience_validation.py` | 是否把未验证经验错误提升为可执行规则 |
| 植物状态语义 | `agents/plant_agent/plant_state_builder.py` | 是否改变状态契约、数据质量或 Agent 上下文 |
| MQTT 接入与数据契约 | `services/ingestion/mqtt_direct_gauss.py` | 是否改变 Topic、时间修正、规范表或审计语义 |

## 建议阅读顺序

1. [ARCHITECTURE.md](ARCHITECTURE.md) 与 [DATA_FLOW.md](DATA_FLOW.md)；
2. `services/control/phase3/soil3/main.py`，了解周期和 `DecisionBrain` 初始化；
3. `decision_brain.py` 中的 `SensorLayer`、`PhysicsLayer`、`GateLayer`、`Phase2Predictor`、`CostCourt`、`ActuatorLayer` 与 `DecisionBrain`；
4. `auditor.py`、`adaptive_evidence.py`、`experience_validation.py`，了解审计与学习证据；
5. `plant_state_builder.py` 与对应测试，理解系统向 Agent 暴露的状态；
6. 最后阅读 OpenClaw 工具。不要从 OpenClaw 工具反推 Phase3 权限。

## soil3 与其他节点的区别

- **soil1**：以基础采集、Phase1 探索和控制流程验证为主，保留早期实现和实验价值。
- **soil2**：在 soil1 基础上增加预测、控制与人工交互能力，仍保留自身设备状态和适配边界。
- **soil3**：拥有当前最完整的 Phase1/Phase2/Phase3、审计、反馈、OpenClaw 与 Agent 接口组合，是公共化设计的优先参考来源。

这三个目录不是要同步修改的副本。对 soil3 的变更只有在功能、测试、来源和安全边界均已验证后，才能单独提议抽取为公共模块。

## 修改规则

1. 先确认问题属于传感接入、状态理解、决策、执行、审计还是设备端；不要跨层猜测。
2. 不要直接修改生产阈值、泵时长、MQTT Topic、运行 state 或数据库数据来验证想法。
3. 不要把 soil3 的学习参数、设备配置或运行状态复制给 soil1/soil2。
4. 控制变更必须与普通重构分开提交，并说明泵权限、门控位置和无设备测试证据。
5. Phase2、Agent、Web 和 OpenClaw 的输出只能是候选、事件或受限请求；最终自动泵权威仍是 Phase3。

## 当前限制

本仓库的 soil3 代码是来源基线，尚未作为独立部署包验证。真实运行还依赖 openEuler、MQTT、openGauss/gsql、设备私有配置、运行 state 与 ESP32 固件；后两项不会随本仓库提供。对真实水泵的改动必须在独立安全审查和部署流程中进行。
