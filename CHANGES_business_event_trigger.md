# Trigger 业务事件准入补丁 · 变更记录

**日期:2026-11-10**
**分支:feature/business-event-trigger**
**基线:REVISION e8a734ccbfaaf2b65da3d5c2292be87f4f709dd0(plant-intelligence-release)**

## 1. 为什么改

原 Trigger 准入只判断 `semantic_projection` 是否变化,其中 `trend_directions` 主导。审计:137 次准入调用中 130 次仅趋势分类在「平稳/缓慢下降」间小抖动触发,而加湿回升前谷底、转入持续下滑起点等关键转折反而缺调用。

## 2. 改了什么(7 文件,+1080/−3)

| 文件 | 改动 |
|---|---|
| `services/soil3/cloud_trigger/events_v1.py` | **新增** 业务事件检测(E1 下滑/加速、E2 谷底确认、E3 回升阈值穿越、E4 长平台),纯 stdlib、Python 3.9 兼容 |
| `services/soil3/cloud_trigger/context_v1.py` | +42/−1 新增可选入参 `humidity_series`,输出 `humidity_series_6h` |
| `services/soil3/cloud_trigger/evaluator_v1.py` | +59/−2 新增可选 `trigger_policy/event_policy/memory`;决策增加可选 `business_event/trigger_signals/trigger_memory` |
| `services/soil3/cloud_trigger/shadow_store.py` | +94/−2 可选事件字段持久化;新增 `recent_humidity_series()` |
| `services/soil3/cloud_trigger/trigger_shadow.v1.schema.json` | +16/−0 两个可选属性(不进 required) |
| `services/soil3/agent_runtime/runtime_v1.py` | +48/−2 新增可选配置 `trigger_policy/trigger_event_policy`;业务模式接线 |
| `tests/test_cloud_trigger_events.py` | **新增** 10 用例 |

## 3. 行为语义

- **失败关闭优先**:硬安全/数据过期/保护生效仍先于一切,返回 LOCAL_SAFETY_ONLY / SKIP_CLOUD。
- **业务模式无序列 = 不调用**(`event_input_unavailable` → SKIP)。
- **每事件只调用一次**(去重记忆 `trigger_state/trigger_memory.json`;记忆写失败即失败关闭)。
- **默认 `classification_v1` 行为不变**;`business_event_v1` 需显式配置开启。
- **默认 `allowed_scope=diagnose_only`**(只诊断,不提剂量)。
- **未改** Gate / Validator / Prompt / Phase3 / Bridge / 受控执行。

## 4. 验证

- 服务器完整 release 临时副本全仓 pytest:**532 passed, 31 errors**;31 个 error 全在 Phase3,根因板子 Python 3.9 无法解析 `config_manager.py` 的 `Path | None`(PEP 604,3.10 语法),已在未打补丁原始 release 对照报同一错误,与本次改动无关。
- **真实数据回放**:3853 条 trigger_shadow 记录 → 新策略把调用从 604 压到 18,18 次全落在谷底/下滑起点等关键转折。

## 5. 回滚

- 删除 `runtime.json` 中 `trigger_policy` 字段或改回 `"classification_v1"`,立即恢复旧行为,无需改代码。
- 代码回滚:releases 目录 `git checkout e8a734ccbfaaf2b65da3d5c2292be87f4f709dd0`。