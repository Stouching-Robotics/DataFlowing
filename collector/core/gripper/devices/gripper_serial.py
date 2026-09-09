"""ESP32S3 夹爪串口的唯一资源所有者。"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import serial
import serial.tools.list_ports


class GripperSerial:
    """拥有并封装唯一的 ``serial.Serial`` 实例。

    ``connect`` 内的 ``?`` 是打开后的握手，不属于应用命令。握手完成以后，所有
    同步和异步应用命令都应交给 :class:`SerialCommandWorker`，由它串行调用
    :meth:`send`。
    """

    CONNECT_BAUD = 115200
    CONNECT_TIMEOUT = 0.5
    CONNECT_WRITE_TIMEOUT = 0.5
    CONNECT_BOOT_DELAY = 2.0
    CONNECT_ATTEMPTS = 8
    CONNECT_RETRY_DELAY = 0.3
    COMMAND_TIMEOUT = 1.0

    def __init__(
        self,
        serial_factory: Optional[Callable[..., object]] = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        logger: Callable[..., None] = print,
    ):
        self._serial_factory = serial_factory or serial.Serial
        self._sleep = sleep
        self._clock = clock
        self._logger = logger
        self._serial = None
        self._last_error = None
        # connect/disconnect 与 worker send 共用同一把可重入锁；断开不会在一条
        # 命令写入和读取响应之间关闭文件描述符。
        self._io_lock = threading.RLock()

    @property
    def ser(self):
        """Return the owner-held handle for read-only connection diagnostics."""
        with self._io_lock:
            return self._serial

    @property
    def is_connected(self) -> bool:
        with self._io_lock:
            return bool(
                self._serial is not None
                and getattr(self._serial, "is_open", False)
            )

    @property
    def last_error(self) -> Optional[str]:
        """返回最近一次连接失败的准确原因，不用握手超时覆盖打开错误。"""
        with self._io_lock:
            return self._last_error

    def connect(self, port: str, baud: int = CONNECT_BAUD) -> bool:
        """打开串口并用原有的八次 ``?`` 握手确认主控在线。"""
        with self._io_lock:
            self._disconnect_locked()
            self._last_error = None
            try:
                opened = self._serial_factory(
                    port,
                    baud,
                    timeout=self.CONNECT_TIMEOUT,
                    write_timeout=self.CONNECT_WRITE_TIMEOUT,
                )
                self._serial = opened
                self._sleep(self.CONNECT_BOOT_DELAY)
                opened.reset_input_buffer()
                for attempt in range(self.CONNECT_ATTEMPTS):
                    opened.write(b"?\r\n")
                    line = opened.readline().decode(
                        errors="ignore").strip()
                    self._logger(
                        f"[Serial] connect attempt {attempt + 1}/"
                        f"{self.CONNECT_ATTEMPTS}: [{line[:60]}]"
                    )
                    if line.startswith("STATE"):
                        return True
                    self._sleep(self.CONNECT_RETRY_DELAY)
            except Exception as exc:
                self._last_error = (
                    f"serial open/handshake failed: {exc}"
                )
                self._disconnect_locked()
                return False
            self._last_error = (
                "serial handshake timeout after "
                f"{self.CONNECT_ATTEMPTS} attempts"
            )
            self._disconnect_locked()
            return False

    def disconnect(self) -> None:
        """幂等关闭串口；即使底层已关闭也清除 owner 引用。"""
        with self._io_lock:
            self._disconnect_locked()

    def _disconnect_locked(self) -> None:
        opened, self._serial = self._serial, None
        if opened is None:
            return
        try:
            if getattr(opened, "is_open", False):
                # CDC-ACM 端点停止接收时，close(fd) 也可能等待 tty 输出
                # 队列排空。先中断 pyserial 写等待并丢弃未发送字节，保证
                # Connect 失败和退出路径都有界。
                cancel_write = getattr(opened, "cancel_write", None)
                if callable(cancel_write):
                    try:
                        cancel_write()
                    except Exception:
                        pass
                reset_output = getattr(
                    opened, "reset_output_buffer", None)
                if callable(reset_output):
                    try:
                        reset_output()
                    except Exception:
                        pass
                opened.close()
        except Exception:
            # 关闭失败时也不能把已经失效的句柄重新暴露给调用方。
            pass

    def send(self, command: str) -> str:
        """发送一条文本命令并返回匹配的完整响应行。"""
        with self._io_lock:
            opened = self._serial
            if opened is None or not getattr(opened, "is_open", False):
                return "ERR"
            try:
                # 固件在 RECORDING 状态会主动输出 DATA 行。先清旧输入，再只
                # 接受当前命令的响应前缀，保持原程序的防串线行为。
                try:
                    opened.reset_input_buffer()
                except Exception:
                    pass
                opened.write((str(command) + "\r\n").encode())
                expected = {
                    "?": ("STATE",),
                    "G": ("AS5048A", "AS5048A ERR"),
                    "C": ("OK CONNECT", "ERR"),
                    "I": ("OK INIT", "ERR"),
                    "F": ("OK READY", "ERR"),
                    "E": ("OK ERROR", "ERR"),
                    "R": ("OK ASR", "AS5048A ERR"),
                    "S": ("OK START", "ERR"),
                }.get(str(command)[:1], ())
                deadline = self._clock() + self.COMMAND_TIMEOUT
                last_line = ""
                while self._clock() < deadline:
                    line = opened.readline().decode(
                        errors="ignore").strip()
                    if not line:
                        continue
                    last_line = line
                    if not expected or line.startswith(expected):
                        return line
                return last_line or "ERR"
            except Exception:
                return "ERR"

    @staticmethod
    def list_ports():
        """返回系统当前可用串口设备名。"""
        return [
            port.device for port in serial.tools.list_ports.comports()
        ]
