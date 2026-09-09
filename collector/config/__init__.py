"""config 包 —— 应用配置、国际化与版本号。

版本号集中定义于此：settings.APP_VERSION 及其他模块均从这里取值。
"""

__version__ = "1.3.0" # 新增 UMI 夹爪全链接入（Fays S80M 双目 SLAM 位姿/轨迹 + Sightac 左右触觉力与 250×250 力矩阵 + DECXIN RGB；面板夹爪分组、资源缺失自动隐藏），支持双臂双夹爪同录（rig1 旧槽位/数据列契约不变，rig2 加 gripper_2_ 前缀；两 rig 独立 CPU 亲和分区零共享物理核 + raw 流专用核 + 30fps 空桶看门狗）；SLAM 位姿坐标约定按新 ORB 核修正（X=右/Y=正对/Z=上，原点后姿态相对化，起始不跳变）；左目显示稳 15fps（奇偶抽帧）且 raw 流断线自动重连；轨迹并入 episode parquet 列不再落 txt 侧车；夹爪脱离 online/ 自持（原生资源镜像 core/gripper/native/ 不入库、Sightac SDK pyarmor 加密随包、新增 tools/import_gripper_calibration.py 接入新夹爪标定）；多台 S80M 按序列号/USB 拓扑路径区分（--device-serial/--device-path）。