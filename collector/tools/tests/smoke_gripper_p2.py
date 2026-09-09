"""P2 真机冒烟：GripperBridge 相机组 + DECXIN RGB + 触觉双进程全链。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/smoke_gripper_p2.py [esp_serial]

成功路径: opened 后观察到 RGB 帧与左右触觉结果 → 0 退出。
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
             "last_force_l": None, "last_force_r": None}
    deadline = time.monotonic() + 150.0

    def on_rgb(frame, hw_ns):
        stats["rgb"] += 1
        if stats["rgb"] <= 2 or stats["rgb"] % 300 == 0:
            print(f"[RGB] #{stats['rgb']} {frame.shape} hw={hw_ns}",
                  flush=True)

    def on_tactile(side, heatmap, force, matrix):
        key = "tactile_l" if side == "left" else "tactile_r"
        stats[key] += 1
        stats[f"last_force_{side[0]}"] = force
        if stats[key] <= 2 or stats[key] % 100 == 0:
            print(f"[TACTILE] {side} #{stats[key]} force={tuple(force)}"
                  f" heatmap={heatmap.shape} matrix="
                  f"{'None' if matrix is None else matrix.shape}",
                  flush=True)

    def on_opened():
        print("[OPENED] 相机 + 触觉链路就绪", flush=True)

    def on_error(msg):
        print(f"[ERROR] {msg}", flush=True)
        finish(2)

    def finish(code):
        bridge.close()
        print(f"── 汇总: rgb={stats['rgb']} "
              f"tactile_l={stats['tactile_l']} tactile_r={stats['tactile_r']} "
              f"force_l={stats['last_force_l']} force_r={stats['last_force_r']}",
              flush=True)
        app.exit(code)

    bridge.rgb_frame_ready.connect(on_rgb)
    bridge.tactile_ready.connect(on_tactile)
    bridge.opened.connect(on_opened)
    bridge.error.connect(on_error)
    bridge.log.connect(lambda m: print(f"[LOG] {m}", flush=True))

    def watch():
        if time.monotonic() > deadline:
            print("[TIMEOUT] 150s 未完成验证", flush=True)
            finish(3)
            return
        if (stats["rgb"] >= 3 and stats["tactile_l"] >= 3
                and stats["tactile_r"] >= 3):
            print("[DONE] RGB + 左右触觉结果均已到达", flush=True)
            finish(0)
            return
        QTimer.singleShot(500, watch)

    QTimer.singleShot(500, watch)
    print(f"[START] esp_serial={esp_serial}", flush=True)
    bridge.open(esp_serial)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
