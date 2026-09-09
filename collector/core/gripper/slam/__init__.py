"""core.gripper SLAM 子集：桥接进程控制器与 stdout 协议。

原版 barrel 引用的 frame_reader（/dev/shm 预览 jpg 读取）未复制；主程序
双目显示走 orb_raw_stream 原始流（recording.fays_raw_client）。
"""

from __future__ import annotations

from .process_controller import SlamProcessController
from .protocol import PoseSample

__all__ = ["PoseSample", "SlamProcessController"]
