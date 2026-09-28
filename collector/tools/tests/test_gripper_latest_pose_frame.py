#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_latest_pose / read_latest_pose 的帧契约：必须是**发布帧**（v1.3.13 修正）。

    venv/bin/python tools/tests/test_gripper_latest_pose_frame.py

背景：这两个符号叫同一个名字，装过两个不同的帧 —— read_latest_pose 的
docstring 自称 "the dedicated control/GUI channel"，而 _handle_event 里的
`self._latest_pose = pose` 取的是 **native** 那一份：_publish_pose 只重绑定
它自己的局部名（PoseSample 是 frozen dataclass，rotate_pose_z90 直接 new、
finish_publish_pose 走 replace），**入参对象本身没有被换帧**。同一函数里
写进 _pose_buffer / _on_pose 的才是发布帧。

它当时零消费者，所以没显形；一旦有人照 docstring 把它接上位姿显示或控制环，
画出来的是 native 帧的夹爪，与同屏轨迹（发布帧）差 M·Rz(+90°)（位置；姿态
那侧还多一层**体轴改名** Mᵀ，即 M·Rz(+90°)·R·Mᵀ），症状正是
v1.3.13 那次「坐标系还是没改」——只不过这次连 M 都少了一步。

覆盖:
  1. 同一帧内 _latest_pose 是发布帧（= 独立算出的 M·Rz(+90°)·native），
     与 _pose_buffer 那条路逐分量相同；且**不是** native、也不是中间帧
  2. _latest_pose is not _last_accepted_pose —— 后者按契约留 native 帧
     （连续性判据靠它），两者是同一对象就说明赋值又被写回去了
  3. read_latest_pose() 返回的就是它（含 raw_seq / lost_count）
  4. 没配 pose_paths（落盘未启用）时 _latest_pose 不能变成 None ——
     _publish_pose 在这条分支上早退，谁把返回值写成裸 return 就会踩到
  5. 原点重锚（origin 事件）之后仍然成立
  6. 开关拨到 KSQ_GRIPPER_SLAM_FRAME=right_forward_up 时，_latest_pose 跟着
     变成**中间帧**（那才是这次真正发出去的那一帧）——契约是"永远等于发布
     出口发出去的那一份"，不是"永远等于 M·Rz(+90°)·native"。帧是构造时
     解析一次的，所以这里必须另起一个 controller。
