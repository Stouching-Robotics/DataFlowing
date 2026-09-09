"""UMI 夹爪 rig 集成包（从 online/gripper_version1 分叉）。

原生二进制/SDK/标定随包交付在 core/gripper/native/，触觉 Sightac SDK 在
core/gripper/sightac_sdk/（pyarmor 加密）；本包导入时先把 fays_runtime 的
路径常量补丁到 native/ 下（见 paths.patch_fays_runtime_constants）。资源
缺失时主程序通过 paths.gripper_resources_available() 隐藏夹爪设备条目。
"""

from __future__ import annotations

from core.gripper import paths

paths.patch_fays_runtime_constants()

__all__ = ["paths"]
