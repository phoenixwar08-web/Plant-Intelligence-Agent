# Phase 1-D：首次公开前整理报告

**审查日期：** 2026-08-23  
**范围：** `Plant-Intelligence-Agent` 工作树；静态文件、文档、配置模板和依赖契约。  
**未执行：** openEuler 访问、服务操作、MQTT 发布、数据库操作、设备操作、模型训练或测试套件。

## 结论

仓库已达到“**可作为源码与研究资料首次公开**”的文档、目录与敏感资产边界要求，但**不是可部署或可连接真实设备的发行版**。在首次 Git commit 前，必须由密钥持有人撤销并轮换曾出现在导入工作树中的视觉 API Key；轮换完成后再执行暂存区复扫。

此外，公开仓库尚缺少许可证选择，且内置的 `web/static/js/chart.min.js` 未在仓库中登记许可来源。两项不影响安全审查结论，但应在公开前由维护者完成许可确认。

## README 审查

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 项目定位 | 通过 | 根目录 README 明确本仓库是植物智能体系统主线，不是旧仓库整理版或生产镜像。 |
| 演进关系 | 通过 | 明确 `soil1 → soil2 → soil3` 为演进链。 |
| soil3 定位 | 通过 | soil3 明确标记为当前 `reference implementation`；同时禁止复制其设备私有配置。 |
| 公开使用边界 | 通过 | README 指向依赖说明与配置模板，并声明不能直接连接真实设备。 |

## 目录与来源说明

`agents/`、`config/`、`deployment/`、`experiments/`、`firmware/`、`manifest/`、`scripts/`、`services/`、`tests/`、`web/` 均有目录级 README，说明功能、来源、依赖或当前状态。

来源变更可在 `manifest/import-history.yml` 追溯。公开化处理没有改变控制决策：仅替换了导入源码中的明文凭据与部署私有默认配置，并已在 manifest 中登记。

## 配置与依赖整理

| 资产 | 状态 | 说明 |
| --- | --- | --- |
| `config/environment.example` | 新增 | 只含变量名与占位符：视觉 API、邮件告警、Phase2 配置路径和最小权限开关。 |
| `config/phase2/config.example.json` | 新增 | 与 Phase2 配置合并机制兼容的脱敏示例，不含生产路径、坐标、数据或权重。 |
| `.gitignore` | 更新 | 忽略真实 Phase2 `config.json`、`.orig`、密钥、运行 state、日志、数据与模型工件。 |
| `docs/DEPENDENCIES.md` | 新增 | 记录 Python 版本下限、静态识别的第三方依赖、外部运行依赖与不应直接运行的模块。 |

## 公开安全审查

### 已通过

- 未发现 GitHub/云 API Key、AWS Key、私钥 PEM、长格式 token 字面量或私有网段地址；
- 未发现日志、原始 CSV/数据集、模型权重、固件二进制、数据库文件、运行 state、session、memory、media 或 spool 文件；
- `config/phase2/config.example.json` 已通过 JSON 解析；
- 两个公开化修改过的 Python 文件已通过仅内存语法编译，未生成 `__pycache__`。

### 已处理

1. 视觉 Agent 中的明文 API Key 已替换为 `PLANT_VISION_API_KEY` 环境变量读取。
2. Phase2 默认配置中的生产绝对路径和精确天气坐标已替换为通用目录与 `0.0/0.0`；真实部署须使用仓库外 `WYC_PHASE2_CONFIG` 文件覆盖。

### 仍需注意，但不构成源码泄密

- 四个 `*.orig` 历史副本仍在本地工作树，已被 `.gitignore` 排除；首次暂存时不得使用强制添加。
- 一些已导入控制与 OpenClaw 源码仍保留历史 Linux 运行路径。这些路径不是凭据，但也不应被理解为公开部署说明；未来应在独立重构任务中逐模块抽离。
- 视觉 API Key 必须在密钥提供方侧撤销并重新生成。即使当前仓库从未提交或推送，也不应继续使用该值。

## 验证边界

本阶段没有运行测试、安装依赖或启动任何代码。原因是部分来源模块可触及 MQTT、GaussDB、OpenClaw 或水泵控制路径。当前验证仅限静态审查、模板 JSON 解析和内存语法编译。

## 首次 Commit 前检查清单

- [ ] 撤销并轮换视觉 API Key，更新部署侧的仓库外凭据；
- [ ] 在 Git 暂存区执行一次凭据、数据、权重和运行状态复扫；
- [ ] 确认 `*.orig` 保持未暂存；
- [ ] 选择并添加仓库许可证；核对或补充 `chart.min.js` 的来源与许可说明；
- [ ] 只提交本报告、README、文档、模板、忽略规则和经审查的源码；
- [ ] 不推送、不部署，直到用户另行确认。

## 公开后优先事项

1. 为每个可运行模块建立隔离环境与可重复测试；
2. 审查并模板化 systemd 与设备 profile；
3. 获取 ESP32 源码、构建 hash 和设备执行安全证据；
4. 在独立设计任务中从 soil3 提炼可验证的公共能力，保持 soil1/soil2 的历史边界。
