"""ESP32S3 夹爪串口的唯一资源所有者。"""

from __future__ import annotations

import re
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
    # 握手不再固定白等 2 秒：在最长 CONNECT_BOOT_TIMEOUT 内反复发 ``?``，
    # 收到 STATE 立刻返回。刚上电的 ESP32 仍在窗口内，已就绪的不必空等。
    CONNECT_BOOT_TIMEOUT = 2.0
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
        """独占打开串口，在有限窗口内用 ``?`` 握手确认主控在线。

        等于 :meth:`open_readonly` + :meth:`handshake`（打开后先丢弃陈旧
        输入再握手）。诊断路径要在「握手不上」时仍然拿到板子 TX 侧的
        证据，所以两半可以分开单独调。
        """
        with self._io_lock:
            if not self._open_locked(port, baud):
                return False
            return self._handshake_locked()

    def open_readonly(self, port: str, baud: int = CONNECT_BAUD) -> bool:
        """只打开串口、一个字节都不写（只读诊断的第一步）。

        打开参数与 :meth:`connect` 完全一致（独占、同样的读/写超时），
        区别是不发握手 ``?``：板子收 FIFO 满导致写阻塞时，**写**这条路
        走不通，**打开+读**这条路仍然走得通 —— 这是把「板子哑了」与
        「板子会说话但不收主机写入」分开的唯一手段。
        """
        with self._io_lock:
            return self._open_locked(port, baud)

    def handshake(self) -> bool:
        """在已打开的串口上补做握手（:meth:`connect` 的后半段）。"""
        with self._io_lock:
            return self._handshake_locked()

    def _open_locked(self, port: str, baud: int) -> bool:
        self._disconnect_locked()
        self._last_error = None
        try:
            # 独占模式：串口被其他进程占用时立刻失败，而不是两个进程
            # 各自读走半条响应，把「被占用」伪装成「握手超时」。
            opened = self._serial_factory(
                port,
                baud,
                timeout=self.CONNECT_TIMEOUT,
                write_timeout=self.CONNECT_WRITE_TIMEOUT,
                exclusive=True,
            )
            self._serial = opened
            opened.reset_input_buffer()
        except Exception as exc:
            self._last_error = f"serial open/handshake failed: {exc}"
            self._disconnect_locked()
            return False
        return True

    def _handshake_locked(self) -> bool:
        opened = self._serial
        if opened is None or not getattr(opened, "is_open", False):
            self._last_error = "serial handshake skipped: port not open"
            return False
        attempts_made = 0
        last_line = ""
        try:
            deadline = self._clock() + self.CONNECT_BOOT_TIMEOUT
            for attempt in range(self.CONNECT_ATTEMPTS):
                if attempt and self._clock() >= deadline:
                    break
                attempts_made = attempt + 1
                opened.write(b"?\r\n")
                last_line = opened.readline().decode(
                    errors="ignore").strip()
                self._logger(
                    f"[Serial] connect attempt {attempt + 1}/"
                    f"{self.CONNECT_ATTEMPTS}: [{last_line[:60]}]"
                )
                if last_line.startswith("STATE"):
                    return True
                if self._clock() < deadline:
                    self._sleep(self.CONNECT_RETRY_DELAY)
        except Exception as exc:
            # 中断在哪一次写、最后一次读回什么，都要留痕：板子收 FIFO 没人
            # 排空时第一笔写就阻塞，日志里一条 `connect attempt` 都不会有，
            # 光看上面那行会以为「没试过」。这一行把两者区分开。
            self._logger(
                f"[Serial] 握手中断于第 {attempts_made} 次写"
                f"（最后一次读回 {last_line!r}）: {exc}")
            self._last_error = (
                f"serial open/handshake failed: {exc}"
            )
            self._disconnect_locked()
            return False
        # 报实际尝试次数，不报上限：被抢占/占用时上限是误导
        self._last_error = (
            "serial handshake timeout after "
            f"{attempts_made} attempts"
        )
        self._disconnect_locked()
        return False

    def read_lines(self, duration: float, *, limit: int = 200):
        """在 ``duration`` 秒内收集板上主动到达的整行（只读诊断）。

        不发任何命令：健康的 ESP32 空闲时也会周期性输出 ``STATE ...``，
        一行都收不到就说明没有程序在写这个口。超时那次 ``readline`` 返回
        空串，继续等到达时间上限；串口没打开时返回空列表。
        """
        with self._io_lock:
            opened = self._serial
            if opened is None or not getattr(opened, "is_open", False):
                return []
            lines = []
            deadline = self._clock() + max(0.0, float(duration))
            while len(lines) < limit and self._clock() < deadline:
                try:
                    line = opened.readline().decode(
                        errors="ignore").strip()
                except Exception:
                    # 板子掉线/句柄失效：已收到的行仍然有效，如实返回
                    break
                if line:
                    lines.append(line)
            return lines

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

    def query_fays_serial(self) -> str:
        """读取 ESP32 NVS 中绑定的 Fays 产品序列号；未绑定/无效返回空串。"""
        response = self.send("QF")
        prefix = "FAYS_SERIAL:"
        if not response.startswith(prefix):
            return ""
        return response[len(prefix):].strip()

    def set_fays_serial(self, serial: str) -> bool:
        """写入并持久化 ESP32 绑定的 Fays 产品序列号。"""
        value = str(serial or "").strip()
        if (
            not value
            or len(value) > 64
            or re.fullmatch(r"[A-Za-z0-9._-]+", value) is None
        ):
            raise ValueError("Fays 序列号只能是 1-64 位字母、数字、点、下划线或短横线")
        response = self.send(f"WF:{value}")
        if response.startswith("OK FAYS_SERIAL_SET:"):
            return True
        if response.startswith("ERR "):
            raise RuntimeError(f"ESP32 写入 Fays 序列号失败: {response}")
        raise RuntimeError(f"ESP32 写入 Fays 序列号无有效响应: {response or '<empty>'}")

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
                command_text = str(command)
                if command_text.startswith("WF:"):
                    # 写绑定：三种终态都算「本条命令的响应」，其余行继续等，
                    # 免得把固件周期性的 STATE 当成写入结果。
                    expected = (
                        "OK FAYS_SERIAL_SET:",
                        "ERR FAYS_SERIAL_INVALID",
                        "ERR FAYS_SERIAL_SAVE",
                    )
                else:
                    table = {
                        "QF": ("FAYS_SERIAL:", "ERR FAYS_SERIAL_NOT_SET"),
                        "?": ("STATE",),
                        "G": ("AS5048A", "AS5048A ERR"),
                        "C": ("OK CONNECT", "ERR"),
                        "I": ("OK INIT", "ERR"),
                        "F": ("OK READY", "ERR"),
                        "E": ("OK ERROR", "ERR"),
                        "R": ("OK ASR", "AS5048A ERR"),
                        "S": ("OK START", "ERR"),
                    }
                    # 先按完整命令查（QF 是两字符命令），再退回首字符。
                    # 只按 [:1] 查会让 "QF" 这条永远命中不到，等于宣布
                    # 「第一行就是响应」——固件周期性的 STATE 会顶替真响应。
                    expected = table.get(command_text, table.get(
                        command_text[:1], ()))
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
