# 模块迁移规划

> 阶段：Phase 1-B，仅做资产分类与导入规划。本文不读取、复制、移动或修改生产/历史业务源码。

## 导入原则

1. 新仓库从独立治理和资产清单开始；openEuler 与旧 `auto_water` 都是来源，不是待整体搬迁的根目录。
2. 每个可导入模块必须有完整来源 manifest、公开性审查和独立 Git 提交；不发生直接 merge。
3. 首次导入保留现有模块和设备边界，不进行包化、去重或行为优化。
4. `active` 仅表示生产服务正在运行，不表示代码已验证、可公开或已导入。

## 设备版本关系

`soil1`、`soil2`、`soil3` 是演进关系，而非同版本的平行模块：`soil1 → soil2 → soil3`。其中 `soil3` 是当前功能最完整的 **reference implementation**；它是后续通用能力分析的优先来源，不是将配置或控制参数复制给其他设备的模板。

首次导入保留每个设备目录、状态边界和历史用途。只有完成来源比对、逐项安全审查与回放验证后，才可将 soil3 中不依赖设备私有配置的能力提议为公共模块。

## 当前模块总表

| 模块 | 当前来源路径 | 当前作用 | 来源代码状态 | 目标位置 | 是否进入新仓库 |
| --- | --- | --- | --- | --- | --- |
| MQTT ingestion | openEuler `/root/agent/pi_agnet/mqtt_direct_gauss.py` | MQTT 事件接收、规范传感/灌溉事件入库 | production active；与历史 GitHub 有差异 | `services/ingestion/` | 是，待受控导入 |
| 数据库 schema | openEuler `/root/agent/pi_agnet/irrigation_schema.sql` | 数据表、索引和数据契约 | 生产 active contract；与历史 GitHub 核心版本同步 | `services/ingestion/schema/` | 是，待受控导入 |
| Phase3 soil1 | openEuler `/root/water/phase3/soil1/` | 早期自动安全控制与流程验证记录 | production active；早期实现 | `services/control/phase3/soil1/` | 是，按设备保留 |
| Phase3 soil2 | openEuler `/root/water/phase3/soil2/` | 增强控制、预测与人工交互记录 | production active；中期实现 | `services/control/phase3/soil2/` | 是，按设备保留 |
| Phase3 soil3 | openEuler `/root/water/phase3/soil3/` | 最完整自动安全控制、执行层、审计与反馈实现 | production active；当前 reference implementation；关键文件与历史 GitHub 有差异 | `services/control/phase3/soil3/` | 是，优先单模块导入和后续公共能力分析 |
| Phase3 soil_test | openEuler `/root/water/phase3/soil_test/` | 测试设备控制路径 | production inactive | `services/control/phase3/soil_test/` | 是，实验设备代码 |
| Phase1 | openEuler `/root/water/phase1_test/` | 初始响应学习与参数交接前流程 | 服务当前 inactive；仍是生产资产 | `services/control/phase1/` | 是，待迁移 |
| Phase handoff | openEuler `/root/water/phase_handoff/`、关联 timers | Phase1→Phase3 生命周期交接 | 部分 timer 可见 | `services/control/handoff/` | 是，随控制模块导入 |
| Web | openEuler `/root/water/web/`；历史 `auto_water/src/web/` | 状态展示；正式直控路由禁用 | production active；核心版本曾同步 | `web/` | 是，待迁移 |
| Phase2 predictor / shadow train | openEuler `/root/water/wyc_training/` | 预测候选、影子训练与评估 | production active；核心代码已对照同步 | `experiments/phase2/` | 是，研究模块 |
| Plant Agent | openEuler `/root/agent/plant_agent/` | 状态构建、趋势/视觉/推理、事件和测试 | 生产代码存在；历史 GitHub 未完整收录 | `agents/plant_agent/` | 是，待迁移 |
| OpenClaw 工具与规则 | openEuler `/root/.openclaw/workspace/tools/`、规则/人格文件 | 受限交互、人工确认与事件辅助 | 部分 watcher active；仅可白名单导入 | `agents/openclaw/` | 待审核后部分进入 |
| OpenClaw runtime | openEuler `/root/.openclaw/workspace/{memory,state,sessions,media,...}` | 会话、记忆、账号、缓存与运行产物 | production runtime state | 无 | 否，永不进入 |
| systemd 服务/定时器 | openEuler `/etc/systemd/system/` 和 Unit 入口 | 服务编排和定时任务 | 多服务 active | `deployment/systemd/` | 是，仅无密 `.example` |
| ESP32 firmware | 当前生产基线未定位可追溯源码 | 命令消费、Relay/Pump 执行与回执 | 来源/版本/构建信息缺失 | `firmware/` | 暂缓 |
| hardware 资料 | 现场与历史资料，未形成受控清单 | 接线、BOM、校准、安全说明 | 待盘点 | `hardware/` | 待审核 |
| 数据集与数据导出 | GaussDB、MQTT、历史 CSV | 实验和分析数据 | 生产/运行数据 | `dataset/` | 仅 schema、脱敏样例、manifest |
| 模型权重与运行产物 | openEuler Phase2 运行目录 | 模型参数与实验输出 | 有 hash 基线，不公开导入 | `manifest/models/` | 仅 manifest/模型卡 |
| 历史 GitHub `auto_water` | GitHub 与本地快照 | 比较基线、已整理 soil3 工程 | 非生产全量镜像 | `manifest/sources/` | 仅来源说明/对照记录 |
| 备份/补丁/临时目录 | `.bak*`、backup、CSV、日志、spool | 审计和恢复线索 | 非实现源 | `manifest/archive/` | 仅索引 |

