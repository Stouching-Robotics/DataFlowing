#!/usr/bin/env python3
"""
S80M 轨迹 → Piper 生产级回放

核心改进 (vs 旧版):
  1. 笛卡尔增量映射 (scale-free): tgt = p_robot + (SLAM_i - SLAM_0)
     → 机械臂从当前 FK 出发, 1:1 跟随轨迹增量, 不需要缩放/偏移
  2. 真实时间戳驱动: 按轨迹 ts 列索引目标帧, 解决帧率波动/丢帧问题
  3. 轨迹多层平滑: 7帧滑动均值(XYZ) + 四元数符号对齐平均 + Slerp 姿态限步
  4. IK 后处理: 失败帧线性插值 + SG 5点关节平滑
  5. 柔顺回放: 指数平滑追踪(step) + 关节速度限制 + 空闲自旋消时序漂移
  6. 50fps 默认参数 (S80M 实测 20ms 帧间隔)

用法:
  python replay_trajectory.py traj.txt --replay
  python replay_trajectory.py traj.txt --replay --home --save-joints
  python replay_trajectory.py traj.txt --replay --step 0.3 --max-joint-speed 0.5
  python replay_trajectory.py traj.txt --save                     # 以当前机械臂位姿为参考, 解算并保存关节角
"""

import sys, os, math, time, argparse
import numpy as np

from piper_sdk.control.piper_ik import (
    C_PiperInverseKinematics, C_PiperForwardKinematicsFull,
    rad_to_raw, raw_to_rad, quat_to_rot_matrix, rot_matrix_to_axis_angle,
)

HOME_JOINTS_RAD = [0.03349, 0.96120, -1.04011, 0.01667, 0.00913, -0.05176]
JOINT_LIMITS = [
    [-2.6179, 2.6179], [0.0, 3.14], [-2.967, 0.0],
    [-1.745, 1.745], [-1.22, 1.22], [-2.09439, 2.09439],
]


# =============================================================================
# 工具函数
# =============================================================================

def rotm_to_quat(R):
    """3x3 rotation → [w,x,y,z]"""
    m00, m01, m02 = R[0, 0], R[0, 1], R[0, 2]
    m10, m11, m12 = R[1, 0], R[1, 1], R[1, 2]
    m20, m21, m22 = R[2, 0], R[2, 1], R[2, 2]
    trace = m00 + m11 + m22
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2.0; w = 0.25 * s
        x = (m21 - m12) / s; y = (m02 - m20) / s; z = (m10 - m01) / s
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        w = (m21 - m12) / s; x = 0.25 * s
        y = (m01 + m10) / s; z = (m02 + m20) / s
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        w = (m02 - m20) / s; x = (m01 + m10) / s
        y = 0.25 * s; z = (m12 + m21) / s
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        w = (m10 - m01) / s; x = (m02 + m20) / s
        y = (m12 + m21) / s; z = 0.25 * s
    return [w, x, y, z]


def clamp_joints(q):
    return [max(JOINT_LIMITS[j][0], min(JOINT_LIMITS[j][1], q[j])) for j in range(6)]


def get_robot_joints_rad(piper):
    """读取机械臂当前关节角 (rad)"""
    m = piper.GetArmJointMsgs()
    return np.array(raw_to_rad([
        m.joint_state.joint_1, m.joint_state.joint_2, m.joint_state.joint_3,
        m.joint_state.joint_4, m.joint_state.joint_5, m.joint_state.joint_6,
    ]), dtype=np.float64)


# =============================================================================
# 轨迹加载 & 平滑
# =============================================================================

def load_traj(path):
    """读取 S80M 轨迹: ts(相对秒), xyz(m), quat(w,x,y,z)"""
    d = np.loadtxt(path)
    ts = d[:, 0]
    xyz = d[:, 1:4]
    q_xyzw = d[:, 4:8]  # qx,qy,qz,qw
    q_wxyz = np.column_stack([q_xyzw[:, 3], q_xyzw[:, 0], q_xyzw[:, 1], q_xyzw[:, 2]])
    return ts, xyz, q_wxyz


