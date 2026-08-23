# 首次代码导入路线图

> 本路线图只规划后续提交，不执行复制、移动、merge、提交、推送或部署。\
> 除 Commit 1 外，每一个阶段都需要用户对具体来源范围再次确认。

## 前置条件

- 新仓库治理文档、来源边界和忽略规则已建立；
- 每个候选模块在 `manifest/` 中有完整来源文件清单与 SHA-256；
- 密钥、运行产物、模型/固件二进制已排除；
- 导入动作不会修改 openEuler、旧 `auto_water` 或任何生产服务。

## Commit 1：基础目录与文档

| 项目 | 规划 |
| --- | --- |
| 提交类型 | `docs: initialize Plant-Intelligence-Agent governance baseline` |
| 导入内容 | README、项目来源、范围、架构、模块/目录映射、导入政策、asset manifest、忽略规则 |
| 来源 | 新仓库 Phase 1 文档；生产基线元数据，不含生产文件内容 |
| 风险 | 低；避免将规划误写为生产证明 |
| 人工确认 | 已确认初始化；提交/推送前仍需检查目标 GitHub remote 状态 |
| 明确不含 | 业务源码、配置实值、数据、权重、固件或 runtime state |

## Commit 2：数据采集与数据契约

| 项目 | 规划 |
| --- | --- |
| 提交类型 | `chore: import production MQTT ingestion baseline` |
| 导入内容 | `mqtt_direct_gauss.py`、schema、依赖说明、Topic/payload 契约、无密配置模板、来源已有的测试 |
| 来源 | openEuler `/root/agent/pi_agnet/`；历史 `auto_water` 只作差异对照 |
| 目标位置 | `services/ingestion/`、`services/ingestion/schema/`、`tests/contract/` |
| 风险 | 高：数据时间、Topic 关联、命令留痕与规范表是控制链上游；生产版本与旧 GitHub 已分叉 |
| 人工确认 | **必须**：确认完整文件白名单和 manifest 后才能导入 |
| 不允许 | 修改 MQTT Topic、数据库 SQL 语义、连接参数或生产服务 |

## Commit 3：控制服务

| 项目 | 规划 |
| --- | --- |
| 提交类型 | `chore: import phase control baseline for <device>` |
| 导入内容 | 首次优先导入 soil3 Phase3 目录，随后按需导入其他设备的 Phase3、对应 Phase1/handoff 与无密 Unit 模板 |
| 来源 | openEuler `/root/water/phase3/<device>/`、`phase1_test/`、`phase_handoff/` |
| 目标位置 | `services/control/phase3/<device>/`、`services/control/phase1/`、`services/control/handoff/`、`deployment/systemd/` |
| 风险 | 最高：物理控制路径、设备配置、运行 state 引用和直接 MQTT 发布能力 |
| 人工确认 | **每个设备必须确认**；soil3 作为当前 reference implementation 必须最先完成来源与安全审查，随后保留 soil1、soil2、soil_test 的独立历史/设备实现 |
| 不允许 | 改 Phase3、阈值/时长/Topic、systemd 行为或重新部署 |

## Commit 4：Agent 与交互模块

| 项目 | 规划 |
| --- | --- |
| 提交类型 | `chore: import plant agent baseline`；随后 `chore: import reviewed OpenClaw tools` |
| 导入内容 | Plant Agent 源码/schema/tests；审核通过的 OpenClaw tools/rules/templates；无密部署示例 |
| 来源 | openEuler `/root/agent/plant_agent/` 与 `/root/.openclaw/workspace/` 白名单文件 |
| 目标位置 | `agents/plant_agent/`、`agents/openclaw/`、`deployment/systemd/` |
| 风险 | 高：Agent 接触真实设备状态与人工交互；OpenClaw 易混入账号、记忆、会话和直接 MQTT 路径 |
| 人工确认 | **必须**：先批准白名单，后导入；每个工具标记泵与数据权限 |
| 不允许 | 导入 memory/session/state/media/token、扩大工具权限或改变 OpenClaw 生产行为 |

## Commit 5：实验与展示模块

| 项目 | 规划 |
| --- | --- |
| 提交类型 | `experiment: import Phase2 reproducibility baseline`；随后 `chore: import read-only web baseline` |
| 导入内容 | Phase2 predictor/shadow-train 源码、实验配置、数据/模型 manifest、评估脚本；Web 正式源码和模板 |
| 来源 | openEuler `/root/water/wyc_training/`、`/root/water/web/`，历史 GitHub 作对照 |
| 目标位置 | `experiments/phase2/`、`dataset/`、`web/`、`tests/` |
| 风险 | 中高：权重和真实数据不可混入；Web 不可恢复直发泵能力；实验不可成为执行权威 |
| 人工确认 | **必须**：确认数据/权重排除清单与 Web 只读行为 |
| 不允许 | 导入权重、生产数据、训练输出、真实 Web 配置，或让 predictor/Web 直接控制泵 |

## Firmware 与 hardware：并行阻塞任务

固件不应被遗忘到最后，但当前没有确认的生产来源，不能规划为直接导入。Commit 1 后即应建立来源和设备台账；只有获得源码、构建 hash、设备映射、命令处理和失效安全测试证据后，才新增独立提交：

```text
chore: import verified ESP32 firmware baseline
```

该提交同样需要人工确认，且不得包含 Wi-Fi/Broker 凭据或未经审查的二进制。

## 停止条件

任一 Commit 导入完成后的状态都必须是“已导入、未部署”。如发现业务逻辑差异、直接泵旁路、密钥/数据泄露风险、缺失 manifest 或无法完成回放，立即停在该模块，不跳过风险进入下一提交。
