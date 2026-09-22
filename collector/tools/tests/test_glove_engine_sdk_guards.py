#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UsbGloveEngine 对厂商 SDK 2.1.0 三处传输缺陷的**调用纪律兜底**:

    venv/bin/python tools/tests/test_glove_engine_sdk_guards.py

背景（迁移计划 §2.2）：厂商 2.1.0 的 `glove_io/streams.py` 是重写版，
`core/glove_usb`（fork 档）修过的三处它**一处都没有**：

  R1 `stop()` 不终结 —— `start()` 无条件 `stop_event.clear()`，而
     `read()`/`frames()` 每次调用都先 `start()` ⇒ stop 之后任何一次读都
     会复活一个**没人持有引用的孤儿读线程**，永久占着 tty，症状是
     「必须重启程序」。**这条必须由我方兜住**（`_stream_lock` +
     `_stream_closed`，整段读持锁）。
  R2 `exclusive=True` 缺失 —— 第二句柄静默双开（回归记账，不改）。
  R3 读重试无上限无退避 —— 3~5 次/秒重连风暴（回归记账，不改）。

本测试跑真 SDK + pty，不碰硬件。**关键在"反证"那一节**：同一台流绕过
`_take_frame` 直接读，必须真的复活读线程 —— 否则「读完没复活」这条断言
可能只是因为读根本没走到 `start()`（恒真陷阱）。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from PyQt5.QtWidgets import QApplication            # noqa: E402

import core.glove_sdk_boot as boot                  # noqa: E402
import core.usb_glove_engine as engine_mod          # noqa: E402
from core.usb_glove_engine import UsbGloveEngine    # noqa: E402

FAILS = []
READER_THREAD = "stouch-usb-sensor"     # SDK 读线程名（streams.py:228）


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def reader_threads():
    return [t for t in threading.enumerate()
            if t.name == READER_THREAD and t.is_alive()]


