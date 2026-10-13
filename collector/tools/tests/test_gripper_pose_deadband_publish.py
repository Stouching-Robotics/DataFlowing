#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布出口的死区集成测试：走真 SlamProcessController（v1.3.14）。

    venv/bin/python tools/tests/test_gripper_pose_deadband_publish.py

背景：用户报「大幅漂移没了，静止时轨迹线/末端点还有小抖」。实测那是估计器的
毫米级有界游走（严格静止窗：位置游走半径逐会话 p50 1.06-1.85mm/max 1.39-4.98mm；
姿态峰峰 p50 0.25°/max 1.80°），单帧步长远在跳变门
限值之下，所以跳变门（比相邻样本）管不着。修法是在**发布出口**对"上次发出的
位置"设死区：不足死区就发上次的值。

单元测试（test_pose_publish_deadband.py）钉的是函数语义；这里钉的是**接线**：
死区必须真的作用在 _publish_pose 出口那条路上（UI/_on_pose/轨迹/orb_pose.json
全从这一条路出去），而且不能把下面这些既有契约弄坏：

  1. 静止游走：发出去的值冻结（_on_pose 收到的位置恒定），末端点不再抖
  2. 采样节拍不变：每帧仍算 valid、raw_seq 仍逐帧递增、lost_count 不得虚增
     （死区是"发旧值"，不是"丢帧"——丢帧会让 GUI 的位姿丢失计数虚报）
  3. orb_pose.json 里也是冻结值（GUI/控制通道读的就是它）
  4. 真实移动照常跟踪（误差 ≤ 死区）
  5. 重设原点后死区参照被丢掉（否则会拿旧锚下的位置比新锚下的位置）
  6. 落盘未启用的分支（pose_paths 为空）同样生效

退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from core.gripper.slam.process_controller import (              # noqa: E402
    SlamProcessController,
    SlamState,
)
from core.gripper.slam.protocol import (                        # noqa: E402
    POSE_PUBLISH_DEADBAND_M,
    PoseSample,
    ProtocolEvent,
    parse_slam_line,
    relabel_published_frame,
    rotate_pose_z90,
)

FAILS: list[str] = []
EPS = POSE_PUBLISH_DEADBAND_M


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def make_controller(pose_paths=(None, None), log=None) -> SlamProcessController:
    return SlamProcessController(
        SlamState(),
        executable="/bin/true",
        vocabulary="/dev/null",
        settings="/dev/null",
        device_config="/dev/null",
        trajectory="/dev/null",
        work_dir="/tmp",
        pose_paths=pose_paths,
        log=log,
        runtime_env_factory=lambda: {},
        cpu_roles={
            "fays_left_orb": (0,),
            "fays_right_orb": (1,),
            "fays_background": (2,),
        },
    )


def feed(controller, counters, timestamp, position,
         quat=(1.0, 0.0, 0.0, 0.0)):
    """喂一行**真格式**的 native 位姿（走真解析器，别手工造 PoseSample）。"""
    x, y, z = position
    qw, qx, qy, qz = quat
    line = (f"[{timestamp:.6f}] XYZ:({x:.6f},{y:.6f},{z:.6f}) "
            f"Quat:(w={qw:.6f},x={qx:.6f},y={qy:.6f},z={qz:.6f})")
    event = parse_slam_line(line)
    assert event is not None and event.kind == "pose", f"{line!r} -> {event!r}"
    controller._handle_event(event, counters, arrival_monotonic=timestamp,
                            arrival_wall_time=timestamp)
    pose, _, _ = controller.read_latest_pose()
    return pose


def rest_wander(frames, seed=20261013, sigma=0.00010, a=0.96):
    """实测形状的静止游走：有界小球（AR(1)），不是越走越远的随机游走。"""
    rng = random.Random(seed)
    pos = [0.0, 0.0, 0.0]
    out = []
    for _ in range(frames):
        pos = [a * pos[0] + rng.gauss(0.0, sigma),
               a * pos[1] + rng.gauss(0.0, sigma),
               a * pos[2] + rng.gauss(0.0, sigma)]
        out.append(tuple(pos))
    return out


def published_frame_images(raw_positions):
    """把每个原始（中间帧）位置独立过一遍发布变换，得到"合法发布值"集合。

    发布值若都落在其中，说明死区是**替换**（发的是某一帧真值），而不是插值/
    平均/自造的值 —— 这是"发上次的值"这句话唯一能被证伪的形式（第 1 节那句
    「连续帧逐分量相同 or distinct<=3」是恒真的，钉不住东西）。

    ⚠ 先按喂入时的**线上量化**（feed 用 %.6f 写行）还原，否则算出来的像与
    控制器真正收到的值差在 6 位小数之外，集合对不上。
    """
    images = set()
    for p in raw_positions:
        q = tuple(float(f"{float(v):.6f}") for v in p)
        s = relabel_published_frame(rotate_pose_z90(
            PoseSample(position=q, rotation=(0.0, 0.0, 0.0, 1.0),
                       timestamp=0.0)))
        images.add(tuple(round(float(v), 12) for v in s.position))
    return images


