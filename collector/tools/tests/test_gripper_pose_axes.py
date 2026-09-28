"""发布位姿坐标帧的数学性质单测（离线，无硬件）。

发布帧 = 中间帧 {X=右, Y=正对, Z=上} 再叠一次世界重标（v1.3.13 起）
       = **+X=下 / +Y=左 / +Z=前**，右手系。
链路（见 process_controller._publish_pose / protocol.finish_publish_pose）：

    native --rotate_pose_z90--> 中间帧 --相对化--> --relabel--> 发布帧

- native 约定（2026-09-09 真机实测定案，新 ORB 核心上线后）：
  R_z(−90°)，native X=正对、Y=左、Z=上（实测：物理 +Y(正对) 位移
  → native +X；物理 +X(右) → native −Y）
- 第 1 步 rotate_pose_z90 = 纯左乘 R_z(+90°)：位置 (−y,x,z)，
  四元数 q_z(+90°)⊗q 化简为 c·(x−y, y+x, z+w, w−z)，c=√2/2
  → 中间帧 X=右、Y=夹爪正对、Z=上（等价 ENU）
- 第 3 步 relabel：位置**左乘** M，姿态**左右各乘一次** M·R·Mᵀ。
  换基矩阵（行 = 新基在旧坐标下的分量）
  M = R_y(−90°)·R_z(−90°) = [[0,0,-1],[-1,0,0],[0,1,0]]，
  det=+1、绕 (1,-1,-1)/√3 转 120°，四元数 q_M=(0.5,-0.5,-0.5,0.5)，
  位置 (x,y,z)→(−z,−x,y)
  姿态右边那次 Mᵀ 是**夹爪体轴改名**：体轴从此跟世界轴共用一套词
  （+X=体下 / +Y=体左 / +Z=体前）。只左乘不右乘的话，发出去的姿态是
  "世界换了名、体轴没换名"，GUI 里跟随夹爪的三支箭头与世界三箭头差
  一个常数 M（约 120°）——用户判"跟随夹爪的坐标轴还没改"。
- **原点那一瞬发布姿态 = M·Mᵀ = 单位矩阵**：夹爪三箭头与世界三箭头
  重合，这是"一眼看出用的是哪一帧"的判据（第 4 节钉住）。
- 相对化与重标的**顺序不再影响结果**：共轭让 M 在两步里都自消，
  (M·R0·Mᵀ)ᵀ(M·R·Mᵀ) = M·R0ᵀ·R·Mᵀ 与"先相对化再重标"逐元素相同。
  第 5 节把这条等价性钉住（旧版顺序是雷，v1.3.13 第二版起不是了）。

物理验收判据（第 1、2 节）：正对→+Z、左→+Y、下→+X、右→−Y、上→−X。
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.gripper.slam.protocol import (
    PUBLISH_RELABEL_MATRIX, PoseSample, _PUBLISH_RELABEL_QUAT,
    finish_publish_pose, quat_conjugate, quat_product,
    relabel_published_frame, rotate_pose_z90,
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
    rng = np.random.default_rng(20260928)
    Rzp90 = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    # 发布帧重标 M（协议里硬编码的 q_M 的矩阵形式，这里独立写一遍做对照）
    M = np.array([[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    q_M = (0.5, -0.5, -0.5, 0.5)
    # 部署桥接校正矩阵（source 值 R_y(90°)·R_z(−90°)），与新核心
    # 世界系合成 native 约定；静止首帧 native 姿态 = correction
    correction = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0],
                           [0.0, -1.0, 0.0]])
    c = math.sqrt(0.5)
    failures = []

    if not np.allclose(quat_to_matrix(*q_M), M, atol=1e-12):
        failures.append("q_M 与 M 不一致（协议常量写错了）")
    if not np.isclose(np.linalg.det(M), 1.0, atol=1e-12):
        failures.append("M 不是纯旋转（det != +1）")

    # 0. ★协议里的**两个独立字面量**必须互为同一张 M，而且本文件那两个副本
    #    也必须与它们逐元素相同 —— 上面那两条比的是本文件自己声明的两份，
    #    对模块里的常量一个字都没验。
    #    漏掉这条的代价（变异实测）：把 PUBLISH_RELABEL_MATRIX 换成
    #    [[0,0,-1],[-1,0,0],[0,-1,0]]（det = −1，左手系、物理上不可能，
    #    画出来夹爪会**镜像**，而它身上的箭头照样对）—— 五个测试全绿。
    #    两个常量分头写、又只跟自己的副本对照，就是这个盲区。
    module_M = np.asarray(PUBLISH_RELABEL_MATRIX, dtype=float)
    if not np.allclose(module_M, quat_to_matrix(*_PUBLISH_RELABEL_QUAT),
                       atol=1e-12):
        failures.append(
            "protocol 的 PUBLISH_RELABEL_MATRIX 与 _PUBLISH_RELABEL_QUAT "
            f"不是同一张 M：矩阵\n{module_M}\n四元数给出\n"
            f"{quat_to_matrix(*_PUBLISH_RELABEL_QUAT)}")
    if not np.isclose(np.linalg.det(module_M), 1.0, atol=1e-12):
        failures.append(
            f"PUBLISH_RELABEL_MATRIX 的 det = {np.linalg.det(module_M):.6f}"
            "（应为 +1）—— det = −1 是镜像：夹爪模型会被画成左右手互换，"
            "而三支体轴箭头看不出问题")
    if not np.allclose(module_M @ module_M.T, np.eye(3), atol=1e-12):
        failures.append("PUBLISH_RELABEL_MATRIX 不是正交阵")
    if not np.allclose(module_M, M, atol=1e-12):
        failures.append(
            f"PUBLISH_RELABEL_MATRIX 与本文件的 M 不一致：\n{module_M}\n{M}")

    # 1. 轴映射：native 物理轴 → 发布帧
    #    native: X=正对 Y=左 Z=上；发布帧: X=下 Y=左 Z=前
    axis_cases = [
        # (native 位移, 期望发布帧位移, 说明)
        ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), "正对(native X) → +Z 前"),
        ((0.0, 1.0, 0.0), (0.0, 1.0, 0.0), "左(native Y) → +Y 左"),
        ((0.0, 0.0, 1.0), (-1.0, 0.0, 0.0), "上(native Z) → −X（上 = −下）"),
        ((0.0, -1.0, 0.0), (0.0, -1.0, 0.0), "右(native −Y) → −Y 右"),
        ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), "下(native −Z) → +X 下"),
        ((-1.0, 0.0, 0.0), (0.0, 0.0, -1.0), "后(native −X) → −Z 后"),
    ]
    for native, expected, label in axis_cases:
        pose = PoseSample(position=native, rotation=(0, 0, 0, 1),
                          timestamp=1.0)
        out = relabel_published_frame(rotate_pose_z90(pose))
        if not np.allclose(out.position, expected, atol=1e-12):
            failures.append(
                f"轴映射错误 [{label}]: {out.position} != {expected}")

    # 2. 物理轴复现：物理 +Y(正对) → 发布 +Z；物理 +X(右) → 发布 −Y；
    #    物理 +Z(上) → 发布 −X；物理 −Z(下) → 发布 +X
    #    native = R_z(−90°)·物理
    Rzm90 = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    for phys_axis, expected, label in [
        (np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, 1.0]), "物理正对"),
        (np.array([1.0, 0.0, 0.0]), np.array([0.0, -1.0, 0.0]), "物理右"),
        (np.array([0.0, 0.0, 1.0]), np.array([-1.0, 0.0, 0.0]), "物理上"),
        (np.array([0.0, 0.0, -1.0]), np.array([1.0, 0.0, 0.0]), "物理下"),
    ]:
        native = tuple(Rzm90 @ phys_axis)
        out = relabel_published_frame(rotate_pose_z90(PoseSample(
            position=native, rotation=(0, 0, 0, 1), timestamp=1.0)))
        if not np.allclose(np.array(out.position), expected, atol=1e-12):
            failures.append(
                f"{label} 在发布帧里应为 {expected}，实际 {out.position}")

    # 3. 随机位姿：两步都是纯左乘，且都是正规旋转
    for i in range(200):
        pos = tuple(float(v) for v in rng.uniform(-2, 2, 3))
        quat = random_unit_quaternion(rng)
        pose = PoseSample(position=pos, rotation=quat, timestamp=1.0 + i)
        # 第 1 步：中间帧
        mid = rotate_pose_z90(pose)
        if tuple(mid.position) != (-pos[1], pos[0], pos[2]):
            failures.append(f"#{i} 中间帧位置应为 (−y,x,z)")
            continue
        R = quat_to_matrix(*quat)
        if not np.allclose(quat_to_matrix(*mid.rotation), Rzp90 @ R,
                           atol=1e-12):
            failures.append(f"#{i} 中间帧 R' != R_z(+90°)·R")
            continue
        # 第 3 步：发布帧
        pub = relabel_published_frame(mid)
        if not np.allclose(pub.position, (-mid.position[2], -mid.position[0],
                                          mid.position[1]), atol=1e-12):
            failures.append(f"#{i} 发布帧位置应为 (−z,−x,y)")
            continue
        Rm = quat_to_matrix(*pub.rotation)
        if not np.allclose(Rm, M @ (Rzp90 @ R) @ M.T, atol=1e-12):
            failures.append(f"#{i} 发布帧 R' != M·R_z(+90°)·R·Mᵀ")
            continue
        if not np.allclose(Rm @ Rm.T, np.eye(3), atol=1e-12):
            failures.append(f"#{i} 发布帧 R' 非正交")
        if not np.isclose(np.linalg.det(Rm), 1.0, atol=1e-9):
            failures.append(f"#{i} det(M·R·Mᵀ) != +1（M 是纯旋转）")

    # 4. 静止首帧：native = correction → 中间帧 R_z(90°)·correction
    #    = R_x(−90°)（部署校正常数残留，非单位矩阵）；经原点相对化
    #    q0⁻¹⊗q 后中间帧 = 单位矩阵；**再经重标 = M·I·Mᵀ = 单位矩阵**。
    #    发布帧首帧就是单位矩阵 —— 夹爪体轴与世界轴在原点那一瞬逐轴
    #    重合（+X=下、+Y=左、+Z=前），GUI 里"跟随夹爪的三支箭头"和世界
    #    三箭头指向同一处，用户要的"跟随夹爪的轴也改了"就是这一条。
    q_corr = (-0.5, 0.5, -0.5, 0.5)   # R_y(90°)·R_z(−90°) 的四元数
    mid0 = rotate_pose_z90(PoseSample(position=(0.0, 0.0, 0.0),
                                      rotation=q_corr, timestamp=1.0))
    Rxm90 = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])
    if not np.allclose(quat_to_matrix(*mid0.rotation), Rxm90, atol=1e-12):
        failures.append("中间帧首帧应为 R_x(−90°)（常数残留）")
    pub0 = finish_publish_pose(mid0, mid0.rotation)
    R0 = quat_to_matrix(*pub0.rotation)
    if not np.allclose(R0, np.eye(3), atol=1e-12):
        failures.append("发布帧首帧应为单位矩阵（体轴与世界轴在原点重合）")
    for index, name in enumerate("XYZ"):
        if not np.allclose(R0[:, index], np.eye(3)[index], atol=1e-12):
            failures.append(
                f"原点那一瞬体轴 {name} 未与世界 {name} 轴重合：{R0[:, index]}")
    # 静止不动的一串位姿：发布帧姿态逐帧恒等（没有跳变）
    for i in range(5):
        again = finish_publish_pose(
            rotate_pose_z90(PoseSample(position=(0.0, 0.0, 0.0),
                                       rotation=q_corr,
                                       timestamp=2.0 + i)),
            mid0.rotation)
        if not np.allclose(quat_to_matrix(*again.rotation), np.eye(3),
                           atol=1e-12):
            failures.append(f"静止第 {i} 帧发布姿态不是单位矩阵（有跳变）")

    # 5. ★体轴改名 + 顺序等价（本文件的核心回归）★
    #    native t0 = R_z(−90°)（夹爪正对）；t1 = R_z(−180°)（物理右转 90°）。
    #    发布姿态的**体轴列**必须等于物理方向，且体轴的名字已跟世界轴统一：
    #      t1 体前(局部 +Z) = −Y（物理右）、体右(局部 −Y) = −Z（物理后）
    mid_t0 = rotate_pose_z90(PoseSample(position=(0.0, 0.0, 0.0),
                                        rotation=(0.0, 0.0, -c, c),
                                        timestamp=1.0))
    mid_t1 = rotate_pose_z90(PoseSample(position=(0.0, 0.0, 0.0),
                                        rotation=(0.0, 0.0, -1.0, 0.0),
                                        timestamp=2.0))
    if not np.allclose(quat_to_matrix(*mid_t0.rotation), np.eye(3),
                       atol=1e-12):
        failures.append("第 5 节前提不成立：中间帧 t0 应为单位矩阵")
    pub_t1 = finish_publish_pose(mid_t1, mid_t0.rotation)
    R1 = quat_to_matrix(*pub_t1.rotation)
    if not np.allclose(R1[:, 2], (0.0, -1.0, 0.0), atol=1e-12):
        failures.append(
            f"体轴改名：右转 90° 后体前(局部 +Z)应为物理右 (0,-1,0)，"
            f"实际 {R1[:,2]}")
    if not np.allclose(-R1[:, 1], (0.0, 0.0, -1.0), atol=1e-12):
        failures.append(
            f"体轴改名：右转 90° 后体右(局部 −Y)应为物理后 (0,0,-1)，"
            f"实际 {-R1[:,1]}")
    # 顺序等价：先重标再相对化 == 先相对化再重标 —— 共轭让 M 在两步里都自消。
    # （旧版重标只左乘 M，顺序是雷：反过来 M 被整体约掉、姿态留在中间帧
    #   而位置已换帧，GUI 的夹爪模型会画成转了 90° 的姿势。）
    mid_t0b = relabel_published_frame(mid_t0)
    mid_t1b = relabel_published_frame(mid_t1)
    swapped = quat_product(quat_conjugate(mid_t0b.rotation), mid_t1b.rotation)
    if not np.allclose(quat_to_matrix(*swapped), R1, atol=1e-12):
        failures.append("顺序等价性不成立：先重标再相对化给出了不同姿态")
    # 反向钉死两条：①去掉右乘（旧实现只左乘 M）必须给出**不同**的答案，
    # 否则"体轴也改了名"这件事没有回归保护；②旧实现在原点那一瞬是常数 M
    # —— 正是用户看到"跟随夹爪的坐标轴还没改"的原因。
    rel_q = quat_product(quat_conjugate(mid_t0.rotation), mid_t1.rotation)
    left_only = quat_to_matrix(*quat_product(q_M, rel_q))
    if np.allclose(left_only, R1, atol=1e-12):
        failures.append("体轴改名没生效：只左乘 M 与左右各乘一次结果相同")
    left_anchor = quat_to_matrix(*quat_product(
        q_M, quat_product(quat_conjugate(mid_t0.rotation), mid_t0.rotation)))
    if not np.allclose(left_anchor, M, atol=1e-12):
        failures.append("旧实现（只左乘）在原点那一瞬应为常数 M")
    if np.allclose(left_anchor, np.eye(3), atol=1e-12):
        failures.append("旧实现在原点竟然是单位矩阵——那用户不会判没改")
    # relabel=False（过渡期回退旧约定）必须原样返回中间帧：相对化照样做
    # （t1 相对 t0 = R_z(−90°)），只是不再叠 M、体轴也不改名 —— 与发布帧
    # 结果差 M·__·Mᵀ，这条同时把"回退只关掉重标、不关掉相对化"钉住。
    legacy = finish_publish_pose(mid_t1, mid_t0.rotation, relabel=False)
    if not np.allclose(quat_to_matrix(*legacy.rotation), Rzm90, atol=1e-12):
        failures.append("relabel=False 应发布中间帧（此例应为 R_z(−90°)）")
    if not np.allclose(M @ quat_to_matrix(*legacy.rotation) @ M.T, R1,
                       atol=1e-12):
        failures.append("发布帧结果应 = M·(relabel=False 的结果)·Mᵀ")

    # 6. 双应用 rotate_pose_z90 = R_z(+180°)：位置 (−x,−y,z)
    pose = PoseSample(position=(1.0, 2.0, 3.0),
                      rotation=random_unit_quaternion(rng), timestamp=1.0)
    twice = rotate_pose_z90(rotate_pose_z90(pose))
    if tuple(twice.position) != (-1.0, -2.0, 3.0):
        failures.append("双应用位置应为 (−x,−y,z)")
    if not np.allclose(quat_to_matrix(*twice.rotation),
                       Rzp90 @ Rzp90 @ quat_to_matrix(*pose.rotation),
                       atol=1e-12):
        failures.append("双应用旋转应为 R_z(+180°)·R")
    # 重标是 120° 旋转 ⇒ 阶为 3（不是 2，别照着 r_z90 的"双应用"抄）
    thrice = pose
    for _ in range(3):
        thrice = relabel_published_frame(thrice)
    if not np.allclose(quat_to_matrix(*thrice.rotation),
                       quat_to_matrix(*pose.rotation), atol=1e-12):
        failures.append("重标三次应回到原姿态（M³ = I）")
    if not np.allclose(thrice.position, pose.position, atol=1e-12):
        failures.append("重标三次应回到原位置（M³ = I）")

    # 7. host_mono_ns（取样帧宿主时刻）必须原样透传：三步变换都不动时基。
    #    丢了它位姿就退回"只能按行号/相机钟对齐"，而落盘侧正是靠它与
    #    hardware_ns 直接比较来配 slam 点↔视频帧。
    stamped = PoseSample(position=(1.0, 2.0, 3.0),
                         rotation=random_unit_quaternion(rng), timestamp=1.0,
                         host_mono_ns=287607702508638)
    if rotate_pose_z90(stamped).host_mono_ns != 287607702508638:
        failures.append("rotate_pose_z90 丢了 host_mono_ns")
    if relabel_published_frame(stamped).host_mono_ns != 287607702508638:
        failures.append("relabel_published_frame 丢了 host_mono_ns")
    if (finish_publish_pose(rotate_pose_z90(stamped), (0, 0, 0, 1))
            .host_mono_ns != 287607702508638):
        failures.append("finish_publish_pose 丢了 host_mono_ns")
    # 无戳（旧 native 二进制）透传 None，不能被伪造成 0
    if rotate_pose_z90(pose).host_mono_ns is not None:
        failures.append("无戳位姿的 host_mono_ns 应为 None")
    if relabel_published_frame(pose).host_mono_ns is not None:
        failures.append("无戳位姿经重标后 host_mono_ns 应为 None")
    if pose.timestamp != 1.0 or relabel_published_frame(pose).timestamp != 1.0:
        failures.append("重标改动了 timestamp")

    # 8. 轨迹形状不变（等距）：两步都是旋转，相邻点距离一致
    traj = [
        PoseSample(position=tuple(float(v) for v in rng.uniform(-1, 1, 3)),
                   rotation=random_unit_quaternion(rng), timestamp=float(i))
        for i in range(50)
    ]
    for a, b in zip(traj, traj[1:]):
        d1 = np.linalg.norm(np.array(a.position) - np.array(b.position))
        ma, mb = relabel_published_frame(rotate_pose_z90(
            a)), relabel_published_frame(rotate_pose_z90(b))
        d2 = np.linalg.norm(np.array(ma.position) - np.array(mb.position))
        if not np.isclose(d1, d2, atol=1e-12):
            failures.append("轨迹相邻点距离被换帧改变")

    # 8b. finish_publish_pose(p, None) == relabel_published_frame(p)
    #     （还没锚定的阶段走无基准分支，两条路径必须等价）
    for i in range(50):
        p = PoseSample(position=tuple(float(v) for v in rng.uniform(-1, 1, 3)),
                       rotation=random_unit_quaternion(rng), timestamp=1.0)
        if not np.allclose(
                finish_publish_pose(p, None).rotation,
                relabel_published_frame(p).rotation, atol=1e-12):
            failures.append("#{} 无基准分支与纯重标不等价".format(i))
            break

    # 9. 四元数辅助：共轭逆 = 单位矩阵；Hamilton 积 = 矩阵左乘
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

    # 10. 原点相对化：首帧姿态 q0（含部署校正/启动朝向的任意常数
    #     旋转），此后 q0⁻¹⊗q 应还原为相对原点的真实转动；再重标
    #     之后发布姿态 = M·(相对转动)
    for i in range(200):
        q0r = random_unit_quaternion(rng)
        q_rel = random_unit_quaternion(rng)
        q = quat_product(q0r, q_rel)
        recovered = quat_product(quat_conjugate(q0r), q)
        if not np.allclose(recovered, q_rel, atol=1e-12):
            failures.append(f"#{i} 相对化未还原相对转动")
            break
        pub = finish_publish_pose(
            PoseSample(position=(0.0, 0.0, 0.0), rotation=q, timestamp=1.0),
            q0r)
        if not np.allclose(quat_to_matrix(*pub.rotation),
                           M @ quat_to_matrix(*q_rel) @ M.T, atol=1e-12):
            failures.append(f"#{i} 发布姿态应为 M·(相对转动)·Mᵀ")
            break

    if failures:
        print("FAIL:")
        for f in failures[:5]:
            print("  ", f)
        return 1
    print("PASS: 发布帧(+X下/+Y左/+Z前) 数学性质全部通过"
          "（轴映射/物理轴复现/200 随机位姿 M·R_z90·R·Mᵀ/首帧=单位阵即"
          "体轴与世界轴在原点重合/★体轴改名+顺序等价★/双应用与M³/"
          "时基透传/轨迹等距/四元数积/原点相对化）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
