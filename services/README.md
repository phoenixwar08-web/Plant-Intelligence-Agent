# Services

可部署服务源码的归档位置：MQTT 数据接入，以及 Phase1、Phase3 与 handoff 控制生命周期。

- 来源：openEuler 生产基线的白名单文件。
- 依赖：Python、MQTT、GaussDB、每设备运行配置与 systemd（未在本仓库部署）。
- 当前状态：已原样导入、未部署。未改变业务逻辑、Topic、数据库结构或 Phase3 决策。

`services/ingestion/` 与 `services/control/` 保留生产模块边界；公共抽取和 import 调整不属于本次整理。
