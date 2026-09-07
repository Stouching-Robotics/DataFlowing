"""config 包 —— 应用配置、国际化与版本号。

版本号集中定义于此：settings.APP_VERSION 及其他模块均从这里取值。
"""

__version__ = "1.1.4" # 新增 USB Type-C 手套接入（STM32 CDC 串口引擎，60fps IMU + 16×16 触觉）、录制时实时解算 MANO 21 关键点回填 hand_pose 占位列（含主界面骨架渲染）与 BLE 手套双序列号配置更新，并新增单文件 PyQt5 查看器 demo（RGB/深度/触觉/骨架/IMU 面板、实时播放与可拖动的进度条）及配套测试。