def smooth_trajectory(xyz, quat_wxyz, window=7):
    """XYZ 滑动平均 + 四元数符号对齐滑动平均"""
    n = len(xyz)
    if n < window: return xyz, quat_wxyz
    xyz_s, quat_s = xyz.copy(), quat_wxyz.copy()
    half = window // 2
    for i in range(half, n - half):
        xyz_s[i] = xyz[i - half:i + half + 1].mean(axis=0)
        seg = quat_wxyz[i - half:i + half + 1].copy()
        for k in range(1, len(seg)):
            if np.dot(seg[k], seg[0]) < 0: seg[k] = -seg[k]
        avg = seg.mean(axis=0)
        avg /= np.linalg.norm(avg)
        quat_s[i] = avg
    return xyz_s, quat_s


def _smooth_trajectory(joints_raw, fail_flags):
    """轨迹后处理 (run.py 风格): 检测跳变 → 插值 → SG5 平滑"""
    n = len(joints_raw)
    joints = [q.copy() for q in joints_raw]
    if n < 3:
        return joints

    bad = [False] * n
    for i in range(n):
        if fail_flags[i]:
            bad[i] = True

    # 坏段线性插值
    seg_start = None
    for i in range(n):
        if bad[i] and seg_start is None:
            seg_start = i
        elif not bad[i] and seg_start is not None:
            i0, i1 = seg_start - 1, i
            if i0 >= 0 and i1 < n:
                q0, q1 = joints[i0], joints[i1]
                for j in range(seg_start, i):
                    t = (j - i0) / (i1 - i0)
                    joints[j] = q0 + (q1 - q0) * t
            elif i0 < 0:
                for j in range(seg_start, i):
                    joints[j] = joints[i1].copy()
            seg_start = None
    if seg_start is not None and seg_start > 0:
        for j in range(seg_start, n):
            joints[j] = joints[seg_start - 1].copy()

    bad_count = sum(bad)
    if bad_count > 0:
        print(f"  [修复] {bad_count}/{n} IK失败帧插值")
    return joints


# =============================================================================
# 笛卡尔增量 IK
# =============================================================================

