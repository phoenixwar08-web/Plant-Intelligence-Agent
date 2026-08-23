# Production Import 政策

## 不变原则

每个导入只改变“代码进入新主线的位置”，不改变业务逻辑、服务行为、MQTT Topic、数据库语义或生产配置。部署是独立操作，默认不发生。

## 每模块流程

1. 从来源环境只读生成完整文件 manifest。
2. 标记 active/候选/归档状态、公开性与敏感信息风险。
3. 以单模块提交导入，保留来源 hash。
4. 仅在目录移动阻断启动时做最小 import/path 适配，并记录旧入口、新入口和回放结果。
5. 运行静态检查、已有测试及历史回放；控制模块额外覆盖拒绝路径。
6. 写入导入记录，状态为“未部署”。

## 推荐顺序

1. 治理文档、manifest、忽略规则和模板；
2. MQTT/数据契约与 schema；
3. 多设备 Phase3；
4. Phase1 与 handoff；
5. 只读 Web；
6. Phase2 experiments；
7. Plant Agent；
8. OpenClaw 白名单工具；
9. firmware/hardware。

firmware 虽然代码导入排在后面，但其来源获取、版本和安全测试从第 1 步起就是阻塞性资产追踪项。

## 禁止混入导入提交的变更

- 算法、阈值、泵时长、MQTT Topic、数据库 schema 或 systemd 行为调整；
- 跨设备抽象、重复代码合并、重命名和大规模格式化；
- 新模型接入、Web 直控恢复、OpenClaw 权限扩大；
- 任何密钥、运行数据、模型权重或状态文件。

## 导入记录最小字段

`source_path`、`collected_at`、`sha256`、`source_status`、`target_path`、`import_commit`、`path_change`、`checks`、`replay_result`、`safety_review`、`deployment_status`。
