"""落盘侧帧空洞计量测试（v1.3.10）—— 合成帧 + 假 writer，无真机无真 ffmpeg。

覆盖：写线程按帧自带 hardware_ns 计量空洞（口径与 tools/audit_frame_gaps.py
一致）、空洞闭合到门槛时当场打日志、门槛以下只记账不出声、无时间戳槽位退化为
宿主单调钟且不产生伪空洞、**每段重新锁时间基座**（否则第二段的钟差会被记成
一次天文数字级的空洞）、drop_stats 注入的键一律不算帧数、note_drop_stats 的
峰值/累加语义。

走**真实** `start_recording → _start_async` 路径（不是直接置 `_recording=True`
绕过），这样 `_ext_gap_watches` 的每段重置才真的被覆盖到。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_ext_frame_gap.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

import core.pipeline as pipeline_mod
from core.pipeline import CameraPipeline, frame_drop_total

OUT_ROOT = "/tmp/ext_frame_gap_test"
SLOT = "gripper_rgb"          # 帧自带 hardware_ns
SLOT_MONO = "gripper_2_rgb"   # 恒传 hw_ns=0（lite 路径）
H, W = 8, 8

STEP = 33_333_333             # 30fps
T0 = 7_000_000_000_000        # 第一段的帧钟基座
T1 = T0 + 1_000_000_000_000   # 第二段：换个基座（模拟设备/宿主钟不同源）

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


class FakeWriter(QObject):
    """假 writer：只记 set_drop_stats，其余接口吸收调用。"""

    log_occurred = pyqtSignal(str)

    encoder_label = "TEST"
    episode_index = 1
    task_dir = OUT_ROOT

    def __init__(self):
        super().__init__()
        self.drop_stats = None
        self.rows = 0
        self.ended = False

    def start_episode(self, *a, **k):
        return True

    def write_video_frame(self, *a, **k):
        pass

    def write_frame_row(self, *a, **k):
        self.rows += 1

    def write_depth_frame(self, *a, **k):
        pass

    def set_drop_stats(self, stats):
        self.drop_stats = dict(stats)

    def end_episode(self):
        self.ended = True

    def abort_episode(self):
        self.ended = True


def wait_for(pred, timeout=5.0, app=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    return False


def frame():
    return np.zeros((H, W, 3), dtype=np.uint8)


def main():
    app = QApplication(sys.argv)
    writer = FakeWriter()
    pipeline_mod.EgoDataWriter = lambda: writer

    pip = CameraPipeline(OUT_ROOT)
    pip.register_external_source(SLOT, (H, W), fps=30.0)
    pip.register_external_source(SLOT_MONO, (H, W), fps=30.0)

    logs = []
    pip.recording_log.connect(logs.append)

    # ── 第一段 ────────────────────────────────────────────
    pip.start_recording(SLOT)
    check(wait_for(lambda: pip._write_thread is not None
                   and pip._write_thread.is_alive(), app=app),
          "写线程已启动（真实 start_recording → _start_async 路径）")
    check(pip._ext_gap_watches == {},
          "每段起始空洞看门狗为空（_start_async 重置）")

    f = frame()
    # 10 帧正常（33.3ms 一个）
    for i in range(10):
        pip.write_external_frame(SLOT, f, T0 + i * STEP)
    # 一个 200ms 的洞：≥ 门槛(100ms) 但 < 告警门槛(500ms) → 只记账不出声
    t = T0 + 9 * STEP + 233_333_333
    for i in range(5):
        pip.write_external_frame(SLOT, f, t + i * STEP)
    # 一个 600ms 的洞 → 当场告警
    hole_start = t + 4 * STEP
    hole_len = 633_333_333
    for i in range(5):
        pip.write_external_frame(SLOT, f, hole_start + hole_len + i * STEP)
    # 无时间戳槽位：hw_ns 恒 0 → 退化为出队瞬间的宿主单调钟
    for _ in range(5):
        pip.write_external_frame(SLOT_MONO, f, 0)

    def consumed(slot):
        w = pip._ext_gap_watches.get(slot)
        return w.snapshot()["frames"] if w else 0

    n_expect = 20                      # 10 + 5 + 5（本槽位）
    check(wait_for(lambda: consumed(SLOT) >= n_expect),
          f"写线程消费完 {n_expect} 帧（实际 {consumed(SLOT)}）")

    snap = pip._ext_gap_watches[SLOT].snapshot()
    check(snap["source"] == "hw", "帧自带时间戳的槽位基座 = hw")
    check(snap["first_ns"] == T0, "基座帧 = 本段首帧")
    check(snap["gap_count"] == 2, f"两个洞（200ms 与 633ms），实际 {snap['gap_count']}")
    check(abs(snap["gap_max_ns"] - (hole_len - STEP)) < 1000,
          f"最大洞 ≈ 600ms（实际 {snap['gap_max_ns'] / 1e6:.1f}ms）")
    check(snap["gap_max_at_ns"] == hole_start,
          "最大洞起点 = 洞前最后一帧的时刻")
    expect_total = (233_333_333 - STEP) + (hole_len - STEP)
    check(abs(snap["gap_ns"] - expect_total) < 2000,
          f"累计空洞 ≈ {expect_total / 1e6:.1f}ms（实际 {snap['gap_ns'] / 1e6:.1f}ms）")

    # 告警由写线程 emit、经 Qt 队列投到主线程（与 bridge 同源），要 processEvents
    got_alert = wait_for(lambda: any("RGB 帧空洞" in l for l in logs), app=app)
    alerts = [l for l in logs if "RGB 帧空洞" in l]
    check(got_alert and len(alerts) == 1,
          f"只对 ≥500ms 的那个洞告警（实际 {len(alerts)} 行）")
    if alerts:
        print(f"       {alerts[0]}")
        check("槽 gripper_rgb" in alerts[0] and "开录后 0.7s 起" in alerts[0],
              "告警行含槽位与「开录后多久」（对应洞起点，不是告警时刻）")

    mono = pip._ext_gap_watches.get(SLOT_MONO)
    check(mono is not None and mono.snapshot()["source"] == "mono",
          "hw_ns=0 的槽位退化为宿主单调钟基座")
    check(mono is not None and mono.snapshot()["gap_count"] == 0,
          "无时间戳槽位按出队节奏计量 → 不产生伪空洞")

    # ── 第一段落盘注入 ────────────────────────────────────
    finished = []
    pip.recording_finished.connect(lambda sid, p: finished.append(sid))
    pip.finish_recording(SLOT)
    check(wait_for(lambda: finished, app=app),
          "第一段结束（_finish_async 走完，writer 与 session 已清）")
    stats = pip.last_drop_stats
    check(stats.get(f"{SLOT}_gap_count") == 2, f"gap_count 落进 drop_stats: {stats}")
    check(stats.get(f"{SLOT}_gap_max_ms") == 600,
          f"gap_max_ms 取毫秒峰值: {stats.get(f'{SLOT}_gap_max_ms')}")
    check(stats.get(f"{SLOT}_gap_ms") == round(expect_total / 1e6),
          f"gap_ms 为累计毫秒: {stats.get(f'{SLOT}_gap_ms')}")
    check(stats.get(f"{SLOT_MONO}_gap_count") is None,
          "零空洞的槽位不落键（不写一堆没意义的 0）")
    check(frame_drop_total(stats) == 0,
          f"空洞键不算帧数（frame_drop_total = {frame_drop_total(stats)}）")
    check(writer.drop_stats == stats, "同一份统计注入 writer 元数据")

    # ── 第二段：换时间基座，不许把钟差记成空洞 ──────────────
    check(pip._writer is None and pip._session_path is None,
          "第一段的 writer/session 引用已清（第二段不会被 _finish_async 回冲）")
    writer.ended = False
    logs.clear()
    pip.start_recording(SLOT)
    check(wait_for(lambda: pip._write_thread is not None
                   and pip._write_thread.is_alive(), app=app), "第二段写线程已启动")
    check(pip._ext_gap_watches == {}, "第二段起始看门狗被重置（上一段的洞不带过来）")
    for i in range(5):
        pip.write_external_frame(SLOT, f, T1 + i * STEP)
    check(wait_for(lambda: pip._ext_gap_watches.get(SLOT)
                   and pip._ext_gap_watches[SLOT].snapshot()["frames"] >= 5),
          "第二段消费 5 帧")
    snap2 = pip._ext_gap_watches[SLOT].snapshot()
    check(snap2["first_ns"] == T1, "基座重新锁到第二段首帧")
    check(snap2["gap_count"] == 0 and snap2["gap_ns"] == 0,
          "换了钟基座也不产生伪空洞（这就是每段必须重置的理由）")

    # ── note_drop_stats 语义 ──────────────────────────────
    pip.note_drop_stats({f"{SLOT}_readfail_max_ms": 4681,
                         f"{SLOT}_readfail_ms": 4681,
                         f"{SLOT}_reconnect_count": 2,
                         f"{SLOT}_overwrite_count": 0})
    snapped = pip._drop_stats.snapshot()
    check(snapped.get(f"{SLOT}_readfail_max_ms") == 4681
          and snapped.get(f"{SLOT}_readfail_ms") == 4681
          and snapped.get(f"{SLOT}_reconnect_count") == 2,
          f"采集侧计数并入本段: {snapped}")
    check(f"{SLOT}_overwrite_count" not in snapped, "零值不落键")
    pip.note_drop_stats({f"{SLOT}_readfail_max_ms": 120,
                         f"{SLOT}_reconnect_count": 1})
    snapped = pip._drop_stats.snapshot()
    check(snapped[f"{SLOT}_readfail_max_ms"] == 4681,
          "*_max_ms 是峰值语义（后到的 120 不覆盖 4681）")
    check(snapped[f"{SLOT}_reconnect_count"] == 3, "其余键累加")
    check(frame_drop_total(snapped) == 0,
          f"采集侧诊断键同样不算帧数（{frame_drop_total(snapped)}）")

    pip.finish_recording(SLOT)
    check(wait_for(lambda: writer.ended, app=app), "第二段结束")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: ext_frame_gap 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
