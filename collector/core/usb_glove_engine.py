"""USB (Type-C) 手套数据引擎 —— 厂商 SDK，IMU 四元数 + 16×16 触觉。

传输与触觉降噪都走**厂商 SDK**（`glove_io.streams.RawImuStream` /
`gui.tactile_processing.TactilePreprocessor`），由 `core.glove_sdk_boot`
统一装配 —— 模块内不 import `sdk.*`，也不自己插 `sys.path`。RawImuStream
自带 daemon 串口线程（自动重连、CRC 校验、队列限长），本引擎在其上再起
IMU / 触觉两个消费线程；对外信号与属性口径与 SensorBLEEngine 完全对齐，
GloveWidget 通过 engine= 参数注入即可无缝切换两种手套。

两条厂商 SDK 侧的坑，本文件是**唯一**的兜底处（详见
`docs/postmortem_glove_sdk_migration.md`）：

1. **`stop()` 不终结**：SDK 的 `RawImuStream.start()` 无条件
   `stop_event.clear()`，而 `read()`/`frames()` 每次调用都先 `start()`。
   于是 stop 之后任何一次读都会清掉停止标志、重开串口、复活一个**没人
   持有引用的孤儿读线程**，永久占着 tty ⇒ 症状是「必须重启程序」。
   `core/glove_usb`（fork 档）修过这条，厂商 2.1.0 里没有。
2. **触觉重复喂**：姿态与压力是设备侧两条独立 60Hz 流，压力矩阵会挂在
   后续若干帧上。本引擎的 `process_frame()` 由 UI 按 30ms 轮询，设备一
   停摆就会把同一个矩阵反复喂进时间域滤波器（`TactilePreprocessor`），
   时间常数被缩短、预热提前结束。按**代际计数**去重（SDK 引擎里叫
   `tactile_fresh`，README-SDK.md:84-87 明写要求调用方自己按它去重）。
"""

from __future__ import annotations

import threading
import time

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

from core.glove_sdk_boot import (
    raw_imu_stream_cls,
    stream_errors,
    tactile_preprocessor_cls,
)

MATRIX_ROWS = 16
MATRIX_COLS = 16
IMU_COUNT = 16
QUAT_COMPONENTS = 4
TARGET_FPS = 80          # 手套 IMU 额定帧率（触觉与 IMU 复用同一串口流）

#: 同类错误文本的重复上报间隔（秒）。`disconnected` **不**受此限 —— 它是
#: 状态机边沿，漏一次面板就永远停在"已连接"。串口抖动时 SDK 每 0.2s 重试
#: 一次，不限频会把面板刷爆、把真线索埋掉。
ERROR_RATE_LIMIT_S = 5.0

#: 单次读的超时（秒）。决定消费线程**持 `_stream_lock` 的最长时间**，
#: 因此也就是 `disconnect()` 的最坏等待。0.1s 对 60~80Hz 的流足够（帧间隔
#: 12~17ms），又不会让断开时明显卡顿。
READ_TIMEOUT_S = 0.1

#: `_read_locked` 的哨兵：流已关闭（与"这一轮没读到帧"的 None 区分）
_CLOSED = object()


