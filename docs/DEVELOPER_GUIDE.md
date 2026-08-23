# 开发者指南

本指南面向希望阅读、验证或扩展 `Plant-Intelligence-Agent` 的开发者。当前仓库是经来源审计后的工程基线，不是对生产环境的镜像，也不是一键部署包。

## 1. 项目快速理解

`Plant-Intelligence-Agent` 是一个面向真实物理环境的植物智能体系统：设备采集状态，服务记录规范数据，控制层在安全约束下决定是否执行养护动作，Agent 与实验模块提供受限的状态理解、分析和反馈能力。

它不是“湿度低就开泵”的简单自动浇水程序。物理执行、数据质量、状态机、审计记录和人工交互都属于系统边界。

### 节点演进

```text
soil1（早期采集、Phase1 探索、控制流程验证）
  → soil2（预测、控制、人工交互增强）
    → soil3（当前 reference implementation）
```

`soil3` 是当前功能最完整、后续公共能力设计时优先参考的实现；`soil1`、`soil2` 保留为历史和设备适配记录。**不要把 soil3 的阈值、Topic、时长、学习参数或运行 state 复制到其他设备。**

## 2. 开发目录导航

| 目录 | 作用 | 从哪里开始阅读 |
| --- | --- | --- |
| `services/` | MQTT 接入、Phase1、Phase3 与交接控制 | `services/ingestion/mqtt_direct_gauss.py`、`services/control/` |
| `agents/` | Plant Agent 状态、分析、事件与受审查 OpenClaw 工具 | `agents/plant_agent/plant_state_builder.py` |
| `experiments/` | Phase2 预测、影子训练、评估研究代码 | `experiments/phase2/README.md` |
| `web/` | Web 展示与来源测试入口 | `web/water_web.py` |
| `firmware/` | ESP32 固件与设备执行证据的预留位置 | `firmware/README.md`；当前没有已验证固件源码 |
| `deployment/` | 无密部署模板的预留位置 | `deployment/README.md`；当前未导入生产 Unit |
| `config/` | 公开配置模板与未来设备 profile 位置 | `config/environment.example`、`config/phase2/config.example.json` |
| `tests/` | 从来源保留的测试 | `tests/README.md` |
| `docs/`、`manifest/` | 架构、来源、导入和安全边界记录 | `docs/ARCHITECTURE.md`、`manifest/import-history.yml` |

### 按需求定位

| 如果要理解或修改 | 优先查看 | 不要顺手改动 |
| --- | --- | --- |
| soil3 浇水决策 | `services/control/phase3/soil3/decision_brain.py` | MQTT Topic、阈值、执行时长或安全状态，除非完成独立安全审查 |
| soil3 控制启动与周期 | `services/control/phase3/soil3/main.py`、`runtime_io.py` | systemd 与生产运行方式 |
| 传感数据接入 | `services/ingestion/mqtt_direct_gauss.py`、`services/ingestion/schema/` | 生产 Topic、SQL 语义或数据库连接约定 |
| 植物状态理解 | `agents/plant_agent/plant_state_builder.py`、`agents/plant_agent/analytics/` | Agent 权限边界；Agent 不是泵执行权威 |
| Phase2 预测研究 | `experiments/phase2/` | 将预测结果直接接入执行器 |
| OpenClaw 交互 | `agents/openclaw/tools/` | 账号、session、memory 或运行配置 |

## 3. 开发流程

1. 从最新 `main` 创建目标明确的分支，例如 `feature/soil3-new-decision`。
2. 先阅读相关模块 README、`docs/ARCHITECTURE.md` 和来源记录；确认变更是否涉及真实水泵、MQTT、数据库或设备配置。
3. 以最小范围修改代码；控制逻辑、人工旁路、Topic 和参数变更必须与普通文档/重构提交分开。
4. 在隔离环境中执行关联的本地测试或静态检查。不得默认连接生产 MQTT、GaussDB、OpenClaw 账号或真实设备。
5. 更新受影响的文档、来源 manifest 或实验记录。
6. 采用清晰的提交前缀：`feat:`、`fix:`、`refactor:`、`docs:`；研究试验使用 `experiment:`。
7. 创建 Pull Request，并说明：目标、影响模块、测试证据、数据/配置来源，以及是否触及任何控制安全边界。

### 控制相关变更的额外门槛

任何可能改变浇水动作的 PR 都必须明确回答：谁产生决策、谁执行 MQTT 发布、哪些安全门检查生效、如何在无真实设备条件下验证，以及是否影响 soil3 以外的设备。没有这些信息，不应合并或部署。

## 4. 当前限制

- 本仓库不是一键部署版本；没有生产 systemd Unit、真实设备 profile 或生产凭据。
- 真实运行依赖 ESP32、MQTT Broker、openGauss/gsql、openEuler 环境，以及部分 OpenClaw 运行时。
- ESP32 firmware 尚未取得可追溯公开源码和构建证据。
- 模型权重、原始数据、运行 state、日志、账号、token 和生产配置不在仓库中。
- 已导入测试尚未在新目录结构的隔离环境中完整执行；“代码存在”不等于“生产行为已复现”。

进一步的架构关系见 [ARCHITECTURE.md](ARCHITECTURE.md)，实际数据路径见 [DATA_FLOW.md](DATA_FLOW.md)，soil3 的开发入口见 [SOIL3_DEVELOPMENT.md](SOIL3_DEVELOPMENT.md)。