def main() -> int:
    print("发布出口死区集成测试（v1.3.14）")

    # ---------------------------------------------------------- 静止游走
    for label, pose_paths in (("落盘未启用（pose_paths 空）", (None, None)),
                              ("落盘启用（写 orb_pose.json）", None)):
        if pose_paths is None:
            tmpdir = tempfile.mkdtemp(prefix="pose_deadband_")
            pose_paths = (os.path.join(tmpdir, "orb_pose.json.tmp"),
                          os.path.join(tmpdir, "orb_pose.json"))
        print(f"[1] 静止游走冻结 · {label}")
        controller = make_controller(pose_paths)
        counters = {"raw": 0, "valid": 0}

        raw = rest_wander(400)
        published = []
        for i, p in enumerate(raw):
            pose = feed(controller, counters, 1.0 + i * 0.02, p)
            published.append(tuple(pose.position))
        raw_radius = max(math.dist(p, (0.0, 0.0, 0.0)) for p in raw)
        distinct = len(set(published))

        check("合成游走本身有实测量级的幅度（不是空断言）",
              0.0004 < raw_radius < 0.004,
              f"半径 {raw_radius*1000:.2f}mm")
        check("发出去的位置被冻结",
              distinct <= 3,
              f"400 帧只发出 {distinct} 个不同位置"
              f"（死区 {EPS*1000:.0f}mm，游走半径"
              f" {raw_radius*1000:.2f}mm）")
        transitions = sum(1 for i in range(1, len(published))
                          if published[i] != published[i - 1])
        check("末端点保持：相邻帧发布值相同，只有越过死区的那几帧才变",
              transitions <= 3,
              f"{transitions} 次变化 / {len(published) - 1} 帧"
              f"（游走半径 {raw_radius*1000:.2f}mm 全在死区 "
              f"{EPS*1000:.0f}mm 内 ⇒ 本该 0 次）")
        images = published_frame_images(raw)
        rounded = [tuple(round(float(v), 12) for v in p) for p in published]
        check("替换而非平滑：每个发布值都是某一帧原始值的发布帧像",
              all(p in images for p in rounded),
              f"{sum(1 for p in rounded if p not in images)} 个发布值不在"
              "原始值像集合里（插值/平均会落到集合外）")
        check("采样节拍不变：每帧都算 valid",
              counters["valid"] == len(raw),
              f"valid={counters['valid']} / 喂了 {len(raw)} 帧")
        check("采样节拍不变：raw_seq 逐帧递增",
              controller._pose_raw_seq == len(raw))
        check("位姿丢失计数不得虚增（死区是发旧值，不是丢帧）",
              controller.pose_lost_count() == 0,
              f"lost_count={controller.pose_lost_count()}")
        if pose_paths[1]:
            with open(pose_paths[1], encoding="utf-8") as fh:
                on_disk = json.load(fh)
            check("orb_pose.json 里也是冻结值",
                  (on_disk["x"], on_disk["y"], on_disk["z"])
                  == published[-1])
        else:
            check("落盘未启用分支同样生效（有 pose 返回、有冻结）",
                  published[-1] is not None and distinct <= 3)

    # ---------------------------------------------------------- 真实移动
    print("[2] 真实移动照常跟踪")
    controller = make_controller()
    counters = {"raw": 0, "valid": 0}
    raw = [(0.01 * i, 0.0, 0.0) for i in range(200)]   # 10mm/帧 ≈ 0.3m/s
    err = 0.0
    for i, p in enumerate(raw):
        pose = feed(controller, counters, 2.0 + i * 0.02, p)
        expected = _published_position(p)
        err = max(err, math.dist(pose.position, expected))
    check("快速移动逐帧跟上",
          err < 1e-6,
          f"与独立复算的发布帧位置最大差 {err:.2e}m")
    check("移动中丢失计数仍为 0", controller.pose_lost_count() == 0)

    # 慢速移动（每帧 0.5mm < 死区）：量化但不丢，误差不累积
    controller = make_controller()
    counters = {"raw": 0, "valid": 0}
    raw = [(0.0005 * i, 0.0, 0.0) for i in range(200)]
    err = 0.0
    for i, p in enumerate(raw):
        pose = feed(controller, counters, 3.0 + i * 0.02, p)
        err = max(err, math.dist(pose.position, _published_position(p)))
    check("慢速移动（0.5mm/帧）误差不超死区",
          err <= EPS + 1e-9,
          f"最大误差 {err*1000:.2f}mm ≤ {EPS*1000:.0f}mm")
    final, _, _ = controller.read_latest_pose()
    check("慢速移动的末端仍跟到真值附近",
          math.dist(final.position, _published_position(raw[-1])) <= EPS + 1e-9)

    # ---------------------------------------------------------- 重设原点
    # 注意：位置归零是 **native** 做的（原点是 C++ 帧的事），Python 这一层
    # 只把姿态按新锚相对化。所以这里能钉的是"死区参照跟着丢"：重锚后即使
    # 新位置离旧锚下的发布值不到一个死区，也必须照发，而不是被旧参照顶掉。
    print("[3] 重设原点后参照要丢掉")
    controller = make_controller()
    counters = {"raw": 0, "valid": 0}
    anchor_native = (0.3, 0.2, 0.1)
    pose = feed(controller, counters, 4.0, anchor_native)
    before = controller._last_published_position
    before_published = tuple(pose.position)
    check("重设原点前参照已建立", before is not None)
    check("重设原点前参照 = 当前发布位置",
          before is not None and math.dist(before, before_published) < 1e-9)
    controller._handle_event(
        ProtocolEvent("origin", "[ORIGIN SET]", "[ORIGIN SET]"),
        counters, arrival_monotonic=4.1, arrival_wall_time=4.1)
    check("重设原点后参照被丢弃", controller._last_published_position is None,
          f"参照={controller._last_published_position}")
    # 新锚下第一帧：相对旧锚下的发布值只挪 0.5mm（< 死区 3mm）。
    # 参照没丢的话这一帧会被旧值顶掉 —— 那正是"重锚后位置跳变被吞"的病。
    nudged_native = (0.3, 0.2, 0.1005)
    pose = feed(controller, counters, 4.2, nudged_native)
    check("重锚后参照重新建立",
          controller._last_published_position is not None)
    check("重锚后首帧照发（不足死区也放行，没被旧锚的参照顶掉）",
          pose is not None
          and math.dist(pose.position,
                        _published_position(nudged_native)) < 1e-9,
          f"发布位置={tuple(pose.position) if pose else None}"
          f"（期望 {_published_position(nudged_native)}）")
    check("重锚后首帧参照取的是新帧的值",
          math.dist(controller._last_published_position,
                    _published_position(nudged_native)) < 1e-9)

    # ---------------------------------------------------------- 开机可见
    # 用户是拿 GUI 验收的：「改了但界面上看不出来」等于没改。死区值必须在
    # 开机日志里有一行，用户才能在 SLAM 日志面板里确认当次生效的是 3.0mm。
    print("[4] 开机日志里能看见当次生效的死区（界面可验收）")
    lines: list[str] = []
    controller = make_controller(log=lines.append)
    # READY 是 native 报「可以按回车」的那一行，每次起流只来一次
    ready = parse_slam_line("[SLAM] READY")
    assert ready is None or ready.kind != "pose"
    controller._handle_event(ProtocolEvent("ready", "READY", "READY"),
                             {"raw": 0, "valid": 0},
                             arrival_monotonic=0.0, arrival_wall_time=0.0)
    hit = [ln for ln in lines if "死区" in ln]
    check("开机打出一行死区说明（GUI 日志面板能看见）",
          len(hit) == 1,
          f"{hit[0] if hit else '一行都没有'}")
    check("行里的数值就是当次生效的死区",
          bool(hit) and f"{EPS * 1000:.1f}mm" in hit[0],
          f"期望含 {EPS * 1000:.1f}mm")

    if FAILS:
        print(f"\nFAIL: {len(FAILS)} 条")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"\nPASS: 死区 {EPS*1000:.0f}mm 在发布出口生效——静止发旧值（节拍/序号/"
          "丢失计数不变）、移动照常、重锚丢参照")
    return 0


def _published_position(native_position):
    """独立复算发布帧位置（native → R_z(+90°) → 世界重标 M）。"""
    from core.gripper.slam.protocol import (
        PoseSample, relabel_published_frame, rotate_pose_z90)
    return relabel_published_frame(rotate_pose_z90(PoseSample(
        position=native_position, rotation=(0.0, 0.0, 0.0, 1.0),
        timestamp=0.0))).position


if __name__ == "__main__":
    sys.exit(main())
