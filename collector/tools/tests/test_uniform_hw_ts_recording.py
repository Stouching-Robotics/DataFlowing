"""录制期 hardware_ns 归一 —— 覆盖**所有 RGB 视频帧**（v1.3.11）。

走**真实** `start_recording → _start_async` 路径 + 假 writer，无真机无 ffmpeg。

口径（用户要求）：视频帧一帧都不能少，但 `hardware_ns` 必须均匀 + 严格递增。
补帧/跳帧的**事实**不能丢：
  * `raw_hw_ns` 列保留归一前的原始设备戳；
  * D435 链路的 `rgb_dup`/`rgb_gap` 等诊断列不受影响（另见 d435_e2e_test）。

覆盖：
  1. 30fps 槽位：喂进真机实测的伪影（Δ=0 / Δ=2T）→ 帧数不变、步长唯一、严格递增
  2. **15fps 槽位**：T 跟着该槽位自己的 fps 走（证明不是写死 30fps）
  3. 每段重锚：两段录制各自从本段首帧起步（跨段空档不被抹平）
  4. hw=0 的帧（内部 CameraSlot 路径无时间戳）保持 0，且不占序号
  5. raw_hw_ns 原样保留

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_uniform_hw_ts_recording.py
"""
import os
import shutil
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

import core.pipeline as pipeline_mod
from core.d435_camera import TS_SRC_FRAMESET_HW
from core.pipeline import CameraPipeline

OUT_ROOT = "/tmp/uniform_hw_ts_test"
DEV_T30 = 33_344_000          # D435 实测帧周期
N = 90                        # 少于 lock_frames(60) 之后仍要正确；用 90 覆盖锁定点

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


class FakeWriter(QObject):
    log_occurred = pyqtSignal(str)
    encoder_label = "TEST"
    episode_index = 1
    task_dir = OUT_ROOT
    drop_stats = None
    finalize_timing = None

    def __init__(self):
        super().__init__()
        self.rows = []          # (frame_index, hardware_ns, raw_hw_ns)

    def start_episode(self, *a, **k):
        return True

    def end_episode(self, *a, **k):
        return {"episode_index": 1, "length": len(self.rows)}

    def abort_episode(self, *a, **k):
        pass

    def set_drop_stats(self, d):
        pass

    def write_video_frame(self, *a, **k):
        pass

    def write_depth_frame(self, *a, **k):
        pass

    def write_frame_row(self, frame_index, timestamp_s, **k):
        self.rows.append((int(frame_index), int(k.get("hardware_ns", 0)),
                          int(k.get("raw_hw_ns", 0))))

    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop


def run_segment(app, slot, fps, raws, writer, task="t", extra=None):
    """录一段：按给定的原始 timestamp 序列喂帧，返回写出的行。

    extra：逐帧诊断字典。**只有 D435 会带它**（ts_src 1/2 = 设备钟），
    宿主钟来源（夹爪/S80M/裸手）的帧不带 —— 这决定 hardware_ns 要不要归一化。
    """
    pip = CameraPipeline(OUT_ROOT)
    pip.register_external_source(slot, (8, 8), fps=fps)
    pipeline_mod.EgoDataWriter = lambda: writer
    writer.rows = []
    pip.start_recording(slot, task_name=task)
    t_end = time.time() + 4.0
    while ((pip._write_thread is None or not pip._write_thread.is_alive())
           or pip._writer is None) and time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)
    # 按源的真实节奏喂：录制队列 maxsize=30，瞬间灌 90 帧会被**正确地**丢掉
    # （write_external_frame 在 queue.Full 时计 drop_stats），那不是本测试要验的
    frame = np.zeros((8, 8, 3), np.uint8)
    for v in raws:
        pip.write_external_frame(slot, frame, hardware_ns=v, extra=extra)
        t_end = time.time() + 0.2
        while time.time() < t_end:                 # 每帧之间推进事件循环
            app.processEvents()
            time.sleep(0.002)
    q = pip._external_queues[slot]
    t_end = time.time() + 10.0
    while (not q.empty() or len(writer.rows) < len(raws)) and time.time() < t_end:
        app.processEvents()
        time.sleep(0.005)
    pip.finish_recording(slot)
    t_end = time.time() + 5.0
    while pip._write_thread is not None and pip._write_thread.is_alive() \
            and time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)
    return writer.rows


def with_anomalies(n, T, base):
    """构造真机实测的伪影形态：Δ=0 紧跟 Δ=2T，另有孤立 2T。"""
    v = [base + i * T for i in range(n)]
    v[30] = v[29]                    # Δ=0
    v[31] = v[29] + 2 * T            # Δ=2T
    v[60] = v[59] + 2 * T            # 孤立 2T
    return v


def lock_split(steps):
    """返回 (T, 锁定下标)：锁定后每一步都等于 T。

    不写死下标 —— 伪影间隔不会进 T 的样本，锁定点会比 lock_frames 往后挪几帧
    （本例的 Δ=0 / Δ=2T 各被排除，所以 60 个干净样本要到第 62 帧才凑齐）。
    """
    T = steps[-1]
    idx = max((i for i, x in enumerate(steps) if x != T), default=-1) + 1
    return T, idx


