# 生产目录到新仓库的目录映射

> 本文只描述未来映射：不创建目标业务目录，不移动、复制或修改任何来源文件。

## 生产根目录映射

| 当前生产根目录 | 包含内容 | 新仓库目标位置 | 处理方式 |
| --- | --- | --- | --- |
| `/root/water/` | Phase1、Phase3、handoff、Phase2、Web、运行产物/备份 | `services/`、`experiments/`、`web/`、`deployment/` | 子目录逐项白名单映射；运行产物排除 |
| `/root/agent/` | MQTT 接入、Plant Agent、历史 agent 内容 | `services/ingestion/`、`agents/`、`manifest/archive/` | 按模块导入；不整体复制 |
| `/root/.openclaw/` | tools、规则、人格、配置、memory/session/state/media | `agents/openclaw/`、`manifest/` | 仅 tools/rules/templates 白名单；运行态永不导入 |
| `/etc/systemd/system/` | 服务和 timer Unit | `deployment/systemd/` | 仅生成无密 `.example` 与变量说明 |
| Broker/DB/运行数据路径 | Broker 配置、数据库、日志、state、模型/数据 | `manifest/`、`dataset/` 的 schema/摘要 | 不导出真实配置和数据 |
| ESP32/现场硬件来源 | 固件、烧录记录、接线与器件资料 | `firmware/`、`hardware/` | 当前来源未确认；先登记缺口 |

## `/root/water` 细化映射

| 当前路径 | 目标位置 | 导入资格 | 说明 |
| --- | --- | --- | --- |
| `/root/water/phase1_test/` | `services/control/phase1/` | 待迁移 | 按设备保留脚本边界；不与 Phase3 合并 |
| `/root/water/phase3/soil1/` | `services/control/phase3/soil1/` | 待迁移 | 独立导入提交 |
| `/root/water/phase3/soil2/` | `services/control/phase3/soil2/` | 待迁移 | 独立导入提交 |
| `/root/water/phase3/soil3/` | `services/control/phase3/soil3/` | 待迁移 | 先完成生产/历史冲突审查 |
| `/root/water/phase3/soil_test/` | `services/control/phase3/soil_test/` | 待迁移 | 保留测试设备定位 |
| `/root/water/phase_handoff/` | `services/control/handoff/` | 待迁移 | 与 Unit/timer 映射记录一起处理 |
| `/root/water/wyc_training/` | `experiments/phase2/` | 待迁移 | 源码与权重/输出分离 |
| `/root/water/web/` | `web/` | 待迁移 | 只导入正式文件；不导入 `.bak*` |
| `/root/water/**/system_state.json`、日志、CSV、备份 | `manifest/` | 仅 manifest | 仅记录 schema/用途/hash 策略，不复制真实内容 |

## `/root/agent` 细化映射

| 当前路径 | 目标位置 | 导入资格 | 说明 |
| --- | --- | --- | --- |
| `/root/agent/pi_agnet/mqtt_direct_gauss.py` | `services/ingestion/` | 待迁移 | 生产版本优先；单模块审查 |
| `/root/agent/pi_agnet/irrigation_schema.sql` | `services/ingestion/schema/` | 待迁移 | 与数据契约一同记录 |
| `/root/agent/pi_agnet/mqtt_to_opengauss*.py` | `manifest/archive/` | 仅 manifest | 旧接入候选，不进入实现树 |
| `/root/agent/plant_agent/` | `agents/plant_agent/` | 待迁移 | 导入状态/分析/推理/tests；不授予泵权威 |
| `/root/agent/**/venv/`、日志、导出数据 | 无 | 永不进入 | 依赖由锁文件/安装说明重建 |

## `/root/.openclaw` 细化映射

| 当前路径类别 | 目标位置 | 导入资格 | 说明 |
| --- | --- | --- | --- |
| `workspace/tools/` 中受审查的工具 | `agents/openclaw/tools/` | 待审核 | 设备/账号/路径将来模板化；首次导入不改逻辑 |
| `workspace/rules/`、人格/知识模板 | `agents/openclaw/rules/`、`agents/openclaw/templates/` | 待审核 | 仅无密、可公开文件 |
| watchers/timers 的无密定义 | `deployment/systemd/` | 待审核 | 不复制真实 group/account/绝对部署值 |
| `memory/`、`state/`、`sessions/`、`media/`、账号配置 | 无 | 永不进入 | 仅记录数据治理规则 |

## 目标目录职责

| 目录 | 职责 |
| --- | --- |
| `services/` | 可部署数据接入与控制代码 |
| `agents/` | 植物状态、推理、工具与受限交互适配 |
| `modules/` | 未来跨模块共享的领域契约和无副作用工具 |
| `firmware/` | 可追溯 ESP32 源码、构建与测试证据 |
| `web/` | 展示与受限请求界面 |
| `deployment/` | 无密部署模板和运行说明 |
| `experiments/` | Phase2、训练、评估与实验配置 |
| `dataset/` | schema、脱敏样例、查询/导出 manifest |
| `docs/` | 架构、审计、导入记录和维护规范 |
| `manifest/` | 来源、hash、状态和归档索引 |
| `tests/` | 单元、契约、回放和硬件在环测试 |

`modules/` 在首次代码导入前保持空缺规划：不得为了提前创建“公共层”而抽取、改名或改变既有 import。只有多个已导入模块出现被验证的稳定共用契约时，才单独迁入。

## 映射规则

1. 一个生产来源目录可映射到多个新仓库模块，但每个文件只能有一个明确目标位置。
2. 每个目标模块先建立 manifest，再开始复制；历史 GitHub 文件不覆盖生产来源文件。
3. 如目录变化阻断 import 或启动路径，记录为最小兼容适配并单独验证；这不是本阶段工作。
4. 运行数据、设备 profile、state 和权重一律从源码映射中剥离。