## 模块依赖边界

```text
firmware → MQTT → services/ingestion → canonical data contract
                                       ├→ services/control (Phase1 / Phase3 / handoff)
                                       ├→ web
                                       ├→ experiments/phase2
                                       └→ agents/plant_agent → agents/openclaw
```

- Phase2 仅提供候选或实验结果，不成为执行权威。
- Agent 与 Web 仅形成解释、建议或受限请求；本阶段不改变既有泵权限。
- firmware 是设备执行边界的必要资产；未确认来源前不创建伪实现。

## 资产分类

### A. 计划进入 GitHub 源码或公开工程资料

| 类别 | 条件 | 目标 |
| --- | --- | --- |
| Python/shell 源码 | 完整 hash、无密、无运行数据、单模块审查通过 | `services/`、`agents/`、`experiments/`、`scripts/` |
| SQL schema/migration | 不含真实数据与凭据 | `services/ingestion/schema/` |
| Web 模板与静态资源 | 不含账号、密钥或隐私媒体 | `web/` |
| OpenClaw tools/rules/templates | 白名单、无账号/群组/运行路径实值 | `agents/openclaw/` |
| systemd Unit 模板 | 删除真实部署值与环境秘密 | `deployment/systemd/*.example` |
| ESP32 源码 | 来源、设备归属、构建信息和许可已确认 | `firmware/` |
| 测试、回放、脱敏样例 | 不触发真实设备且不含生产数据 | `tests/`、`dataset/examples/` |

### B. 只保留 manifest、模型卡或脱敏摘要

| 资产 | 保留内容 | 不保留内容 |
| --- | --- | --- |
| 模型权重 | hash、大小、代码提交、数据集版本、指标、审批状态 | 权重文件 |
| 实验数据 | schema、查询定义、脱敏样例、版本和统计 | 生产原始传感/用户/图像数据 |
| 运行 state | 字段 schema、状态机定义、样例结构 | 真实 state JSON、设备学习参数 |
| 数据库 | schema、migration、表契约 | dump、连接串、生产行数据 |
| MQTT | Topic/payload 合同、安全策略摘要 | 原始消息流、真实 Broker 配置/凭据 |
| 固件二进制 | 构建 hash、版本、设备映射和测试结果 | `.bin`，除非另行批准 |
| 历史备份 | 来源、日期、用途、是否部署过的索引 | 备份内容与压缩包 |

### C. 永不进入 GitHub

- 密钥、Token、密码、证书、连接串、私有地址、Wi-Fi/Broker 真实凭据；
- OpenClaw `memory/`、`state/`、`sessions/`、`media/`、聊天记录、账号/群组信息；
- 生产日志、PID、缓存、spool、运行 JSON、数据库 dump、原始 MQTT、未脱敏 CSV；
- 虚拟环境、依赖缓存、`__pycache__`、`.pyc`、临时目录；
- 未经授权的媒体、设备标识和没有来源/许可证/安全审查记录的代码或二进制。

## 导入状态定义

| 状态 | 含义 |
| --- | --- |
| 待迁移 | 来源存在并符合目标范围，但尚无完整 manifest 或导入确认 |
| 待审核 | 可能有价值，需审查隐私、账号、权限、许可证或安全边界 |
| 暂缓 | 目标位置已确定，但来源/版本/构建证据不足 |
| 仅 manifest | 不进入源码树，只记录可追溯描述 |
| 永不进入 | 无论技术价值如何，都不允许进入公开仓库 |

本表是后续选择首个 production import 范围的依据，不构成导入授权。
