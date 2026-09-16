"""P3/P5 帧率探针：SLAM 初始化完成后统计 30s 内各流实际速率。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/probe_gripper_rates.py [esp_serial1] [esp_serial2]

单 rig：只给第一个序列号（缺省 CC:BA:97:25:B4:44，与 GUI 同口径）。
双 rig：给两个序列号，两套同时打开——rig1 旧核表（触觉 0/2 + SLAM
4-9，物理核 0,2,4-9），rig2 独立物理核分区（触觉 1/3 + SLAM
10/11/13/15/22/23，物理核 1,3,10,11；见 core.gripper.bridge 的
_select_cpu_partition），两 rig 零物理核共享。分别统计帧率与
双目取帧空桶率（30fps 桶、wall 时钟，口径复用主程序
s80m_drop_watch）——空桶率>10% 即 SLAM 取帧未达 30fps。

输出: 每 rig 的 rgb/stereo_l/pose 计数与 Hz、imu 包计数（raw socket
包级窗口增量，IMU 原始数据不外送）、raw socket 累计
stereo_packets/imu_packets（原生视角）、30s 窗口双目空桶率。
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
    serials = list(sys.argv[1:3]) or ["CC:BA:97:25:B4:44"]
    rigs = []
    for idx, serial in enumerate(serials, start=1):
        bridge = GripperBridge(
            stereo_slot=("gripper_stereo_left" if idx == 1
                         else f"gripper_{idx}_stereo_left"),
            rig_index=idx)
        rigs.append({
            "bridge": bridge,
            "serial": serial,
            "rgb": 0, "stereo_l": 0, "pose": 0, "imu": 0,
            "imu_base": None, "pose_first": None, "error": None,
        })
    stats = {"window_start": None}
    deadline = time.monotonic() + 300.0
    window_s = 30.0

    def wire(rig):
        b, s = rig["bridge"], rig

        def on_rgb(frame, hw_ns, s=s):
            s["rgb"] += 1

        def on_stereo(slot, frame, hw_ns, s=s):
            s["stereo_l"] += 1

        def on_pose(pos, quat, traj=(), timestamp=None, host_ns=None, s=s):
            s["pose"] += 1
            # host_ns 只在签名里显式收下、本探针不消费：PyQt5 会把多余实参
            # 静默截断给「收得少」的槽，漏写这个参数不会报错只会悄悄丢戳，
            # 所以宁可显式列出也不靠截断兜底。
            if s["pose_first"] is None:
                s["pose_first"] = time.monotonic()
                raw = b._raw_client
                if raw is not None:
                    s["imu_base"] = raw.imu_packets
                _maybe_start_window()

        def on_error(msg, s=s):
            s["error"] = msg
            print(f"[ERROR rig {s['serial']}] {msg}", flush=True)
            _maybe_start_window()

        def on_log(msg, s=s):
            print(f"[LOG {s['serial'][-4:]}] {msg}", flush=True)

        b.rgb_frame_ready.connect(on_rgb)
        b.stereo_frame_ready.connect(on_stereo)
        b.pose_ready.connect(on_pose)
        b.error.connect(on_error)
        b.log.connect(on_log)

    def _maybe_start_window():
        # 所有未报错的 rig 都出首 pose 后统一开窗（对齐统计窗口）；
        # 全部 rig 报错则直接结束（单 rig 模式=旧行为）
        if stats["window_start"] is not None:
            return
        pending = [r for r in rigs if r["error"] is None]
        if not pending:
            finish(2)
            return
        if all(r["pose_first"] is not None for r in pending):
            stats["window_start"] = time.monotonic()
            # 空桶口径与录制同款：窗口起点重置
            for r in rigs:
                r["bridge"].reset_stereo_drop_watch()
            print("[WINDOW] 全部 rig 首 pose 已到，开窗 30s", flush=True)

    def finish(code):
        if stats["window_start"] is not None:
            window = time.monotonic() - stats["window_start"]
            for r in rigs:
                b = r["bridge"]
                raw = b._raw_client
                if raw is not None and r["imu_base"] is not None:
                    r["imu"] = raw.imu_packets - r["imu_base"]
                dropped, elapsed = b.stereo_drop_snapshot()
                rate = (dropped / elapsed * 100) if elapsed else 0.0
                print(f"── rig {r['serial']} 窗口 {window:.1f}s "
                      f"rgb={r['rgb']}({r['rgb']/window:.1f}Hz) "
                      f"stereo_l={r['stereo_l']}"
                      f"({r['stereo_l']/window:.1f}Hz) "
                      f"pose={r['pose']}({r['pose']/window:.1f}Hz) "
                      f"imu={r['imu']}({r['imu']/window:.1f}Hz) "
                      f"双目空桶={dropped}/{elapsed}({rate:.1f}%)",
                      flush=True)
                if raw is not None:
                    print(f"   raw socket: stereo_packets="
                          f"{raw.stereo_packets} "
                          f"imu_packets={raw.imu_packets}", flush=True)
        else:
            print("── 首 pose 未出现，无窗口统计", flush=True)
        for r in rigs:
            r["bridge"].close()
        app.exit(code)

    for rig in rigs:
        wire(rig)

    def watch():
        if time.monotonic() > deadline:
            print("[TIMEOUT] 300s 内未见全部 pose", flush=True)
            finish(3)
            return
        if stats["window_start"] is not None and (
                time.monotonic() - stats["window_start"] >= window_s):
            finish(0)
            return
        QTimer.singleShot(500, watch)

    QTimer.singleShot(500, watch)
    print(f"[START] serials={serials} window={window_s:.0f}s", flush=True)
    for rig in rigs:
        rig["bridge"].open(rig["serial"])
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
