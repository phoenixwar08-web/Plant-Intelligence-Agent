# V1 协议冻结登记与最小实验基线（Day 6 / Issue #26）

本文档只做登记与追溯：不修改任何协议内容，不改变
[PROTOCOLS_AND_BOUNDARIES.md](PROTOCOLS_AND_BOUNDARIES.md) 中的协议状态，
不修改代码，不依赖 Day 6 的真实接入（Issue #24）或 Replay 全量回归
（Issue #25）结果。协议状态与边界的权威来源始终是
`docs/PROTOCOLS_AND_BOUNDARIES.md`；本文与它冲突时以它为准。

- 登记日期：2026-09-23
- Git 基线点：`main` @ `65ae5ce26002cb7dd6b59fff5ebeccdae5367c1d`
  （2026-09-22 16:34 +0800，Merge PR #47；这是 Day 5 开始前、包含
  Gate v2 / Experience Retrieval / Phase3 Bridge 的最新 `main`）
- 基线含义：本登记把上述 commit 处的各 v1 协议契约登记为后续实验、
  数据积累与本地小模型训练所依赖的 **V1 基线冻结点**。基线点之后对
  implemented 协议的任何改动，仍须走 `PROTOCOLS_AND_BOUNDARIES.md`
  规定的显式协议变更提案流程（版本/兼容处理 + 生产者与消费者清点），
  并由 Owner 决定是否将其正式转为 frozen 状态。本 Issue 不改变任何
  协议的状态列。
- Day 5 的 Trace、完整 Shadow 集成和故障安全测试尚未进入 `main`，因此
  本文当前是 **Day 5 合并前的候选冻结登记**。只有 PR #50、#51、#53
  按固定顺序合并并由 Owner 复核后，才能把合并后的 `main` commit 登记为
  Day 1–5 的正式实验基线；不得把本 commit 描述为 Day 5 已冻结。

## 1. V1 协议登记

Issue #26 要求登记的六个协议，截至基线点的实际状态：

| 协议 | 基线点状态 | 落地 commit（PR，merge 时间） | 备注 |
| --- | --- | --- | --- |
| `state.v1` | frozen | `8b72d6b`（PR #33，merge `3c221b1`，2026-09-17） | 只读、按需构建的规范化当前事实。 |
| `vision.v1` | implemented（未启用） | `7228f8c` 起（PR #31，merge `fe29bf8`，2026-09-18） | 显式决定不冻结、改用 implemented 状态：`1a655a5`。无消费者；未配置且未单独批准时不运行相机或模型请求。 |
| `strategy.v1` | implemented | `ea78efe`（PR #32，merge `102aa9d`，2026-09-18；末次内容修订 `e0335c5`） | soil3 runtime 经 Validator → Gate → dry-run Runner 消费；截至基线点 shipped provider 配置为 disabled，未对 live provider 实测。 |
| `gate.v1` | implemented | `9d8311a`（PR #38，merge `242f0c4`，2026-09-20） | 只读准入 + 本地探索预算预留；不授予设备控制权威。 |
| `episode.v1` | implemented | `6c7ab06`（PR #37，merge `b8b4bed`，2026-09-20） | `open → closed` 生命周期；closed 后不可变。 |
| `feedback.v1` | implemented | `d80457e`（PR #40，merge `7d3b9a0`，2026-09-22；运行时生命周期对齐 `be6fa7b`） | 四窗口观测；观测调度未启用（视觉采集时机为 Issue #30）。 |

六个协议中，截至基线点只有 `state.v1` 在权威协议表中为 **frozen**；
其余五个为 **implemented**（契约有自动化测试覆盖、但按状态词汇表尚未
冻结）。这是如实登记，不是遗漏。

同为 V1 基线组成部分、但不在本 Issue 六协议名单内的已登记协议
（为基线完整性一并记录）：

