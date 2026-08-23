# Plant-Intelligence-Agent

植物智能体系统的正式工程主线。

本仓库面向真实物理环境中的 Plant Agent：传感器感知植物状态，边缘控制服务在安全约束内执行养护动作，智能体、实验模型和交互层提供受限的分析、建议与反馈学习能力。

它**不是**旧 `auto_water` 仓库的整理版，也不是 openEuler 生产目录的镜像。旧仓库、生产环境、历史实验代码和固件资产均是受控来源；本仓库通过可追溯的逐模块导入形成新的长期主线。

## 当前阶段

当前处于 Phase 1：受控资产整理与来源导入。

- 已建立：仓库边界、来源说明、目标架构、导入政策、资产 manifest 与忽略规则。
- 已受控整理：部分无密源码基线及其说明；未提交、未部署，不改变任何生产行为。
- 尚未导入：模型权重、固件源码、数据库数据、运行配置或 OpenClaw 运行状态。
- 未执行：生产部署、服务修改、MQTT 修改或数据库修改。

## 节点演进与参考实现

`soil1`、`soil2`、`soil3` 不是三个同版本的并行产品线，而是当前已知的控制能力演进记录：

```text
soil1（早期验证） → soil2（增强迭代） → soil3（当前参考实现）
```

- `soil1` 保留基础采集、Phase1 探索和控制流程验证的历史价值；
- `soil2` 保留预测、控制和人工交互的增强记录；
- `soil3` 是功能最完整、迭代最先进的 **reference implementation**，后续识别通用能力时优先以它为设计参考；
- 这不是把其他设备升级或复制为 soil3 的授权。设备阈值、时长、Topic、学习状态和部署配置仍必须逐设备保留与审查。

## 目标架构

```text
ESP32 → MQTT → services/ingestion → canonical data store
                                  ↓
                         services/control (Phase1 / Phase3)
                                  ↓
                             device actuation

agents/ and experiments/ provide constrained analysis and candidates;
they are not direct pump authorities.
```

未来目标目录见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。来源与维护边界见 [`docs/PROJECT_ORIGIN.md`](docs/PROJECT_ORIGIN.md)。

## 公开仓库使用边界

首次公开版本用于阅读架构、审查来源基线和开展离线研究；它不是可直接连接真实设备的部署包。依赖范围、已知缺口与本地运行限制见 [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md)，脱敏变量与 Phase2 配置示例见 [`config/`](config/)。

## 安全声明

生产系统的泵控制与设备执行边界仍在审计中。MQTT 发布成功不等于继电器、水泵或水路已物理执行。任何未来导入都必须保持生产行为不变，并通过来源 hash、静态检查和历史回放建立可审计记录。

## 维护原则

1. 生产环境是行为事实源，GitHub 是受控工程主线，不自动覆盖生产。
2. 每个模块以来源 manifest、导入提交和验证结果追溯。
3. 工程服务、Agent、实验代码、数据和运行状态相互隔离。
4. 密钥、生产数据、运行状态、模型权重和设备私有配置不进入 Git。
5. 任何直接影响水泵的逻辑变更必须独立审查，不能混入来源导入提交。
