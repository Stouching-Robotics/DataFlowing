"""P3 真机冒烟：GripperBridge 全链（相机组 + RGB + 触觉 + SLAM + 双目流）。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/smoke_gripper_p3.py [esp_serial]

前提: 夹爪 USB3 头已插 USB3 口（SLAM IMU 依赖 SuperSpeed）。
成功路径: opened 后观察到 RGB 帧、左右触觉结果、左右双目帧与 SLAM 位姿
→ 0 退出。日志顺序应为:
    device.serial=3500000262300098 → [FAYS-CALIB] orb_runtime_yaml= → READY
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, QTimer

from core.gripper.bridge import GripperBridge


def main():
    app = QCoreApplication(sys.argv)
    esp_serial = sys.argv[1] if len(sys.argv) > 1 else "CC:BA:97:25:B4:44"
    bridge = GripperBridge()
    stats = {"rgb": 0, "tactile_l": 0, "tactile_r": 0,
             "stereo_l": 0, "pose": 0,
             "last_force_l": None, "last_force_r": None,
             "last_pos": None}
    deadline = time.monotonic() + 600.0

    def on_rgb(frame, hw_ns):
        stats["rgb"] += 1
        if stats["rgb"] <= 2 or stats["rgb"] % 300 == 0:
            print(f"[RGB] #{stats['rgb']} {frame.shape} hw={hw_ns}",
                  flush=True)

    def on_stereo(slot, frame, hw_ns):
        stats["stereo_l"] += 1
        if stats["stereo_l"] <= 3:
            print(f"[STEREO] {slot} #{stats['stereo_l']} {frame.shape} "
                  f"hw={hw_ns}", flush=True)

    def on_tactile(side, heatmap, force, matrix, capture_ns):
        key = "tactile_l" if side == "left" else "tactile_r"
        stats[key] += 1
        stats[f"last_force_{side[0]}"] = force
        if stats[key] <= 2 or stats[key] % 100 == 0:
            print(f"[TACTILE] {side} #{stats[key]} force={tuple(force)}"
                  f" heatmap={heatmap.shape} matrix="
                  f"{'None' if matrix is None else matrix.shape}",
                  flush=True)

    def on_pose(pos, quat, traj=(), timestamp=None, host_ns=None):
        stats["pose"] += 1
        stats["last_pos"] = pos
        # host_ns 是取样帧的宿主单调钟纳秒（与 hardware_ns 同时基）；None =
        # 当前 native 二进制未打印 Host 字段（只做了 Python 侧改动、没重编
        # 原生库时的预期状态）。真机验收时应看到非 None、且逐点单调递增。
        if host_ns is not None:
            stats["host_ns_last"] = host_ns
        if stats["pose"] <= 3 or stats["pose"] % 100 == 0:
            print(f"[POSE] #{stats['pose']} pos={tuple(pos)} "
                  f"quat={tuple(quat)} t={timestamp} "
                  f"host_ns={host_ns}", flush=True)

    def on_opened():
        print("[OPENED] 相机 + 触觉 + SLAM 链路就绪", flush=True)
        print("[HINT] 请拿起夹爪缓慢晃动/平移 10-20 秒：ORB-SLAM 需要"
              " 运动才能初始化出位姿，静止不会输出 pose", flush=True)

    def on_error(msg):
        print(f"[ERROR] {msg}", flush=True)
        finish(2)

    def finish(code):
        bridge.close()
        print(f"── 汇总: rgb={stats['rgb']} "
              f"tactile_l={stats['tactile_l']} tactile_r={stats['tactile_r']} "
              f"stereo_l={stats['stereo_l']} "
              f"pose={stats['pose']} last_pos={stats['last_pos']} "
              f"mode={bridge._stereo_mode}",
              flush=True)
        app.exit(code)

    bridge.rgb_frame_ready.connect(on_rgb)
    bridge.stereo_frame_ready.connect(on_stereo)
    bridge.tactile_ready.connect(on_tactile)
    bridge.pose_ready.connect(on_pose)
    bridge.opened.connect(on_opened)
    bridge.error.connect(on_error)
    bridge.log.connect(lambda m: print(f"[LOG] {m}", flush=True))

    def watch():
        if time.monotonic() > deadline:
            print("[TIMEOUT] 600s 未完成验证", flush=True)
            finish(3)
            return
        if (stats["rgb"] >= 3 and stats["tactile_l"] >= 3
                and stats["tactile_r"] >= 3
                and stats["stereo_l"] >= 3
                and stats["pose"] >= 3):
            print("[DONE] RGB + 左右触觉 + 左目 + SLAM 位姿均已到达",
                  flush=True)
            finish(0)
            return
        QTimer.singleShot(500, watch)

    QTimer.singleShot(500, watch)
    print(f"[START] esp_serial={esp_serial}", flush=True)
    bridge.open(esp_serial)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