| 协议 | 基线点状态 | 落地 commit（PR） | 备注 |
| --- | --- | --- | --- |
| `replay_sample.v1` | frozen | `04909c1`（PR #35，merge `5401a9b`） | 只读历史重放；本登记不依赖其回归结果（Issue #25）。 |
| `gate.v2` | implemented | `bc3396d`（PR #49，merge `38546d9`） | 在 gate.v1 边界上增加 Gate 本地 `strategy_sha256` 内容绑定；Phase3 Bridge 只接受 gate.v2，拒绝 gate.v1。 |
| `experience_retrieval.v1` | implemented | `985fcaf`（PR #45，merge `2b1968f`） | 对 closed Episode 的只读可解释排序；无策略/执行/写库权威。 |
| `phase3_bridge_request.v1` / `phase3_bridge_response.v1` | implemented | `eab3003`（PR #47，merge `65ae5ce`） | verification-only；V1 不导入、不调用 Phase3，`phase3_called` 与 `physical_actions_performed` 恒为 false。 |

## 2. 版本登记

| 项 | 登记值 | 来源 / 追溯 |
| --- | --- | --- |
| Git 基线（仓库） | `main` = `65ae5ce26002cb7dd6b59fff5ebeccdae5367c1d`（2026-09-22） | `git log origin/main`。 |
| Git 基线（板端部署） | release revision `f8fcd5f9d37c0575a420d0beaa404627529f7f7a`（2026-09-21 部署核验） | `docs/OPEN_EULER_RUNTIME_RUNBOOK.md`；部署点早于基线点，后续发布前须核对 `REVISION` 文件。 |
| Cloud model（策略 shadow 链） | provider_mode `qwen_dashscope`；model `qwen3.8-Flash`；base_url `https://ws-d5yw23tz0yzwob1l.cn-beijing.maas.aliyuncs.com/api/v1`；temperature 0、max_tokens 1200、json_response_format true、timeout 30s、max_retries 1 | `services/soil3/agent_runtime/README.md`（`c94dc2a`，PR #43）。API key 只在板端 `secrets/qwen.env`（0600），不入 Git。发布默认 `provider_mode=offline_fixture`（`config/soil3_agent_runtime.example.json`）；`config/cloud_strategy.example.json` 为 `enabled=false` + `MODEL_ID` 占位。 |
| Cloud model（视觉链） | 由板端环境变量 `QWEN_MODEL` 指定；仓库内无值 → **待补**（密钥/模型配置禁止入库） | `config/soil3.example.json` vision.required_environment。 |
| Prompt version | 无语义版本号；按文件 + 内容哈希登记：`services/soil3/cloud_strategy/prompts/strategy_v1.txt`，blob `9de1d0625f5a11c2b097a3e6fb87d0e38dc85eb3`，末次内容变更 `e0335c5`（PR #32）。链路版本 `chain_version = cloud-strategy-chain.v1` | `services/soil3/cloud_strategy/service.py`；板端 runtime 以 `prompt_path` 指向 release 内同一文件。 |
| Phase3 version | 无版本号常量。源码自 `da3af21`（2026-09-15，rebuild repository from soil3 production baseline）后未再修改；内部标记 `schema_version=1`（auditor、decision_brain）、`trial_reconciliation_version=1`。生产唯一执行权威为板端 `phase3_soil3.service`（`/root/water`） | `git log -- services/soil3/phase3/`。截至基线点，Agent 链未调用 Phase3（Bridge 为 verification-only）。 |
| Gate version | `gate.v1`（`9d8311a`）与 `gate.v2`（`bc3396d`）并存；策略链当前使用 gate.v1，Phase3 Bridge 只接受 gate.v2。策略参数：`warning_age_seconds=900`、`deny_age_seconds=18000`、`window_seconds=86400`、`max_exploration_water_seconds=0` | `config/cloud_gate.example.json` 与 `config/soil3_agent_runtime.example.json` 的 `gate_policy`（两处一致）。 |

## 3. 最小实验基线

后续实验与本地小模型训练以下列已合并事实为最小基线：

**运行形态（proposal-only shadow 链）**

