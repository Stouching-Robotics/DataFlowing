"""Plaintext STM32 USB acquisition used by the public sensor interfaces.

The transport only validates and decodes the firmware wire format.  It does
not remap sensors, correct axes, filter orientations, calibrate a glove, or
solve hand keypoints.  Those operations belong to the protected core behind
the public 21-keypoint interface.
"""

from __future__ import annotations

from queue import Empty, Full, Queue
import threading
import time
from typing import Callable, Iterator

import numpy as np

from core.glove_usb.usb_protocol import (
    STM32_USB_PID,
    STM32_USB_VID,
    is_supported_version,
    USB_TYPE_ADC_MATRIX,
    USB_TYPE_IMU_Q14,
    USB_TYPE_STATUS,
    UsbCdcFrameParser,
    decode_adc_matrix_payload,
    decode_imu_q14_payload,
    find_stm32_cdc_port,
    plausible_quaternion_mask,
)
from core.glove_usb.errors import StreamClosedError, StreamTimeoutError
from core.glove_usb.types import RawImuFrame, TactileFrame


StatusCallback = Callable[[str], None]

# 读失败先当瞬时错误原地重试，连续失败到上限才关句柄重开串口。
# 理由：pyserial 对「select 说可读、但 os.read 返回 0 字节」抛的是和真掉线
# 完全一样的 SerialException（原文 ...device disconnected or multiple access
# on port?），而这种情形在同一 tty 被第二个句柄抢字节时高频出现、句柄本身仍然
# 有效。一失败就 close+reopen 会制造 3~4 次/秒的「已连接/已断开」风暴，而重开
# 又添一个新句柄，自成循环（2026-09-21 实测单段刷 6756 次、持续 15 分钟不自愈）。
_READ_RETRY_LIMIT = 5        # 连续读失败多少次才认定掉线（每次含 ~0.2s 读超时）
_READ_RETRY_DELAY = 0.05     # 原地重试前的短等待
_REOPEN_DELAY_MIN = 0.2      # 重开串口的起始退避
_REOPEN_DELAY_MAX = 2.0      # 退避上限


def _put_latest(queue: Queue, value) -> None:
    """Put a value without allowing a slow consumer to block acquisition."""

    while True:
        try:
            queue.put_nowait(value)
            return
        except Full:
            try:
                queue.get_nowait()
            except Empty:
                return


def _drain(queue: Queue, maximum: int | None = None) -> list:
    values = []
    limit = None if maximum is None else max(0, int(maximum))
    while limit is None or len(values) < limit:
        try:
            values.append(queue.get_nowait())
        except Empty:
            break
    return values


