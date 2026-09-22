"""STM32 USB 手套传输层：stop() 终结性与读失败处置单测（离线，无硬件）。

背景（2026-09-21 双手套高频掉线）：
pyserial 对「select 说可读、os.read 返回 0 字节」抛的异常与真掉线**完全同文**
（...device disconnected or multiple access on port?），而它恰恰在同一个 tty 被
第二个句柄抢字节时高频出现、句柄本身仍然有效。旧实现每次读失败都 close+reopen，
于是刷出 3~4 次/秒的「已连接/已断开」；而 RawImuStream.read()/frames() 里的惰性
start() 又会在 stop() 之后把传输复活成没人持有引用的孤儿线程，永久占着这个 tty，
日志风暴停不下来（单段 6756 次、持续 15 分钟不自愈）。

断言五条：
  1. stop() 终结：之后再 start() 不起线程、不清 stop_event（不复活孤儿）
  2. exclusive=True 真的传到 serial.Serial（第二个句柄该 EBUSY 而不是静默双开）
  3. 瞬时读失败原地重试，不到上限不关不重开；计数按「连续」清零
  4. 连续失败到上限才重开，且重开退避逐次翻倍
  5. 开不了（EBUSY）报 waiting，不进 disconnected 风暴

用法:
    venv/bin/python tools/tests/test_glove_usb_transport.py
"""
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serial as _REAL_SERIAL      # 必须在任何 sys.modules 替身之前绑定

import core.glove_usb.streams as streams_mod
from core.glove_usb.streams import RawImuStream
from core.glove_usb.errors import StreamClosedError
from core.glove_usb.usb_protocol import (
    crc16_ccitt_false,
    USB_MAGIC,
    USB_TYPE_ADC_MATRIX,
    USB_TYPE_IMU_Q14,
)

# pyserial 原文，双句柄抢字节时的真实报错
_MULTI_ACCESS = ("device reports readiness to read but returned no data "
                 "(device disconnected or multiple access on port?)")


class FakeSerialException(OSError):
    pass


class _FakeHandle:
    def __init__(self, owner, index):
        self.owner = owner
        self.index = index

    def read(self, size):
        owner = self.owner
        with owner.lock:
            owner.reads.append((time.monotonic(), self.index))
            seq = sum(1 for _, i in owner.reads if i == self.index)
        behavior = owner.plan(self.index, seq)
        if behavior == "raise":
            raise FakeSerialException(_MULTI_ACCESS)
        if behavior == "empty":
            return b""
        return b"\x00"

    def close(self):
        with self.owner.lock:
            self.owner.closes.append((time.monotonic(), self.index))


class FakeSerialModule:
    """替身 serial 模块：行为由 plan(open_index, seq_in_this_open) 决定。"""

    SerialException = FakeSerialException

    def __init__(self, plan):
        self.plan = plan
        self.lock = threading.Lock()
        self.opens = []    # (t, port, kwargs)
        self.reads = []    # (t, open_index)
        self.closes = []   # (t, open_index)

    def Serial(self, port, **kwargs):
        with self.lock:
            index = len(self.opens)
            self.opens.append((time.monotonic(), port, kwargs))
        return _FakeHandle(self, index)

    # ── 便捷读取 ──────────────────────────────────────
    def open_count(self):
        with self.lock:
            return len(self.opens)

    def reads_in_open(self, index):
        with self.lock:
            return sum(1 for _, i in self.reads if i == index)

    def open_times(self):
        with self.lock:
            return [t for t, _, _ in self.opens]

    def all_exclusive(self):
        with self.lock:
            return [kw.get("exclusive") for _, _, kw in self.opens]


def _install_fake_serial(plan):
    fake = FakeSerialModule(plan)
    sys.modules["serial"] = fake
    return fake


