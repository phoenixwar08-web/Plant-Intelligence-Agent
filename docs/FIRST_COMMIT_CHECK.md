# 首次本地 Commit 前检查

**检查日期：** 2026-08-23  
**检查范围：** Git 候选集合 `git ls-files --others --exclude-standard`。  
**操作边界：** 未执行 `git add`、`git commit`、网络连接、生产环境访问、服务启动、MQTT 发布或数据库操作。

## 当前 Git 状态

- 仓库尚未有已暂存文件；所有候选内容处于未跟踪状态。
- 本次检查开始时，候选集合为 **181 个文件，2,735,497 bytes（2.609 MiB）**。
- 本检查文档写入后会使最终候选集合增加 1 个 Markdown 文件；报告末尾的复核结果记录最终统计。
- 本机 Git 对用户级 global ignore 文件发出权限警告；仓库自身 `.gitignore` 已由 `git check-ignore` 单独验证，候选集合与忽略结果不依赖该全局规则。

## 候选文件统计（写入本报告前）

| 顶级路径 | 文件数 |
| --- | ---: |
| `.gitignore` / 根 README | 2 |
| `agents/` | 52 |
| `config/` | 3 |
| `deployment/` | 1 |
| `docs/` | 12 |
| `experiments/` | 33 |
| `firmware/` | 1 |
| `manifest/` | 4 |
| `scripts/` | 1 |
| `services/` | 33 |
| `tests/` | 33 |
| `web/` | 6 |

文件类型包括 140 个 Python、25 个 Markdown、7 个 SQL、1 个 HTML、1 个 JavaScript、1 个 JSON、2 个 YAML、1 个 shell、1 个文本和忽略/模板文件。最大候选文件为 `services/control/phase3/soil3/decision_brain.py`（303,104 bytes）；没有超过 1 MiB 的候选文件。

## 暂存区前安全扫描

对候选文件内容进行了以下静态模式扫描：云/API Key、GitHub token、AWS Key、私钥 PEM、长格式密码/secret/token 字面量、含认证信息的数据库/MQTT URL，以及私有网段地址。

| 检查项 | 结果 | 说明 |
| --- | --- | --- |
| API Key / Token / 密码字面量 | 通过 | 未发现匹配项。视觉 API Key 已在 Phase 1-D 改为环境变量读取。 |
| 私有连接配置 | 通过 | 未发现私有网段地址、认证 URL、`.env` 或真实 `config.json` 候选文件。 |
| 日志、数据、权重、二进制 | 通过 | 未发现 `.log`、`.csv`、`.jsonl`、数据库文件、`.pt/.pth/.onnx/.bin`、固件产物或运行目录候选。 |
| runtime state | 通过 | 未发现 `state/`、`runtime/`、`sessions/`、`memory/`、`media/` 或 `spool/` 目录中的候选文件。 |
| 大文件 | 通过 | 没有候选文件超过 1 MiB。 |

`git diff --cached --check` 报告了从来源机械导入的尾随空格和部分末尾空行。这些是既有格式问题，不是凭据、数据或行为差异；首次基线提交保留原样，后续如需格式化必须作为独立的非功能性提交。

## 忽略规则验证

`.gitignore` 已生效。以下 4 个历史 `*.orig` 副本被 `git check-ignore` 确认排除，不在候选提交集合内：

- `experiments/phase2/phase2_predictor/segmented_online.py.orig`
- `experiments/phase2/phase2_predictor/service.py.orig`
- `experiments/phase2/phase2_predictor/watering_models.py.orig`
- `tests/phase2/test_config_and_loss.py.orig`

真实 `config/phase2/config.json` 同样被忽略，只有 `config/phase2/config.example.json` 可以进入提交。

## 已知边界与风险

1. 候选源码中仍有 **183 处**历史 Linux 运行路径（`/root/`、`/usr/local/`、`/dev/shm/`）引用，主要来自直接导入的控制与 OpenClaw 源码。它们不含凭据、私网地址或真实运行 state，但说明这些模块尚未完成通用部署配置抽离；不得视为公开的一键部署说明。
2. 本仓库不含系统级依赖锁定、ESP32 固件或 systemd 模板；这不影响首次源码提交，但不支持部署承诺。
3. 依据 [PRE_RELEASE_REPORT.md](PRE_RELEASE_REPORT.md)，视觉 API Key 仍需由密钥持有人在服务提供方侧撤销并轮换；仓库也需要维护者选择许可证并确认 `chart.min.js` 的许可来源。

## 首次 Commit 决策

静态候选扫描通过，且没有任何文件被暂存或提交。建议的下一步仅在得到用户明确确认后执行：

1. 确认外部密钥已轮换，以及许可证处理决定；
2. `git add` 候选文件；
3. 对**暂存区**再次执行相同的敏感内容和大文件扫描；
4. 生成一次本地 Git commit；不推送、不部署。

## 暂存前快照

- 最终候选集合：**182 个文件，约 2.613 MiB**；其中 `docs/` 为 13 个文件。报告自身写入会改变精确 byte 数，首次暂存前应重新取数。
- 已暂存文件：**0**。
- 凭据/私网/认证 URL 静态匹配：**0**。
- 禁止的候选路径或文件类型：**0**。
- 被忽略的 `*.orig` 历史副本：**4**。

## 当前暂存区复扫

- 暂存文件：**182**；未暂存文件：**0**。
- 暂存区凭据/私网/认证 URL 静态匹配：**0**。
- 暂存区禁止文件类型/路径：**0**；超过 1 MiB 的暂存文件：**0**。
- `*.orig` 仍由 `.gitignore` 排除，未进入索引。
- 索引空白检查仅发现从来源原样导入的既有尾随空格和末尾空行，已在本报告登记，未进行格式化。

暂存区满足创建首次本地基线 commit 的安全条件；尚未执行 commit 或 push。