class _UsbSensorTransport:
    """One serial owner that multiplexes raw IMU and tactile frames."""

    def __init__(
        self,
        serial_port: str | None,
        usb_vid: int,
        usb_pid: int,
        imu_queue_size: int,
        tactile_queue_size: int,
        status_callback: StatusCallback | None,
    ):
        self.requested_port = serial_port
        self.usb_vid = int(usb_vid)
        self.usb_pid = int(usb_pid)
        self.imu_queue: Queue[RawImuFrame] = Queue(
            maxsize=max(2, int(imu_queue_size)))
        self.tactile_queue: Queue[TactileFrame] = Queue(
            maxsize=max(2, int(tactile_queue_size)))
        self.status_callback = status_callback
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active_port: str | None = None
        self.connected = False
        self.last_error: str | None = None
        self.last_status: str = "idle"
        self._stopped = False          # stop() 后置位，传输不可复活
        self._state_lock = threading.RLock()

    def _set_status(self, value: str, error: str | None = None) -> None:
        callback = None
        with self._state_lock:
            changed = value != self.last_status or error != self.last_error
            self.last_status = value
            self.last_error = error
            if changed:
                callback = self.status_callback
        if callback is not None:
            callback(value)

    def start(self) -> None:
        # stop() 是终结性的：不再清 stop_event、不再起线程。
        # 否则 RawImuStream.read()/frames() 里的惰性 start() 会在 stop() 之后
        # 把传输「复活」成一个没人持有引用的孤儿线程——引擎侧已 self._stream=None
        # 并 _threads.clear()，谁都调不到它的 stop()，于是它永久占着这个 tty 并
        # 持续 open/close，status_callback 还指着活着的引擎，日志里就成了刷不完的
        # 「已连接/已断开」。
        if self._stopped:
            return
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="stouch-usb-sensor",
            daemon=True,
        )
        self.thread.start()

    def stop(self, timeout_s: float = 3.0) -> None:
        self._stopped = True
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_s)))
        with self._state_lock:
            self.connected = False
        self._set_status("stopped")

    @property
    def stopped(self) -> bool:
        """stop() 之后恒 True（传输已终结，start() 不再生效）。"""
        return self._stopped

    def _run(self) -> None:
        try:
            import serial
        except ImportError as exc:
            self._set_status("error", "pyserial is not installed")
            return

        parser = UsbCdcFrameParser()
        serial_handle = None
        read_failures = 0
        reopen_delay = _REOPEN_DELAY_MIN
        while not self.stop_event.is_set():
            if serial_handle is None:
                try:
                    port = self.requested_port or find_stm32_cdc_port(
                        self.usb_vid, self.usb_pid)
                    if not port:
                        self._set_status(
                            "waiting",
                            f"STM32 USB CDC {self.usb_vid:04X}:{self.usb_pid:04X} not found",
                        )
                        self.stop_event.wait(0.5)
                        continue
                    # exclusive=True：同口被第二个句柄打开时直接 EBUSY 报错，
                    # 而不是静默成功、两个句柄互相抢字节（系统 pyserial 3.5 的
                    # exclusive 默认 None，不加锁）。flock 是按 open file
                    # description 的，同进程内第二次 open 同样会被挡住。
                    serial_handle = serial.Serial(
                        port, baudrate=115200, timeout=0.2, exclusive=True)
                    parser.reset()
                    read_failures = 0
                    # 退避**不在这里**清零：open 成功只说明句柄拿到了，不代表
                    # 读得到数据。「开得上、读不到」正是双手套抢同一 tty 的形态，
                    # 在 open 处清零会让退避永远停在起始值、退化成固定 0.2s 循环。
                    # 只有真读到数据才算这次连接可用（见下方 read 成功分支）。
                    with self._state_lock:
                        self.active_port = str(port)
                        self.connected = True
                    self._set_status("connected", None)
                except (OSError, serial.SerialException) as exc:
                    with self._state_lock:
                        self.connected = False
                    self._set_status("waiting", f"cannot open serial port: {exc}")
                    serial_handle = None
                    self.stop_event.wait(0.5)
                    continue

            try:
                data = serial_handle.read(4096)
            except (OSError, serial.SerialException) as exc:
                read_failures += 1
                if read_failures < _READ_RETRY_LIMIT:
                    # 瞬时错误（多为同口多句柄抢字节）：句柄仍有效，原地重试，
                    # 不关不重开 —— 这正是以前 3~4 次/秒风暴的来源。
                    self.stop_event.wait(_READ_RETRY_DELAY)
                    continue
                self._set_status(
                    "disconnected",
                    f"serial read failed ({read_failures}x): {exc}")
                with self._state_lock:
                    self.connected = False
                try:
                    serial_handle.close()
                except Exception:
                    pass
                serial_handle = None
                read_failures = 0
                self.stop_event.wait(reopen_delay)
                reopen_delay = min(reopen_delay * 2.0, _REOPEN_DELAY_MAX)
                continue

            if not data:
                continue
            read_failures = 0
            reopen_delay = _REOPEN_DELAY_MIN
            for wire_frame in parser.feed(data):
                host_timestamp_us = time.time_ns() // 1_000
                if not is_supported_version(wire_frame.version):
                    self._set_status(
                        "error",
                        f"unsupported USB protocol version {wire_frame.version}",
                    )
                    continue
                try:
                    if wire_frame.message_type == USB_TYPE_IMU_Q14:
                        quaternions = decode_imu_q14_payload(wire_frame.payload)
                        present = np.asarray([
                            bool(wire_frame.flags & (1 << index))
                            for index in range(16)
                        ], dtype=bool)
                        plausible = plausible_quaternion_mask(quaternions)
                        _put_latest(self.imu_queue, RawImuFrame(
                            sequence=int(wire_frame.sequence),
                            device_timestamp_us=int(wire_frame.timestamp_us),
                            host_timestamp_us=int(host_timestamp_us),
                            quaternions_xyzw=quaternions,
                            present_mask=present,
                            valid_mask=present & plausible,
                        ))
                    elif wire_frame.message_type == USB_TYPE_ADC_MATRIX:
                        matrix = decode_adc_matrix_payload(wire_frame.payload)
                        _put_latest(self.tactile_queue, TactileFrame(
                            sequence=int(matrix.sequence),
                            timestamp_us=int(matrix.scan_time_us),
                            samples=matrix.samples,
                            processed=False,
                        ))
                    elif wire_frame.message_type == USB_TYPE_STATUS:
                        status = wire_frame.payload.decode(
                            "utf-8", errors="replace").strip()
                        if status:
                            self._set_status(f"device: {status}", None)
                except ValueError as exc:
                    self._set_status("error", f"invalid USB payload: {exc}")

        if serial_handle is not None:
            try:
                serial_handle.close()
            except Exception:
                pass
        with self._state_lock:
            self.connected = False


