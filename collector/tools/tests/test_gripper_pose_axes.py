"""rotate_pose_z90 数学性质单测（离线，无硬件）。

验证 S80M 坐标约定（正对=+Y、Z=上、X=右）旋转的几何正确性。
真机实测定案（2026-09-09 新 ORB 核心上线后）：native 输出相对物理系
为 R_z(−90°)（native X=正对、Y=左、Z=上，与桥接 .cc 注释约定一致；
旧核心时代为 R_x(+90°)，已随换核失效）；
修正 = 位置与姿态统一纯左乘 R_z(+90°)（纯世界重标，无体轴共轭）：
- 位置 (x,y,z)→(−y,x,z)；四元数 q' = q_z(+90°)⊗q
- 轴映射：native X(正对)→+Y，native −Y(右)→+X，native Z(上)→+Z
- 用户实测复现：物理 +Y → native +X → 显示 +Y；物理 +X → native −Y
  → 显示 +X
- 静止首帧姿态 = R_z(90°)·correction（部署校正常数残留 =
  R_x(−90°)，非单位矩阵）；原点后姿态相对化（process_controller
  捕获首帧显示姿态为基准，q0⁻¹⊗q）后首帧恒为单位矩阵——帧轴与
  世界轴对齐，无起始跳变；转体后绘制帧轴 = 真实相对姿态轴
- 随机位姿：R' = R_z(+90°)·R（正规旋转：正交、det=+1）
- 双应用 = R_z(+180°)；轨迹相邻点距离不变（旋转是等距变换）
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.gripper.slam.protocol import (
    PoseSample, quat_conjugate, quat_product, rotate_pose_z90,
)


def quat_to_matrix(qx, qy, qz, qw):
    return np.array([
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw),
         2 * (qx * qz + qy * qw)],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz),
         2 * (qy * qz - qx * qw)],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw),
         1 - 2 * (qx * qx + qy * qy)],
    ])


def random_unit_quaternion(rng):
    v = rng.standard_normal(4)
    v /= np.linalg.norm(v)
    return tuple(v)   # (qx, qy, qz, qw)


def main():
    rng = np.random.default_rng(20260909)
    Rzp90 = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    # 部署桥接校正矩阵（source 值 R_y(90°)·R_z(−90°)），与新核心
    # 世界系合成 native 约定；静止首帧 native 姿态 = correction
    correction = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0],
                           [0.0, -1.0, 0.0]])
    c = math.sqrt(0.5)
    failures = []

    # 1. 轴映射（native 系：X=正对 Y=左 Z=上 → 目标：X=右 Y=正对 Z=上）
    axis_cases = [
        # (native 位移, 期望显示位移, 说明)
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), "正对(native X) → +Y"),
        ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), "右(native −Y) → +X"),
        ((0.0, 0.0, 1.0), (0.0, 0.0, 1.0), "上(native Z) → +Z"),
        ((0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), "左(native Y) → −X"),
    ]
    for native, expected, label in axis_cases:
        pose = PoseSample(position=native, rotation=(0, 0, 0, 1),
                          timestamp=1.0)
        out = rotate_pose_z90(pose)
        if tuple(out.position) != expected:
            failures.append(f"轴映射错误 [{label}]: {out.position} != {expected}")

    # 2. 用户实测复现（新核心，2026-09-09）：物理 +Y → 显示 +Y；
    #    物理 +X → 显示 +X；物理 +Z → 显示 +Z
    #    native = R_z(−90°)·物理（native X=正对、Y=左、Z=上）
    Rzm90 = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    for phys_axis, label in [
        (np.array([0.0, 1.0, 0.0]), "物理+Y"),
        (np.array([1.0, 0.0, 0.0]), "物理+X"),
        (np.array([0.0, 0.0, 1.0]), "物理+Z"),
    ]:
        native = tuple(Rzm90 @ phys_axis)
        out = rotate_pose_z90(PoseSample(position=native,
                                         rotation=(0, 0, 0, 1),
                                         timestamp=1.0))
        if not np.allclose(np.array(out.position), phys_axis, atol=1e-12):
            failures.append(f"{label} 修正后 != 物理轴: {out.position}")

    # 3. 随机位姿：R' = R_z(+90°)·R（纯左乘），正规性
    for i in range(200):
        pos = tuple(float(v) for v in rng.uniform(-2, 2, 3))
        quat = random_unit_quaternion(rng)
        pose = PoseSample(position=pos, rotation=quat, timestamp=1.0 + i)
        out = rotate_pose_z90(pose)
        expected_pos = (-pos[1], pos[0], pos[2])
        if tuple(out.position) != expected_pos:
            failures.append(f"#{i} position 应为 (−y,x,z)")
            continue
        R = quat_to_matrix(*quat)
        Rm = quat_to_matrix(*out.rotation)
        if not np.allclose(Rm, Rzp90 @ R, atol=1e-12):
            failures.append(f"#{i} R' != R_z(+90°)·R")
            continue
        if not np.allclose(Rm @ Rm.T, np.eye(3), atol=1e-12):
            failures.append(f"#{i} R' not orthonormal")
        if not np.isclose(np.linalg.det(Rm), 1.0, atol=1e-9):
            failures.append(f"#{i} det(R') != +1")

    # 4. 静止首帧姿态：native = correction → 显示 R_z(90°)·correction
    #    = R_x(−90°)（部署校正常数残留，非单位矩阵）；经原点相对化
    #    q0⁻¹⊗q 后 = 单位矩阵（帧轴与世界轴对齐，无起始跳变）
    q_corr = (-0.5, 0.5, -0.5, 0.5)   # R_y(90°)·R_z(−90°) 的四元数
    out = rotate_pose_z90(PoseSample(position=(0.0, 0.0, 0.0),
                                     rotation=q_corr, timestamp=1.0))
    Rxm90 = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])
    if not np.allclose(quat_to_matrix(*out.rotation), Rxm90, atol=1e-12):
        failures.append("静止首帧显示姿态应为 R_x(−90°)（常数残留）")
    q0 = out.rotation   # process_controller 捕获的首帧基准
    zeroed = quat_product(quat_conjugate(q0), q0)
    if not np.allclose(zeroed, (0.0, 0.0, 0.0, 1.0), atol=1e-12):
        failures.append("原点相对化后首帧姿态应为单位矩阵（帧轴=世界轴）")

    # 5. 转体跟踪：夹爪转向右（物理 R_z(−90°)，原点 ENTER=I）→
    #    native = correction·R_z(−90°)；显示 = R_z(90°)·native，再经
    #    相对化（q0⁻¹⊗q，q0 取自用例 4）→ R_z(−90°)，帧 Y 轴 = 右
    q_z_m90 = (0.0, 0.0, -c, c)
    q_native = quat_product(q_corr, q_z_m90)
    out = rotate_pose_z90(PoseSample(position=(0.0, 0.0, 0.0),
                                     rotation=q_native, timestamp=1.0))
    q_disp = quat_product(quat_conjugate(q0), out.rotation)
    drawn_y = quat_to_matrix(*q_disp)[:, 1]
    if not np.allclose(drawn_y, (1.0, 0.0, 0.0), atol=1e-12):
        failures.append(f"转向右后帧 Y 轴应为 (1,0,0)，实际 {drawn_y}")

    # 6. 双应用 = R_z(+180°)：位置 (−x,−y,z)，四元数 (−y,x,w,−z)
    pose = PoseSample(position=(1.0, 2.0, 3.0),
                      rotation=random_unit_quaternion(rng), timestamp=1.0)
    twice = rotate_pose_z90(rotate_pose_z90(pose))
    if tuple(twice.position) != (-1.0, -2.0, 3.0):
        failures.append("双应用位置应为 (−x,−y,z)")
    expected_quat = (-pose.rotation[1], pose.rotation[0],
                     pose.rotation[3], -pose.rotation[2])
    if not np.allclose(twice.rotation, expected_quat, atol=1e-12):
        failures.append("双应用四元数应为 (−y,x,w,−z)")
    R2 = quat_to_matrix(*twice.rotation)
    Rz180 = Rzp90 @ Rzp90
    if not np.allclose(R2, Rz180 @ quat_to_matrix(*pose.rotation),
                       atol=1e-12):
        failures.append("双应用旋转应为 R_z(+180°)·R")

    # 7. 轨迹形状不变（等距）：相邻点距离一致
    traj = [
        PoseSample(position=tuple(float(v) for v in rng.uniform(-1, 1, 3)),
                   rotation=random_unit_quaternion(rng), timestamp=float(i))
        for i in range(50)
    ]
    for a, b in zip(traj, traj[1:]):
        d1 = np.linalg.norm(np.array(a.position) - np.array(b.position))
        ma, mb = rotate_pose_z90(a), rotate_pose_z90(b)
        d2 = np.linalg.norm(np.array(ma.position) - np.array(mb.position))
        if not np.isclose(d1, d2, atol=1e-12):
            failures.append("轨迹相邻点距离被旋转改变")

    # 8. 四元数辅助：共轭逆 = 单位矩阵；Hamilton 积 = 矩阵左乘
    for i in range(200):
        a = random_unit_quaternion(rng)
        b = random_unit_quaternion(rng)
        prod = quat_product(quat_conjugate(a), a)
        if not np.allclose(prod, (0.0, 0.0, 0.0, 1.0), atol=1e-12):
            failures.append(f"#{i} q⁻¹⊗q != 单位四元数")
            break
        Ra, Rb = quat_to_matrix(*a), quat_to_matrix(*b)
        Rab = quat_to_matrix(*quat_product(a, b))
        if not np.allclose(Rab, Ra @ Rb, atol=1e-12):
            failures.append(f"#{i} (a⊗b) 矩阵 != A·B")
            break

    # 9. 原点相对化：首帧姿态 q0（含部署校正/启动朝向的任意常数
    #    旋转），此后 q0⁻¹⊗q 应还原为相对原点的真实转动
    for i in range(200):
        q0r = random_unit_quaternion(rng)
        q_rel = random_unit_quaternion(rng)
        q = quat_product(q0r, q_rel)
        recovered = quat_product(quat_conjugate(q0r), q)
        if not np.allclose(recovered, q_rel, atol=1e-12):
            failures.append(f"#{i} 相对化未还原相对转动")
            break
        if not np.allclose(
            quat_to_matrix(*recovered),
            quat_to_matrix(*q_rel), atol=1e-12,
        ):
            failures.append(f"#{i} 相对化矩阵不一致")
            break

    if failures:
        print("FAIL:")
        for f in failures[:5]:
            print("  ", f)
        return 1
    print("PASS: rotate_pose_z90 数学性质全部通过（轴映射/实测复现/200 随机位姿/静止残留+相对化/转体跟踪/双应用/轨迹等距/四元数积）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
