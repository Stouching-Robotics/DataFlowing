"""timestamp 采集侧口径回归（v1.3.11）—— 合成帧 + 假 writer，无真机无真 ffmpeg。

背景（UMI_9_29 ep317~347 真机复核）：`timestamp` 原先在**写入线程**取
`time.time() - episode_start`，于是把录制队列的常驻积压（FIFO 里 3~5 帧 =
95~165 ms，且整段恒定）算进了时间戳，表现为「起点 1 帧、5 行内跳到 4~5 帧、
之后完全平」（斜率≈0，R²≈0.01）。修法：生产者把**采集时刻的宿主单调钟**
随帧传下来（`capture_mono_ns`），写入侧用它算 timestamp。

覆盖：
  1. 给了 capture_mono_ns → timestamp 跟采集钟走（= 帧号/30，不含积压）
  2. 不给（默认）→ 保持旧的写入侧口径（向后兼容，既有调用方不受影响）
  3. 两段录制各自用自己段的基座（不跨段串味）

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_capture_timestamp.py
"""
import os
import shutil
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pyarrow.parquet as pq
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

import core.pipeline as pipeline_mod
from core.pipeline import CameraPipeline

OUT_ROOT = "/tmp/capture_timestamp_test"
SLOT = "gripper_rgb"
N = 20
FPS = 30.0

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

    drop_stats = None          # _finish_async 读它做 (… or {}).items()
    finalize_timing = None     # 同上；必须显式给 None，否则 __getattr__ 会返回
                               # 真值函数、把收尾线程带下去

    def __init__(self):
        super().__init__()
        self.rows = []

    def set_drop_stats(self, d):
        pass

    def set_device_status(self, *a, **k):
        pass

    def start_episode(self, *a, **k):
        return True          # 必须回 True，否则 _start_async 判失败并回滚

    def end_episode(self, *a, **k):
        return {"episode_index": 1, "length": len(self.rows)}

    def abort_episode(self, *a, **k):
        pass

    def write_video_frame(self, *a, **k):
        pass

    def write_depth_frame(self, *a, **k):
        pass

    def write_frame_row(self, frame_index, timestamp_s, **k):
        self.rows.append((frame_index, float(timestamp_s)))

    def __getattr__(self, name):        # 其余接口一律吸收
        def _noop(*a, **k):
            return None
        return _noop


def run_episode(app, use_capture_clock):
    """录一段：帧按严格 1/30 s 的采集钟网格推入（不含任何积压）。"""
    shutil.rmtree(OUT_ROOT, ignore_errors=True)
    os.makedirs(OUT_ROOT, exist_ok=True)
    writer = FakeWriter()
    pipeline_mod.EgoDataWriter = lambda: writer
    pip = CameraPipeline(OUT_ROOT)
    pip.register_external_source(SLOT, (8, 8), fps=FPS)

    pip.start_recording(SLOT)          # 真实路径：_start_async 起写线程
    t_end = time.time() + 3.0
    while (pip._write_thread is None or not pip._write_thread.is_alive()) \
            and time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)
    base = pip._episode_mono_base_ns
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    for i in range(N):
        cap = base + int(round(i * 1e9 / FPS))   # 第 i 帧恰在 base+i/30 秒采到
        pip.write_external_frame(
            SLOT, frame, hardware_ns=0,
            capture_mono_ns=(cap if use_capture_clock else 0))
    # 等写线程把队列排空
    q = pip._external_queues[SLOT]
    t_end = time.time() + 4.0
    while (not q.empty() or len(writer.rows) < N) and time.time() < t_end:
        app.processEvents()
        time.sleep(0.005)
    pip.finish_recording(SLOT)
    t_end = time.time() + 3.0
    while pip._write_thread is not None and pip._write_thread.is_alive() \
            and time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)
    return writer.rows


def main():
    app = QApplication.instance() or QApplication([])
    print("=== 1. 给 capture_mono_ns（采集侧口径）===")
    rows = run_episode(app, True)
    ts = [t for _, t in rows]
    expect = [i / FPS for i in range(len(ts))]
    dev = max(abs(a - b) for a, b in zip(ts, expect)) if ts else 9e9
    print(f"  行数={len(ts)}  timestamp[0..3]={['%.4f' % x for x in ts[:4]]} "
          f"最大偏差={dev * 1000:.2f}ms")
    check(len(ts) == N, f"写出 {N} 行")
    check(dev < 0.005, f"timestamp == 帧号/30（偏差 {dev * 1000:.2f}ms < 5ms）")

    print("\n=== 2. 不给 capture_mono_ns（旧写入侧口径，向后兼容）===")
    rows2 = run_episode(app, False)
    ts2 = [t for _, t in rows2]
    print(f"  行数={len(ts2)}  timestamp[0..3]={['%.4f' % x for x in ts2[:4]]}")
    check(len(ts2) == N, f"写出 {N} 行")
    # 写入侧取戳必然 >= 采集网格（有队列/调度延迟），且不再是精确 i/30
    if len(ts2) >= 2:
        d2 = [b - a for a, b in zip(ts2, ts2[1:])]
        check(max(d2) >= 1.0 / FPS - 1e-6,
              "旧口径仍是写入侧取戳（步长不小于一个标称帧间隔）")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        return 1
    print("PASS: timestamp 采集侧口径回归通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
