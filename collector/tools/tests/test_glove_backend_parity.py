#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手套两个后端（fork / sdk）的**逐位**等价性（迁移计划 T1）:

    venv/bin/python tools/tests/test_glove_backend_parity.py

这是迁移期最强的一张网：同一串字节流灌给两个后端，`UsbGloveEngine` 的
输出必须**逐位相同**。它同时钉住三件事：

  1. 传输层替换（`core/glove_usb/streams.py` → `glove_io/streams.py`）
     没有改变任何一帧的解析结果；
  2. 触觉预处理器替换（`core/glove_usb/tactile_processing.py` →
     `gui/tactile_processing.py`）输出逐位一致 —— 计划 §2.4 说两份"契约
     逐项相同"，这里把它变成断言。实测两份**除行尾（SDK 是 CRLF）外逐
     字节相同**，所以本该一致；
  3. 回归记账的那两处（R2 `exclusive`、R3 重试退避）**不影响数据面**：
     它们改的是打开的互斥性与重连节奏，不改帧内容。若哪天影响到这里，
     说明记账记错了。

**为什么能逐位比**：两份预处理器都是纯帧驱动、不读挂钟（实测 `time.`
零命中），所以输出只取决于输入帧序列。为此本测试**逐帧驱动**：写一帧、
等消费到、再取一次 `process_frame()`，两个后端走完全相同的顺序。

**不比的**：`latest_data_ts_us`（宿主时刻，本来就不同）、帧率（只断言
>0）。真机 golden 对比（T2）另有一套，那套不能逐位。

⚠️ **pty 陷阱（实测踩过，别删那段等待）**：`connect_device()` 返回**不等于**
传输层已经打开串口 —— 在此之前写进 master 的帧会被**静默丢掉**
（`os.write` 照样返回成功，症状是「ts 一直没到，实际 0」）。所以每一步都必须
先等 `stream.connected`，再写。