def _wait_for(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _make_stream(statuses):
    """requested_port 非空 ⇒ 不去碰真的 list_ports。"""
    stream = RawImuStream(
        serial_port="/dev/fake-acm",
        status_callback=statuses.append,
        buffer_size=8,
        tactile_buffer_size=8,
    )
    return stream


def scenario_stop_is_terminal():
    """stop() 之后惰性 start() 不得复活传输（孤儿线程回归）。"""
    fake = _install_fake_serial(lambda index, seq: "empty")
    statuses = []
    stream = _make_stream(statuses)
    stream.start()
    assert _wait_for(lambda: fake.open_count() == 1), "首次 open 未发生"
    assert _wait_for(lambda: stream.connected), "未进入 connected"

    stream.stop()
    thread_after_stop = stream._transport.thread
    statuses_at_stop = list(statuses)

    # 消费者线程仍持有这个 RawImuStream —— 旧实现这里会复活出一个孤儿
    for _ in range(3):
        try:
            stream.read(timeout=0.05)
        except StreamClosedError:
            pass
    list(stream.frames(timeout=0.05))
    stream.start()

    time.sleep(0.3)
    ok = (
        stream._transport.stopped
        and stream._transport.stop_event.is_set()      # 事件仍置位
        and stream._transport.thread is thread_after_stop
        and not thread_after_stop.is_alive()
        and fake.open_count() == 1
        and statuses == statuses_at_stop
    )
    print(("  PASS" if ok else "  FAIL"),
          "1: stop() 终结，惰性 start()/read()/frames() 不复活传输"
          f"（opens={fake.open_count()} statuses={statuses}）")
    return ok


def scenario_exclusive_flag():
    """exclusive=True 必须真的传给 serial.Serial。"""
    fake = _install_fake_serial(lambda index, seq: "empty")
    stream = _make_stream([])
    stream.start()
    _wait_for(lambda: fake.open_count() >= 1)
    stream.stop()
    flags = fake.all_exclusive()
    ok = flags and all(flag is True for flag in flags)
    print(("  PASS" if ok else "  FAIL"),
          f"2: exclusive=True 传到 serial.Serial（实测 {flags}）")
    return ok


def scenario_transient_retry():
    """瞬时读失败原地重试；计数按连续清零（3 次失败 + 1 次成功 + 5 次才断）。"""
    # 第 1 次 open：失败 3 次 → 成功 1 次 → 再连续失败 5 次（到上限）→ 关
    def plan(index, seq):
        if index == 0 and (seq <= 3 or seq >= 5):
            return "raise"
        if index == 0:
            return "data"        # 第 4 次成功 → 失败计数必须清零
        return "empty"

    fake = _install_fake_serial(plan)
    statuses = []
    stream = _make_stream(statuses)
    stream.start()

    got = _wait_for(lambda: len(fake.closes) >= 1, timeout=3.0)
    reads_before_close = fake.reads_in_open(0)
    reopened = _wait_for(lambda: fake.open_count() >= 2, timeout=2.0)
    stream.stop()

    ok = (
        got and reopened
        and reads_before_close == 9          # 3 失败 + 1 成功 + 5 失败
        and "disconnected" in statuses
        and "connected" in statuses          # 重开后回到 connected
    )
    print(("  PASS" if ok else "  FAIL"),
          "3: 读失败原地重试、计数按连续清零"
          f"（首个句柄读 {reads_before_close} 次才关，期望 9）")
    return ok


def scenario_reopen_backoff():
    """连续失败到上限才重开，退避逐次翻倍（每个周期读次数恒 = 上限）。"""
    fake = _install_fake_serial(lambda index, seq: "raise")
    stream = _make_stream([])
    stream.start()

    _wait_for(lambda: fake.open_count() >= 4, timeout=6.0)
    stream.stop()

    times = fake.open_times()
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    per_cycle_reads = [fake.reads_in_open(i) for i in range(len(times))]
    grow = len(gaps) >= 2 and (gaps[1] - gaps[0]) >= 0.12   # 退避 0.2→0.4
    every_limit = all(n == streams_mod._READ_RETRY_LIMIT
                      for n in per_cycle_reads[:-1])
    ok = len(times) >= 3 and every_limit and grow
    print(("  PASS" if ok else "  FAIL"),
          "4: 到上限才重开 + 退避翻倍"
          f"（每周期读次数 {per_cycle_reads}，间隔 "
          f"{[round(g, 3) for g in gaps]}）")
    return ok


def scenario_open_busy():
    """开不了（第二个句柄占着 → EBUSY）报 waiting，不刷 disconnected。"""
    def plan(index, seq):
        raise AssertionError("不该读到数据")

    def fake_open(port, **kwargs):
        raise FakeSerialException(
            "[Errno 16] could not open port /dev/fake-acm: "
            "[Errno 16] Device or resource busy")

    fake = _install_fake_serial(plan)
    fake.Serial = fake_open            # 实例属性覆盖方法：永远开不了
    statuses = []
    stream = _make_stream(statuses)
    stream.start()
    _wait_for(lambda: len(statuses) >= 1, timeout=2.0)
    time.sleep(1.0)          # 开不了会 0.5s 一轮重试：期间不得再刷屏
    stream.stop()

    waiting = [s for s in statuses if s == "waiting"]
    ok = (len(waiting) == 1 and "disconnected" not in statuses
          and "connected" not in statuses)
    print(("  PASS" if ok else "  FAIL"),
          f"5: 开不了报 waiting 不刷 disconnected（statuses={statuses}）")
    return ok


# ── 真 pyserial + pty：不碰硬件也能验「第二个句柄」和端到端解帧 ──

def _real_serial():
    """替身只活在 sys.modules 里，模块顶部的引用始终是真 pyserial。"""
    sys.modules["serial"] = _REAL_SERIAL
    return _REAL_SERIAL


def _open_pty():
    master_fd, slave_fd = os.openpty()
    return master_fd, slave_fd, os.ttyname(slave_fd)


def _build_frame(message_type, payload, sequence=1, timestamp_us=1234,
                 flags=0xFFFF, version=(1, 2, 2)):
    """按 usb_protocol 的 18 字节头 + CRC16 拼一帧（与固件同格式）。"""
    body = bytearray(USB_MAGIC)
    body += bytes(version)                                  # [2:5]
    body.append(message_type)                               # [5]
    body += int(sequence).to_bytes(4, "little")             # [6:10]
    body += int(timestamp_us).to_bytes(4, "little")         # [10:14]
    body += len(payload).to_bytes(2, "little")              # [14:16]
    body += int(flags).to_bytes(2, "little")                # [16:18]
    body += payload
    return bytes(body) + crc16_ccitt_false(bytes(body)).to_bytes(2, "little")


def scenario_real_exclusive_lock():
    """真 pyserial + pty：exclusive=True 挡住第二个句柄，不加就静默双开。"""
    real = _real_serial()
    master_fd, slave_fd, port = _open_pty()
    first = real.Serial(port, baudrate=115200, timeout=0.2, exclusive=True)

    blocked = None
    try:
        real.Serial(port, baudrate=115200, timeout=0.2, exclusive=True)
    except real.SerialException as exc:
        blocked = str(exc)

    # 反面：不加 exclusive 时第二个句柄**静默成功** —— 这就是旧行为的病根
    silent = None
    try:
        silent = real.Serial(port, baudrate=115200, timeout=0.2)
        silent.close()
    except real.SerialException as exc:
        silent = f"意外被挡: {exc}"

    first.close()
    reopened = None
    try:                        # 关掉后锁释放，必须能再开
        reopened = real.Serial(port, baudrate=115200, timeout=0.2,
                               exclusive=True)
        reopened.close()
    except real.SerialException as exc:
        reopened = f"重开失败: {exc}"

    for fd in (master_fd, slave_fd):
        os.close(fd)

    refused = isinstance(blocked, str) and bool(blocked)
    silently_ok = silent is not None and not isinstance(silent, str)
    reopen_ok = reopened is not None and not isinstance(reopened, str)
    ok = refused and silently_ok and reopen_ok
    print(("  PASS" if ok else "  FAIL"),
          "6: 真 pyserial 下 exclusive=True 挡第二个句柄"
          f"（已锁时再加锁={'被拒' if refused else '放行了'}，"
          f"不加锁={'静默打开' if silently_ok else '被拒'}，"
          f"释放后重开={'OK' if reopen_ok else '失败'}）")
    return ok


def scenario_end_to_end_pty():
    """真 pyserial + pty + 真帧格式：IMU 与触觉都能从串口读出来。"""
    real = _real_serial()
    master_fd, slave_fd, port = _open_pty()

    imu_payload = struct.pack(
        "<64h", *([16384, 0, 0, 0] * 16))       # 每通道 w=1 → (x,y,z,w)=单位四元数
    adc_values = [0] * 256
    adc_values[5] = 1000
    adc_payload = (bytes([16, 16, 12, 0])
                   + (7).to_bytes(4, "little")
                   + (999).to_bytes(4, "little")
                   + struct.pack("<256H", *adc_values))

    statuses = []
    stream = RawImuStream(serial_port=port, status_callback=statuses.append,
                          buffer_size=64, tactile_buffer_size=64)
    stream.start()
    opened = _wait_for(lambda: stream.connected, timeout=3.0)

    imu_frame = tactile_frame = None
    err = ""
    try:
        os.write(master_fd, _build_frame(USB_TYPE_IMU_Q14, imu_payload))
        imu_frame = stream.read(timeout=2.0)
        os.write(master_fd, _build_frame(USB_TYPE_ADC_MATRIX, adc_payload,
                                         sequence=7))
        tactile_frame = stream.tactile_stream().read(timeout=2.0)
    except Exception as exc:                    # noqa: BLE001 —— 失败信息要打出来
        err = f"{type(exc).__name__}: {exc}"
    stream.stop()
    for fd in (master_fd, slave_fd):
        os.close(fd)

    quat_ok = (imu_frame is not None
               and bool(imu_frame.valid_mask.all())
               and abs(float(imu_frame.quaternions_xyzw[0][3]) - 1.0) < 1e-6)
    tactile_ok = (tactile_frame is not None
                  and tactile_frame.samples.shape == (16, 16)
                  and float(tactile_frame.samples[0][5]) == 1000.0)
    ok = opened and quat_ok and tactile_ok and not err
    print(("  PASS" if ok else "  FAIL"),
          "7: 真 pyserial 端到端（pty 灌帧）"
          f"（IMU={'OK' if quat_ok else 'FAIL'} "
          f"触觉={'OK' if tactile_ok else 'FAIL'} {err}）")
    return ok


def main():
    results = [
        scenario_stop_is_terminal(),
        scenario_exclusive_flag(),
        scenario_transient_retry(),
        scenario_reopen_backoff(),
        scenario_open_busy(),
        scenario_real_exclusive_lock(),
        scenario_end_to_end_pty(),
    ]
    if all(results):
        print("\nPASS: STM32 USB 手套传输层测试全部通过")
        return 0
    print("\nFAIL: 存在未通过场景")
    return 1


if __name__ == "__main__":
    sys.exit(main())
