"""位姿连续性门的门槛单测（v1.3.14）。

`process_controller._publish_pose` 前的那道门（`protocol.pose_rejection_reason`）
和 native 桥里的同门（`fayssense_orb_slam.cc` 的 `kPoseStepAllowanceM` 一族）
是**同一个判据的两份实现**，谁动谁都得同步。历史上没测试钉过它们，于是
「60ms 帧把限值抬到 0.23m」这个洞一直是靠肉眼发现——直到 2026-10-12 它在
真机上放行了一次 0.224m 的平移跳变，GUI 里就是那个「抖/飘」。

这里钉三件事：
  ① 两处常量必须逐字相等（防镜像漂移）；
  ② 实测包络：69061 条发布位姿里，**真实**单帧步长最大 0.0601m（p99 0.043）、
     最小异常步长 0.1075m —— 限值必须夹在中间，且在降采样 60ms 帧上不变；
  ③ 门给出的 reason 文本仍然区分 平移跳/旋转跳/时间戳回退/非法分量。

实测来源：logging 里的 `[SLAM] Pose held:` 与 native `[POSE_REBASE]`，
以及 logs/slam_native/20261012_*_3500000262300100_slam_stdout.log 的
A/B 分支交替段（同一四元数、仅平移在两组之间来回）。

用法:
    venv/bin/python tools/tests/test_pose_guard_limits.py
"""
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.gripper.slam import protocol as proto  # noqa: E402

NATIVE_CC = os.path.join(
    ROOT,
    "core/gripper/orb_slam_src/ORB-SLAM/Examples/fays/fayssense_orb_slam.cc",
)

# 实测包络（见模块 docstring）。
GENUINE_MAX_STEP_M = 0.0601        # 真实单帧平移步长上限
GENUINE_P99_STEP_M = 0.0430
ARTIFACT_MIN_STEP_M = 0.1075       # 最小的一次估计器平移跳变
ARTIFACT_MAX_STEP_M = 0.2238       # 旧的 60ms 限值下被原样放行的那一档
GENUINE_MAX_ROT_DEG = 15.75
ARTIFACT_ROT_DEG = 83.405502       # main.log 2026-10-12 14:25:51 rotation_jump

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def quat_about_x(deg):
    half = math.radians(deg) / 2.0
    return (math.cos(half), math.sin(half), 0.0, 0.0)


def pose(position, timestamp, rotation=(1.0, 0.0, 0.0, 0.0)):
    return proto.PoseSample(
        position=tuple(position), rotation=tuple(rotation),
        timestamp=timestamp, sequence=1,
    )


def step(distance_m, dt_s, base=(0.0, 0.0, 0.0), rot=(1.0, 0.0, 0.0, 0.0)):
    """沿 +x 走 distance_m，间隔 dt_s，返回 (reason, previous, current)。"""
    prev = pose(base, 1000.0)
    cur = pose(
        (base[0] + distance_m, base[1], base[2]), 1000.0 + dt_s, rotation=rot)
    return proto.pose_rejection_reason(cur, prev), prev, cur


def test_constants_mirror():
    """① native 与 Python 的门槛必须逐字相等。"""
    if not os.path.exists(NATIVE_CC):
        check(False, f"找不到 native 源文件 {NATIVE_CC}（镜像检查无法进行）")
        return
    with open(NATIVE_CC, encoding="utf-8") as fh:
        src = fh.read()

    def native(name):
        m = re.search(rf"{name}\s*=\s*(-?[\d.]+)f?\s*;", src)
        return float(m.group(1)) if m else None

    def native_degrees(name):
        """角度常量在 native 里写成 `20.0f * M_PI / 180.0f`，取度数那一项。"""
        m = re.search(rf"{name}\s*=\s*(-?[\d.]+)f?\s*\*\s*static_cast", src)
        if m is None:
            m = re.search(rf"{name}\s*=\s*(-?[\d.]+)f?\s*\*\s*M_PI", src)
        return float(m.group(1)) if m else None

    pairs = (
        ("kPoseStepAllowanceM", "POSE_STEP_ALLOWANCE_M"),
        ("kPoseMaxLinearSpeedMps", "POSE_MAX_LINEAR_SPEED_MPS"),
        ("kPoseMaxGuardIntervalS", "POSE_MAX_GUARD_INTERVAL_S"),
    )
    for native_name, py_name in pairs:
        nv = native(native_name)
        pv = getattr(proto, py_name)
        check(
            nv is not None and abs(nv - pv) < 1e-12,
            f"{native_name}={nv} 与 {py_name}={pv} 一致",
        )

    deg_native = native_degrees("kPoseAngleAllowanceRad")
    check(
        deg_native is not None
        and abs(math.radians(deg_native) - proto.POSE_ANGLE_ALLOWANCE_RAD) < 1e-12,
        f"kPoseAngleAllowanceRad（{deg_native}°）与 POSE_ANGLE_ALLOWANCE_RAD 一致",
    )
    speed_native = native_degrees("kPoseMaxAngularSpeedRadS")
    check(
        speed_native is not None
        and abs(math.radians(speed_native) - proto.POSE_MAX_ANGULAR_SPEED_RAD_S) < 1e-12,
        f"kPoseMaxAngularSpeedRadS（{speed_native}°/s）"
        "与 POSE_MAX_ANGULAR_SPEED_RAD_S 一致",
    )


