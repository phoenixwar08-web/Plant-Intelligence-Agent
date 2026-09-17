# Plant Intelligence Agent

> **Plant Intelligence Agent 是一个面向真实植物长期运行的智能养护系统。项目通过传感器、视觉、控制系统与大小模型协同，使植物智能体能够根据真实环境和植物反馈逐步调整养护策略，而不是长期依赖固定阈值和预设浇水方式。**

## 这是什么项目

植物养护并非只由某一个土壤阈值决定。温湿度、光照、植物生长状态、浇水后的响应以及环境变化，都会影响下一次养护决策。

本项目正在建设一套边云协同的植物智能体：云端大模型根据实时状态、环境变化、视觉状态和历史反馈提出策略；现有边缘控制链在安全边界内执行；真实反馈再沉淀为可追踪的经验数据。随着数据积累，后续本地小模型将学习那些能够可靠处理的场景，并把复杂或低置信度场景交回云端模型分析。

当前研究重点是让系统从真实植物反馈中逐步学习和适应，而不是长期依赖固定阈值和预设浇水方式。

## 从状态到学习

```text
传感器 / 摄像头
      ↓
植物状态 State
      ↓
云端策略模型
      ↓
StrategyRequest
      ↓
Validator / Cloud Gate
      ↓
Phase3
      ↓
ActionPlan
      ↓
MQTT / 真实设备
      ↓
植物反馈
      ↓
Episode
      ↓
后续小模型训练
```

系统中的各部分各司其职：

| 组件 | 负责内容 |
| --- | --- |
| Phase1 | 初始标定与参数识别。 |
| Phase2 | 预测与候选轨迹能力，为策略分析提供参考。 |
| Phase3 | 最终安全控制和真实设备执行。 |
| 云端策略模型 | 当前开发方向：分析状态并生成 `StrategyRequest`，支持动态策略与安全范围内的探索。 |
| 视觉模型 | 当前开发方向：将植物图片转成结构化视觉状态，补充传感器无法直接表达的生长信息。 |
| Episode | 当前开发方向：持续记录 `State → Strategy → Action → Feedback`，形成可回溯的真实经验。 |

## 真实执行边界

```text
StrategyRequest
→ Validator
→ Cloud Gate
→ Phase3
→ ActionPlan
→ MQTT
```

云端模型、视觉模型和后续本地小模型都只能参与状态理解或策略生成，不能直接发布真实水泵命令。Phase3 是当前唯一的真实执行权威；所有进入设备的动作都必须经过这条边界。

## 当前目录

```text
Plant-Intelligence-Agent/
├── config/                       示例配置与运行参数入口
├── docs/                         技术计划与项目文档
├── ops/                          systemd、cron 等运行模板
├── services/
│   └── soil3/
│       ├── phase1/               初始标定与参数识别代码
│       ├── phase2_predictor/     预测与候选轨迹代码
│       ├── phase3/               安全控制、动作计划与设备执行代码
│       ├── telemetry/            状态与事件数据适配代码
│       └── vision/               Vision V1 单次观测代码
└── tests/                        当前边界检查与后续自动化测试入口
```

## Vision V1 Day 2

仓库内 Python 接口 `services.soil3.vision.capture_and_analyze_once()` 只完成一次观测：抓取一帧、完整保存原始帧证据、按预设区域裁出每个植物 zone、逐区调用视觉模型、校验并持久化合法的 `vision.v1`。它不提供 HTTP、不启动后台调度、不接入 Episode，也不修改 `state.v1`、Phase3、MQTT 或水泵控制。

当前画面里同时存在两处植物，所以一次调用按 `SOIL3_VISION_ZONES_PATH` 配置的 zone 分别产出记录（例如 `plant_zone_1`、`plant_zone_2`）：

