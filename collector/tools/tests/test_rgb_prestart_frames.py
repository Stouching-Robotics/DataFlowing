"""外部帧源（夹爪 RGB）「录制开始前的帧被写进新视频」回归测试。

背景：`_external_queues` 从注册起就一直在收帧——夹爪 RGB 常驻读取线程
（bridge `_rgb_run`）从设备打开起持续投递，只有录制期间才被 `_write_loop`
消费。而录制边界（`_start_async` 的清空段、`finish_recording`）只排空了
`CameraSlot.frame_queue` 与 `_sensor_queue`，**漏了外部队列**，于是

  ① 上一段停止时残留的（深缓冲 maxsize=30 ≈1s，满队列 ~110MB/槽）
  ② 本段 `_recording=True` 之后、写线程就绪之前累积的（`start_episode`
     里编码器探测 + 建目录要几百 ms，这段窗口内外部帧照投不误）

被原样写成新 mp4 的开头。实测每段开头约 28 帧与前一段 episode 的**末帧**
逐帧匹配，跳变索引随段次棘轮式涨到饱和（30），这解释了「有时候」会中招。

本测试走**真实** `start_recording → _start_async` 路径（不是像
test_imu_pending 那样直接置 `_recording=True` 绕过，那样测不出本 bug）：
FakeWriter 的 `start_episode` 由 Event 闸门拉住，把启动窗口放大到可控。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_rgb_prestart_frames.py
"""
import os
import sys
import time
import queue
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

import core.pipeline as pipeline_mod
from core.pipeline import CameraPipeline

OUT_ROOT = "/tmp/rgb_prestart_test"
SLOT = "gripper_rgb"
H, W = 8, 8                     # 小帧：只需能读出标记像素

RESIDUE_TAGS = (11, 12, 13, 14, 15)   # 上一段残留（直接塞进队列模拟）
PRESTART_BASE = 100                    # 启动窗口内投递 → 属于「录制开始前」
REAL_BASE = 1000                       # 写线程就绪后投递 → 属于本段真实画面
FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def tagged(tag: int) -> np.ndarray:
    """把序号编码进前两个颜色通道，供断言逐帧识别。"""
    f = np.zeros((H, W, 3), dtype=np.uint8)
    f[..., 0] = tag & 0xFF
    f[..., 1] = (tag >> 8) & 0xFF
    return f


def decode(frame: np.ndarray) -> int:
    return int(frame[0, 0, 0]) | (int(frame[0, 0, 1]) << 8)


class FakeWriter(QObject):
    """假 writer：记录 write_video_frame 收到的帧标记。"""

    log_occurred = pyqtSignal(str)

    encoder_label = "TEST"
    episode_index = 1
    task_dir = OUT_ROOT

    def __init__(self, gate: threading.Event):
        super().__init__()
        self._gate = gate
        self.frames = []
        self.rows = 0
        self.ended = False

    def start_episode(self, *a, **k):
        # 放大启动窗口：真实实现要跑编码器探测 + 建目录（几百 ms），
        # 期间 _recording 已是 True，外部帧会持续入队
        self._gate.wait(timeout=5.0)
        return True

    def write_video_frame(self, sid, frame, flip_vertical=True):
        self.frames.append(decode(frame))

    def write_frame_row(self, *a, **k):
        self.rows += 1

    def write_depth_frame(self, *a, **k):
        pass

    def set_drop_stats(self, stats):
        pass

    def end_episode(self):
        self.ended = True


