#!/usr/bin/env python3
"""FS-VI-S80M 实时 3D 轨迹 — 完全照搬 realtime_plot.py 架构"""
import subprocess, signal, os, sys, threading, queue, re, time, datetime
import numpy as np
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from scipy.spatial.transform import Rotation as R

class RealtimePlotter:
    def __init__(self, slam_cmd, env=None):
        self.slam_cmd = slam_cmd
        self.slam_env = env or os.environ.copy()
        self.positions = []
        self.quaternions = []
        self.timestamps = []
        self.data_queue = queue.Queue()
        self.running = True

        plt.ion()
        self.fig = plt.figure(figsize=(10, 8))
        self.ax = self.fig.add_subplot(111, projection='3d')
        self.traj_line, = self.ax.plot([], [], [], 'b-', linewidth=1.0, alpha=0.8)
        self.current_pt, = self.ax.plot([], [], [], 'ro', markersize=8)
        self.axis_quivers = []
        self.world_quivers = []

        self.ax.set_xlabel('X (m)'); self.ax.set_ylabel('Y (m)'); self.ax.set_zlabel('Z (m)')
        self.ax.set_title('FS-VI-S80M Trajectory\nWaiting for pose data...')
        self.ax.view_init(elev=30, azim=-60)

        axis_len = 0.3
        Rz = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
        self.world_quivers = []
        for dx, dy, dz, color in [(axis_len, 0, 0, 'r'), (0, axis_len, 0, 'g'), (0, 0, axis_len, 'b')]:
            rx, ry, rz = Rz @ [dx, dy, dz]
            self.world_quivers.append(
                self.ax.quiver(0, 0, 0, rx, ry, rz, color=color, linewidth=3, arrow_length_ratio=0.12))

    def parse_pose(self, line):
        m = re.search(
            r'\[([\d.]+)\]\s*XYZ:\s*\(([-\d.e]+),\s*([-\d.e]+),\s*([-\d.e]+)\)\s*'
            r'Quat:\s*\(w=([-\d.e]+),\s*x=([-\d.e]+),\s*y=([-\d.e]+),\s*z=([-\d.e]+)\)',
            line
        )
        if m:
            ts = float(m.group(1))
            x, y, z = float(m.group(2)), float(m.group(3)), float(m.group(4))
            qw, qx, qy, qz = float(m.group(5)), float(m.group(6)), float(m.group(7)), float(m.group(8))
            return ts, [x, y, z], [qx, qy, qz, qw]
        return None

    def reader_thread(self, process):
        for line in iter(process.stdout.readline, ''):
            if not self.running: break
            print(line, end='', flush=True)
            result = self.parse_pose(line)
            if result:
                self.data_queue.put(result)

    def update_plot(self, frame):
        new_data = False
        while not self.data_queue.empty():
            item = self.data_queue.get()
            ts, pos, quat = item
            self.timestamps.append(ts)
            self.positions.append(pos)
            self.quaternions.append(quat)
            new_data = True

        if not new_data or len(self.positions) < 1:
            return [self.traj_line, self.current_pt] + self.world_quivers + self.axis_quivers

        pa = np.array(self.positions)
        lp = pa[-1]; lq = self.quaternions[-1]

        # 3D 显示绕 Z 轴旋转 90° (纯视角, 不改真实坐标)
        Rz90 = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
        pa_disp = pa @ Rz90.T
        lp_disp = Rz90 @ np.array(lp)
        # 四元数也旋转 (用于显示相机坐标系)
        qz90 = R.from_euler('z', 90, degrees=True)
        lq_disp = (qz90 * R.from_quat(lq) * qz90.inv()).as_quat()  # xyzw

        self.traj_line.set_data(pa_disp[:, 0], pa_disp[:, 1])
        self.traj_line.set_3d_properties(pa_disp[:, 2])
        self.current_pt.set_data([lp_disp[0]], [lp_disp[1]])
        self.current_pt.set_3d_properties([lp_disp[2]])

        for q in self.axis_quivers: q.remove()
        self.axis_quivers.clear()
        rng = max(np.ptp(pa_disp, axis=0).max(), 0.01)
        axis_len = rng * 0.15
        rot = R.from_quat(lq_disp)  # 显示用旋转后的四元数
        for direction, color in [([1, 0, 0], 'r'), ([0, -1, 0], 'g'), ([0, 0, 1], 'b')]:
            wd = rot.apply(direction)
            qv = self.ax.quiver(lp_disp[0], lp_disp[1], lp_disp[2], wd[0], wd[1], wd[2],
                               length=axis_len * 2, color=color, alpha=0.9,
                               arrow_length_ratio=0.15, linewidth=3)
            self.axis_quivers.append(qv)

        for q in self.world_quivers: q.remove()
        self.world_quivers.clear()
        wlen = max(axis_len * 2.5, 0.1)
        # 世界轴也绕 Z 旋转 90° (纯显示)
        for dx, dy, dz, color in [(wlen, 0, 0, 'r'), (0, wlen, 0, 'g'), (0, 0, wlen, 'b')]:
            rx, ry, rz = Rz90 @ [dx, dy, dz]
            self.world_quivers.append(
                self.ax.quiver(0, 0, 0, rx, ry, rz, color=color, linewidth=3, arrow_length_ratio=0.12))

        max_range = max(rng, 0.5)
        mid = pa_disp.mean(axis=0)
        self.ax.set_xlim(mid[0] - max_range / 2, mid[0] + max_range / 2)
        self.ax.set_ylim(mid[1] - max_range / 2, mid[1] + max_range / 2)
        self.ax.set_zlim(mid[2] - max_range / 2, mid[2] + max_range / 2)

        n = len(self.positions)
        dist = np.sqrt(np.sum((pa_disp[-1] - pa_disp[0]) ** 2)) if n > 1 else 0
        self.ax.set_title(f'FS-VI-S80M | Poses:{n} | Dist:{dist:.2f}m', fontsize=9, color='#cccccc')

        return [self.current_pt, self.traj_line] + self.world_quivers + self.axis_quivers

    def run(self):
        print(f"Launch: {' '.join(self.slam_cmd)}")
        self.process = subprocess.Popen(
            self.slam_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env=self.slam_env,
            cwd=os.path.dirname(self.slam_cmd[0])
        )
        reader = threading.Thread(target=self.reader_thread, args=(self.process,), daemon=True)
        reader.start()

        def on_key(event):
            if event.key in ('enter', ' '):
                self.process.stdin.write('\n')
                self.process.stdin.flush()
        self.fig.canvas.mpl_connect('key_press_event', on_key)

        try:
            ani = FuncAnimation(self.fig, self.update_plot, interval=100, blit=False, cache_frame_data=False)
            plt.show(block=True)
        finally:
            self.running = False
            print("Shutting down SLAM...")
            try:
                self.process.send_signal(signal.SIGINT)
                self.process.wait(timeout=10)
            except:
                self.process.kill()

