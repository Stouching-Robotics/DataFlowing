#!/usr/bin/env python3
"""实时 3D 坐标系可视化 — Open3D, 融入 run_orb_slam.sh"""
import sys, os, time, glob
import numpy as np
import open3d as o3d

def quat_to_R(qx, qy, qz, qw):
    return np.array([
        [1-2*qy**2-2*qz**2,   2*qx*qy-2*qw*qz,     2*qx*qz+2*qw*qy],
        [2*qx*qy+2*qw*qz,     1-2*qx**2-2*qz**2,   2*qy*qz-2*qw*qx],
        [2*qx*qz-2*qw*qy,     2*qy*qz+2*qw*qx,     1-2*qx**2-2*qy**2]])

def main():
    traj = sys.argv[1] if len(sys.argv) > 1 else None
    if not traj:
        fs = sorted(glob.glob(os.path.dirname(__file__)+'/trajectories/traj_*.txt'))
        traj = fs[-1] if fs else None
    if not traj or not os.path.exists(traj):
        print(f"Usage: {sys.argv[0]} <traj.txt>"); sys.exit(1)

    print(f"[3D] Waiting for data: {traj}")
    vis = o3d.visualization.Visualizer()
    vis.create_window("Camera 3D Pose  |  Red=X Green=Y Blue=Z", 1024, 768)

    # 世界原点参考
    origin = o3d.geometry.TriangleMesh.create_coordinate_frame(0.3)
    vis.add_geometry(origin, reset_bounding_box=False)

    frame = o3d.geometry.TriangleMesh.create_coordinate_frame(0.12)
    vis.add_geometry(frame, reset_bounding_box=False)

    traj_line = o3d.geometry.LineSet()
    vis.add_geometry(traj_line, reset_bounding_box=False)

    # 初始视角
    vc = vis.get_view_control()
    vc.set_front([0.5, -1, 0.5])
    vc.set_lookat([0, 0, 0])
    vc.set_zoom(1.0)

    last_n = 0
    while vis.poll_events():
        # 读文件
        try:
            data = np.loadtxt(traj)
        except Exception:
            vis.update_renderer()
            time.sleep(0.2)
            continue

        if data.ndim == 1:
            data = data.reshape(1, -1)
        n = len(data)
        if n == 0:
            vis.update_renderer()
            time.sleep(0.2)
            continue

        # 只在有新数据时更新几何体
        if n == last_n:
            vis.update_renderer()
            time.sleep(0.05)
            continue
        last_n = n

        last = data[-1]
        x, y, z = last[0:3]
        qx, qy, qz, qw = last[3:7]
        R = quat_to_R(qx, qy, qz, qw)

        # 坐标架
        T = np.eye(4); T[:3, :3] = R; T[:3, 3] = [x, y, z]
        vis.remove_geometry(frame)
        local_frame = o3d.geometry.TriangleMesh.create_coordinate_frame(0.12)
        local_frame.transform(T)
        vis.add_geometry(local_frame, reset_bounding_box=False)
        frame = local_frame

        # 轨迹
        pts = data[:, :3]
        vis.remove_geometry(traj_line)
        new_line = o3d.geometry.LineSet()
        if len(pts) >= 2:
            new_line.points = o3d.utility.Vector3dVector(pts)
            idx = np.column_stack([np.arange(len(pts)-1), np.arange(1, len(pts))])
            new_line.lines = o3d.utility.Vector2iVector(idx)
            new_line.colors = o3d.utility.Vector3dVector(np.tile([0.4]*3, (len(idx), 1)))
        vis.add_geometry(new_line, reset_bounding_box=False)
        traj_line = new_line

        vis.update_renderer()
        time.sleep(0.05)

    vis.destroy_window()

if __name__ == '__main__':
    main()
