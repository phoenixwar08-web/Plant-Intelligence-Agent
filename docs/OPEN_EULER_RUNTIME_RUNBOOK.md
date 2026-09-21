# openEuler soil3 运行手册

本手册记录 2026-09-21 在 soil3 openEuler 板端核验过的部署布局和常用
Day1--Day3 验证命令。它是运行参考，**不是**修改生产环境、密钥、Phase3 或
MQTT 的授权。

当时部署的 release revision 为
`f8fcd5f9d37c0575a420d0beaa404627529f7f7a`。执行前应先核对本机实际
revision；以后部署的路径可以保持不变，但代码 revision 和运行结果会改变。

## 非执行边界

- `state.v1`、Cloud Strategy、Cloud Gate、Strategy Runner 和 Episode 仅是
  proposal/shadow 链路。它们不能发布 MQTT，也不能调用水泵。
- `phase3_soil3.service` 是唯一的真实控制权威。不得为了验证 Agent 或 Vision
  停止、重启、调用或改写 Phase3。
- 不打印、`cat`、提交或粘贴任何 `*.env` 的值。两份下列密钥文件均为
  `root:root`、权限 `0600`。
- 需要隔离运行 Pipeline 时，仅停止
  `plant-agent-soil3-pipeline.timer`；不接 Phase3 Bridge，也不启动水泵。

## 已核验目录

| 用途 | 板端路径 | 说明 |
| --- | --- | --- |
| 生产根目录 | `/root/water` | root-only 运行根目录。 |
| 当前 release | `/root/water/releases/plant-intelligence-release` | Agent 源码部署副本；`REVISION` 保存部署 revision。 |
| Agent 运行数据 | `/root/water/runtime/instances/soil3/agent_chain` | `state`、`strategy`、`gate`、`runs`、`audit`、`episodes` 与运行配置。不是源码。 |
| Agent Qwen 密钥 | `/root/water/runtime/instances/soil3/agent_chain/secrets/qwen.env` | 仅由 Pipeline systemd `EnvironmentFile` 读取。 |
| Vision 密钥/相机环境 | `/root/water/runtime/secrets/vision.test.env` | 手动 Vision 验证时 source；包含 RTSP 与 Qwen 配置，禁止显示内容。 |
| Vision 虚拟环境 | `/root/water/runtime/venvs/soil3-vision` | **Vision 必须使用此 venv，不得使用 `/usr/bin/python3`。** |
| Vision 人工检查工具 | `/root/water/tools/vision_v1_check` | 当前板端辅助工具目录。 |
| Phase3 运行状态 | `/root/water/runtime/instances/soil3/phase3` | 既有控制模块运行数据，不写入。 |
| Phase3 长期数据 | `/root/water/data/soil3/phase3` | 既有长期数据，不写入。 |
| Phase3 日志 | `/root/water/logs/soil3/phase3` | 既有控制日志，只读排查。 |

快速核对 release：

```bash
sed -n '1p' /root/water/releases/plant-intelligence-release/REVISION
```

## systemd 单元

| 单元 | 当前角色 | 调度/边界 |
| --- | --- | --- |
| `plant-agent-soil3-state.service` | 生成一次 `state.v1` 快照 | `static` oneshot；由 State timer 管理。 |
| `plant-agent-soil3-state.timer` | State 调度 | 已启用；每 5 分钟触发。 |
| `plant-agent-soil3-pipeline.service` | Strategy → Gate → Runner dry-run → Episode | `static` oneshot；读取 Qwen 密钥；proposal-only。 |
| `plant-agent-soil3-pipeline.timer` | Shadow Pipeline 调度 | 已启用；每 5 分钟触发。 |
| `phase3_soil3.service` | 最终安全/执行权威 | 保护服务；不由本手册的 Agent 命令调用。 |
| `mqtt_direct_gauss.service`、`mqtt_logger3.service` | 既有 MQTT 入库/日志 | 保护服务；不由本手册启动、停止或发布消息。 |

查看当前状态，而不改变服务：

```bash
systemctl status --no-pager \
  plant-agent-soil3-state.timer \
  plant-agent-soil3-pipeline.timer \
  phase3_soil3.service \
  mqtt_direct_gauss.service \
  mqtt_logger3.service

systemctl list-timers 'plant-agent-soil3-*' --all
```

## Day1：真实 `state.v1`

检查最近一次 State 服务结果和最新快照：

