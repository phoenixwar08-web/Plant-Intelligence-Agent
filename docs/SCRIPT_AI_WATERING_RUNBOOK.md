# 脚本触发AI补水：本地验收与部署手册

2026-11-16。本地待Review，生产未启用。本文面向第一次接触项目的人；所有生产步骤都是待执行清单，不是已经完成的记录。

2026-11-17补充：已完成隔离实机610项Linux测试及两次获准非执行云调用；首次Gate拒绝、复测证据路径校验失败，视觉未完成，生产仍未切换。详见`docs/reviews/2026-11-17-device-smoke.md`，不要将下文原待办误认为已全部验收。

后续修复：Gate结构化/到期解析和输入引用目录已通过623项实机测试。已找回原RTSP密钥环境及soil3-vision解释器，并成功读取当前帧和裁剪；候选worker需按新模板加载原视觉环境及新的无密钥模型/区域覆盖配置。详见`docs/reviews/2026-11-17-gate-input-vision-fix.md`。生产仍未切换，Qwen视觉输出本轮未重新调用。

## 系统现在怎样工作

显式启用SCRIPT_AI模式后，原Phase3脚本仍计算普通补水候选，但在两个普通正剂量执行点之前交给请求协调器：低湿恢复分支和常规候选分支。原生紧急处置、停泵、PendingSoak及供水故障的受控复测仍归Phase3，不等待AI，不属于云端普通补水决策。

一个逻辑请求最多等待300秒，包含准备上下文的时间。原脚本不提前执行这次普通补水。新请求写入active.json，由专用systemd path服务唤醒worker。worker读取新鲜State、已启用的视觉及经验上下文，使用六字段输出提示词，经Validator→Gate→dry-run Runner→Bridge校验，写回绑定的提案。它没有执行权限。

Phase3睡眠期间每秒只检查小型请求记录；回复就绪或到截止时间时提前进入下一轮，重新读状态后审核。网络调用不会阻塞Phase3心跳。普通调用失败立即进入脚本回退，脚本用该轮新读数重算；输出非法时不执行，保持原截止时间，届时回退。安全拒绝不会被当作网络故障而绕过。

AI和脚本只能有一个领取执行资格。领取后执行异常或进程退出的请求保持claimed，不能自动重试；需核对实际命令与设备状态。AI迟到只记录结果，不能再次执行。

## 代码从哪里看

| 文件 | 查看内容 |
|---|---|
| services/soil3/phase3/decision_brain.py | `_offer_script_ai`候选拦截、`_resume_script_ai`回复消费、`_run_model_cycle`最终安全、`_finish_result`实际结果关联 |
| services/soil3/agent_runtime/watering_request.py | 固定输入、300秒状态机、文件锁、防重复领取、迟到处理 |
| services/soil3/agent_runtime/watering_worker.py | 云端调用及完整非执行校验链 |
| services/soil3/cloud_strategy/watering_decision.py | 新输入封装、六字段校验、旧策略适配 |
| services/soil3/agent_runtime/watering_context.py | 同一过程中的实际动作、基线、阶段变化和视觉前后JSON |
| services/soil3/cloud_strategy/prompts/watering_decision_v1.txt | 实际交给模型的任务说明 |
| deploy/systemd/plant-agent-soil3-watering-worker.path、.service | 新请求唤醒、启动待处理检查及进程超时；尚未安装 |

## 必须显式设置的配置

现有示例仍是offline_fixture，不会自动变为真实控制。不要照抄一个随意剂量预算上线。

| Phase3工程常量 | 默认与要求 |
|---|---|
| SCRIPT_AI_ENABLED | 默认false；部署切换时显式true |
| SCRIPT_AI_REQUEST_DIR | 默认null；与worker的`--request-dir`完全相同 |
| SCRIPT_AI_MIN_CHECK_SEC、SCRIPT_AI_MAX_CHECK_SEC | 默认null；部署前按现有采样/保护确认有限正数及顺序；模型复查建议必须在范围内 |
| MODEL_PROPOSAL_BUDGET_24H_SEC | Owner确认30秒；所有Phase3开泵强制共用滚动账本，缺少可信初始化则拒绝ON |
| MODEL_PROPOSAL_MAX_AGE_SEC、MODEL_PROPOSAL_MAX_HUMIDITY_DRIFT | 仍独立检查，不能因为最多等待300秒而扩大旧读数有效期 |
| WATER_SEC_MIN、WATER_SEC_MAX_HARD、SOAK_WAIT_SEC等 | 沿用现有独立安全；本文不设新植物适宜阈值 |

SCRIPT_AI模式使用绑定请求里的提案，不依赖旧latest.json。不要同时启用另一个普通AI执行入口。旧MODEL_PROPOSAL模式可以保留代码作兼容，但生产切换需明确唯一模式。

