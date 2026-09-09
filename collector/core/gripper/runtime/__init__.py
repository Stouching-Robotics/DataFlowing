"""core.gripper 运行时的最小子集：CPU 绑核、线程亲和与设备访问互斥。

主程序只依赖 cpu_policy/thread_affinity/device_access/interruptible_queue；
原版 barrel 引用的 app_config/log_policy/performance_reporter/shutdown 未复制。
"""

from __future__ import annotations
