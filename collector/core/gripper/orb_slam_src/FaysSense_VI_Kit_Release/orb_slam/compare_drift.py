#!/usr/bin/env python3
"""
对照分析: 纯双目 vs 双目+IMU 的 Z轴移动漂移量
=================================================
输入: 两个轨迹文件 (纯双目 + stereo-inertial)
      都跑同样的动作: 静止 → 沿 Z 轴移动 ~40cm → 停止

输出: 诊断结论
"""

import sys, os, re
import numpy as np

def load_traj(path):
    """加载 ORB-SLAM3 轨迹 (timestamp x y z qx qy qz qw 格式)"""
    data = np.loadtxt(path)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    return data


def analyze_motion(data):
    """
    分析一条轨迹: 取前 N 帧的质心 vs 后 N 帧的质心, 计算移动量.
    返回 (start_pos, end_pos, delta, total_path_length)
    """
    # 取前 5% 作为起始段, 后 5% 作为结束段
    n = len(data)
    n_start = max(1, n // 20)
    n_end = max(1, n // 20)

    start_pts = data[:n_start, :3]
    end_pts = data[-n_end:, :3]

    start_center = start_pts.mean(axis=0)
    end_center = end_pts.mean(axis=0)
    delta = end_center - start_center

    # 总路径长度
    diffs = np.diff(data[:, :3], axis=0)
    path_length = np.sum(np.sqrt(np.sum(diffs**2, axis=1)))

    return start_center, end_center, delta, path_length


def print_section(title, filepath, start, end, delta, path_len):
    print(f"\n{'─'*55}")
    print(f"  {title}")
    print(f"  文件: {filepath}")
    print(f"{'─'*55}")
    print(f"  起始位置 (质心): ({start[0]:.4f}, {start[1]:.4f}, {start[2]:.4f}) m")
    print(f"  结束位置 (质心): ({end[0]:.4f}, {end[1]:.4f}, {end[2]:.4f}) m")
    print(f"  移动量 Δ:        X={delta[0]:+.4f}m  Y={delta[1]:+.4f}m  Z={delta[2]:+.4f}m")
    print(f"  总路径长度:      {path_len:.3f}m")

    # Z 轴是主运动方向
    z_abs = abs(delta[2])
    if z_abs > 0.01:
        lateral = np.sqrt(delta[0]**2 + delta[1]**2)
        ratio = lateral / z_abs * 100
        print(f"  横向漂移 / Z移动: {ratio:.1f}% (X/Y={lateral*100:.1f}cm / Z={z_abs*100:.1f}cm)")


def main():
    if len(sys.argv) < 2:
        # 自动找最新的两个轨迹
        traj_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trajectories')
        files = sorted([
            os.path.join(traj_dir, f) for f in os.listdir(traj_dir)
            if f.endswith('.txt')
        ], key=os.path.getmtime, reverse=True)

        stereo_files = [f for f in files if 'stereo_only' in f]
        inertial_files = [f for f in files if 'stereo_only' not in f and 'traj_' in f]

        if len(stereo_files) < 1 or len(inertial_files) < 1:
            print("用法: 先跑两个测试，然后运行此脚本对比")
            print(f"\n  终端1 (纯双目):  cd {os.path.dirname(os.path.abspath(__file__))} && ./run_stereo_only.sh")
            print(f"  终端2 (stereo+IMU): cd {os.path.dirname(os.path.abspath(__file__))} && ./run_orb_slam.sh")
            print(f"\n  跑完后: python3 {sys.argv[0]}")
            print(f"\n  或手动指定: python3 {sys.argv[0]} stereo_traj.txt inertial_traj.txt")
            sys.exit(1)

        stereo_path = stereo_files[-1]
        inertial_path = inertial_files[-1]
    elif len(sys.argv) == 3:
        stereo_path = sys.argv[1]
        inertial_path = sys.argv[2]
    else:
        print(f"Usage: {sys.argv[0]} [stereo_traj.txt] [inertial_traj.txt]")
        sys.exit(1)

    if not os.path.exists(stereo_path):
        print(f"文件不存在: {stereo_path}"); sys.exit(1)
    if not os.path.exists(inertial_path):
        print(f"文件不存在: {inertial_path}"); sys.exit(1)

    stereo_data = load_traj(stereo_path)
    inertial_data = load_traj(inertial_path)

    s_start, s_end, s_delta, s_path = analyze_motion(stereo_data)
    i_start, i_end, i_delta, i_path = analyze_motion(inertial_data)

    print("\n" + "="*55)
    print("  📊 对照实验结果")
    print("="*55)

    print_section("A. 纯双目 (Stereo Only)", stereo_path,
                  s_start, s_end, s_delta, s_path)
    print_section("B. 双目+IMU (Stereo-Inertial)", inertial_path,
                  i_start, i_end, i_delta, i_path)

    # ---- 诊断 ----
    print(f"\n{'='*55}")
    print("  🔍 诊断")
    print(f"{'='*55}")

    s_lateral = np.sqrt(s_delta[0]**2 + s_delta[1]**2) * 100  # cm
    i_lateral = np.sqrt(i_delta[0]**2 + i_delta[1]**2) * 100  # cm

    print(f"\n  纯双目    横向漂移 (X/Y): {s_lateral:.1f} cm")
    print(f"  双目+IMU  横向漂移 (X/Y): {i_lateral:.1f} cm")
    print(f"  差异:                    {i_lateral - s_lateral:+.1f} cm")

    if i_lateral > s_lateral * 3 and i_lateral > 3.0:
        print(f"\n  {'▶'*30}")
        print(f"  结论: IMU 外参有问题！")
        print(f"  纯双目横向漂移 {s_lateral:.1f}cm，加 IMU 后变成 {i_lateral:.1f}cm")
        print(f"  IMU 引入的额外漂移 = {i_lateral - s_lateral:.1f}cm")
        print(f"\n  → 需要重新标定 Camera-IMU 外参")
        print(f"  → 推荐工具: Kalibr (kalibr_allan + kalibr_calibrate_imu_camera)")
        print(f"  → 标定方法见: docs/manual/calibration_data.md")
    elif s_lateral > 3.0:
        print(f"\n  {'▶'*30}")
        print(f"  结论: 相机内参或基线有问题！")
        print(f"  纯双目就已经有 {s_lateral:.1f}cm 的横向漂移")
        print(f"  IMU 不是主要原因 (加 IMU 后漂移 {i_lateral:.1f}cm)")
        print(f"\n  → 需要重新标定相机内参和双目外参")
        print(f"  → 推荐工具: Kalibr (kalibr_calibrate_cameras)")
    else:
        print(f"\n  {'▶'*30}")
        print(f"  结论: 两个模式横向漂移都小 ({s_lateral:.1f}cm / {i_lateral:.1f}cm)")
        print(f"  IMU 外参和相机内参/基线都没问题")
        print(f"  → 如果之前漂移大，可能是那次测试的偶然因素")

    print()


if __name__ == '__main__':
    main()