运行配置仍使用既有RuntimeConfig。云端模式必须显式qwen_dashscope，凭据使用已有受控环境文件，禁止写入Git或报告。当前model_key只有cloud；第二模型接口尚无实际实现。视觉必须同时核实决策采集与反馈采集两个独立开关，禁用/不可用会保持null。能力存在不等于已经启用。

## 本地复核

在独立工作树根目录运行下列离线命令。测试提供商、读数与执行器均为fake/mock，无真实MQTT、摄像头或云端调用：

```powershell
python -c "import tempfile,pathlib,unittest,sys;p=pathlib.Path('.test-tmp').resolve();p.mkdir(exist_ok=True);tempfile.tempdir=str(p);s=unittest.TestSuite(unittest.defaultTestLoader.discover('tests',pattern=x) for x in ['test_watering_request.py','test_watering_decision_contract.py','test_script_ai_handoff.py','test_watering_worker.py','test_ai_phase3_policy.py','test_cloud_strategy.py']);r=unittest.TextTestRunner().run(s);sys.exit(not r.wasSuccessful())"
```

Windows环境的test_feedback_collector_v1包含既有`os.kill(pid,0)`存活判断测试，可能结束测试进程，本轮广回归排除该16项；未修改无关收集器实现。Linux正式集成验收必须补跑。通过本地测试不等于生产部署成功。

## 怎么查看一次决策

以部署实际配置路径为准，以下是拟采用的路径布局：

1. `agent_chain/watering_requests/active.json`：当前决策ID，不包含执行权限。
2. `agent_chain/watering_requests/<decision_id>.json`：脚本候选输入、最终模型输入及哈希、过程ID、deadline、owner、状态、结果、实际动作和迟到事件。
3. `agent_chain/watering_decisions/<decision_id>/input.json`：真正发给模型的数据；`chain.json`包含校验结果、模型版本、usage与六字段输出；`gate.json`和`bridge.json`说明是否准入。
4. 请求`outcome.feedback_action_id`：对照既有feedback_actions的真实命令证据；命令完成不等于已独立确认实际出水。`ep-`加action_id尾部是既有Collector创建的反馈Episode，与云端提案Episode分别保留，不能混为同一对象。
5. 同一care_process_id关联后续决策。current_episode保留基线、已知动作、读数变化、视觉前后JSON。只有previous_image_id确实指向基线图时，才把视觉模型change_vs_previous作为该对图片的直接对比，否则unknown。

没有反馈、视觉、精确成本或实际流量时明确缺失。2026-11-17已补齐Phase3统一命令预算；首次部署初始化及外部MQTT限制见[统一补水预算](SHARED_WATERING_BUDGET.md)，不能冒充全设备独立流量计量。

## 统一部署前后清单

项目要求Issue级Review及Owner合并、Day级统一部署。尚无本变更的已合并Issue/PR证据；不自动推送、合并或连接生产。

- 部署前：核实实际release、唯一Phase3服务、数据库可读性、故障状态初始化、配置白名单、当前泵状态及在途请求；保存代码和配置回滚副本，不复制或覆盖runtime/data/logs。
- 在Linux补跑完整回归，并对新.path/.service运行`systemd-analyze verify`；确认真实解释器、发布目录、数据库socket及写目录权限。
- 确认专用worker可读相机/视觉配置；本地测试未证明实际摄像头设备及PrivateTmp环境下的可用性。
- 关闭旧定时pipeline调用入口，防止另一个定时云分析继续产生冗余调用；保留需要的反馈采集调度，不能把所有Agent服务一起关掉。
- 安装新.path和.service后，只在统一切换时启用监听。path启动会先建立监听，再启动一次幂等worker检查已有请求；之后只由新active.json变动唤醒。
- 确认两端相同request目录、明确复查范围及预算；切换唯一Phase3模式。不得用额外真实浇水充当连通性测试。
- 观察正常监测：无需求不调用，出现需求有请求，调用有六字段输出，审核/拒绝有原因；发生实际正常补水时核对命令及后续反馈，全链路ID一致。

回滚时恢复受审版本和开关，停用新worker监听；在途AI结果不再消费。保留claimed与命令回执，不清空记录后重试；不自动重放未确认动作。运行中的泵仍由既有Phase3停泵路径处置。

## 当前不宣称已经解决的问题

- 未部署、未真实调用云端、未验证摄像头或真实水泵。
- 未训练路由，未实现第二模型，未计算奖励或贝叶斯反馈。
- care_process保持open用于可追溯关联，未擅自定义植物学周期终点；正式实验前仍须确定过程切分与评价条件。
- 自动请求内的传感器序列来自既有Gauss健康快照查询；未额外扩张数据库查询。当前查询未返回的光照、窗口泵秒及全来源动作不能补造。