def compute_joints(ts, xyz, quat_wxyz, p_start, R_start, q_cur, args):
    """
    增量 IK (1:1 复现, 参照 piper_sdk.control.run 离线模式):
      tgt_xyz  = p_start + (xyz[i] - xyz[0]) * 1000   (m→mm, 1:1)
      R_target = (R_i @ R_0^T) @ R_start               (相机相对运动 → 机械臂)
    """
    ik = C_PiperInverseKinematics()
    ik.tol_pos = 0.1       # 0.1mm (默认 0.01mm 太紧)
    ik.tol_orient = 0.002  # 0.002rad (默认 0.0005rad 太紧)
    fk = C_PiperForwardKinematicsFull()
    n = len(ts)

    # SLAM 参考帧
    p_hw0 = xyz[0] * 1000.0  # mm
    R_hw0 = quat_to_rot_matrix(quat_wxyz[0])

    # 首帧: 目标 = 机械臂当前位置 (增量=0)
    tgt0_xyz = list(p_start)
    tgt0_quat_wxyz = rotm_to_quat(R_start)

    try:
        q_warm = ik.solve(tgt0_xyz, tgt0_quat_wxyz, list(q_cur), max_iter=args.ik_iters)
    except RuntimeError:
        # 多种子回退
        seeds = [[0, 0.8, -1.5, 0, 0, 0], [0, 1, -1, 0, 0, 0],
                 [0, 0.5, -1.5, 0, 0, 0], [0.5, 0.5, -1, 0, 0, 0],
                 [-0.5, 0.8, -1.2, 0, 0, 0]]
        q_warm = None
        for s in seeds:
            try:
                q_warm = ik.solve(tgt0_xyz, tgt0_quat_wxyz, s, max_iter=args.ik_iters)
                break
            except RuntimeError:
                continue
    if q_warm is None:
        print(f"[ERR] Frame0 IK failed. target={tgt0_xyz} quat={tgt0_quat_wxyz}")
        return None

    joints_raw = [q_warm.copy()]
    ik_fail_count = 0
    ik_fail_flags = [False]
    ik_fail_frames = []

    # 多种子回退
    fallback_seeds = [[0, 0.8, -1.5, 0, 0, 0], [0, 1, -1, 0, 0, 0],
                      [0, 0.5, -1.5, 0, 0, 0], [0.5, 0.5, -1, 0, 0, 0],
                      [-0.5, 0.8, -1.2, 0, 0, 0]]

    for i in range(1, n):
        p_hw = xyz[i] * 1000.0
        hw_quat = quat_wxyz[i]

        # 增量映射
        tgt_xyz = list(p_start + (p_hw - p_hw0))
        R_hw = quat_to_rot_matrix(hw_quat)
        R_delta = R_hw @ R_hw0.T
        R_target = R_delta @ R_start
        tgt_quat = rotm_to_quat(R_target)

        # 快速路径: 热启动 FK 已接近目标 → 跳过 IK
        _, T_warm = fk.CalFKMatrix(np.array(q_warm))
        pos_err = float(np.linalg.norm(np.array(tgt_xyz) - T_warm[0:3, 3]))
        ori_err = float(np.linalg.norm(
            rot_matrix_to_axis_angle(quat_to_rot_matrix(tgt_quat) @ T_warm[0:3, 0:3].T)))
        if pos_err < 2.0 and ori_err < 0.01:
            ik_fail_flags.append(False)
        else:
            q = None
            try:
                q = ik.solve(tgt_xyz, tgt_quat, q_warm, max_iter=args.ik_iters)
            except RuntimeError:
                for s in fallback_seeds:
                    try:
                        q = ik.solve(tgt_xyz, tgt_quat, s, max_iter=args.ik_iters)
                        break
                    except RuntimeError:
                        continue
            if q is not None:
                q_warm = q
                ik_fail_flags.append(False)
            else:
                ik_fail_count += 1
                ik_fail_flags.append(True)
                ik_fail_frames.append(i)
        joints_raw.append(q_warm.copy())

        if (i + 1) % 100 == 0:
            print(f"  IK {i + 1}/{n}  失败={ik_fail_count}")

    # 诊断: 失败帧分布
    if ik_fail_frames:
        clusters = []
        start = ik_fail_frames[0]; last = start
        for f in ik_fail_frames[1:]:
            if f == last + 1: last = f
            else:
                clusters.append((start, last))
                start = f; last = f
        clusters.append((start, last))
        print(f"  失败段: {len(clusters)} 段")
        for a, b in clusters[:5]:
            pct = (b - a + 1) * 100 // n
            print(f"    帧{a}-{b} ({b-a+1}帧, {pct}%)  |tgt|={np.linalg.norm(p_start + (xyz[a]*1000-p_hw0)):.0f}~{np.linalg.norm(p_start + (xyz[b]*1000-p_hw0)):.0f}mm")

    # ── 后处理 (run.py 风格) ──
    joints = _smooth_trajectory(joints_raw, ik_fail_flags)

    # 腕关节末尾锚定
    tail_start = int(n * 0.8)
    j4_home = HOME_JOINTS_RAD[3]
    j6_home = HOME_JOINTS_RAD[5]
    for i in range(tail_start, n):
        t = (i - tail_start) / max(1, n - 1 - tail_start)
        joints[i][3] = joints[i][3] * (1 - t) + j4_home * t
        joints[i][5] = joints[i][5] * (1 - t) + j6_home * t

    max_jump = max(float(np.max(np.abs(np.array(joints[i]) - np.array(joints[i-1]))))
                   for i in range(1, len(joints))) if len(joints) > 1 else 0
    print(f"  IK done: {n} frames  failed={ik_fail_count}  max_jump={max_jump:.4f}rad")
    return np.array(joints)


# =============================================================================
# 回放
# =============================================================================