def test_genuine_steps_accepted():
    """② 真实步长（含 p99）在任何帧间隔下都必须放行。"""
    for dt in (0.02, 0.06):
        for dist in (GENUINE_P99_STEP_M, GENUINE_MAX_STEP_M):
            reason, _, _ = step(dist, dt)
            check(
                reason is None,
                f"真实步长 {dist:.4f}m @ dt={dt:.2f}s 被放行（reason={reason}）",
            )
    # 静止时的估计器微调也是真实数据的一部分，不能被误伤。
    reason, _, _ = step(0.0, 0.06)
    check(reason is None, "零位移 @ dt=0.06s 被放行")


def test_artifacts_rejected_at_every_interval():
    """② 估计器平移跳变在 20ms 和 60ms 帧上都必须被拦下。

    修的就是这条：旧门槛 charge 了 60ms，限值 0.23m，把 0.107-0.224m
    原样放了出去。降采样帧不该享有 3 倍宽限——跳变恰好落在那一帧上。
    """
    for dt in (0.02, 0.04, 0.06):
        for dist in (ARTIFACT_MIN_STEP_M, ARTIFACT_MAX_STEP_M):
            reason, prev, cur = step(dist, dt)
            check(
                reason is not None and "translation step" in reason,
                f"跳变 {dist:.4f}m @ dt={dt:.2f}s 被拦下（reason={reason}）",
            )
        limit = proto.POSE_STEP_ALLOWANCE_M + (
            proto.POSE_MAX_LINEAR_SPEED_MPS * min(
                dt, proto.POSE_MAX_GUARD_INTERVAL_S)
        )
        check(
            limit < ARTIFACT_MIN_STEP_M,
            f"dt={dt:.2f}s 的限值 {limit:.4f}m 低于最小跳变 "
            f"{ARTIFACT_MIN_STEP_M:.4f}m",
        )
        check(
            limit > GENUINE_MAX_STEP_M,
            f"dt={dt:.2f}s 的限值 {limit:.4f}m 高于最大真实步长 "
            f"{GENUINE_MAX_STEP_M:.4f}m",
        )


def test_interval_cap_removes_decimation_inflation():
    """② 限值不再随 60ms 降采样帧膨胀——这是修复的本质。

    限值对 dt 仍是线性的（正常：帧隔得久，允许走得远），但只线性到
    名义帧周期为止。修复前它一直线性到 0.1s，于是 60ms 帧拿到 0.23m——
    而跳变恰好落在那一帧上。现在 60ms 帧与 30ms 帧同额。
    """
    def limit(dt):
        return (
            proto.POSE_STEP_ALLOWANCE_M
            + proto.POSE_MAX_LINEAR_SPEED_MPS
            * min(dt, proto.POSE_MAX_GUARD_INTERVAL_S)
        )

    cap = proto.POSE_MAX_GUARD_INTERVAL_S
    check(
        cap <= 0.04,
        f"间隔上限 {cap}s 不超过名义帧周期量级（20/20/60ms 的 60ms）",
    )
    check(
        limit(0.06) == limit(cap) == limit(0.5),
        f"60ms 及更长的帧与 {cap * 1000:.0f}ms 帧同额"
        f"（限值 {limit(0.06):.4f}m，旧门槛这里是 0.2300m）",
    )
    check(
        limit(0.02) < limit(0.06),
        f"20ms 帧仍比 60ms 帧严（{limit(0.02):.4f}m < {limit(0.06):.4f}m），"
        "即限值随真实帧间隔单调、不是常数",
    )
    check(
        limit(0.06) < ARTIFACT_MIN_STEP_M,
        f"最宽的 60ms 档限值 {limit(0.06):.4f}m 仍低于最小跳变 "
        f"{ARTIFACT_MIN_STEP_M:.4f}m",
    )