def main():
    app = QApplication(sys.argv)
    gate = threading.Event()
    writer = FakeWriter(gate)

    saved = pipeline_mod.EgoDataWriter
    pipeline_mod.EgoDataWriter = lambda: writer
    try:
        pip = CameraPipeline(OUT_ROOT)
        pip.register_external_source(SLOT, (H, W), fps=30.0)
        q = pip._external_queues[SLOT]

        # ① 上一段停止时没被消费掉的残留帧（录制边界不清空就会留到本段开头）
        for t in RESIDUE_TAGS:
            q.put_nowait((tagged(t), 0, None))

        # ② 常驻 RGB 读取线程：从录制开始起持续投递，直到闸门放开
        prestart = []
        stop = threading.Event()

        def producer():
            n = PRESTART_BASE
            while not stop.is_set():
                if not gate.is_set():
                    # 闸门未放 = 写线程还没就绪 → 这帧属于「录制开始前」
                    prestart.append(n)
                pip.write_external_frame(SLOT, tagged(n), 0)
                n += 1
                time.sleep(0.005)       # 200fps，快速填满 30 深队列

        prod = threading.Thread(target=producer, daemon=True)
        prod.start()
        time.sleep(0.05)                # 未录制：write_external_frame 会直接丢弃

        # ③ 真实启动路径（异步；_recording 同步置位 → 生产者立刻开始入队）
        pip.start_recording(SLOT)
        time.sleep(0.30)                # 启动窗口：编码器探测期间照投不误
        # 先停生产者再放闸门：排空发生在 start_episode 返回之后，若生产者
        # 还在跑，它会在排空与写线程启动之间再塞进几帧（测试自身的不确定）
        stop.set()
        prod.join(timeout=2.0)

        depth_at_gate = q.qsize()
        check(depth_at_gate > 0,
              f"启动窗口内队列确有积压（{depth_at_gate} 帧，来自残留+启动窗口）")
        gate.set()                      # 放开 start_episode → 排空 → 起写线程

        # 等写线程真正起来（它启动前执行过排空）
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if pip._write_thread is not None and pip._write_thread.is_alive():
                break
            time.sleep(0.01)
        check(pip._write_thread is not None and pip._write_thread.is_alive(),
              "写线程已启动（真实 start_recording → _start_async 路径）")
        check(q.empty(),
              f"写线程就绪时队列已被排空（实际剩 {q.qsize()} 帧）")

        # ④ 排空之后投递的真实帧必须逐帧落进视频
        for t in range(REAL_BASE, REAL_BASE + 4):
            pip.write_external_frame(SLOT, tagged(t), 0)
        time.sleep(0.30)                # 30fps × 4 帧 ≈ 0.13s，留足余量

        pip.finish_recording(SLOT)
        deadline = time.time() + 5.0
        while time.time() < deadline and not writer.ended:
            app.processEvents()
            time.sleep(0.02)

        # ── 断言 ──────────────────────────────────────────────
        bad = [t for t in writer.frames if t in prestart]
        check(len(prestart) >= 10,
              f"启动窗口内确实投递过足够多的帧（{len(prestart)} 帧）")
        check(not bad,
              f"录制开始前的帧一个都没进视频（越界 {len(bad)} 帧: {bad[:5]}）")
        residue = [t for t in writer.frames if t in RESIDUE_TAGS]
        check(not residue,
              f"上一段残留帧一个都没进视频（越界 {len(residue)} 帧: {residue}）")
        real = [t for t in writer.frames if REAL_BASE <= t < REAL_BASE + 4]
        check(real == list(range(REAL_BASE, REAL_BASE + 4)),
              f"排空后的真实帧逐帧落盘（实际 {real}）")
        check(writer.frames and writer.frames[0] >= REAL_BASE,
              f"视频首帧即真实帧（实际首帧标记 {writer.frames[0] if writer.frames else None}）")
        check(q.empty(), "停止后队列不残留（满队列 ~110MB/槽）")

        # ⑤ 停止路径的排空（写线程已退出，隔离验证）
        pip._recording = True
        pip._write_thread = None
        for t in RESIDUE_TAGS:
            q.put_nowait((tagged(t), 0, None))
        pip.finish_recording(SLOT)
        check(q.empty(), "finish_recording 排空外部队列")
        pip._recording = True
        for t in RESIDUE_TAGS:
            q.put_nowait((tagged(t), 0, None))
        pip.abort_recording(SLOT)
        check(q.empty(), "abort_recording 排空外部队列")

        print()
        if FAILS:
            print(f"FAIL: {len(FAILS)} 项未通过")
            return 1
        print(f"PASS: 段首旧帧回归通过（启动窗口 {len(prestart)} 帧 + "
              f"残留 {len(RESIDUE_TAGS)} 帧全部丢弃，真实帧 {len(real)}/{4} 落盘）")
        return 0
    finally:
        pipeline_mod.EgoDataWriter = saved


if __name__ == "__main__":
    sys.exit(main())