if __name__ == '__main__':
    HOME = os.path.expanduser("~")
    FAYS = os.path.join(HOME, "FaysSense_VI_Kit_Release")
    ORB  = os.path.join(HOME, "ORB-SLAM")
    arch = 'x86_64'

    vocab  = os.path.join(ORB, "Vocabulary", "ORBvoc.txt")
    orb_y  = os.path.join(FAYS, "orb_slam", "s80m_stereo_inertial.yaml")
    cam_y  = os.path.join(FAYS, "config", "fays_vikit_s80m.yaml")
    traj_dir = os.path.join(FAYS, "orb_slam", "trajectories")
    os.makedirs(traj_dir, exist_ok=True)
    traj = os.path.join(traj_dir, f"traj_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
    binary = os.path.join(FAYS, "orb_slam", "build", "fayssense_orb_slam")

    # 环境变量
    env = os.environ.copy()
    libs = ':'.join([
        f'{FAYS}/thirdparty/opencv-4.2.0-linux-{arch}/lib',
        f'{FAYS}/lib/fays_atrak/{arch}/Release',
        f'{FAYS}/thirdparty/ft602-linux-{arch}',
        f'{FAYS}/compat_libs',
        f'{ORB}/lib',
    ])
    env['LD_LIBRARY_PATH'] = libs + ':' + env.get('LD_LIBRARY_PATH', '')

    # USB 复位 + 端口检测 (等同 run_orb_slam.sh 的功能)
    import subprocess as _sp
    reset = os.path.join(FAYS, "orb_slam", "reset_usb")
    if os.path.exists(reset):
        _sp.run([reset], capture_output=True); time.sleep(1.5)
    # 检测端口
    import subprocess as _sp2
    try:
        out = _sp2.check_output(['v4l2-ctl','--list-devices'], text=True, timeout=5)
        import re as _re
        ftdi = ''
        in_ftdi = False
        for line in out.split('\n'):
            if 'FTDI' in line: in_ftdi = True; continue
            if line and not line.startswith('\t') and not line.startswith(' '): in_ftdi = False
            if in_ftdi:
                m = _re.search(r'/dev/video(\d+)', line)
                if m: ftdi += m.group(1) + ' '
        ports = ftdi.strip().split()
        if len(ports) >= 4:
            _sp2.run(['sed','-i','-E',f's|stereo_dev_port:.*|stereo_dev_port: /dev/video{ports[0]}|', cam_y])
            _sp2.run(['sed','-i','-E',f's|imu_dev_port:.*|imu_dev_port: /dev/video{ports[2]}|', cam_y])
            _sp2.run(['sed','-i','-E','s|rgb_dev_port:.*|rgb_dev_port: NULL|', cam_y])
            print(f"[DETECT] stereo=/dev/video{ports[0]} imu=/dev/video{ports[2]}")
    except: pass

    cmd = [binary, vocab, orb_y, cam_y, traj]
    plotter = RealtimePlotter(cmd, env)
    plotter.run()