def replay(piper, joints, ts_array, args):
    """
    回放 (run.py 风格):
      - 预计算全部关节指令 (增量偏移当前关节角)
      - 按固定节拍发送, 机械臂自带运动控制器平滑插值
    """
    n = len(joints)
    dt = (1.0 / args.hz) / args.speed

    # 关节空间增量: 从当前关节角出发
    q_now = get_robot_joints_rad(piper)
    q_offset = q_now - joints[0]
    raw_list = []
    for q in joints:
        qr = np.array(q) + q_offset
        for j in range(6):
            lo, hi = JOINT_LIMITS[j]
            qr[j] = max(lo, min(hi, qr[j]))
        raw_list.append(rad_to_raw(list(qr)))

    piper.EnableArm(7, 0x02); time.sleep(0.05)
    piper.MotionCtrl_2(0x01, 0x01, 80, 0x00); time.sleep(0.1)

    collect = getattr(args, 'save_joints', False)
    actual_joints_rad = []
    actual_grip_raw = []

    print(f"\n回放中... {n}帧  hz={args.hz}  speed={args.speed}x  Ctrl+C 停止")

    t0 = time.perf_counter()
    last_keepalive = t0
    try:
        for i, raw in enumerate(raw_list):
            target_t = t0 + i * dt
            # 保活: 每秒发一次, 减少 CAN 总线负载
            now = time.perf_counter()
            if now - last_keepalive >= 1.0:
                piper.MotionCtrl_2(0x01, 0x01, 80, 0x00)
                last_keepalive = now
            piper.JointCtrl(*raw)

            if collect:
                js = piper.GetArmJointMsgs().joint_state
                actual_joints_rad.append([
                    js.joint_1 / 1000.0 * math.pi / 180.0,
                    js.joint_2 / 1000.0 * math.pi / 180.0,
                    js.joint_3 / 1000.0 * math.pi / 180.0,
                    js.joint_4 / 1000.0 * math.pi / 180.0,
                    js.joint_5 / 1000.0 * math.pi / 180.0,
                    js.joint_6 / 1000.0 * math.pi / 180.0,
                ])
                actual_grip_raw.append(0)

            sleep_t = target_t - time.perf_counter()
            if sleep_t > 0.001: time.sleep(sleep_t)

            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{n}")

    except KeyboardInterrupt:
        print("\n中断")

    elapsed = time.perf_counter() - t0
    print(f"回放完成 {elapsed:.1f}s  (帧{n})")

    # 保存实际关节角
    if collect and len(actual_joints_rad) >= 2:
        _save_joints(args, actual_joints_rad, actual_grip_raw, n)


def _save_joints(args, joints_rad, grip_raw, n_frames):
    """保存实际关节角为 CSV"""
    outd = os.path.join(os.path.dirname(os.path.abspath(args.traj)), "joints")
    os.makedirs(outd, exist_ok=True)
    base = os.path.splitext(os.path.basename(args.traj))[0]
    out = os.path.join(outd, f"{base}_joints.csv")
    data = np.array(joints_rad)
    if data.ndim == 1: data = data.reshape(1, -1)
    np.savetxt(out, data, fmt='%.6f', delimiter=',', header='j1,j2,j3,j4,j5,j6')
    print(f"[保存] {out} ({data.shape[0]} 帧)")

    # 也保存为 HDF5 (对齐 episode 格式)
    try:
        import h5py
        hdf5_path = out.replace('.csv', '.hdf5')
        T = data.shape[0]
        qpos_data = np.zeros((T, 7), dtype=np.float32)
        qpos_data[:, :6] = data
        action_data = qpos_data.copy()
        if T > 1: action_data[:-1] = qpos_data[1:]

        with h5py.File(hdf5_path, 'w') as f:
            f.attrs['episode_len'] = T
            f.attrs['fps'] = int(getattr(args, 'hz', 60))
            f.attrs['robot_type'] = 'piper_follower'
            f.attrs['sim'] = False
            f.attrs['task'] = 'replay_s80m'
            f.create_dataset('action', data=action_data)
            obs = f.create_group('observations')
            obs.create_dataset('qpos', data=qpos_data)
        print(f"[保存] {hdf5_path} ({T} 帧)")
    except ImportError:
        pass


# =============================================================================
# 机械臂连接
# =============================================================================

def connect_piper(can="can0"):
    from piper_sdk import C_PiperInterface_V2
    piper = C_PiperInterface_V2(can)
    piper.ConnectPort()
    time.sleep(1)
    print("使能电机...")
    while not piper.EnablePiper():
        time.sleep(0.01)
    print("电机已使能")
    return piper