class UsbGloveEngine(QObject):
    """STM32 USB 手套采集引擎（信号口径 = SensorBLEEngine）。

    信号
    ----
    connected(str)          — 串口已打开，设备地址（端口路径）
    disconnected()          — 串口已断开
    fps_updated(float)      — IMU 实测帧率
    error_occurred(str)     — 错误消息
    calibration_progress(int) — 触觉基线校准进度 (0-100)
    """

    connected = pyqtSignal(str)
    disconnected = pyqtSignal()
    fps_updated = pyqtSignal(float)
    error_occurred = pyqtSignal(str)
    calibration_progress = pyqtSignal(int)

    def __init__(self, serial_port: str = ""):
        super().__init__()
        self.serial_port = serial_port

        # ── 触觉（16×16 float32 ADC，口径同 ble_engine.data_array）──
        self.data_array = np.zeros((MATRIX_ROWS, MATRIX_COLS),
                                   dtype=np.float32)
        self.data_lock = threading.Lock()
        self.latest_data_ts_us = 0          # 宿主收帧时刻（μs，帧龄显示用）
        # 触觉代际：每收到一个**新扫描**才 +1（见模块 docstring 第 2 点）
        self._tactile_generation = 0
        self._tactile_fed_gen = -1
        self._last_processed = None
        self._last_peak = 0.0

        # ── IMU（16×4 XYZW 四元数 + 有效性掩码）──
        self.imu_array = np.zeros((IMU_COUNT, QUAT_COMPONENTS), dtype=np.float64)
        self.imu_valid = np.zeros(IMU_COUNT, dtype=bool)
        self.imu_lock = threading.Lock()
        self.latest_imu_ts_us = 0           # 设备时钟 μs（信息用）
        self.imu_present_count = 0          # 有效 IMU 传感器数（覆盖条显示）

        # ── 帧率（1s 滑动窗）──
        self.hardware_fps = 0.0             # IMU fps（主显示）
        self.tactile_fps = 0.0

        # ── 触觉降噪管线（SDK 的 TactilePreprocessor，含自动基线校准）──
        # SDK 装配失败时**不抛异常**：本引擎被 GloveWidget 在模块级 import，
        # 构造时抛会把整个程序带走。改成把原因存下来，连接时报到面板上
        # （与「主程序照常启动，只是手套功能不可用」的承诺一致）。
        self._backend_error = ""
        self._preprocessor = None
        try:
            self._preprocessor = tactile_preprocessor_cls()()
        except Exception as exc:
            self._backend_error = f"手套 SDK 不可用：{exc}"
        if self._preprocessor is not None:
            self.is_calibrating = True
            self.drift_baseline_val = 0.0
            # 覆盖条渲染参数（GloveWidget 直接读，口径同 ble_engine）
            self.base_noise_gate = self._preprocessor.base_gate
            self.dynamic_noise_ratio = self._preprocessor.dynamic_noise_ratio
            self.spatial_filter_enabled = self._preprocessor.spatial_filter
        else:
            self.is_calibrating = False
            self.drift_baseline_val = 0.0
            self.base_noise_gate = 0.0
            self.dynamic_noise_ratio = 0.0
            self.spatial_filter_enabled = False

        # ── 运行状态 ──
        self._running = False
        self._was_connected = False
        self._stream = None
        self._threads: list[threading.Thread] = []
        # R1 兜底：**整段读**都持这把锁，不是进读之前检查一次 —— 后者留的
        # 窄竞态症状恰好就是「偶发必须重启」。disconnect() 在同一把锁里
        # stop()，于是 SDK 的 start() 不可能排在 stop() 之后。
        # 只有**一个**读线程（见 `_reader_loop` 里"为什么不是两个线程"），
        # 所以这把锁不会被争抢，持锁时长 = 一次 poll 或一个读超时。
        # 用 read(timeout) 而不是 SDK 的 frames() 生成器 —— 后者会把锁持有
        # 到有帧为止 ⇒ disconnect 死锁，见 `_read_locked`。
        self._stream_lock = threading.Lock()
        self._stream_closed = False
        # 错误限频台账（detail → 上次上报时刻 / 被抑制次数）
        self._err_last: dict[str, float] = {}
        self._err_suppressed: dict[str, int] = {}

    # ── 连接控制 ──────────────────────────────────────

    def connect_device(self, port: str = ""):
        """打开串口并启动消费线程（幂等：重复调用只更新端口）。"""
        if port:
            self.serial_port = port
        if self._running:
            return
        if self._backend_error:
            self._emit_error(self._backend_error)
            return
        try:
            stream_cls = raw_imu_stream_cls()
        except Exception as exc:
            self._backend_error = f"手套 SDK 不可用：{exc}"
            self._emit_error(self._backend_error)
            return
        self._running = True
        self._was_connected = False
        with self._stream_lock:
            self._stream = stream_cls(
                serial_port=self.serial_port or None,
                status_callback=self._on_status,
            )
            self._stream_closed = False
        self._threads = [
            threading.Thread(target=self._reader_loop,
                             name="usb-glove-reader", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def disconnect(self):
        """停止串口与消费线程（消费线程看见 `_stream_closed` 后自行退出）。

        `stop()` 必须在 `_stream_lock` 里调用：消费线程整段读都持这把锁，
        所以这里拿到锁 = 没有任何线程正卡在 `read()` 里，SDK 的
        `start()` 也就没机会把 `stop_event` 清回去（模块 docstring 第 1 点）。
        反过来说，消费线程持锁时长必须**有界**（`READ_TIMEOUT_S`），
        否则这里会一直等不到锁 ⇒ 见 `_read_locked` 的注释。
        """
        self._running = False
        with self._stream_lock:
            self._stream_closed = True
            stream = self._stream
            self._stream = None
            if stream is not None:
                stream.stop(timeout_s=1.0)
        for t in self._threads:
            if t is not threading.current_thread() and t.is_alive():
                t.join(timeout=1.5)
        self._threads.clear()

    # ── 状态回调（串口线程 → Qt 信号，自动排队）──

    def _emit_error(self, detail: str):
        """error_occurred 限频：同一 detail `ERROR_RATE_LIMIT_S` 一次。

        限频不是"少说话"，是**让线索可读**：串口抖动时同一条错误能刷几千
        行。被抑制的次数在下一次真正上报时补在尾巴上（"同类消息已抑制 N
        次"），所以没有信息真正丢失。
        """
        detail = detail or "未知错误"
        key = detail[:120]
        now = time.monotonic()
        last = self._err_last.get(key)
        if last is not None and now - last < ERROR_RATE_LIMIT_S:
            self._err_suppressed[key] = self._err_suppressed.get(key, 0) + 1
            return
        suppressed = self._err_suppressed.pop(key, 0)
        self._err_last[key] = now
        if suppressed:
            detail = f"{detail}（同类消息已抑制 {suppressed} 次）"
        self.error_occurred.emit(detail)

    def _on_status(self, status: str):
        """RawImuStream status_callback：connected/waiting/error/… 转 Qt 信号。"""
        if status == "connected" and not self._was_connected:
            self._was_connected = True
            port = self._stream.port if self._stream else self.serial_port
            self.connected.emit(str(port or ""))
        elif status in ("disconnected", "stopped"):
            if self._was_connected:
                self._was_connected = False
                # 掉线原因别再吞掉：串口层会给出 "serial read failed (5x): …"，
                # 这是区分「真被拔了」与「同一 tty 被第二个句柄抢字节」的唯一线索
                # （2026-09-21 那次双手套掉线风暴靠采样 /proc fd 才看出来，日志里
                # 一个字都没有）。真正 stop() 时这里取不到 error，不会误报。
                detail = self._stream.last_error if self._stream else ""
                if detail:
                    self._emit_error(detail)
                self.disconnected.emit()
        elif status.startswith("error"):
            detail = self._stream.last_error if self._stream else ""
            self._emit_error(detail or status)
        elif status.startswith("waiting") and self._was_connected:
            # 已连后读失败回落为 waiting：视为掉线（串口线程会重连）
            self._was_connected = False
            self.disconnected.emit()
        elif status.startswith("waiting"):
            # 从未连上：面板可见"未找到设备"原因（同类文本限频到 5s 一次）
            detail = self._stream.last_error if self._stream else ""
            self._emit_error(detail or status)

    # ── 消费线程 ──────────────────────────────────────

    def _read_locked(self, read, closed_exc, timeout_exc):
        """在锁里读一帧 → 帧 / None（本次超时）/ `_CLOSED`（已关闭）。

        **不能用 SDK 的 `frames()` 生成器**：它内部 `except
        StreamTimeoutError: continue`，于是 `next()` 会一直阻塞到有帧为止。
        锁被持有那么久，`disconnect()` 就永远拿不到锁 —— 而 `disconnect()`
        拿不到锁就不会 `stop()`，生成器也就永远等不到 `stop_event` ⇒
        **死锁**（实测：pty 不灌数据时整份测试挂死，只能 kill）。
        直接调 `read(timeout=...)` 才能把持锁时长压在一个读超时之内。

        超时返回 None 而不是让调用方 `continue`：`with` 块要退出再重进，
        才会重新检查 `_stream_closed`。
        """
        with self._stream_lock:
            if self._stream_closed:
                return _CLOSED
            try:
                return read(timeout=READ_TIMEOUT_S)
            except timeout_exc:
                return None
            except closed_exc:
                return _CLOSED

    def _reader_loop(self):
        """**单**读线程：先非阻塞取空两条队列，都空时才阻塞等一帧。

        **为什么不是 IMU / 触觉各一个线程**（第一版就是那样，被实测咬过）：
        两条流共用一把 `threading.Lock`，而 CPython 的 Lock **不保证公平**
        —— 某条流静默时，它那个消费线程会在 `release()` 后立刻重新
        `acquire()`，把刚被唤醒的另一条线程**挤掉**。实测（pty 只灌触觉帧、
        IMU 全程静默）：触觉侧连续 1.1s 一次锁都没抢到（11 次 IMU 读超时
        之间夹 0 次触觉读），症状是「矩阵半天不跳」；同一份代码换个相位又
        一切正常 —— 纯看运气。单线程没有争抢，行为确定。

        **取空队列而不是只取一帧**：`poll()` 一次把积压全拿出来，帧率计数
        才是真的（只取一帧会把 60Hz 数成 10Hz），最后一帧即最新值。
        两条流任一条有数据时循环就被数据推着走、不会空转；都空才阻塞
        一个读超时，所以静默时也不烧 CPU。
        """
        stream = self._stream
        if stream is None:
            return
        tactile = stream.tactile_stream()
        closed, timeout = stream_errors()
        imu_count = tac_count = 0
        window_start = time.monotonic()
        try:
            while True:
                with self._stream_lock:
                    if self._stream_closed or not self._running:
                        break
                    imu_frames = stream.poll()
                    tac_frames = tactile.poll()
                if not imu_frames and not tac_frames:
                    # 两条都空才阻塞等一帧。走 `_read_locked`（不是直接 read）
                    # 是因为「关闭后不再读」这条兜底只该有一份实现，也正是
                    # 测试单独调用的那个入口。
                    frame = self._read_locked(stream.read, closed, timeout)
                    if frame is _CLOSED or not self._running:
                        break
                    if frame is not None:
                        imu_frames = [frame]
                for frame in imu_frames:
                    self._store_imu(frame)
                for frame in tac_frames:
                    self._store_tactile(frame)
                imu_count += len(imu_frames)
                tac_count += len(tac_frames)
                now = time.monotonic()
                if now - window_start >= 1.0:
                    elapsed = now - window_start
                    self.hardware_fps = imu_count / elapsed
                    self.tactile_fps = tac_count / elapsed
                    self.fps_updated.emit(self.hardware_fps)
                    imu_count = tac_count = 0
                    window_start = now
        except closed:
            pass
        except Exception as exc:  # 串口级异常已在 transport 内处理，兜底
            if self._running:
                self._emit_error(f"手套读线程异常: {exc}")

    def _store_imu(self, frame):
        with self.imu_lock:
            self.imu_array = np.asarray(frame.quaternions_xyzw,
                                        dtype=np.float64)
            self.imu_valid = np.asarray(frame.valid_mask, dtype=bool)
            self.latest_imu_ts_us = int(frame.device_timestamp_us)
            self.imu_present_count = int(np.count_nonzero(self.imu_valid))

    def _store_tactile(self, frame):
        with self.data_lock:
            self.data_array = np.asarray(frame.samples,
                                         dtype=np.float32).copy()
            self.latest_data_ts_us = time.time_ns() // 1_000
            self._tactile_generation += 1

    # ── 数据拉取（UI 线程 30ms 轮询，口径同 ble_engine）──

    def process_frame(self):
        """→ (processed 16×16, peak)；校准期间返回 (None, 0.0)。

        **只在收到新扫描时才喂滤波器**（模块 docstring 第 2 点）。同一个矩阵
        重复喂会缩短时间常数、让预热提前结束，而本方法是按 30ms 轮询的：
        设备正常时（触觉 60Hz）每次都喂得上，停摆时才会走到去重那条路。
        """
        if self._preprocessor is None:
            return None, 0.0
        with self.data_lock:
            fresh = self._tactile_generation != self._tactile_fed_gen
            gen = self._tactile_generation
            raw = self.data_array.copy() if fresh else None
        if not fresh:
            # 没有新扫描：返回上一次的结果，画面不闪、滤波器时间轴不动
            if self._preprocessor.is_calibrating:
                self._emit_calibration_progress()
            return self._last_processed, self._last_peak
        self._tactile_fed_gen = gen
        processed, peak = self._preprocessor.process(raw)
        if processed is None:
            if self._preprocessor.is_calibrating:
                self._emit_calibration_progress()
        else:
            if self.is_calibrating:
                self.is_calibrating = False
                self.calibration_progress.emit(100)
            self.drift_baseline_val = self._preprocessor.drift_baseline_val
        self._last_processed, self._last_peak = processed, peak
        return processed, peak

    def _emit_calibration_progress(self):
        done, total = self._preprocessor.calibration_progress
        pct = min(100, done * 100 // max(total, 1))
        self.calibration_progress.emit(pct)

    def latest_imu(self):
        """→ (quats 64×float32, valid 16×float32, device_ts_us)；从未收帧返回 None。

        device_ts_us 为帧的设备时间戳（微秒），骨架解算的时间轴对齐用。
        """
        with self.imu_lock:
            if not np.any(self.imu_valid):
                return None
            quats = np.asarray(self.imu_array, dtype=np.float32).ravel().copy()
            valid = np.asarray(self.imu_valid, dtype=np.float32).ravel().copy()
            ts_us = int(getattr(self, "latest_imu_ts_us", 0) or 0)
        return quats, valid, ts_us