def main():
    os.makedirs(OUT_ROOT, exist_ok=True)
    app = QApplication([])

    print("=== 1. 30fps 槽位 + 真机实测伪影形态 ===")
    w = FakeWriter()
    raws = with_anomalies(N, DEV_T30, 5_000_000_000)
    rows = run_segment(app, "d435_rgb", 30.0, raws, w,
                       extra={"ts_src": TS_SRC_FRAMESET_HW})
    hw = [r[1] for r in rows]
    raw = [r[2] for r in rows]
    all_steps = [b - a for a, b in zip(hw, hw[1:])]
    T, lock_i = lock_split(all_steps)
    pre = sorted(set(all_steps[:lock_i]))
    print(f"  喂 {N} 帧 → 写 {len(rows)} 行")
    print(f"  锁定于第 {lock_i} 步；锁定后步长={T}；锁定前取值={pre}")
    check(len(rows) == N, "一帧都没少")
    check(all(x > 0 for x in all_steps), "严格递增")
    check(len(set(all_steps[lock_i:])) == 1,
          f"锁定后步长唯一（均匀）：{set(all_steps[lock_i:])}")
    pre_dev = max(abs(x - T) for x in all_steps[:lock_i])
    check(pre_dev < 1e6,
          f"锁定前偏差有界 <1ms（实测 {pre_dev / 1e3:.1f}µs：标称值 + 一次追赶步）")
    check(raw == raws, "raw_hw_ns 原样保留（含 Δ=0 / Δ=2T 的原始值）")
    check(len(set(hw)) == len(hw), "无重复时间戳")
    print(f"  实测 T={T} ns（干净间隔均值；设备实测周期 {DEV_T30}）")
    check(abs(T - DEV_T30) < 1_000,
          f"T 与设备实测周期一致（差 {abs(T - DEV_T30)} ns）")

    print("\n=== 2. 15fps 槽位：T 跟着该槽位自己的 fps ===")
    w2 = FakeWriter()
    T15 = 66_666_666
    raws15 = with_anomalies(N, T15, 8_000_000_000)
    rows2 = run_segment(app, "uvc_rgb", 15.0, raws15, w2,
                        extra={"ts_src": TS_SRC_FRAMESET_HW})
    hw2 = [r[1] for r in rows2]
    s2 = [b - a for a, b in zip(hw2, hw2[1:])]
    T2, lock2 = lock_split(s2)
    print(f"  喂 {N} 帧 → 写 {len(rows2)} 行；锁定于第 {lock2} 步，T={T2}")
    check(len(rows2) == N, "一帧都没少")
    check(all(x > 0 for x in s2), "严格递增")
    check(len(set(s2[lock2:])) == 1 and abs(T2 - T15) < 1_000_000,
          f"锁定后步长唯一且≈15fps 周期（T={T2}）")
    check(T2 != T, f"与 30fps 槽位用的是各自的 T（15fps={T2} vs 30fps={T}）")

    print("\n=== 3. 两段录制各自重锚（跨段空档不抹平）===")
    w3 = FakeWriter()
    seg1 = with_anomalies(N, DEV_T30, 1_000_000_000)
    r1 = run_segment(app, "d435_rgb", 30.0, seg1, w3, task="t1",
                     extra={"ts_src": TS_SRC_FRAMESET_HW})
    seg2 = with_anomalies(N, DEV_T30, 1_000_000_000 + N * DEV_T30 + 500_000_000_000)
    r2 = run_segment(app, "d435_rgb", 30.0, seg2, w3, task="t2",
                     extra={"ts_src": TS_SRC_FRAMESET_HW})
    # 第 1 段的行可能还留在 w3.rows 里，取各自首行的差值即可
    print(f"  第1段首行 hw={r1[0][1]}  第2段首行 hw={r2[0][1]}  "
          f"原始差={(seg2[0] - seg1[0]) / 1e9:.1f}s")
    check(r1[0][1] == seg1[0] and r2[0][1] == seg2[0],
          "每段首帧都锚在自己的原始设备戳上（空档保留）")

    print("\n=== 4. hw=0 保持 0 且不占序号 ===")
    w4 = FakeWriter()
    mixed = with_anomalies(N, DEV_T30, 3_000_000_000)
    mixed[40] = 0                     # 该帧没有时间戳
    rows4 = run_segment(app, "d435_rgb", 30.0, mixed, w4,
                        extra={"ts_src": TS_SRC_FRAMESET_HW})
    hw4 = [r[1] for r in rows4]
    step_before = hw4[39] - hw4[38]
    print(f"  第 40 帧 hw={hw4[40]}；hw[39]={hw4[39]} → hw[41]={hw4[41]}"
          f"（差 {hw4[41] - hw4[39]}，单拍={step_before}）")
    check(hw4[40] == 0, "无时间戳的帧仍然写 0")
    check(hw4[41] - hw4[39] == step_before,
          "无戳帧不占序号（跨过它只前进一拍）")

    print("\n=== 5. 宿主钟帧：hardware_ns 原样落盘（不再重排成网格）===")
    w5 = FakeWriter()
    host_raw = [3_000_000_000 + i * DEV_T30 for i in range(N)]
    for i in range(5, N, 10):
        host_raw[i] += 5_000_000          # 宿主戳的真实双峰抖动（晚到 5ms）
    rows5 = run_segment(app, "gripper_rgb", 30.0, host_raw, w5, extra=None)
    hw5 = [r[1] for r in rows5]
    raw5 = [r[2] for r in rows5]
    steps5 = [b - a for a, b in zip(hw5, hw5[1:])]
    print(f"  喂 {N} 帧 → 写 {len(rows5)} 行；步长取值 {sorted(set(steps5))[:4]}…")
    check(len(rows5) == N, "一帧都没少")
    check(hw5 == host_raw, "宿主钟帧的 hardware_ns 与原始戳逐帧相同（不重排）")
    check(raw5 == host_raw, "raw_hw_ns 与 hardware_ns 相同（没有发生归一化）")
    check(len(set(steps5)) > 1, f"保留真实抖动而不是单一网格步长（{len(set(steps5))} 种）")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        return 1
    print("PASS: 所有 RGB 视频帧的 hardware_ns 归一回归通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
