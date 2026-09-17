#!/usr/bin/env python3
"""离线显示一条 ORB-SLAM 轨迹的 3D 路径。

输入文件格式：``timestamp x y z qx qy qz qw``。
本脚本只读取轨迹文件；不启动 SLAM、不访问相机或 USB 设备。

用法：
    python3 view_3d.py [trajectory.txt]

未指定文件时读取当前项目的 ``ORB-SLAM/traj.txt``。
"""

from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np


# 诊断脚本只读取当前 Fays 项目生成的主轨迹。
ORB_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRAJECTORY = ORB_PROJECT_ROOT / "traj.txt"


def load_trajectory(path: Path):
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

    timestamps = data[:, 0]
    positions = data[:, 1:4]
    if not np.isfinite(timestamps).all() or not np.isfinite(positions).all():
        raise ValueError(f"{path} 含有非数值数据")
    return timestamps, positions


def set_equal_axes(ax, positions: np.ndarray):
    low = positions.min(axis=0)
    high = positions.max(axis=0)
    center = (low + high) / 2.0
    radius = max(float((high - low).max()) / 2.0, 0.05)
    ax.set_xlim(center[0] - radius, center[0] + radius)
    ax.set_ylim(center[1] - radius, center[1] + radius)
    ax.set_zlim(center[2] - radius, center[2] + radius)


def main() -> int:
    if len(sys.argv) > 2:
        print(f"用法: {Path(sys.argv[0]).name} [trajectory.txt]")
        return 2

    path = Path(sys.argv[1]).expanduser() if len(sys.argv) == 2 else DEFAULT_TRAJECTORY
    if not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    try:
        timestamps, positions = load_trajectory(path)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    displacement = positions[-1] - positions[0]
    distance = np.linalg.norm(np.diff(positions, axis=0), axis=1).sum()
    duration = timestamps[-1] - timestamps[0]
    print(f"文件: {path}")
    print(f"帧数: {len(positions)}  时长: {duration:.2f}s  累计路径: {distance:.3f}m")
    print(f"起终点位移: X={displacement[0]:+.3f}m Y={displacement[1]:+.3f}m Z={displacement[2]:+.3f}m")

    figure = plt.figure(figsize=(9, 7))
    axis = figure.add_subplot(111, projection="3d")
    axis.plot(positions[:, 0], positions[:, 1], positions[:, 2], color="#2878b5", linewidth=1.2)
    axis.scatter(*positions[0], color="#2ca02c", s=50, label="起点")
    axis.scatter(*positions[-1], color="#d62728", s=50, label="终点")
    axis.set_xlabel("X (m)")
    axis.set_ylabel("Y (m)")
    axis.set_zlabel("Z (m)")
    axis.set_title(f"ORB-SLAM 轨迹 | {len(positions)} 帧 | {distance:.3f} m")
    axis.legend()
    set_equal_axes(axis, positions)
    figure.tight_layout()
    plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
