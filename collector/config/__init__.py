"""config 包 —— 应用配置、国际化与版本号。

版本号集中定义于此：settings.APP_VERSION 及其他模块均从这里取值。
"""

__version__ = "1.3.6"

# v1.3.6 —— 夹爪并入极简版（只录不显）+ SLAM 轨迹点带上取样帧的宿主时刻戳。
#   1) 极简版新增夹爪：力 / 10×10×3 力矩阵 / 夹爪状态 / SLAM 轨迹照常落盘，界面
#      不显示任何夹爪信息，夹爪 RGB 直接当主视频源。载荷是硬约束——Linux 包把
#      ~460MB 原生栈当必需资源下发，start_lite.sh 逐项校验、缺则报 [错误 B] 拒启
#      （老机器升级会被拦住而不是跑一半才炸）；Windows 包有意不带、靠载荷缺失降级。
#      力矩阵编码搬到 core/gripper_codec.py（数据契约只此一份，UI 侧 re-export）。
#   2) SLAM 点新增 observation.{prefix}slam_trajectory_ns：native 在 stereoCallback
#      入口取一次 CLOCK_MONOTONIC，随帧走到 stdout 的 Host:(<ns>)，与点列表锁步落盘。
#      真机验收（episode-085/086）：点数=戳数、零全 0 行、最近邻残差中位 6.9ms
#      （按行号配对为 83ms）。旧 native 二进制不打印该字段，照旧可读。
#   另回收 native 树 339MB 被解引用成实体副本的 .so（tools/dedup_gripper_native.py）。
#
# 历史版本一行一条，完整说明见 README 的「更新记录」：
#   v1.3.5  相机服务静默卡死自愈；夹爪扫描配对改走 ESP32 NVS 序列号
#   v1.3.4  新夹爪接入即自动生成运行标定并连接，不再需要人工两步
#   v1.3.3  力矩阵 / 触觉力与 RGB 视频的时间戳对齐（补 capture_ns 稀疏列）
#   v1.3.2  SLAM 陈旧帧入库前拦截；轨迹显示去掉主线程深拷贝
#   v1.3.1  力矩阵落盘规格改 5 档可选，倍率记在每段一份的 meta 行
#   v1.3.0  UMI 夹爪全链接入（Fays S80M 双目 SLAM 位姿/轨迹 + 触觉力 + RGB）
