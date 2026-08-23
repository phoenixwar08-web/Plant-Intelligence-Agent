# 生产基线索引：2026-08-23

来源：openEuler 当前生产环境的只读基线审计。该目录不包含生产文件内容。

已知 active 服务类别：MQTT/GaussDB 接入、soil1/soil2/soil3 Phase3、Phase2 predictor/shadow train、Web、soil2/soil3 OpenClaw watcher。

已登记的关键缺口：ESP32 固件源码与构建信息未定位；Broker 匿名发布配置与设备物理执行回执不足仍是安全审计项。

完整导入 manifest 必须在首个模块 production import 获批后生成。