```text
State（5 分钟 timer，只读构建 state.v1）
→ Strategy（offline_fixture 为发布默认；qwen_dashscope 为显式 shadow 模式）
→ Validator → Gate → Runner（仅 dry-run）→ Episode（open）→ Feedback（窗口附着/终结）
```

- 每次运行记录必须满足 `physical_actions_performed=false`、
  `phase3_called=false`；Gate `deny` 是预期安全结果（Runner
  `skipped_due_to_gate_deny`，Episode 保持 open 等待反馈生命周期）。
- 首次部署 `exploration_requested=false`；`max_exploration_water_seconds=0`。
- Qwen 失败不自动回退 fixture：要么保持 pipeline timer 停用，要么显式
  恢复 `offline_fixture`（agent_runtime README）。
- 数据落点（板端运行数据，非源码）：
  `/root/water/runtime/instances/soil3/agent_chain/{state,strategy,gate,runs,audit,episodes}`。

**策略与反馈约束**

- Validator 上限：`max_actions=12`、`max_pump_seconds=120`、
  `max_wait_seconds=86400`、`max_total_pump_seconds=240`、
  `max_total_seconds=86400`。
- Feedback 四窗口：30min、2–3h、6–12h、24h；各评估保持独立、
  带窗口来源，不计算统一 reward；聚合 Outcome 一次性写入并关闭 Episode。
- 经验回流：`experience_retrieval.v1` 只对 closed Episode 做只读排序，
  成功/失败案例分离，缺失证据降低 coverage，不猜测标签。

**本地小模型训练基线**

- 目标模型：Qwen3.5-2B（README「V1 开发方向」、计划书 V3 §十二/§十四）。
  现阶段只积累与整理训练数据；正式微调明确暂不实施（计划书 V3 §十三）。
- 训练数据来源：真实闭环 Episode（State → Strategy → Action →
  Feedback → Outcome），按计划书 §十二 于月底整理成训练集。
- 冻结后变更纪律（计划书 V3 §十二）：State / Strategy / Episode /
  Vision Schema、Prompt 主结构、Safety Gate 核心规则尽量不再修改，
  除非出现 Bug、明显安全问题或数据无法正常记录。
- 现状记录：`phase2_predictor` 是 observed legacy 预测器，不是 V3 云端
  策略模型，不作为本基线的一部分；其默认配置（CPU、`input_dim=6`、
  `sequence_length=12`、`hidden_dim=32`、权重
  `/root/water/wyc/brain_weights_v3.pth`）仅按
  `services/soil3/phase2_predictor/config.py` 如实登记。

**未合入项（不属于本基线）**

- `origin/issue-21-shadow-runtime`（GitHub PR #51，对应 Issue #21，Day 5 完整 Shadow
  链集成，含 trace.v1）与 `origin/issue-23-trace-experiment-data`
  （GitHub PR #50，对应 Issue #23，Day 5 Trace 和实验数据），以及
  `origin/issue-22-fault-safety-tests`（GitHub PR #53，对应 Issue #22，
  Day 5 故障与安全测试）截至基线点未合入 `main`，
  不在本基线内；合入后由各自 Issue 补登记。
- Day 6 的 Issue #24（受控真实植物接入）与 Issue #25（历史 Replay 全量
  回归验收）结果不是本登记的输入。

## 4. 待补项清单

| 项 | 状态 |
| --- | --- |
| 视觉链云端模型 ID（板端 `QWEN_MODEL` 环境变量值） | 待补（禁止入库） |
| `strategy.v1` 对 live provider 的实测结果 | 截至基线点未实测；依赖 Issue #24 |
| Replay 全量回归验收结果 | 待补；依赖 Issue #25 |
| trace.v1、完整 Shadow 链与故障安全测试 | 待补；PR #50 / #51 / #53 未合入，合并后须刷新正式 `main` 基线 commit |
| Qwen3.5-2B 训练集整理规范 | 待补；月底整理（计划书 V3 §十二） |
| `tools/vision_v1_check/run_once.py` 纳入版本管理 | 待补；由后续独立 Issue 处理（runbook 已记录建议） |