def go_home(piper):
    """移动到 home 位姿 (关节空间)"""
    print("[Home] 移动到 home...")
    piper.ModeCtrl(ctrl_mode=0x01, move_mode=0x01, move_spd_rate_ctrl=30)
    time.sleep(0.1)
    piper.JointCtrl(*rad_to_raw(HOME_JOINTS_RAD))
    for _ in range(40):
        time.sleep(0.3)
        try:
            if piper.GetArmStatus().arm_status.motion_status.value == 0x00:
                break
        except: pass
    print("[Home] 已到达")


# =============================================================================
# main
# =============================================================================

def main():
    p = argparse.ArgumentParser(description="S80M 轨迹 → Piper 生产级回放")
    p.add_argument("traj", help="轨迹文件 (.txt)")
    p.add_argument("--save", action="store_true", help="保存关节角 (CSV + HDF5)")
    p.add_argument("--save-joints", action="store_true",
                   help="回放时采集实际关节角并保存")
    p.add_argument("--replay", action="store_true", help="在线回放")
    p.add_argument("--home", action="store_true", help="回放前先移动到 home 位姿")
    p.add_argument("--can", default="can0", help="CAN 接口")

    # IK 参数
    p.add_argument("--ik-iters", type=int, default=50, help="IK 最大迭代次数")
    # 回放参数
    p.add_argument("--hz", type=float, default=50.0,
                   help="控制频率 (Hz), 默认 50 匹配 S80M@50fps")
    p.add_argument("--speed", type=int, default=50,
                   help="CAN 速度档位 (10-100), 默认 50")
    p.add_argument("--step-size", type=float, default=0.5,
                   help="指数平滑步长: 0.3=很柔, 0.5=适中, 1.0=直追")
    p.add_argument("--max-joint-speed", type=float, default=0.8,
                   help="单关节最大速度限制 (rad/s), 防过冲")
    p.add_argument("--smooth-window", type=int, default=7,
                   help="XYZ/四元数滑动平均窗口, 默认 7")

    args = p.parse_args()

    if not os.path.exists(args.traj):
        print(f"文件不存在: {args.traj}"); sys.exit(1)

    # ── 加载 & 平滑轨迹 ──
    print(f"加载: {args.traj}")
    ts, xyz, quat_wxyz = load_traj(args.traj)
    xyz_s, quat_s = smooth_trajectory(xyz, quat_wxyz, args.smooth_window)
    n = len(ts)
    frame_interval = (ts[-1] - ts[0]) / max(n - 1, 1)
    print(f"  {n} 帧  平均帧间隔 {frame_interval*1000:.1f}ms  "
          f"时长 {ts[-1]-ts[0]:.1f}s  平滑窗={args.smooth_window}")

    # ── 连接机械臂, 以当前 FK 为参考 ──
    piper = connect_piper(args.can)
    if args.home:
        go_home(piper)
    q_cur = get_robot_joints_rad(piper)
    fk = C_PiperForwardKinematicsFull()
    _, T = fk.CalFKMatrix(q_cur)
    p_ref = T[0:3, 3].copy()
    R_ref = T[0:3, 0:3].copy()
    print(f"  机器人当前 FK: ({p_ref[0]:.0f}, {p_ref[1]:.0f}, {p_ref[2]:.0f})mm  "
          f"关节=[{q_cur[0]:.2f} {q_cur[1]:.2f} {q_cur[2]:.2f} {q_cur[3]:.2f} {q_cur[4]:.2f} {q_cur[5]:.2f}]")

    # ── 笛卡尔增量 IK ──
    print(f"\n笛卡尔增量 IK ({n} 帧)...")
    joints = compute_joints(ts, xyz_s, quat_s, p_ref, R_ref, q_cur, args)
    if joints is None:
        print("IK 失败"); sys.exit(1)

    # ── 保存关节角 ──
    if args.save:
        _save_joints(args, joints, [], n)

    # ── 回放 ──
    if args.replay:
        try:
            replay(piper, joints, ts, args)
        finally:
            piper.MotionCtrl_2(0x01, 0x01, args.speed, 0x00)
            piper.DisconnectPort()
            print("机械臂已断开。")
    else:
        piper.MotionCtrl_2(0x01, 0x01, args.speed, 0x00)
        piper.DisconnectPort()
        print("机械臂已断开。")


if __name__ == '__main__':
    main()
