# 初始资产清单

| 资产 | 来源类别 | 目标归属 | 当前状态 | 导入前置条件 |
| --- | --- | --- | --- | --- |
| MQTT/GaussDB 接入 | openEuler production | `services/ingestion/` | active，待受控导入 | 完整 manifest、schema/Topic 契约审查 |
| Phase3 soil1 | openEuler production | `services/control/phase3/soil1/` | 早期验证实现，active | 保留历史价值；独立 hash、回放和安全审计 |
| Phase3 soil2 | openEuler production | `services/control/phase3/soil2/` | 增强实现，active | 保留历史价值；独立 hash、回放和安全审计 |
| Phase3 soil3 | openEuler production | `services/control/phase3/soil3/` | 当前 reference implementation，active | 优先作为通用能力设计参考；不得复制设备私有配置 |
| Phase3 soil_test | openEuler production | `services/control/phase3/soil_test/` | 测试设备实现，inactive | 每设备独立 hash、回放和安全审计 |
| Phase1 / handoff | openEuler production | `services/control/phase1/`、`handoff/` | 部分 inactive/定时器 | 与 Phase3 互斥关系记录 |
| Web | openEuler production | `web/` | active，只读直控禁用 | 保留拒绝直控的测试 |
| Phase2 predictor/shadow train | openEuler production | `experiments/phase2/` | active 服务/研究候选 | 代码、模型 manifest、数据集边界分离 |
| Plant Agent | openEuler production | `agents/plant_agent/` | GitHub 缺失 | 包含 tests；不授予控制权 |
| OpenClaw tools/rules | openEuler runtime | `agents/openclaw/` | 受控交互 | 白名单、脱敏、模板化 |
| ESP32 firmware | 未定位的生产来源 | `firmware/` | 阻塞项 | 源码、构建 hash、设备安全测试 |
| Hardware evidence | 现场/历史资料 | `hardware/` | 待补 | 来源、许可、接线与安全信息 |
| Historical auto_water | 历史 GitHub | `manifest/sources/` | 对照来源 | 不直接复制，不覆盖生产事实 |
