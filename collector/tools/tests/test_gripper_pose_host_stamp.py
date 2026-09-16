#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""位姿行 Host 字段（取样帧宿主时刻）解析契约自检（离线，无硬件）。

    venv/bin/python tools/tests/test_gripper_pose_host_stamp.py

背景：`slam_trajectory` 每点的 `t` 是**相机传感器钟**，而视频行的
`hardware_ns` 是**宿主单调钟**，两者不同源、只差一个只能拟合的偏移
（实测反推估计散布 ~104ms，已超一个帧间隔）。v1.3.6 起 native 在取样帧
进进程时取一次 CLOCK_MONOTONIC，随帧走到 stdout：

    [0.0000] XYZ:(...) Quat:(...) Host:(287607702508638)

本测试钉死解析侧的向后兼容：**现场已部署的旧 native 二进制不打印 Host
字段**，Python 侧升级后必须照旧能解析那些行（host_mono_ns = None），
否则整条位姿流会断——采集链路直接不可用。

覆盖:
  1. 新格式（带 Host）→ 全部字段正确、host_mono_ns 取到
  2. 旧格式（无 Host）→ 照旧解析，host_mono_ns = None（向后兼容）
  3. 带 Δq 第二行的变体：首行仍解析出位姿（正则只吃首行）
  4. Host 值畸形/缺失不污染其他字段
  5. 宿主戳是 int 不是 float（纳秒精度不能被浮点吃掉）
  6. 落盘链路的两步变换（rotate_pose_z90 + dataclasses.replace）
     都不丢戳、不改戳
退出码 0 = 全部通过。
"""

from __future__ import annotations

import dataclasses
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from core.gripper.slam.protocol import (                    # noqa: E402
    _parse_pose, quat_conjugate, quat_product, rotate_pose_z90,
)

# 与 core/gripper/native/.../fayssense_orb_slam.cc 的 printf 逐字对应
NEW_LINE = ("[1.2345] XYZ:(0.1000,-0.2000,0.3000) "
            "Quat:(w=1.0000,x=0.0000,y=0.0000,z=0.0000) "
            "Host:(287607702508638)")
OLD_LINE = ("[1.2345] XYZ:(0.1000,-0.2000,0.3000) "
            "Quat:(w=1.0000,x=0.0000,y=0.0000,z=0.0000)")
DQ_LINE = ("[2.0000] XYZ:(1.0000,2.0000,3.0000) "
           "Quat:(w=0.7071,x=0.7071,y=0.0000,z=0.0000) "
           "Host:(287607705000000)\n"
           "       Δq(10f):1.23° axis=(0.000,0.000,1.000)")

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def main() -> int:
    print("[1] 新格式（native 已重编，带 Host 字段）")
    pose = _parse_pose(NEW_LINE)
    check("解析成功", pose is not None)
    check("position 正确", pose.position == (0.1, -0.2, 0.3),
          str(pose.position))
    check("rotation 正确（w 在前 → 内部 x,y,z,w）",
          pose.rotation == (0.0, 0.0, 0.0, 1.0), str(pose.rotation))
    check("timestamp 正确", abs(pose.timestamp - 1.2345) < 1e-12,
          str(pose.timestamp))
    check("host_mono_ns = 287607702508638",
          pose.host_mono_ns == 287607702508638, str(pose.host_mono_ns))
    check("宿主戳是 int 不是 float（纳秒精度不被浮点吃掉）",
          isinstance(pose.host_mono_ns, int)
          and not isinstance(pose.host_mono_ns, bool),
          type(pose.host_mono_ns).__name__)
    check("valid 通过", pose.valid)

    print("[2] 旧格式（现场已部署的 native 二进制，不打印 Host）")
    old = _parse_pose(OLD_LINE)
    check("照旧解析成功（否则整条位姿流会断）", old is not None)
    check("几何字段与新版一致", old.position == pose.position
          and old.rotation == pose.rotation
          and abs(old.timestamp - pose.timestamp) < 1e-12)
    check("host_mono_ns = None（不是 0）",
          old.host_mono_ns is None, repr(old.host_mono_ns))
    check("valid 通过（无戳不影响位姿有效性）", old.valid)

    print("[3] Δq 变体：正则只吃首行，第二行不干扰")
    dq = _parse_pose(DQ_LINE)
    check("解析成功", dq is not None)
    check("取到首行位姿", dq.position == (1.0, 2.0, 3.0), str(dq.position))
    check("取到首行宿主戳", dq.host_mono_ns == 287607705000000,
          str(dq.host_mono_ns))

    print("[4] Host 畸形/截断不污染其他字段")
    for bad, why in (
        ("[1.0] XYZ:(1.0,2.0,3.0) Quat:(w=1.0,x=0.0,y=0.0,z=0.0) Host:()",
         "空括号"),
        ("[1.0] XYZ:(1.0,2.0,3.0) Quat:(w=1.0,x=0.0,y=0.0,z=0.0) Host:(abc)",
         "非数字"),
        ("[1.0] XYZ:(1.0,2.0,3.0) Quat:(w=1.0,x=0.0,y=0.0,z=0.0) Host:(12",
         "括号未闭合"),
        ("[1.0] XYZ:(1.0,2.0,3.0) Quat:(w=1.0,x=0.0,y=0.0,z=0.0) Host:123",
         "缺括号"),
    ):
        got = _parse_pose(bad)
        check(f"{why}：位姿字段仍完好、戳退化为 None",
              got is not None and got.position == (1.0, 2.0, 3.0)
              and got.host_mono_ns is None,
              f"pose={None if got is None else got.position} "
              f"ns={None if got is None else got.host_mono_ns}")

    print("[5] 落盘链路两步变换不丢戳/不改戳")
    # _publish_pose 实际做的是：rotate_pose_z90 → dataclasses.replace
    # （原点后姿态相对化）。两步都必须原样带过宿主戳。
    rolled = rotate_pose_z90(pose)
    check("rotate_pose_z90 透传", rolled.host_mono_ns == 287607702508638,
          str(rolled.host_mono_ns))
    zero = (0.7071051, 0.0, 0.0, -0.7071051)
    relativized = dataclasses.replace(
        rolled,
        rotation=quat_product(quat_conjugate(zero), rolled.rotation))
    check("dataclasses.replace 相对化后仍在",
          relativized.host_mono_ns == 287607702508638,
          str(relativized.host_mono_ns))
    check("位置/时间戳未被这两步改动",
          relativized.position == rolled.position
          and relativized.timestamp == rolled.timestamp)

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 — {_FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