def test_rotation_steps():
    """② 真实旋转步长放行，83° 的旋转跳变拦下。"""
    limit_60 = proto.POSE_ANGLE_ALLOWANCE_RAD + (
        proto.POSE_MAX_ANGULAR_SPEED_RAD_S
        * min(0.06, proto.POSE_MAX_GUARD_INTERVAL_S)
    )
    check(
        math.degrees(limit_60) > GENUINE_MAX_ROT_DEG * 2.0,
        f"dt=0.06s 的旋转限值 {math.degrees(limit_60):.1f}° 仍是最大真实旋转步长"
        f" {GENUINE_MAX_ROT_DEG}° 的 2 倍以上余量",
    )
    for dt in (0.02, 0.06):
        reason, _, _ = step(0.0, dt, rot=quat_about_x(GENUINE_MAX_ROT_DEG))
        check(
            reason is None,
            f"真实旋转步长 {GENUINE_MAX_ROT_DEG}° @ dt={dt:.2f}s 被放行",
        )
        reason, _, _ = step(0.0, dt, rot=quat_about_x(ARTIFACT_ROT_DEG))
        check(
            reason is not None and "rotation step" in reason,
            f"旋转跳变 {ARTIFACT_ROT_DEG}° @ dt={dt:.2f}s 被拦下"
            f"（reason={reason}）",
        )


def test_reason_taxonomy():
    """③ reason 文本仍然区分四类，且优先级不变（时间戳先于跳变）。"""
    prev = pose((0.0, 0.0, 0.0), 1000.0)

    cur = pose((1.0, 0.0, 0.0), 1000.0 + 0.02)
    check(
        proto.pose_rejection_reason(cur, prev) ==
        "translation step 1.000m exceeds 0.070m",
        "平移跳的 reason 文本与数值格式未变",
    )

    cur = pose((0.0, 0.0, 0.0), 1000.0)
    check(
        proto.pose_rejection_reason(cur, prev) ==
        "non-monotonic pose timestamp",
        "时间戳回退的 reason 未变",
    )

    cur = pose((0.0, 0.0, float("nan")), 1000.0 + 0.02)
    check(
        proto.pose_rejection_reason(cur, prev) == "invalid pose components",
        "非法分量的 reason 未变",
    )

    check(
        proto.pose_rejection_reason(prev, None) is None,
        "无前帧时不设限（会话首帧必须放行）",
    )

    # 时间戳回退优先于平移跳：先判时间基，再判步长。
    cur = pose((1.0, 0.0, 0.0), 1000.0)
    check(
        proto.pose_rejection_reason(cur, prev) ==
        "non-monotonic pose timestamp",
        "时间戳回退优先于平移跳报出",
    )


def main():
    print("位姿门门槛单测（v1.3.14）")
    print("[1] 常量镜像（native ⇄ Python）")
    test_constants_mirror()
    print("[2] 实测包络")
    test_genuine_steps_accepted()
    test_artifacts_rejected_at_every_interval()
    test_interval_cap_removes_decimation_inflation()
    test_rotation_steps()
    print("[3] reason 分类")
    test_reason_taxonomy()

    if FAILS:
        print(f"\nFAIL: {len(FAILS)} 条")
        for f in FAILS:
            print("  -", f)
        return 1
    print(
        "\nPASS: 门槛夹在实测包络中间（真实 ≤0.0601m 放行 / 异常 ≥0.1075m 拦下），"
        "限值不再随 20/20/60ms 降采样帧膨胀，native 与 Python 逐字一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
