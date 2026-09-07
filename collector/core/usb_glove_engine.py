"""USB (Type-C) 手套数据引擎 —— STM32 CDC 串口，IMU 四元数 + 16×16 触觉。

复用 core.glove_usb 明文协议栈：RawImuStream 自带 daemon 串口线程
（自动重连、CRC 校验、队列限长），本引擎在其上再起 IMU / 触觉两个消费
线程；对外信号与属性口径与 SensorBLEEngine 完全对齐，GloveWidget 通过
engine= 参数注入即可无缝切换两种手套。
"""

from __future__ import annotations

import threading
import time

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

from core.glove_usb import RawImuStream, TactilePreprocessor
from core.glove_usb.errors import StreamClosedError, StreamTimeoutError

MATRIX_ROWS = 16
MATRIX_COLS = 16
IMU_COUNT = 16
QUAT_COMPONENTS = 4
TARGET_FPS = 80          # 手套 IMU 额定帧率（触觉与 IMU 复用同一串口流）


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

        # ── IMU（16×4 XYZW 四元数 + 有效性掩码）──
        self.imu_array = np.zeros((IMU_COUNT, QUAT_COMPONENTS), dtype=np.float64)
        self.imu_valid = np.zeros(IMU_COUNT, dtype=bool)
        self.imu_lock = threading.Lock()
        self.latest_imu_ts_us = 0           # 设备时钟 μs（信息用）
        self.imu_present_count = 0          # 有效 IMU 传感器数（覆盖条显示）

        # ── 帧率（1s 滑动窗）──
        self.hardware_fps = 0.0             # IMU fps（主显示）
        self.tactile_fps = 0.0

        # ── 触觉降噪管线（工具包 TactilePreprocessor，含自动基线校准）──
        self._preprocessor = TactilePreprocessor()
        self.is_calibrating = True
        self.drift_baseline_val = 0.0
        # 覆盖条渲染参数（GloveWidget 直接读，口径同 ble_engine）
        self.base_noise_gate = self._preprocessor.base_gate
        self.dynamic_noise_ratio = self._preprocessor.dynamic_noise_ratio
        self.spatial_filter_enabled = self._preprocessor.spatial_filter

        # ── 运行状态 ──
        self._running = False
        self._was_connected = False
        self._stream: RawImuStream | None = None
        self._threads: list[threading.Thread] = []

    # ── 连接控制 ──────────────────────────────────────

    def connect_device(self, port: str = ""):
        """打开串口并启动消费线程（幂等：重复调用只更新端口）。"""
        if port:
            self.serial_port = port
        if self._running:
            return
        self._running = True
        self._was_connected = False
        self._stream = RawImuStream(
            serial_port=self.serial_port or None,
            status_callback=self._on_status,
        )
        self._threads = [
            threading.Thread(target=self._imu_loop,
                             name="usb-glove-imu", daemon=True),
            threading.Thread(target=self._tactile_loop,
                             name="usb-glove-tactile", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def disconnect(self):
        """停止串口与消费线程（线程随 frames() 生成器退出，join 收尾）。"""
        self._running = False
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop(timeout_s=1.0)
        for t in self._threads:
            if t is not threading.current_thread() and t.is_alive():
                t.join(timeout=1.5)
        self._threads.clear()

    # ── 状态回调（串口线程 → Qt 信号，自动排队）──

    def _on_status(self, status: str):
        """RawImuStream status_callback：connected/waiting/error/… 转 Qt 信号。"""
        if status == "connected" and not self._was_connected:
            self._was_connected = True
            port = self._stream.port if self._stream else self.serial_port
            self.connected.emit(str(port or ""))
        elif status in ("disconnected", "stopped"):
            if self._was_connected:
                self._was_connected = False
                self.disconnected.emit()
        elif status.startswith("error"):
            detail = self._stream.last_error if self._stream else ""
            self.error_occurred.emit(detail or status)
        elif status.startswith("waiting") and self._was_connected:
            # 已连后读失败回落为 waiting：视为掉线（串口线程会重连）
            self._was_connected = False
            self.disconnected.emit()
        elif status.startswith("waiting"):
            # 从未连上：每 0.5s 报一次（面板可见"未找到设备"原因）
            detail = self._stream.last_error if self._stream else ""
            self.error_occurred.emit(detail or status)

    # ── 消费线程 ──────────────────────────────────────

    def _imu_loop(self):
        """IMU 帧消费：最新四元数 + 1s 滑动窗帧率。"""
        stream = self._stream
        if stream is None:
            return
        count, window_start = 0, time.monotonic()
        try:
            for frame in stream.frames():
                if not self._running:
                    break
                with self.imu_lock:
                    self.imu_array = np.asarray(frame.quaternions_xyzw,
                                                dtype=np.float64)
                    self.imu_valid = np.asarray(frame.valid_mask, dtype=bool)
                    self.latest_imu_ts_us = int(frame.device_timestamp_us)
                    self.imu_present_count = int(np.count_nonzero(
                        self.imu_valid))
                count += 1
                now = time.monotonic()
                if now - window_start >= 1.0:
                    self.hardware_fps = count / (now - window_start)
                    self.fps_updated.emit(self.hardware_fps)
                    count, window_start = 0, now
        except StreamClosedError:
            pass
        except Exception as exc:  # 串口级异常已在 transport 内处理，兜底
            if self._running:
                self.error_occurred.emit(f"IMU 线程异常: {exc}")

    def _tactile_loop(self):
        """触觉帧消费：最新 16×16 原始 ADC + 宿主时间戳 + 帧率。"""
        stream = self._stream
        if stream is None:
            return
        tactile = stream.tactile_stream()
        count, window_start = 0, time.monotonic()
        try:
            for frame in tactile.frames():
                if not self._running:
                    break
                host_us = time.time_ns() // 1_000
                with self.data_lock:
                    self.data_array = np.asarray(frame.samples,
                                                 dtype=np.float32).copy()
                    self.latest_data_ts_us = host_us
                count += 1
                now = time.monotonic()
                if now - window_start >= 1.0:
                    self.tactile_fps = count / (now - window_start)
                    count, window_start = 0, now
        except StreamClosedError:
            pass
        except Exception as exc:
            if self._running:
                self.error_occurred.emit(f"触觉线程异常: {exc}")

    # ── 数据拉取（UI 线程 30ms 轮询，口径同 ble_engine）──

    def process_frame(self):
        """→ (processed 16×16, peak)；校准期间返回 (None, 0.0)。"""
        with self.data_lock:
            raw = self.data_array.copy()
        processed, peak = self._preprocessor.process(raw)
        if processed is None:
            if self._preprocessor.is_calibrating:
                done, total = self._preprocessor.calibration_progress
                pct = min(100, done * 100 // max(total, 1))
                self.calibration_progress.emit(pct)
        else:
            if self.is_calibrating:
                self.is_calibrating = False
                self.calibration_progress.emit(100)
            self.drift_baseline_val = self._preprocessor.drift_baseline_val
        return processed, peak

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
