# 依赖与运行边界

本文件依据已导入源码的静态 import 与 `experiments/phase2/requirements.txt` 整理；不是已验证的生产部署配方。

## Python 版本

已导入源码使用 Python 3.10 的联合类型语法（例如 `str | None`）。建议以 **Python 3.10 或更高版本**进行静态检查和未来的隔离式本地实验。生产运行时版本尚未在本仓库锁定。

## 已识别的第三方依赖

| 范围 | 依赖 | 证据与状态 |
| --- | --- | --- |
| MQTT 接入与控制 | `paho-mqtt` | 由 ingestion、Phase1/3 与 OpenClaw 工具 import；未建立统一版本锁定。 |
| Web | `Flask` | 由 `web/` import；未建立专用 requirements。 |
| Phase2 实验 | `numpy>=1.21`、`requests>=2.26`、`torch>=1.10` | 已有 `experiments/phase2/requirements.txt`。 |
| Phase2/分析 | `pandas`、`matplotlib`、`duckdb` | 源码 import；版本待后续实验环境锁定。 |
| Plant Agent | `requests`、`pydantic` | 源码 import；版本待锁定。 |
| 视觉分析 | `opencv-python`（提供 `cv2`） | 源码 import；模型服务凭据须通过环境变量提供。 |

其余 import 为 Python 标准库或仓库内模块。`gsql`/openGauss、MQTT Broker、systemd、ESP32 与 OpenClaw 是外部运行依赖，当前未在仓库中提供部署实现。

## 安全使用规则

1. 不安装后直接启动 `services/control/`、OpenClaw 浇水工具或生产 Web；它们保留来源控制路径。
2. Phase2 仅可使用脱敏数据、无权重的本地实验目录和仓库外配置；`config/phase2/config.example.json` 仅描述配置形状。
3. 真实凭据、设备 profile、数据库角色文件、模型权重、数据集和运行 state 均不应进入 Git。

## 当前缺口

- 没有根目录统一依赖锁定文件；在模块独立回放和测试环境确认前，不应伪造一个“可一键生产部署”的 requirements 文件。
- ESP32 固件、systemd 模板以及设备端测试证据仍未导入。
- 测试尚未在新的目录结构与隔离环境中执行；首次运行前需要单独的测试和安全审批。
