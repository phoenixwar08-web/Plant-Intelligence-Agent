# 导入日志

> 本日志记录 Phase 1-C 的机械源码整理。所有条目均为“已导入、未部署”；没有修改来源或业务逻辑。

| 模块 | 来源路径 | 来自 openEuler | 来自 auto_water | 目标路径 | 当前状态 |
| --- | --- | --- | --- | --- | --- |
| MQTT ingestion | `/root/agent/pi_agnet/mqtt_direct_gauss.py` | 是 | 否，仅作历史对照 | `services/ingestion/` | 已导入、hash 已核对 |
| 数据库 schema | `/root/agent/pi_agnet/irrigation_schema.sql` | 是 | 否，仅作历史对照 | `services/ingestion/schema/` | 已导入、hash 已核对 |
| Phase1 | `/root/water/phase1_test/` 四个设备脚本 | 是 | 否 | `services/control/phase1/` | 已导入、hash 已核对 |
| Phase3 soil1/soil2/soil3/soil_test | `/root/water/phase3/<device>/` 白名单主源码 | 是 | 否 | `services/control/phase3/` | 已导入、主锚点 hash 已核对 |
| Phase handoff | `/root/water/phase_handoff/` | 是 | 否 | `services/control/handoff/` | 已导入、未部署 |
| Phase2 | `/root/water/wyc_training/` 的源码/README/依赖/测试 | 是 | 否，仅作历史对照 | `experiments/phase2/`、`tests/phase2/` | 已导入、权重/数据/配置排除 |
| Web | `/root/water/web/` 白名单文件 | 是 | 否，仅作历史对照 | `web/` | 已导入、主文件 hash 已核对 |
| Plant Agent | `/root/agent/plant_agent/` 主源码和测试 | 是 | 否 | `agents/plant_agent/`、`tests/plant_agent/` | 已导入、主锚点 hash 已核对 |
| OpenClaw tools | `/root/.openclaw/workspace/tools/` 白名单 | 是 | 否 | `agents/openclaw/tools/` | 已导入、运行态/账号相关工具排除 |
| Phase3 tests | soil1/soil3 来源测试 | 是 | 否 | `tests/control/phase3/` | 已导入、未执行 |
| Firmware | 未定位可追溯来源 | 否 | 否 | `firmware/` | 未导入，缺口已登记 |
| systemd Unit | `/etc/systemd/system/` | 已盘点 | 否 | `deployment/systemd/` | 未导入，待脱敏审查 |

## 排除清单

- 所有密钥、Token、账号、群组、真实连接参数、私有网络信息；
- OpenClaw `memory/`、`state/`、`sessions/`、`media/` 与配置/发送/重置工具；
- `manual_water_soil3.py`、`manual_water_soil_test.py`、旧 MQTT 接入脚本、运行修复/回填脚本；
- `.bak*`、`*backup*`、日志、CSV、运行 JSON、spool、数据库数据、虚拟环境、`node_modules`、缓存；
- Phase2 模型权重、训练输出、`config.json` 与原始实验数据；
- systemd Unit 原件和 ESP32 固件/二进制。

## 发现的问题

1. Phase3、OpenClaw 工具和部分维护脚本均可能具有直接 MQTT 发布能力；本次仅复制来源代码，未改变权限或运行行为。
2. `manual_water_soil3.py` 既是旁路又曾在静态审计中解析失败，因此未导入源码树。
3. 生产 systemd Unit 可能暴露运行路径和参数，不能在未审查情况下作为公开模板导出。
4. ESP32 固件来源、构建 hash 和设备端安全机制仍缺失，`firmware/` 为空。
5. 新目录的运行依赖、测试路径与部署模板尚未建立；本次未执行任何代码。
