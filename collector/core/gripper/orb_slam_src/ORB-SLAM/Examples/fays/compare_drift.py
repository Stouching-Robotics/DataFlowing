#!/usr/bin/env python3
"""离线对比两条 ORB-SLAM 轨迹的横向漂移。

输入文件格式：``timestamp x y z qx qy qz qw``。
本脚本只读取已有轨迹文件，不启动 SLAM、不访问相机或 USB 设备。

用法：
    python3 compare_drift.py stereo.txt stereo_inertial.txt

相对文件名会从当前项目的 ``ORB-SLAM`` 目录读取。
"""

from pathlib import Path
import sys

import numpy as np


# 诊断脚本只读取当前 Fays 项目生成和保存的轨迹。
SOURCE_TRAJECTORY_DIR = Path(__file__).resolve().parents[2]


def resolve_trajectory(value: str) -> Path:
    """优先使用给定路径；仅文件名时回退到当前项目的 ORB-SLAM 目录。"""
    path = Path(value).expanduser()
    if path.is_file() or path.is_absolute():
        return path
    return SOURCE_TRAJECTORY_DIR / path


def load_positions(path: Path) -> np.ndarray:
    """读取轨迹的 x/y/z 三列；首列 timestamp 不参与空间计算。"""
    try:
        data = np.loadtxt(path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {path}: {exc}") from exc

    if data.ndim == 1:
        data = data.reshape(1, -1)
    if data.ndim != 2 or data.shape[1] < 4:
        raise ValueError(
            f"{path} 格式错误：需要至少 4 列 timestamp x y z，实际为 {data.shape}"
        )

    positions = data[:, 1:4]
    if not np.isfinite(positions).all():
        raise ValueError(f"{path} 含有非数值坐标")
    return positions


def analyze_motion(positions: np.ndarray):
    """计算起止段质心、位移和累计路径长度。"""
    n = len(positions)
    n_segment = max(1, n // 20)  # 前/后 5%
    start = positions[:n_segment].mean(axis=0)
    end = positions[-n_segment:].mean(axis=0)
    delta = end - start
    path_length = np.linalg.norm(np.diff(positions, axis=0), axis=1).sum()
    return start, end, delta, path_length


def print_section(title: str, path: Path, start, end, delta, path_length: float):
    print(f"\n{'─' * 55}\n  {title}\n  文件: {path}\n{'─' * 55}")
    print(f"  起始位置: ({start[0]:.4f}, {start[1]:.4f}, {start[2]:.4f}) m")
    print(f"  结束位置: ({end[0]:.4f}, {end[1]:.4f}, {end[2]:.4f}) m")
    print(f"  位移 Δ:   X={delta[0]:+.4f}m  Y={delta[1]:+.4f}m  Z={delta[2]:+.4f}m")
    print(f"  累计路径: {path_length:.3f}m")


def main() -> int:
    if len(sys.argv) != 3:
        print(f"用法: {Path(sys.argv[0]).name} <stereo_traj.txt> <stereo_inertial_traj.txt>")
        return 2

    stereo_path, inertial_path = (resolve_trajectory(value) for value in sys.argv[1:])
    for path in (stereo_path, inertial_path):
        if not path.is_file():
            print(f"文件不存在: {path}", file=sys.stderr)
            return 1

    try:
        stereo = analyze_motion(load_positions(stereo_path))
        inertial = analyze_motion(load_positions(inertial_path))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    print("\n" + "=" * 55 + "\n  轨迹漂移对照\n" + "=" * 55)
    print_section("A. 纯双目", stereo_path, *stereo)
    print_section("B. 双目 + IMU", inertial_path, *inertial)

    stereo_lateral = float(np.hypot(stereo[2][0], stereo[2][1])) * 100.0
    inertial_lateral = float(np.hypot(inertial[2][0], inertial[2][1])) * 100.0
    print(f"\n  纯双目横向漂移:     {stereo_lateral:.1f} cm")
    print(f"  双目 + IMU 横向漂移: {inertial_lateral:.1f} cm")
    print(f"  差异:               {inertial_lateral - stereo_lateral:+.1f} cm")

    if inertial_lateral > stereo_lateral * 3 and inertial_lateral > 3.0:
        print("\n  诊断：加入 IMU 后横向漂移显著增大，优先检查 Camera-IMU 外参。")
    elif stereo_lateral > 3.0:
        print("\n  诊断：纯双目已存在明显横向漂移，优先检查相机内参与双目基线。")
    else:
        print("\n  诊断：两种模式的横向漂移均未超过当前阈值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