需要真 pty，不需要硬件。退出码 0 = 全部通过。
"""
import os
import struct
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from PyQt5.QtWidgets import QApplication            # noqa: E402

import config.settings as settings                  # noqa: E402
import core.glove_sdk_boot as boot                  # noqa: E402
from core.usb_glove_engine import UsbGloveEngine    # noqa: E402

FAILS = []
TRANSPORT_THREAD = "stouch-usb-sensor"      # 两个后端同名（实测）

#: 触觉帧数：够跑完自动基线校准（30 帧）再多几帧出真输出
TACTILE_FRAMES = 34


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def wait_for(pred, timeout=3.0, interval=0.005):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def transport_threads():
    return [t for t in threading.enumerate()
            if t.name == TRANSPORT_THREAD and t.is_alive()]


# ── 组帧（格式与固件同，常量取自 fork；下面有前提断言核对 SDK 侧同值）──

def _build_frame(message_type, payload, sequence=1, timestamp_us=1234,
                 flags=0xFFFF):
    from core.glove_usb.usb_protocol import USB_MAGIC, crc16_ccitt_false
    body = bytearray(USB_MAGIC)
    body += bytes((1, 2, 2))                                # [2:5]
    body.append(message_type)                               # [5]
    body += int(sequence).to_bytes(4, "little")             # [6:10]
    body += int(timestamp_us).to_bytes(4, "little")         # [10:14]
    body += len(payload).to_bytes(2, "little")              # [14:16]
    body += int(flags).to_bytes(2, "little")                # [16:18]
    body += payload
    return bytes(body) + crc16_ccitt_false(bytes(body)).to_bytes(2, "little")


def _imu_payload(values):
    """16 通道 × (x,y,z,w) 的 Q14 定点。values 是 64 个整数。"""
    return struct.pack("<64h", *values)


def _adc_payload(values, scan_us):
    """ADC 矩阵：帧自带 sequence 与 scan_time_us（头部那两个被忽略）。"""
    return (bytes([16, 16, 12, 0])
            + (7).to_bytes(4, "little")
            + int(scan_us).to_bytes(4, "little")
            + struct.pack("<256H", *values))


#: Q14 单位四元数 w=1 → (x,y,z,w) = (0,0,0,16384)
UNIT = [0, 0, 0, 16384]
#: 全零四元数：范数 0 < 0.25 ⇒ **不可信**（覆盖「全无效」帧）
ZERO = [0, 0, 0, 0]


def imu_plan():
    """(flags, payload, device_ts_us) 序列，覆盖全有效 / 全无效 / 部分有效。

    第 5 帧是**哨兵**：只有它 w=8192（0.5），用来确认最后一帧确实到了 ——
    否则快照可能停在第 3 帧而两边碰巧一致，比的是空数据。
    """
    ramp = [(i * 100) % 16384 for i in range(64)]
    sentinel = list(ramp)
    sentinel[63] = 8192
    return [
        (0xFFFF, _imu_payload(list(UNIT) * 16), 1000),      # 全有效
        (0xFFFF, _imu_payload(list(ZERO) * 16), 2000),      # 全零 → 全无效
        (0x00FF, _imu_payload(ramp), 3000),                 # 低 8 通道有效
        (0xFF00, _imu_payload(ramp), 4000),                 # 高 8 通道有效
        (0xFFFF, _imu_payload(sentinel), 5000),             # 哨兵
    ]


def tactile_plan():
    """每帧一个确定图案：值随帧号走，另有一个移动的强信号点。"""
    out = []
    for i in range(TACTILE_FRAMES):
        values = [(i * 7 + c) % 400 for c in range(256)]
        values[(i % 16) * 16 + (i % 16)] = 3000     # 移动的强信号
        values[5] = 1000 + i                        # 固定点，随帧号线性涨
        out.append(_adc_payload(values, 900 + i))
    return out


# ── 单个后端跑一遍 ────────────────────────────────────

class Recorder:
    """收集一个后端的全部可比输出。"""

    def __init__(self):
        self.tactile = None          # 最后一帧的 16×16
        self.imu = None              # latest_imu() 三元组
        self.present = -1
        self.proc = []               # 每次 process_frame() 的结果
        self.leaked = None           # disconnect 后残留的传输线程数


def run_backend(name, imu_frames, tac_frames):
    """在 pty 上跑一遍 `name` 后端，返回 Recorder（失败时返回 None）。"""
    settings.GLOVE_USB_BACKEND = name
    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    port = os.ttyname(slave_fd)
    engine = UsbGloveEngine(port)
    rec = Recorder()
    try:
        engine.connect_device(port)
        if not wait_for(lambda: engine._stream is not None, timeout=3.0):
            print(f"  ⚠️  [{name}] 流未建立")
            return None
        # 必须等传输层真的把串口打开 —— 在那之前写进 master 的帧会被静默
        # 丢掉（见模块 docstring 的 pty 陷阱）。再 settle 一下，让读线程
        # 走到阻塞读里，第一帧不会和「还没起读」错开。
        if not wait_for(lambda: engine._stream.connected, timeout=5.0):
            print(f"  ⚠️  [{name}] 串口未连接（status="
                  f"{getattr(engine._stream, 'status', None)}）")
            return None
        time.sleep(0.2)

        # IMU：逐帧写、等设备时间戳到位 —— 保证两个后端喂入顺序相同
        for flags, payload, ts in imu_frames:
            if not _write_retry(master_fd,
                                _build_frame(0x02, payload,
                                             timestamp_us=ts, flags=flags)):
                print(f"  ⚠️  [{name}] IMU 帧写不进 pty")
                return None
            if not wait_for(lambda ts=ts: engine.latest_imu_ts_us == ts,
                            timeout=3.0):
                print(f"  ⚠️  [{name}] IMU ts={ts} 未到"
                      f"（实际 {engine.latest_imu_ts_us}）")
                return None

        # 触觉：同样逐帧，且**每帧取一次 process_frame()**，顺序即一致
        for payload in tac_frames:
            base = engine._tactile_generation
            if not _write_retry(master_fd,
                                _build_frame(0x03, payload)):
                print(f"  ⚠️  [{name}] 触觉帧写不进 pty")
                return None
            if not wait_for(lambda base=base:
                            engine._tactile_generation > base, timeout=3.0):
                print(f"  ⚠️  [{name}] 触觉帧未被消费（代际卡在 {base}）")
                return None
            rec.proc.append(engine.process_frame())

        rec.tactile = np.array(engine.data_array, copy=True)
        rec.present = int(engine.imu_present_count)
        got = engine.latest_imu()
        rec.imu = None if got is None else (
            np.array(got[0], copy=True), np.array(got[1], copy=True), int(got[2]))
    finally:
        engine.disconnect()
        time.sleep(0.3)                       # 给线程收尾留点时间
        rec.leaked = len(transport_threads())
        for fd in (master_fd, slave_fd):
            os.close(fd)
    return rec


def _write_retry(fd, data, give_up=2.0):
    """pty 缓冲区满会 EAGAIN —— 退回重试，别把帧丢了。"""
    end = time.monotonic() + give_up
    while True:
        try:
            os.write(fd, data)
            return True
        except OSError:
            if time.monotonic() > end:
                return False
            time.sleep(0.002)


# ── 比对 ──────────────────────────────────────────────

def array_same(a, b):
    """逐位相同（含 dtype 与形状）—— 不是 allclose。"""
    return (isinstance(a, np.ndarray) and isinstance(b, np.ndarray)
            and a.dtype == b.dtype and a.shape == b.shape
            and a.tobytes() == b.tobytes())


def compare(ref, got):
    check(array_same(ref.tactile, got.tactile),
          f"触觉 data_array 逐位相同（{ref.tactile.shape} "
          f"{ref.tactile.dtype}，{ref.tactile.tobytes()[:8].hex()}…）")

    check(ref.imu is not None and got.imu is not None,
          "两边都取到了 latest_imu()")
    if ref.imu and got.imu:
        check(array_same(ref.imu[0], got.imu[0]),
              "IMU 四元数（64 float32）逐位相同")
        check(array_same(ref.imu[1], got.imu[1]),
              f"IMU 有效掩码逐位相同（有效通道数 "
              f"{int(ref.imu[1].sum())}）")
        check(ref.imu[2] == got.imu[2],
              f"IMU 设备时间戳相同（{ref.imu[2]}）")

    check(ref.present == got.present,
          f"imu_present_count 相同（{ref.present}）")

    # process_frame() 序列：校准期是 (None, 0.0)，之后是 (16,16)+peak
    check(len(ref.proc) == len(got.proc) == TACTILE_FRAMES,
          f"process_frame() 取样数都是 {TACTILE_FRAMES}（"
          f"{len(ref.proc)} / {len(got.proc)}）")
    same, detail = True, ""
    for i, ((rp, rk), (gp, gk)) in enumerate(zip(ref.proc, got.proc)):
        if (rp is None) != (gp is None) or float(rk) != float(gk):
            same, detail = False, f"第 {i} 帧 (None/peak) 不同: " \
                                  f"{(rp is None, rk)} vs {(gp is None, gk)}"
            break
        if rp is not None and not array_same(rp, gp):
            same, detail = False, f"第 {i} 帧 processed 矩阵不同"
            break
    check(same, f"process_frame() 全序列逐位相同"
                f"（{TACTILE_FRAMES} 帧）{detail}")

    # 校准必须真的跑完过，否则上面比的是一片 (None, 0.0)
    n_none = sum(1 for p, _ in ref.proc if p is None)
    check(0 < n_none < TACTILE_FRAMES,
          f"校准期确实发生过且已结束（前 {n_none} 帧为 None，"
          f"共 {TACTILE_FRAMES} 帧）⇒ 上面不是恒真")


def main():
    app = QApplication.instance() or QApplication([])      # noqa: F841

    # ── 前提：两个后端的线格式常量必须一致（否则比的是两种协议）──
    from core.glove_usb.usb_protocol import (
        USB_MAGIC as F_MAGIC, USB_TYPE_ADC_MATRIX as F_ADC,
        USB_TYPE_IMU_Q14 as F_IMU, crc16_ccitt_false as f_crc)
    boot.ensure_sdk()
    from common.usb_cdc import (
        USB_MAGIC as S_MAGIC, USB_TYPE_ADC_MATRIX as S_ADC,
        USB_TYPE_IMU_Q14 as S_IMU, crc16_ccitt_false as s_crc)
    check((F_MAGIC, F_IMU, F_ADC) == (S_MAGIC, S_IMU, S_ADC),
          f"前提成立：两个后端的魔数/类型码一致（{F_MAGIC!r} "
          f"{F_IMU}/{F_ADC}）")
    check(f_crc(bytes(range(64))) == s_crc(bytes(range(64))),
          "前提成立：CRC16-CCITT-FALSE 两个后端实现同值")

    # ── 前提：两份触觉预处理器除行尾外相同（§2.4 的"契约逐项相同"）──
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    # SDK 那份**问 boot 要路径**，不写死 "glove_sdk"：写死的话 SDK 一挪地方
    # 这条就变成"读文件失败"而不是"两份不同"，看起来像环境问题而不是搬家漏改。
    sdk_dir = boot.find_sdk_dir()
    check(bool(sdk_dir), f"前提：找得到 SDK 目录（{boot.sdk_relpath()}）")
    try:
        with open(os.path.join(here, "core", "glove_usb",
                              "tactile_processing.py"), "rb") as fh:
            fork_src = fh.read().replace(b"\r\n", b"\n")
        with open(os.path.join(sdk_dir, "gui",
                              "tactile_processing.py"), "rb") as fh:
            sdk_src = fh.read().replace(b"\r\n", b"\n")
        check(fork_src == sdk_src,
              "前提成立：两份 TactilePreprocessor 除行尾外逐字节相同"
              "（所以输出本该逐位一致）")
    except OSError as exc:
        check(False, f"前提：读预处理器源码失败 {exc}")

    # ── 主体：同一份脚本，两个后端各跑一遍 ──
    imu_frames, tac_frames = imu_plan(), tactile_plan()
    snaps = {}
    for name in ("fork", "sdk"):
        print(f"── 后端 {name} ──")
        rec = run_backend(name, imu_frames, tac_frames)
        if rec is None:
            check(False, f"后端 {name} 跑完（见上面 ⚠️）")
            continue
        snaps[name] = rec
        check(rec.leaked == 0,
              f"[{name}] disconnect() 后无残留传输线程"
              f"（实测 {rec.leaked} 个）⇒ R1 兜底对两档都成立")

    if len(snaps) == 2:
        print("── 逐位比对 ──")
        compare(snaps["fork"], snaps["sdk"])
    else:
        check(False, "两个后端都跑成功才能比对"
                     f"（成功 {sorted(snaps)}）")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: 手套两后端逐位等价")
    return 0


if __name__ == "__main__":
    sys.exit(main())