class RawImuStream:
    """Public physical-channel IMU stream.

    Frames are exactly the decoded Q14 values in STM32 physical channel order.
    No sensor mapping, axis conversion, smoothing, calibration, or FK is
    applied.  Use :class:`glove_sdk.HandSolver.process` for 21-keypoint output.
    """

    def __init__(
        self,
        serial_port: str | None = None,
        *,
        usb_vid: int = STM32_USB_VID,
        usb_pid: int = STM32_USB_PID,
        buffer_size: int = 256,
        tactile_buffer_size: int = 2048,
        status_callback: StatusCallback | None = None,
        _transport: _UsbSensorTransport | None = None,
    ):
        self._transport = _transport or _UsbSensorTransport(
            serial_port,
            usb_vid,
            usb_pid,
            buffer_size,
            tactile_buffer_size,
            status_callback,
        )

    def start(self) -> "RawImuStream":
        self._transport.start()
        return self

    def stop(self, timeout_s: float = 3.0) -> None:
        self._transport.stop(timeout_s)

    close = stop

    def __enter__(self) -> "RawImuStream":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def connected(self) -> bool:
        return self._transport.connected

    @property
    def port(self) -> str | None:
        return self._transport.active_port

    @property
    def status(self) -> str:
        return self._transport.last_status

    @property
    def last_error(self) -> str | None:
        return self._transport.last_error

    def read(self, timeout: float | None = None) -> RawImuFrame:
        self.start()
        try:
            return self._transport.imu_queue.get(timeout=timeout)
        except Empty as exc:
            if self._transport.stop_event.is_set():
                raise StreamClosedError("raw IMU stream is closed") from exc
            raise StreamTimeoutError("timed out waiting for a raw IMU frame") from exc

    def poll(self, maximum: int | None = None) -> list[RawImuFrame]:
        """Return all currently buffered frames without blocking."""

        return _drain(self._transport.imu_queue, maximum)

    def frames(self, timeout: float = 0.25) -> Iterator[RawImuFrame]:
        self.start()
        while not self._transport.stop_event.is_set():
            try:
                yield self.read(timeout=timeout)
            except StreamTimeoutError:
                continue

    def tactile_stream(self) -> "TactileStream":
        """Return a tactile interface sharing this stream's serial handle."""

        return TactileStream(imu=self)

    def poll_tactile(self, maximum: int | None = None) -> list[TactileFrame]:
        return _drain(self._transport.tactile_queue, maximum)


class TactileStream:
    """Public raw 16x16 tactile stream.

    Pass ``imu=raw_stream`` to share the same USB connection.  A standalone
    tactile stream owns its transport and discards unconsumed IMU frames.
    """

    def __init__(
        self,
        imu: RawImuStream | None = None,
        *,
        serial_port: str | None = None,
        usb_vid: int = STM32_USB_VID,
        usb_pid: int = STM32_USB_PID,
        buffer_size: int = 2048,
        status_callback: StatusCallback | None = None,
    ):
        self._owner = imu is None
        self._imu = imu or RawImuStream(
            serial_port,
            usb_vid=usb_vid,
            usb_pid=usb_pid,
            buffer_size=16,
            tactile_buffer_size=buffer_size,
            status_callback=status_callback,
        )

    def start(self) -> "TactileStream":
        self._imu.start()
        return self

    def stop(self, timeout_s: float = 3.0) -> None:
        if self._owner:
            self._imu.stop(timeout_s)

    close = stop

    def __enter__(self) -> "TactileStream":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def connected(self) -> bool:
        return self._imu.connected

    def read(self, timeout: float | None = None) -> TactileFrame:
        self.start()
        try:
            return self._imu._transport.tactile_queue.get(timeout=timeout)
        except Empty as exc:
            if self._imu._transport.stop_event.is_set():
                raise StreamClosedError("tactile stream is closed") from exc
            raise StreamTimeoutError("timed out waiting for a tactile frame") from exc

    def poll(self, maximum: int | None = None) -> list[TactileFrame]:
        return self._imu.poll_tactile(maximum)

    def frames(self, timeout: float = 0.25) -> Iterator[TactileFrame]:
        self.start()
        while not self._imu._transport.stop_event.is_set():
            try:
                yield self.read(timeout=timeout)
            except StreamTimeoutError:
                continue


# Explicit name for callers that want one object for both sensor families.
SensorStream = RawImuStream


__all__ = ["RawImuStream", "SensorStream", "TactileStream"]
