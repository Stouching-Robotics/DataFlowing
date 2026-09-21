"""Client for the Fays ORB bridge's raw stereo/IMU recording socket.

主程序适配版：原始上位机把包直接交给 LerobotV3 writer；这里改为
``on_stereo`` 回调（bridge 拆左右目、只取左目转 BGR 投递 UI）。
IMU 原始数据不累积、不外送：SLAM 解算在桥接进程内部完成，
主程序只落盘 SLAM 位姿/轨迹（imu_packets 仅作包级诊断计数）。
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, Optional

from core.gripper.rgb_quality import RawStallWatch


RAW_HEADER = struct.Struct("<6I3Q4i2hi2I")
RAW_STREAM_MAGIC = 0x53544F55
RAW_STREAM_VERSION = 1
RAW_PACKET_STEREO = 1
RAW_PACKET_IMU = 2
HEADER_SIZE = 80
IMU_PAYLOAD_SIZE = 48
# 断线重连节奏：1s 一次尝试，累计 60s 仍连不上才算致命错误
RECONNECT_ATTEMPT_INTERVAL_S = 1.0
RECONNECT_TOTAL_TIMEOUT_S = 60.0

# 接收停滞口径（L0-2，v1.3.11）。正常包间隔 ~1ms 级（双目 30fps + IMU
# 1kHz 交替），100ms 已不可能是调度抖动；服务端队列 16 包 ≈0.53s 满即
# 主动 close，所以 300ms 是「差一档就要被断链」的预警位——不是为了当场
# 做什么，而是把「客户端到底卡了多久」变成 parquet 里的一个数。
RAW_STALL_NS = 100_000_000        # >100ms 计入停滞
RAW_STALL_ALERT_NS = 300_000_000  # ≥300ms 打一行（去抖）
RAW_STALL_REPEAT_NS = 5_000_000_000


class FaysRawStreamError(RuntimeError):
    """The native raw stream is incomplete or malformed."""


class FaysRawStreamClient:
    """Convert one native raw stream into bridge stereo/IMU callbacks."""

    def __init__(
        self,
        socket_path: str,
        on_stereo: Callable[..., None],
        *,
        logger: Callable[[str], object] = print,
        on_error: Optional[Callable[[str], None]] = None,
        on_reconnected: Optional[Callable[[], None]] = None,
        raw_cpu: Optional[int] = None,
    ) -> None:
        self.socket_path = str(socket_path)
        self._on_stereo = on_stereo
        self._logger = logger
        self._on_error = on_error
        # 断线重连成功回调（bridge 用来清 paired 待配对帧 + 重置
        # 空桶看门狗：断连窗口的空桶是显示侧问题，不计入取帧质量）
        self._on_reconnected = on_reconnected
        # 接收线程专用核（P5 补丁）：None = 不绑（继承掩码即可）
        self._raw_cpu = raw_cpu
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._last_stereo_sequence: Optional[int] = None
        self._last_stereo_timestamp_ns = 0
        self._reconnect_count = 0
        # L0-2：接收侧停滞仪表（录制开始时由 bridge 归零，见 reset_stall_watch）
        self._stall_watch = RawStallWatch(
            stall_ns=RAW_STALL_NS,
            alert_ns=RAW_STALL_ALERT_NS,
            repeat_ns=RAW_STALL_REPEAT_NS,
        )
        self.imu_packets = 0
        self.stereo_packets = 0
        self.connected = threading.Event()
        self.first_packet = threading.Event()
        self.error: Optional[str] = None

    @property
    def alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def reconnect_count(self) -> int:
        """累计重连次数（跨录制段累计；bridge 在录制开始时取基座）。"""
        return int(self._reconnect_count)

    def reset_stall_watch(self) -> None:
        """录制开始归零接收停滞读数（与 bridge.reset_rgb_quality 同一时刻）。"""
        self._stall_watch.reset()

    def stall_snapshot(self) -> dict:
        """接收停滞读数（ns/count；线程安全，主线程在录制结束时读）。"""
        return self._stall_watch.snapshot()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Fays raw stream is already running")
            self._stop.clear()
            self.error = None
            thread = threading.Thread(
                target=self._run,
                name="fays-raw-stream",
                daemon=False,
            )
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        with self._lock:
            sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, float(timeout)))
        with self._lock:
            self._thread = None
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    def _fail(self, message: str) -> None:
        self.error = message
        self._logger(f"[Gripper-Raw] Fays raw stream failed: {message}")
        if self._on_error is not None:
            self._on_error(message)
        self._stop.set()

    def _connect(self, deadline_s: float = 30.0) -> socket.socket:
        # 原生桥接在 ORB 系统装载完成后才创建 socket（通常在
        # [FAYS-CALIB] 输出之后数秒），比本客户端的启动时刻晚；
        # 带重试等待其出现（bind/listen 竞争也一并覆盖）。
        # deadline_s：首次连接 30s（SLAM 进程还在装载），断线重连
        # 收短到 5s（服务端 accept 循环常驻，正常秒级即可连上）
        deadline = time.monotonic() + float(deadline_s)
        last_error: Optional[OSError] = None
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                # 加大接收缓冲，吸收 native 抓帧线程被瞬时抢占时的突发：
                # 每包 ~1.4MB（1280x480x2+IMU），2MB 申请值内核翻倍后
                # 可容纳 ≥2 包突发，避免窗口抖动导致丢包降帧
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                   2 * 1024 * 1024)
                except OSError:
                    pass
                sock.connect(self.socket_path)
                sock.settimeout(0.2)
                return sock
            except OSError as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise FaysRawStreamError(
                        f"raw socket {self.socket_path} not ready "
                        f"after 30s: {exc}"
                    ) from exc
                time.sleep(0.5)
        raise FaysRawStreamError(
            "Fays raw stream client stopped before connect")

    def _recv_exact(self, sock: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size and not self._stop.is_set():
            try:
                chunk = sock.recv(size - len(chunks))
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    return bytes(chunks)
                raise FaysRawStreamError(f"socket read failed: {exc}") from exc
            if not chunk:
                if self._stop.is_set():
                    return bytes(chunks)
                raise FaysRawStreamError("Fays raw stream closed unexpectedly")
            chunks.extend(chunk)
        return bytes(chunks)

    def _close_sock(self, sock) -> None:
        with self._lock:
            if self._sock is sock:
                self._sock = None
        try:
            sock.close()
        except OSError:
            pass

    def _stream_loop(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            header_bytes = self._recv_exact(sock, HEADER_SIZE)
            if not header_bytes:
                break
            header = RAW_HEADER.unpack(header_bytes)
            (
                magic, version, kind, header_size, payload_size,
                _reserved0, sensor_timestamp_ns, host_monotonic_ns,
                host_realtime_ns, sequence, width, height, channels,
                encoding, _reserved1, step, _reserved2, _reserved3,
            ) = header
            if magic != RAW_STREAM_MAGIC or version != RAW_STREAM_VERSION:
                raise FaysRawStreamError("unknown raw stream protocol")
            if header_size != HEADER_SIZE:
                raise FaysRawStreamError(
                    f"unexpected raw header size {header_size}")
            payload = self._recv_exact(sock, payload_size)
            if len(payload) != payload_size:
                break
            # L0-2：包间隔停滞（在 kind 分支之前记，双目/IMU 一起算）。
            # 只记账 + 超门槛时递出一行日志，绝不在这里阻塞接收循环
            for line in self._stall_watch.note_recv(time.monotonic_ns()):
                self._logger(line)
            if kind == RAW_PACKET_IMU:
                if payload_size != IMU_PAYLOAD_SIZE:
                    raise FaysRawStreamError(
                        f"invalid IMU payload size {payload_size}")
                # 只做包级诊断计数：IMU 原始数据不外送（SLAM 解算
                # 在桥接进程内部完成，主程序只落盘 SLAM 位姿/轨迹）
                self.imu_packets += 1
                self.first_packet.set()
                continue
            if kind != RAW_PACKET_STEREO:
                raise FaysRawStreamError(f"unknown raw packet kind {kind}")
            if channels not in (1, 3) or width <= 0 or height <= 0:
                raise FaysRawStreamError(
                    f"invalid stereo frame {width}x{height}x{channels}")
            normalized_step = int(step) if int(step) > 0 else (
                int(width) * int(channels))
            if payload_size < normalized_step * height:
                raise FaysRawStreamError(
                    f"short stereo payload: {payload_size} bytes")
            # 时间戳单调校验仅对 side_by_side（单包含双目）成立：
            # paired_packets 模式左右目交替到达，两目传感器时钟有
            # ~20ms 固定偏移，全局比较会把正常交错误判为回退
            side_by_side = int(width) >= int(height) * 3
            if (side_by_side and self._last_stereo_timestamp_ns and (
                    sensor_timestamp_ns <= self._last_stereo_timestamp_ns)):
                raise FaysRawStreamError(
                    "Fays stereo timestamp regression: "
                    f"previous={self._last_stereo_timestamp_ns} "
                    f"current={sensor_timestamp_ns}")
            self._last_stereo_sequence = int(sequence)
            self._last_stereo_timestamp_ns = int(sensor_timestamp_ns)
            self.stereo_packets += 1
            self.first_packet.set()
            self._on_stereo(
                payload,
                int(sensor_timestamp_ns),
                int(host_monotonic_ns),
                int(sequence),
                int(width),
                int(height),
                int(channels),
                normalized_step,
                int(encoding),
            )
        if not self._stop.is_set():
            raise FaysRawStreamError("Fays raw stream closed by bridge")

    def _run(self) -> None:
        # 线程级自绑专用核（P5 补丁）：接收线程负载轻但节律敏感，
        # 独占一个 SMT 核可保证 native 30fps 取帧不被主进程重线程
        # 抢占；失败不致命（继承掩码已避开 SLAM 分区）
        if self._raw_cpu is not None:
            from core.gripper.affinity import pin_raw_thread
            if pin_raw_thread(self._raw_cpu):
                self._logger(f"[Gripper-Raw] 接收线程已绑定专用核 CPU{self._raw_cpu}")
            else:
                self._logger("[Gripper-Raw] 接收线程绑核失败（沿用继承掩码）")
        # 断线自动重连：服务端（SLAM 进程）在客户端短暂卡顿导致其
        # 原始队列溢出时会主动断开连接（stereo loss detected →
        # close）；显示链路重连即可恢复，不视为致命。只有首连失败
        # 或重连持续失败超时才算错误。
        first_connection = True
        reconnect_deadline = 0.0
        while not self._stop.is_set():
            try:
                sock = self._connect(
                    deadline_s=30.0 if first_connection else 5.0)
            except FaysRawStreamError as exc:
                if self._stop.is_set():
                    return
                if first_connection:
                    # 首连失败=致命（SLAM 进程未起/ipc 目录缺失）
                    self._fail(str(exc))
                    return
                if time.monotonic() >= reconnect_deadline:
                    self._fail(
                        "Fays raw stream reconnect failed for "
                        f"{RECONNECT_TOTAL_TIMEOUT_S:.0f}s: {exc}")
                    return
                time.sleep(RECONNECT_ATTEMPT_INTERVAL_S)
                continue
            first_connection = False
            reconnect_deadline = 0.0
            with self._lock:
                self._sock = sock
            self.connected.set()
            if self._reconnect_count:
                self._logger(
                    "[Gripper-Raw] 原始流已重连（第 {} 次），显示链路恢复"
                    .format(self._reconnect_count))
                if self._on_reconnected is not None:
                    try:
                        self._on_reconnected()
                    except Exception:
                        pass
            else:
                self._logger(
                    "[Gripper-Raw] Fays raw stereo/IMU stream connected")
            try:
                self._stream_loop(sock)
            except FaysRawStreamError as exc:
                self._close_sock(sock)
                if self._stop.is_set():
                    return
                self._reconnect_count += 1
                self._logger(
                    "[Gripper-Raw] 原始流连接中断：{}；{}s 后自动重连"
                    .format(exc, RECONNECT_ATTEMPT_INTERVAL_S))
                reconnect_deadline = (
                    time.monotonic() + RECONNECT_TOTAL_TIMEOUT_S)
                time.sleep(RECONNECT_ATTEMPT_INTERVAL_S)
                continue
            except Exception as exc:
                self._close_sock(sock)
                self._fail(str(exc))
                return
            self._close_sock(sock)
            return