def wait_for(pred, timeout=3.0, interval=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def premise_r1_exists():
    """前提断言：R1 在厂商这版里**确实存在**（否则兜底就是多余的）。"""
    import inspect
    from glove_io.streams import _UsbSensorTransport
    cls = boot.raw_imu_stream_cls()
    clears = "stop_event.clear()" in inspect.getsource(
        _UsbSensorTransport.start)
    starts_in_read = "self.start()" in inspect.getsource(cls.read)
    ok = clears and starts_in_read
    check(ok, f"R1 前提成立: 传输层 start() 无条件 clear 停止标志={clears}，"
              f"read() 每次先 start()={starts_in_read}")
    if not ok:
        print("  ⚠️  厂商似乎已修 R1 —— 本测试与该兜底都该重新评估"
              "（docs/postmortem_glove_sdk_migration.md）")
    return ok


def harness():
    """真 SDK + pty 的引擎（pty 不灌数据 → 读线程会一直空转重试）。"""
    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    return master_fd, slave_fd, os.ttyname(slave_fd)


def scenario_stop_is_terminal():
    """R1 兜底：disconnect() 之后，任何读都不能复活读线程。"""
    master_fd, slave_fd, port = harness()
    engine = UsbGloveEngine(port)
    try:
        engine.connect_device(port)
        appeared = wait_for(lambda: len(reader_threads()) > 0, timeout=3.0)
        check(appeared, "连上后 SDK 读线程已起")
        stream = engine._stream
        engine.disconnect()
        check(not reader_threads(), "disconnect() 后读线程已收干净")

        # 关键：再读一次 —— 这正是消费线程在 stop 之后可能做的事。
        # `_read_locked` 必须在**锁里**看见 _stream_closed 并直接放弃，
        # 而不是去调 read()（那会走 SDK 的 start()）。
        closed_exc, timeout_exc = boot.stream_errors()
        got = engine._read_locked(stream.read, closed_exc, timeout_exc)
        time.sleep(0.4)                     # 复活的话线程会立刻出现在枚举里
        leaked = reader_threads()
        check(got is engine_mod._CLOSED,
              f"_read_locked 在已关闭时返回哨兵（实际 {got!r}）")
        check(not leaked,
              f"关闭后读一次**没有**复活读线程（残留 {len(leaked)} 个）")

        # 反证：同一台流绕过 _read_locked 直接读，必须真的复活 ——
        # 证明上面那条不是恒真（读确实会走到 SDK 的 start()）
        revived = False
        try:
            stream.read(timeout=0.2)
        except Exception:
            pass
        revived = len(reader_threads()) > 0
        check(revived, "反证：绕过兜底直接读**确实**复活了读线程"
                       "（所以上面那条断言有意义）")
        stream.stop(timeout_s=1.0)
        time.sleep(0.3)
    finally:
        engine.disconnect()
        for fd in (master_fd, slave_fd):
            os.close(fd)
    check(not reader_threads(), "收尾：无残留读线程")


def scenario_connect_disconnect_cycles():
    """反复连/断 8 次：读线程数不得单调增长（真实使用里的泄漏形态）。"""
    master_fd, slave_fd, port = harness()
    engine = UsbGloveEngine(port)
    worst = 0
    try:
        for _ in range(8):
            engine.connect_device(port)
            wait_for(lambda: len(reader_threads()) > 0, timeout=2.0)
            engine.disconnect()
            worst = max(worst, len(reader_threads()))
        check(worst == 0, f"8 轮连/断后峰值为 {worst} 个残留读线程（要 0）")
    finally:
        engine.disconnect()
        for fd in (master_fd, slave_fd):
            os.close(fd)


def scenario_silent_stream_does_not_starve():
    """只灌触觉、IMU 全程静默：触觉必须**全速**被消费（防单锁饿死）。

    第一版是 IMU / 触觉各一个消费线程、共用一把 `threading.Lock`。CPython
    的 Lock **不保证公平**：IMU 静默时它那个线程在 `release()` 后立刻重新
    `acquire()`，把刚被唤醒的触觉线程挤掉（实测连续 1.1s 一次锁都没抢到，
    11 次 IMU 读超时之间夹 0 次触觉读）。症状是「矩阵偶发半天不跳」，而且
    **看相位** —— 同一份代码有时全绿，所以这条必须是个能稳定复现的断言。

    判据用「20 帧必须在 2s 内全部到齐」：旧设计每抢到一次锁只取一帧，
    一个来回 ≈200ms（100ms 持锁读超时 + 100ms 等锁）⇒ 20 帧要 ~4s，必然
    失败；单线程 + `poll()` 取空队列则是毫秒级，必然通过。
    """
    import struct
    from common.usb_cdc import USB_TYPE_ADC_MATRIX

    master_fd, slave_fd, port = harness()
    engine = UsbGloveEngine(port)
    total = 20
    try:
        engine.connect_device(port)
        wait_for(lambda: len(reader_threads()) > 0, timeout=3.0)
        time.sleep(0.5)             # 让读线程走到「两条都空」的阻塞里
        adc_values = [0] * 256
        adc_values[5] = 1000
        adc_payload = (bytes([16, 16, 12, 0])
                       + (7).to_bytes(4, "little")
                       + (999).to_bytes(4, "little")
                       + struct.pack("<256H", *adc_values))

        base = engine._tactile_generation
        sent, give_up = 0, time.monotonic() + 10.0
        while sent < total and time.monotonic() < give_up:
            try:                    # pty 缓冲满会 EAGAIN，退回重试
                os.write(master_fd, _build_frame(USB_TYPE_ADC_MATRIX,
                                                 adc_payload,
                                                 sequence=100 + sent))
                sent += 1
            except OSError:
                time.sleep(0.005)
        check(sent == total, f"静默流：{total} 帧全部写进 pty（实际 {sent}）")

        arrived = wait_for(
            lambda: engine._tactile_generation >= base + total, timeout=2.0)
        check(arrived,
              f"IMU 静默时触觉 {total} 帧在 2s 内全部到齐"
              f"（代际 +{engine._tactile_generation - base}）")
    finally:
        engine.disconnect()
        for fd in (master_fd, slave_fd):
            os.close(fd)


def _build_frame(message_type, payload, sequence=1, timestamp_us=1234,
                 flags=0xFFFF, version=(1, 2, 2)):
    """按 SDK 的 18 字节头 + CRC16 拼一帧（与固件/`common.usb_cdc` 同格式）。

    常量从 SDK 自己那份取，不抄字面量 —— 抄了就会在厂商改协议时静默失配。
    """
    import struct
    from common.usb_cdc import (USB_MAGIC, crc16_ccitt_false)
    body = bytearray(USB_MAGIC)
    body += bytes(version)
    body.append(message_type)
    body += int(sequence).to_bytes(4, "little")
    body += int(timestamp_us).to_bytes(4, "little")
    body += len(payload).to_bytes(2, "little")
    body += int(flags).to_bytes(2, "little")
    body += payload
    return bytes(body) + struct.pack("<H", crc16_ccitt_false(bytes(body)))


def scenario_end_to_end_pty():
    """真 SDK + pty + 真帧：IMU 与触觉都得从串口走到引擎属性上。

    这是 S3 唯一一条**穿过 SDK 的**端到端断言 —— 上面几条都是"不该发生的
    没发生"，这条才是"该发生的真发生了"。没有它，把 `read()` 换成永远返
    回 None 也能让全部兜底测试变绿。
    """
    import struct
    from common.usb_cdc import USB_TYPE_IMU_Q14, USB_TYPE_ADC_MATRIX

    master_fd, slave_fd, port = harness()
    engine = UsbGloveEngine(port)
    try:
        engine.connect_device(port)
        opened = wait_for(lambda: len(reader_threads()) > 0, timeout=3.0)
        check(opened, "端到端：SDK 读线程已起")

        imu_payload = struct.pack("<64h", *([16384, 0, 0, 0] * 16))
        adc_values = [0] * 256
        adc_values[5] = 1000
        adc_payload = (bytes([16, 16, 12, 0])
                       + (7).to_bytes(4, "little")
                       + (999).to_bytes(4, "little")
                       + struct.pack("<256H", *adc_values))
        deadline = time.monotonic() + 6.0
        imu_ok = tac_ok = False
        while time.monotonic() < deadline and not (imu_ok and tac_ok):
            try:
                os.write(master_fd, _build_frame(USB_TYPE_IMU_Q14, imu_payload))
                os.write(master_fd, _build_frame(USB_TYPE_ADC_MATRIX,
                                                 adc_payload, sequence=7))
            except OSError:
                pass                        # pty 缓冲满，下一轮再写
            got = engine.latest_imu()
            if got is not None:
                quats, valid, _ = got
                imu_ok = (quats.shape == (64,) and valid.shape == (16,)
                          and float(valid.sum()) == 16.0
                          and abs(float(quats[3]) - 1.0) < 1e-6)
            with engine.data_lock:
                tac_ok = float(engine.data_array[0][5]) == 1000.0
            if not (imu_ok and tac_ok):
                time.sleep(0.05)

        check(imu_ok, "端到端：IMU 四元数经 SDK 走到 latest_imu()")
        check(tac_ok, f"端到端：触觉矩阵经 SDK 走到 data_array"
                      f"（[0][5]={engine.data_array[0][5]}，期望 1000）")
        check(engine._tactile_generation > 0,
              f"端到端：触觉代际已推进（{engine._tactile_generation}）")
        # 校准期：真预处理器要攒够 calibration_frames(=30) 个扫描才出结果，
        # 所以第一帧必然是 (None, 0.0)；灌够之后必须出 (16,16)。
        # （第一版断言"第一帧就该出矩阵"，那是把假预处理器的行为当成契约了。）
        proc, peak = engine.process_frame()
        check(proc is None, "端到端：校准未满时 process_frame 返回 None（符合契约）")

        progressed = 0
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and progressed < 45:
            gen = engine._tactile_generation
            try:
                os.write(master_fd, _build_frame(USB_TYPE_ADC_MATRIX,
                                                 adc_payload, sequence=10 + gen))
            except OSError:
                pass
            if wait_for(lambda: engine._tactile_generation > gen, timeout=1.0):
                proc, peak = engine.process_frame()
                progressed += 1
                if proc is not None:
                    break
        check(proc is not None and proc.shape == (16, 16),
              f"端到端：灌 {progressed} 个扫描后出矩阵"
              f"（{None if proc is None else proc.shape}，peak={peak:.0f}）")
    finally:
        engine.disconnect()
        for fd in (master_fd, slave_fd):
            os.close(fd)


def scenario_error_rate_limit():
    """error_occurred 限频：同文本 5s 一次，计数补在尾巴；不同文本各报。"""
    engine = UsbGloveEngine("")
    seen = []
    engine.error_occurred.connect(seen.append)
    for _ in range(50):
        engine._emit_error("serial read failed: multiple access on port?")
    check(len(seen) == 1, f"同类文本 50 次只上报 1 次（实际 {len(seen)}）")
    engine._emit_error("另一条完全不同的错误")
    check(len(seen) == 2, f"不同文本各报一次（实际 {len(seen)}）")

    # 抑制计数必须在下次真正上报时补出来 —— 否则信息是真丢了。
    # 注意口径：窗口内**第一次**照常上报、之后才计抑制，所以「报 1 次 +
    # 再喂 3 次」被抑制的是 3 次（第一版把上报那次也算进去，期望值就错了）。
    engine._err_last.pop("x", None)
    engine._err_suppressed.pop("x", None)
    seen.clear()
    engine._emit_error("x")                     # 窗口内第一次：上报
    for _ in range(3):
        engine._emit_error("x")                 # 这 3 次计抑制
    engine._err_last["x"] = time.monotonic() - 99   # 窗口过期
    engine._emit_error("x")                     # 上报并把抑制数补在尾巴
    check(len(seen) == 2 and "已抑制 3 次" in seen[-1],
          f"限频窗口过期后补报被抑制次数: {seen}")

    # disconnected 是状态机边沿，**不**受限频影响（漏一次面板就停在"已连接"）
    disc = []
    engine.disconnected.connect(lambda: disc.append(1))
    engine._was_connected = True
    engine._stream = None
    for _ in range(5):
        engine._on_status("disconnected")
        engine._was_connected = True        # 模拟反复掉线
    check(len(disc) == 5, f"disconnected 每次边沿都发（实际 {len(disc)} 次）")


def scenario_tactile_dedup():
    """触觉去重：同一个扫描被反复轮询时，只喂滤波器一次。"""
    engine = UsbGloveEngine("")

    class CountingPre:
        base_gate = 0.0
        dynamic_noise_ratio = 0.0
        spatial_filter = False
        is_calibrating = False
        calibration_progress = (0, 30)
        drift_baseline_val = 0.0
        calls = 0

        def process(self, raw):
            self.calls += 1          # 实例属性：与断言读的 pre.calls 同一个
            return raw, float(raw.max())

    pre = CountingPre()
    engine._preprocessor = pre
    engine.data_array = engine.data_array + 5.0

    # 第一帧之后代际不再变（设备没来新扫描）：轮询 20 次也只该喂 1 次
    # —— 注意 `_tactile_fed_gen` 初值 -1，所以**第一次**调用一定喂。
    pre.calls = 0
    engine.process_frame()
    check(pre.calls == 1, f"首次调用喂一次（实际 {pre.calls} 次）")
    pre.calls = 0
    for _ in range(20):
        engine.process_frame()
    check(pre.calls == 0,
          f"没有新扫描时一次都不喂滤波器（实际喂了 {pre.calls} 次）")

    # 来一个新扫描 → 喂一次；再来一个 → 再喂一次
    engine._tactile_generation += 1
    engine.process_frame()
    engine.process_frame()
    check(pre.calls == 1, f"一个新扫描只喂一次（实际 {pre.calls} 次）")
    engine._tactile_generation += 1
    engine.process_frame()
    check(pre.calls == 2, f"下一个扫描再喂一次（实际 {pre.calls} 次）")


def scenario_backend_missing_is_loud():
    """SDK 不可用时：构造不抛、连接时报出原因（不静默）。

    替换的是 **engine_mod 里的名字**，不是 `boot.tactile_preprocessor_cls`：
    本引擎在模块顶部 `from core.glove_sdk_boot import (...)`，名字在 import
    时就绑好了，改 boot 的属性打不中（实测：patch 了 boot，构造照样拿到了
    真预处理器，断言于是变成假绿）。
    """
    real = engine_mod.tactile_preprocessor_cls

    def boom():
        raise ImportError("未找到厂商 SDK 目录")

    engine_mod.tactile_preprocessor_cls = boom
    try:
        engine = UsbGloveEngine("/dev/null")
    finally:
        engine_mod.tactile_preprocessor_cls = real
    seen = []
    engine.error_occurred.connect(seen.append)
    engine.connect_device("/dev/null")
    check(engine._preprocessor is None and "SDK 不可用" in engine._backend_error,
          f"SDK 缺失时构造不抛（{engine._backend_error[:40]}）")
    check(len(seen) == 1 and "SDK 不可用" in seen[0],
          f"连接时报出原因（{seen[0][:40] if seen else '没报'}）")
    check(engine.process_frame() == (None, 0.0),
          "SDK 缺失时 process_frame 返回 (None, 0.0)")


def main():
    global _APP
    _APP = QApplication.instance() or QApplication([])   # QObject 信号需要

    err = boot.ensure_sdk()
    check(err == "", f"SDK 可用（后端 {boot.backend()}）: {err or 'OK'}")
    check(boot.backend() == "sdk",
          f"后端是 sdk（本测试测的就是 SDK 档）: {boot.backend()}")
    if err:
        print("\nFAIL: SDK 不可用，后续断言无意义")
        return 1

    premise_r1_exists()
    scenario_stop_is_terminal()
    scenario_connect_disconnect_cycles()
    scenario_silent_stream_does_not_starve()
    scenario_end_to_end_pty()
    scenario_error_rate_limit()
    scenario_tactile_dedup()
    scenario_backend_missing_is_loud()

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: UsbGloveEngine 的 SDK 兜底全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