```bash
systemctl show plant-agent-soil3-state.service -p Result -p ExecMainStatus

python3 - <<'PY'
import json
from pathlib import Path

path = Path('/root/water/runtime/instances/soil3/agent_chain/state/latest.json')
state = json.loads(path.read_text(encoding='utf-8'))
print('path:', path)
print('schema_version:', state.get('schema_version'))
print('state_id:', state.get('state_id'))
print('captured_at:', state.get('captured_at'))
print('soil:', state.get('soil'))
print('safety:', state.get('safety'))
PY
```

需要人工生成一个新的非执行 State 快照时，才执行：

```bash
systemctl start plant-agent-soil3-state.service
systemctl show plant-agent-soil3-state.service -p Result -p ExecMainStatus
```

`state.v1` 的缺失 safety fact 必须保持缺失；不得为让 Gate allow 而填充安全默认值。

## Day2：Vision 一次性真机验证

Vision 当前没有常驻 `plant-agent-soil3-vision.service` 或 scheduler。它是手动、
一次性观测：采集 RTSP 一帧、按 zone 调用 Vision provider、持久化图像证据和
合法 `vision.v1`。它不调用 Gate、Runner、Phase3、MQTT 或水泵。

**必须**使用 `soil3-vision` venv。使用系统 `/usr/bin/python3` 会使用系统 OpenCV，
可能没有 FFmpeg RTSP 后端并产生 `CV_IMAGES` 的误导性错误。

```bash
cd /root/water/releases/plant-intelligence-release

set -a
. /root/water/runtime/secrets/vision.test.env
set +a

PYTHONPATH=/root/water/releases/plant-intelligence-release \
  /root/water/runtime/venvs/soil3-vision/bin/python3 \
  /root/water/tools/vision_v1_check/run_once.py
```

成功时输出 `run_status: success`、`frame_id`，并按 plant zone 输出图像路径、
上一张图像 ID 和结构化 observation。失败时保留并报告 `capture_failed`、
`analysis_failed` 或 `partial`，不得把失败伪装为视觉事实。

完成后可清理当前 shell 的敏感环境变量（不会改文件）：

```bash
unset QWEN_API_KEY QWEN_BASE_URL QWEN_MODEL SOIL3_CAMERA_RTSP_URL \
  SOIL3_VISION_DATA_DIR SOIL3_VISION_ZONES_PATH
```

## Day3：Qwen shadow pipeline 与追溯

常规运行由两个 timer 每 5 分钟完成。先读取最近一次运行结果：

```bash
systemctl show plant-agent-soil3-pipeline.service -p Result -p ExecMainStatus

python3 - <<'PY'
import json
from pathlib import Path

root = Path('/root/water/runtime/instances/soil3/agent_chain')
run_path = max((root / 'runs').glob('*.json'), key=lambda p: p.stat().st_mtime)
run = json.loads(run_path.read_text(encoding='utf-8'))
print('run_file:', run_path)
print('provider:', run.get('provider'))
print('model:', run.get('model'))
print('gate_decision:', run.get('gate_decision'))
print('runner_status:', run.get('runner_status'))
print('episode_id:', run.get('episode_id'))
print('execution:', run.get('execution'))
PY
```

只有在 Owner 明确批准隔离 smoke 时，才临时停止 **Pipeline timer**、手动运行一次，
检查结果后恢复 timer：

```bash
systemctl stop plant-agent-soil3-pipeline.timer
systemctl start plant-agent-soil3-pipeline.service
systemctl show plant-agent-soil3-pipeline.service -p Result -p ExecMainStatus
systemctl start plant-agent-soil3-pipeline.timer
```

Gate 产生 `deny` 是正常且有效的安全结果；此时 Runner 必须
`skipped_due_to_gate_deny`。每次检查都应确认 run record 的
`physical_actions_performed` 与 `phase3_called` 均为 `false`。

## `run_once.py` 的版本管理结论

截至本手册编写时，`/root/water/tools/vision_v1_check/run_once.py` 存在于板端，
但 `tools/vision_v1_check/run_once.py` **不在当前 Git `main` 的追踪列表中**。
它不包含密钥或 RTSP 值，只调用仓库公开的
`capture_and_analyze_once()` 并输出已清洗的运行结果；同时它是本手册指定的
Day2 真机 smoke 入口。

因此，它**应当纳入版本管理**，作为受审查的非执行运维/验证工具，并随 Vision
接口变化接受测试。为保持本次变更只限运行文档，本手册没有从生产机复制或修改
该工具；应由后续独立 Issue/PR 将其以源码形式导入仓库，并验证不记录密钥、RTSP
地址、Base64 图像、prompt 或原始 provider 响应。