退出码 0 = 全部通过。
"""

from __future__ import annotations

import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import numpy as np                                              # noqa: E402

from config import settings                                     # noqa: E402
from core.gripper.slam.process_controller import (              # noqa: E402
    SlamProcessController,
    SlamState,
)
from core.gripper.slam.protocol import (                        # noqa: E402
    ProtocolEvent,
    PoseSample,
    parse_slam_line,
    relabel_published_frame,
    rotate_pose_z90,
)

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def make_controller(pose_paths=(None, None)) -> SlamProcessController:
    return SlamProcessController(
        SlamState(),
        executable="/bin/true",
        vocabulary="/dev/null",
        settings="/dev/null",
        device_config="/dev/null",
        trajectory="/dev/null",
        work_dir="/tmp",
        pose_paths=pose_paths,
        runtime_env_factory=lambda: {},
        cpu_roles={
            "fays_left_orb": (0,),
            "fays_right_orb": (1,),
            "fays_background": (2,),
        },
    )


def pose_line(timestamp, position, quat=(1.0, 0.0, 0.0, 0.0), host_ns=None):
    """真实格式的 native stdout 位姿行（走真解析器，别手工造 PoseSample）。"""
    x, y, z = position
    qw, qx, qy, qz = quat
    line = (f"[{timestamp:.6f}] XYZ:({x:.6f},{y:.6f},{z:.6f}) "
            f"Quat:(w={qw:.6f},x={qx:.6f},y={qy:.6f},z={qz:.6f})")
    if host_ns is not None:
        line += f" Host:({host_ns})"
    return line


def feed(controller, counters, timestamp, position, quat=(1.0, 0.0, 0.0, 0.0)):
    """喂一行 native 位姿，断言它确实被当成 pose 事件解析。"""
    line = pose_line(timestamp, position, quat)
    event = parse_slam_line(line)
    assert event is not None and event.kind == "pose", (
        f"位姿行没解析成 pose 事件: {line!r} -> {event!r}")
    controller._handle_event(event, counters, arrival_monotonic=timestamp,
                             arrival_wall_time=timestamp)
    return event


def expected_published(position):
    """独立复算发布帧位置：native → R_z(+90°) → 世界重标 M。"""
    return relabel_published_frame(rotate_pose_z90(PoseSample(
        position=position, rotation=(0.0, 0.0, 0.0, 1.0),
        timestamp=0.0))).position


def main() -> int:
    # 三个帧在同一物理位姿上必须互不相同，否则下面的断言形同虚设。
    native = (0.1, 0.2, 0.3)
    mid = rotate_pose_z90(PoseSample(position=native, rotation=(0, 0, 0, 1),
                                     timestamp=0.0)).position
    pub = expected_published(native)
    print(f"[0] 同一物理位姿的三个帧: native={native} 中间帧={tuple(mid)} "
          f"发布帧={tuple(pub)}")
    check("三个帧互不相同（测试前提）",
          len({native, tuple(mid), tuple(pub)}) == 3)

    for label, pose_paths in (("未配 pose_paths（落盘早退分支）", (None, None)),
                              ("配了 pose_paths（落盘正常分支）", None)):
        if pose_paths is None:
            tmpdir = tempfile.mkdtemp(prefix="latest_pose_frame_")
            pose_paths = (os.path.join(tmpdir, "orb_pose.json.tmp"),
                          os.path.join(tmpdir, "orb_pose.json"))
        print(f"[1] {label}")
        controller = make_controller(pose_paths)
        counters = {"raw": 0, "valid": 0}

        feed(controller, counters, 1.0, native)
        got = controller.read_latest_pose()
        latest, raw_seq, lost = got
        check("read_latest_pose() 不再是 None（首帧就有值）",
              latest is not None, repr(got))
        if latest is None:
            continue
        check("_latest_pose 位置 = 发布帧（M·Rz(+90)·native）",
              np.allclose(latest.position, pub, atol=1e-9),
              f"实得 {latest.position}，发布帧应为 {pub}，"
              f"native 是 {native}（差的就是这次没换）")
        check("_latest_pose 位置 != native（旧 bug 的判定）",
              not np.allclose(latest.position, native, atol=1e-9),
              f"实得 {latest.position}")
        check("_latest_pose 位置 != 中间帧（少换一步也不行）",
              not np.allclose(latest.position, mid, atol=1e-9),
              f"实得 {latest.position}，中间帧 {tuple(mid)}")

        # 与 _on_pose / _pose_buffer 那条路同帧：发布出口只有一个
        buffered = controller.pose_buffer_snapshot()
        key = max(buffered)
        check("_latest_pose 与 _pose_buffer 逐分量相同（同一条发布出口）",
              np.allclose(latest.position, buffered[key][:3], atol=1e-12),
              f"latest={latest.position} buffer={buffered[key][:3]}")
        check("首帧发布姿态是单位阵（相对化后重标：体轴与世界轴在原点重合）",
              np.allclose(latest.rotation, (0.0, 0.0, 0.0, 1.0),
                          atol=1e-9), f"实得 {latest.rotation}")

        # ★ 本文件的核心：native 那一份必须留在 _last_accepted_pose 里
        check("_latest_pose is not _last_accepted_pose（后者按契约留 native）",
              latest is not controller._last_accepted_pose)
        check("_last_accepted_pose 仍是 native 帧",
              np.allclose(controller._last_accepted_pose.position, native,
                          atol=1e-12),
              f"实得 {controller._last_accepted_pose.position}")
        check("read_latest_pose 的 seq/lost 与内部字段一致",
              (raw_seq, lost) == (controller._pose_raw_seq,
                                  controller._pose_lost_count),
              f"{raw_seq}/{lost} vs {controller._pose_raw_seq}/"
              f"{controller._pose_lost_count}")

        # 连续几帧（位姿判据会拒跳变：步长要小）
        for index in range(1, 6):
            spot = (0.1 + 0.002 * index, 0.2, 0.3 + 0.001 * index)
            feed(controller, counters, 1.0 + index, spot)
            expect = expected_published(spot)
            now = controller.read_latest_pose()[0]
            check(f"第 {index + 1} 帧 _latest_pose 仍是发布帧",
                  now is not None and np.allclose(now.position, expect,
                                                  atol=1e-9),
                  f"实得 {None if now is None else now.position}，应为 {expect}")

        # 原点重锚：重锚后发布帧首帧姿态回到单位阵，位置仍走发布帧
        origin = parse_slam_line("NEW ORIGIN SET")
        assert origin is not None and origin.kind == "origin", repr(origin)
        controller._handle_event(origin, counters)
        feed(controller, counters, 8.0, (0.0, 0.0, 0.0))
        later = controller.read_latest_pose()[0]
        check("原点重锚后 _latest_pose 仍是发布帧（首帧=M·0=原点）",
              later is not None and np.allclose(later.position, (0.0, 0.0, 0.0),
                                                atol=1e-12),
              f"实得 {None if later is None else later.position}")
        feed(controller, counters, 8.1, (0.0, 0.0, -0.05))
        moved = controller.read_latest_pose()[0]
        check("重锚后下移 0.05m 落在发布帧 +X（下）",
              moved is not None and np.allclose(moved.position, (0.05, 0.0, 0.0),
                                                atol=1e-9),
              f"实得 {None if moved is None else moved.position}")

    # [2] 开关拨到旧约定：_latest_pose 依然必须是"这一会话实际发布的那一帧"
    #     （回退时发布的就是中间帧），并与 _pose_buffer 同帧。契约不是"永远
    #     = M·Rz(+90°)·native"，而是"永远 = 发布出口发出去的那一份"。
    print("[2] KSQ_GRIPPER_SLAM_FRAME=right_forward_up（回退旧约定）")
    saved_env = os.environ.get(settings.GRIPPER_SLAM_FRAME_ENV)
    os.environ[settings.GRIPPER_SLAM_FRAME_ENV] = (
        settings.GRIPPER_SLAM_FRAME_LEGACY)
    try:
        controller = make_controller()
        check("controller 确实关掉了重标（本次会话解析一次）",
              controller._publish_relabel is False)
        counters = {"raw": 0, "valid": 0}
        feed(controller, counters, 1.0, native)
        latest = controller.read_latest_pose()[0]
        check("回退时 _latest_pose = 中间帧（实际发布的那一帧）",
              latest is not None and np.allclose(latest.position, mid, atol=1e-9),
              f"实得 {None if latest is None else latest.position}，"
              f"中间帧 {tuple(mid)}")
        check("回退时 _latest_pose 仍不是 native",
              latest is not None and not np.allclose(latest.position, native,
                                                     atol=1e-9),
              f"实得 {None if latest is None else latest.position}")
        buffered = controller.pose_buffer_snapshot()
        check("回退时 _latest_pose 与 _pose_buffer 仍同帧",
              latest is not None and np.allclose(
                  latest.position, buffered[max(buffered)][:3], atol=1e-12),
              f"latest={None if latest is None else latest.position} "
              f"buffer={buffered[max(buffered)][:3]}")
    finally:
        if saved_env is None:
            os.environ.pop(settings.GRIPPER_SLAM_FRAME_ENV, None)
        else:
            os.environ[settings.GRIPPER_SLAM_FRAME_ENV] = saved_env

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 — {_FAILS}")
        return 1
    print("ALL PASS: _latest_pose / read_latest_pose 装的是发布帧"
          "（+X下/+Y左/+Z前），native 只留在 _last_accepted_pose")
    return 0


if __name__ == "__main__":
    sys.exit(main())