- 原始帧完整保存在 `frames/`，送给模型的只是按归一化 `[x, y, w, h]` 裁出的 `images/` 区域；ROI 只在模型输入阶段生效，不改变留档图片。
- `change_vs_previous` 只与同一 `plant_zone` 的上一次观测比较，两处植物不会互相追踪。
- 一个 zone 失败不影响另一个 zone；整轮结果可以是 `success`、`image_unusable`、`analysis_failed`、`capture_failed` 或 `partial`。
- `analysis_failed` 记录额外带 `http_status` 与 `provider_error_code`：收到 HTTP 响应就记状态码，provider 给出可安全提取的机器错误码（如 `insufficient_quota`）就记该码；超时/连不上时两者为 `null`。本地校验拒绝时两者也是 `null`，以区别于 provider 故障。原始响应体、密钥、完整 URL、Base64 图片、prompt 与模型原文一律不落盘。

每条记录报告 17 个视觉字段：`image_quality`、`target_detected`、`target_ambiguity`、`leaf_droop`、`leaf_spread`、`wilting`、`yellowing`、`visible_damage`、`browning`、`leaf_curl`、`spots_or_lesions`、`leaf_loss`、`stem_posture`、`occlusion`、`overall_visual_state`、`change_vs_previous`、`confidence`。视觉证据不足时字段必须是 `null`，本地校验会拒绝没有证据支撑的结论；`overall_visual_state` 只描述外观（`normal / mild_abnormality / obvious_abnormality / severe_abnormality / unavailable`），不是健康诊断，也不是浇水或处置建议。

单个 zone 的结果只能是以下四种之一：

| 状态 | 含义 |
| --- | --- |
| `capture_failed` | 未取得可保存图片，或该 zone 无法裁剪；没有 `image_id`，不会生成 `vision.v1`。 |
| `image_unusable` | 已保存图片，但无法可靠观察植物；会保存所有观察字段为 `null` 的 `vision.v1`。 |
| `analysis_failed` | 图片及哈希已保存，但模型、JSON 或本地校验失败；不会生成 `vision.v1`。 |
| `success` | 图片已保存，且合法 `vision.v1` 已持久化。 |

启用前只在运行环境设置示例配置中列出的变量，区域配置见 `config/vision_zones.example.json`；不要把摄像头地址或访问密钥写入仓库。RTSP 取帧依赖带 FFmpeg 后端的 OpenCV（发行版自带的 RPM 构建不支持），因此使用 `requirements.txt` 固定的 `opencv-python-headless`，并装在独立虚拟环境里，不动系统 Python。真实摄像头/模型冒烟测试是手动、非控制操作，未纳入自动化测试，且需要另行批准。

## V1 开发方向

- Unified State Schema
- 视觉结构化状态
- 云端策略大模型
- Strategy Validator
- 宽松的 Cloud Gate
- 多步策略执行
- Episode 与多时间尺度反馈
- 历史经验回流
- 面向后续 Qwen3.5-2B 的训练数据

这些能力正在按边云协同和真实反馈闭环的方向建设；其中尚未落地的部分会在相应实现完成后进入实际运行链路。

## 开发原则

- 真实运行数据与源码分离，源码仓库不承载设备运行状态。
- 凭据、访问地址和其他敏感配置不得提交到 Git。
- 新模型不能绕过 Phase3 的安全控制与执行边界。
- 优先复用已有控制链，不重复建设另一条设备控制通道。
- 每次策略、动作和反馈都应可追踪，为分析与后续训练保留可靠数据。

## 本地开发与验证

建议在项目根目录执行：

```powershell
python -m compileall services tests
python -m pytest -q
```

在运行依赖尚未安装时，先按项目实际环境安装所需 Python 包。`ops/` 中的文件用于部署配置参考；不要将模板中的示例值直接用于真实设备。

详细的边云协同建设计划见 [植物智能体云端大模型接入与边云协同计划书 V3](docs/植物智能体云端大模型接入与边云协同计划书_V3.md)。
