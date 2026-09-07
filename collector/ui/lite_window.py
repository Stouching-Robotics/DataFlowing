"""极简采集窗口 —— 只保留 连接设备 → 采集 → 上传（lite 版）。

与主程序共用同一条采集链（core.pipeline → EgoDataWriter → encoder_probe）
与上传链（UploadManager → APIClient）；砍掉回放/登录/任务页/骨架解算。
本模块不得 import 任何重依赖（torch/scipy/loguru/pydantic/pyvista/
qt_material/mediapipe 及 ui.main_window 等），导入闭包由
tools/tests/lite_import_guard_test.py 断言守护。

设备范围（每类 1 台）:
  - D435 深度相机: RGB(1280×720) + 深度(848×480) 两路录制
  - USB-C 手套:    UsbGloveEngine（触觉 + IMU 四元数，无骨架解算）
  - BLE 手套:      SensorBLEEngine（触觉）
"""

from __future__ import annotations
import glob
import os
import re
import shutil
import threading
import time
from typing import List, Optional

import cv2
import numpy as np
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMainWindow, QMessageBox, QProgressBar,
                             QPushButton, QTextEdit, QVBoxLayout, QWidget)

from config import settings
from config.i18n import tr
from core.api_client import APIClient
from core.ble_engine import SensorBLEEngine
from core.database import db
from core.d435_camera import D435Worker, list_d400_devices
from core.d435_manager import D435DeviceManager
from core.device_detector import (DeviceInfo, _list_ble_devices,
                                  _list_d435_devices,
                                  _list_usb_glove_devices,
                                  _list_uvc_devices,
                                  set_ble_scan_suppressed,
                                  usb_glove_prefer_side)
from core.encoder_probe import list_working_ffmpegs
from core.helpers import delete_pooled_episode, format_duration
from core.pipeline import CameraPipeline
from core.uploader import UploadManager

# USB 手套引擎模块级导入（pyserial 在白名单依赖内）：缺依赖时在
# `import main_lite` 冒烟自检即暴露（错误 E），而不是开手套时才炸
try:
    from core.usb_glove_engine import UsbGloveEngine
    _USB_GLOVE_AVAILABLE = True
except Exception:
    UsbGloveEngine = None
    _USB_GLOVE_AVAILABLE = False

# lite 专属常量（不改 settings.py）
LITE_PREVIEW_FPS = 15          # 预览刷新上限（低端机友好）
LITE_PREVIEW_W, LITE_PREVIEW_H = 640, 360
LITE_UVC_W, LITE_UVC_H = 640, 480   # UVC 采集分辨率（低端机友好）
LITE_UVC_FPS = 30                   # UVC 采集帧率（=录制帧率）；预览刷新仍限 15
LITE_UVC_SLOT = "uvc_rgb"           # UVC 固定槽名（与 d435_rgb 同规格）

# 内置暗色 QSS（替代 qt-material 主题，~20 行）
LITE_QSS = f"""
QWidget {{
    background-color: {settings.COLOR_BG_MAIN};
    color: {settings.COLOR_TEXT_PRIMARY};
    font-size: 12px;
}}
QLineEdit, QComboBox, QListWidget, QTextEdit {{
    background-color: #2b2b2b;
    border: 1px solid {settings.COLOR_BORDER};
    border-radius: 3px;
    padding: 3px;
}}
QPushButton {{
    background-color: {settings.COLOR_BTN_DEFAULT_BG};
    border: none;
    border-radius: 3px;
    padding: 6px 12px;
}}
QPushButton:hover {{ background-color: {settings.COLOR_BTN_HOVER}; }}
QPushButton:disabled {{
    background-color: {settings.COLOR_BTN_DISABLED_BG};
    color: {settings.COLOR_BTN_DISABLED_TEXT};
}}
QPushButton#btnStart {{ background-color: {settings.COLOR_BTN_START}; }}
QPushButton#btnStop {{ background-color: {settings.COLOR_BTN_STOP}; }}
QPushButton#btnAbort {{ background-color: {settings.COLOR_BTN_ABORT}; }}
QGroupBox {{
    border: 1px solid {settings.COLOR_BORDER};
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 4px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; }}
QCheckBox {{ spacing: 4px; }}
QProgressBar {{
    background-color: #2b2b2b;
    border: 1px solid {settings.COLOR_BORDER};
    border-radius: 3px;
    text-align: center;
}}
QProgressBar::chunk {{ background-color: {settings.COLOR_BTN_START}; }}
"""


class LiteGlovePump(QObject):
    """无画面手套数据泵 —— GloveDataPump 的 lite 平移（删骨架解算）。

    30ms 轮询引擎 process_frame → pipeline.write_sensor（16×16 触觉）
    + write_glove_imu（USB 手套 16×4 四元数）；不写骨架关键点
    （hand_pose 占位列保持全零，见 egodata_writer 恒写口径）。
    不能 import GloveDataPump（glove_widget.py 顶层连带 solver/
    render_engine/sensor_hand_config 等重依赖）。
    """

    _TICK_MS = 30

    def __init__(self, sensor_column: str, engine,
                 on_log=None, parent=None):
        super().__init__(parent)
        self.sensor_column = sensor_column
        self._engine = engine
        self._on_log = on_log or (lambda msg: None)
        self._pipeline = None
        self._running = False
        self._fps_logged = False     # 帧率只报一次，避免刷日志
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(self._TICK_MS)

    # ── 连接控制 ──────────────────────────────────────

    def set_pipeline(self, pipeline):
        """由窗口注入当前录制管线引用。"""
        self._pipeline = pipeline

    def start(self, address: str = ""):
        """连接引擎并开始轮询（信号口径与 GloveDataPump.start 一致）。"""
        if self._engine is None:
            return
        self._engine.connected.connect(self._on_connected)
        self._engine.disconnected.connect(self._on_disconnected)
        self._engine.fps_updated.connect(self._on_fps)
        self._engine.calibration_progress.connect(self._on_calib_progress)
        self._engine.error_occurred.connect(self._on_error)
        self._engine.connect_device(address)

    def stop(self):
        """断开连接（与 GloveDataPump.stop 同口径）。"""
        if self._engine:
            try:
                self._engine.disconnect()
            except Exception:
                pass
        self._running = False

    # ── 引擎信号 → 窗口日志 ───────────────────────────

    def _on_connected(self, addr: str):
        self._running = True
        self._on_log(tr("[手套] {} 已连接: {}",
                        self.sensor_column, str(addr)[:16]))
        if self._pipeline:
            self._pipeline.record_event(self.sensor_column, "connected")

    def _on_disconnected(self):
        self._running = False
        self._on_log(tr("[手套] {} 已断开", self.sensor_column))
        if self._pipeline:
            self._pipeline.record_event(self.sensor_column, "disconnected")

    def _on_error(self, msg: str):
        self._on_log(tr("[手套] {} 错误: {}", self.sensor_column, msg))

    def _on_fps(self, fps: float):
        if not self._fps_logged:
            self._fps_logged = True
            self._on_log(tr("[手套] {} 硬件帧率: {:.0f} fps",
                            self.sensor_column, fps))

    def _on_calib_progress(self, progress: int):
        self._on_log(tr("[手套] {} 校准完成", self.sensor_column)
                     if progress >= 100
                     else tr("[手套] {} 校准中… {}%",
                             self.sensor_column, progress))

    # ── 轮询循环（只写录制，不渲染） ──────────────────

    def _tick(self):
        """处理一帧并写入录制管线（逻辑平移自 GloveDataPump._tick，
        删除骨架解算分支）。"""
        if self._engine is None or not self._running:
            return

        processed, _max_signal = self._engine.process_frame()
        if processed is None:
            return

        if self._pipeline is not None and self._pipeline.is_recording:
            capture_ts = self._engine.latest_data_ts_us
            self._pipeline.write_sensor(processed, capture_ts,
                                        sensor_name=self.sensor_column)
            # USB 手套附带 IMU 四元数（BLE 引擎无 latest_imu，hasattr 跳过）
            if hasattr(self._engine, "latest_imu"):
                imu = self._engine.latest_imu()
                if imu is not None:
                    quats, valid, _ts_us = imu
                    self._pipeline.write_glove_imu(self.sensor_column,
                                                   quats, valid)


class LitePreview(QLabel):
    """极简预览 —— 640×360 固定区，15fps 刷新，last-write-wins 防积压。

    不消费任何队列：frame_provider 每次刷新时取"最近一帧"引用，
    D435 帧信号跨线程 queued，慢渲染不会积压 Qt 事件队列。
    """

    def __init__(self, frame_provider=None, parent=None):
        super().__init__(parent)
        self._frame_provider = frame_provider or (lambda: None)
        self.setFixedSize(LITE_PREVIEW_W, LITE_PREVIEW_H)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #000; color: #888;")
        self.setText(tr("无信号"))
        self._render_count = 0
        self._render_t0 = time.time()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(int(1000 / LITE_PREVIEW_FPS))

    def _tick(self):
        frame = self._frame_provider()
        if frame is None or frame.size == 0:
            return
        h, w = frame.shape[:2]
        scale = min(LITE_PREVIEW_W / w, LITE_PREVIEW_H / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        try:
            resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
            if resized.ndim == 2:          # 灰度兜底
                resized = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            img = QImage(rgb.data, nw, nh, 3 * nw,
                         QImage.Format_RGB888).copy()
            self.setPixmap(QPixmap.fromImage(img))
        except Exception:
            return
        self._render_count += 1


class LiteUvcPump(QObject):
    """UVC 摄像头采集泵：cv2.VideoCapture 后台线程 → 帧信号。

    固定 LITE_UVC_W×H @ LITE_UVC_FPS（低端机口径）；测试可注入
    capture 替身（鸭子类型: isOpened/read/release/set）。打开失败或
    摄像头断开时发 error_occurred 并自行停止。
    """
    frames_ready = pyqtSignal(np.ndarray)
    error_occurred = pyqtSignal(str)

    def __init__(self, dev: DeviceInfo, capture=None, parent=None):
        super().__init__(parent)
        self._dev = dev
        self._capture = capture          # 测试注入替身（真实环境 None）
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _open(self):
        """按 by-id 路径（Linux 稳定）或索引（Windows）打开摄像头。

        opencv 5 在设备被占用/打开失败时可能返回 None（而非未打开的
        对象），统一归一为 None 让调用方处理。"""
        src = getattr(self._dev, "by_id_path", None) or self._dev.video_index
        if self._capture is not None:
            return self._capture
        cap = cv2.VideoCapture(src)
        if cap is None:
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, LITE_UVC_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, LITE_UVC_H)
        cap.set(cv2.CAP_PROP_FPS, LITE_UVC_FPS)
        return cap

    def _run(self):
        cap = None
        try:
            for _attempt in range(3):   # 刚插入/被其他程序短暂占用时重试
                cap = self._open()
                if cap is not None and cap.isOpened():
                    break
                if cap is not None:
                    cap.release()
                cap = None
                if not self._running:
                    return
                time.sleep(0.5)
            if cap is None or not cap.isOpened():
                self.error_occurred.emit(tr(
                    "无法打开 UVC 摄像头 {}（可能被其他程序占用，"
                    "或刚插入未就绪）", self._dev.label))
                return
            while self._running:
                ok, frame = cap.read()
                if not ok:
                    if self._running:   # 非主动停止 → 设备断开
                        self.error_occurred.emit(tr("UVC 摄像头 {} 已断开",
                                                    self._dev.label))
                    break
                self.frames_ready.emit(frame)
        except Exception as exc:        # 摄像头驱动异常不拖垮窗口
            self.error_occurred.emit(str(exc))
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass


class LiteUploadManager(UploadManager):
    """上传管理器 lite 版 —— 仅补 ffmpeg 解析。

    生产实现只认 conda/PATH（客户 Windows 机通常两者皆无 → 预压缩
    静默跳过）；lite 优先用 imageio-ffmpeg 静态二进制。
    """

    def _find_working_ffmpeg(self) -> Optional[str]:
        try:
            return list_working_ffmpegs()[0]
        except IndexError:
            return None


class LiteWindow(QMainWindow):
    """极简采集主窗口。

    构造注入点（与 MainWindow 同口径，供离线测试替身）:
        LiteWindow(pipeline=None, upload_manager=None, d435_worker_cls=None,
                   uvc_capture=None)
    """

    _scan_result = pyqtSignal(list, list, list, list)   # (d435, usb, ble, uvc)

    def __init__(self, pipeline: Optional[CameraPipeline] = None,
                 upload_manager: Optional[LiteUploadManager] = None,
                 d435_worker_cls=None, uvc_capture=None):
        # uvc_capture: 测试注入的 cv2.VideoCapture 替身（真实环境为 None）
        super().__init__()
        self.setWindowTitle(tr("DAQ 极简采集 (lite v{})", settings.APP_VERSION))

        # db 必须先于 UploadManager 任何操作（add_task 写 upload_task 表）
        db.init_schema()
        self._pipeline = pipeline or CameraPipeline()
        self._upload_manager = upload_manager or LiteUploadManager(
            settings.load_server_url())
        self._d435_worker_cls = d435_worker_cls or D435Worker
        self._d435_manager = D435DeviceManager(self._pipeline)
        self._uvc_capture = uvc_capture

        # 设备注册表: dev.key → d435 entry / {"kind": glove, "pump": …,
        # "engine": …, "role": …}
        self._workers: dict = {}
        # 预览 ring: slot_id → 最近显示帧（last-write-wins）
        self._ring: dict = {}
        # 上传任务 → (session_path, episode_index)（task_completed 时取回）
        self._upload_task_map: dict = {}
        self._scan_busy = False
        self._last_session_path = ""

        self._build_ui()
        self._wire_pipeline()
        self._wire_upload()

        self._d435_manager.frames_ready.connect(self._on_d435_frames)
        self._d435_manager.log.connect(self._log)
        self._scan_result.connect(self._on_scan_result)

        # 设备扫描（2s 轮询，后台线程；不走 detect_devices —— 它附带
        # S80M 等多余枚举。UVC 单独走 _list_uvc_devices：Linux 用 sysfs
        # 零依赖；Windows 无 pygrabber 时自动退化 DShow 索引探测）
        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._rescan)
        self._scan_timer.start(settings.DEVICE_POLL_INTERVAL_MS)
        self._rescan()

        self._upload_manager.start()
        self._log(tr("极简采集版就绪。请连接设备并开启。"))

    # ── 界面构建 ──────────────────────────────────────

    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ── 顶部: 服务器工具行 ──
        srv_row = QHBoxLayout()
        srv_row.addWidget(QLabel(tr("服务器:")))
        self._url_edit = QLineEdit(settings.load_server_url())
        self._url_edit.editingFinished.connect(self._on_url_changed)
        srv_row.addWidget(self._url_edit, 1)
        self._btn_test = QPushButton(tr("测试连接"))
        self._btn_test.clicked.connect(self._test_connection)
        srv_row.addWidget(self._btn_test)
        self._auto_cb = QCheckBox(tr("自动上传"))
        self._auto_cb.setChecked(settings.UPLOAD_AUTO_SYNC)
        self._auto_cb.toggled.connect(self._on_auto_toggled)
        srv_row.addWidget(self._auto_cb)
        self._delete_cb = QCheckBox(tr("上传后删除"))
        self._delete_cb.setChecked(settings.UPLOAD_DELETE_AFTER)
        self._delete_cb.toggled.connect(self._on_delete_toggled)
        srv_row.addWidget(self._delete_cb)
        root.addLayout(srv_row)

        # ── 主体: 左设备列 + 右采集列 ──
        body = QHBoxLayout()
        root.addLayout(body, 1)

        # 左列: 设备组（每类 1 台上限）
        left = QVBoxLayout()
        left.setSpacing(6)
        self._dev_groups: dict = {}
        for kind, title, key_attr in (
                ("d435", tr("D435 深度相机"), "_d435_key"),
                ("uvc", tr("UVC 摄像头"), "_uvc_key"),
                ("usb", tr("USB-C 手套"), "_usb_key"),
                ("ble", tr("BLE 手套"), "_ble_key")):
            grp = QGroupBox(title)
            gv = QVBoxLayout(grp)
            combo = QComboBox()
            gv.addWidget(combo)
            btn = QPushButton(tr("开启"))
            btn.clicked.connect(lambda _=False, k=kind: self._toggle_group(k))
            gv.addWidget(btn)
            left.addWidget(grp)
            self._dev_groups[kind] = {"combo": combo, "btn": btn}
            setattr(self, key_attr, "")   # 当前已开启设备的 key（空=未开）
        self._btn_rescan = QPushButton(tr("重新扫描"))
        self._btn_rescan.clicked.connect(self._rescan)
        left.addWidget(self._btn_rescan)
        left.addStretch(1)
        body.addLayout(left, 0)

        # 右列: 预览 + 录制 + 上传 + 日志
        right = QVBoxLayout()
        right.setSpacing(6)

        prev_row = QHBoxLayout()
        self._preview = LitePreview(frame_provider=self._preview_frame)
        prev_row.addWidget(self._preview, 1)
        prev_side = QVBoxLayout()
        prev_side.addWidget(QLabel(tr("预览源:")))
        self._src_combo = QComboBox()
        self._src_combo.addItem(tr("RGB"), "d435_rgb")
        self._src_combo.addItem(tr("深度热力图"), "d435_depth")
        self._src_combo.addItem(tr("UVC RGB"), LITE_UVC_SLOT)
        prev_side.addWidget(self._src_combo)
        prev_side.addStretch(1)
        prev_row.addLayout(prev_side)
        right.addLayout(prev_row)

        rec_row = QHBoxLayout()
        rec_row.addWidget(QLabel(tr("任务名:")))
        self._task_edit = QLineEdit()
        self._task_edit.setPlaceholderText(tr("任务名（可选）"))
        rec_row.addWidget(self._task_edit, 1)
        self._btn_start = QPushButton(tr("▶ 开始"))
        self._btn_start.setObjectName("btnStart")
        self._btn_start.clicked.connect(self._start_recording)
        rec_row.addWidget(self._btn_start)
        self._btn_stop = QPushButton(tr("■ 停止"))
        self._btn_stop.setObjectName("btnStop")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop_recording)
        rec_row.addWidget(self._btn_stop)
        self._btn_abort = QPushButton(tr("⛔ 丢弃"))
        self._btn_abort.setObjectName("btnAbort")
        self._btn_abort.setEnabled(False)
        self._btn_abort.clicked.connect(self._abort_recording)
        rec_row.addWidget(self._btn_abort)
        self._rec_status = QLabel(tr("待机"))
        rec_row.addWidget(self._rec_status)
        right.addLayout(rec_row)

        up_row = QHBoxLayout()
        self._upload_list = QListWidget()
        self._upload_list.setMaximumHeight(110)
        up_row.addWidget(self._upload_list, 1)
        up_btns = QVBoxLayout()
        b_up_sel = QPushButton(tr("上传选中"))
        b_up_sel.clicked.connect(self._upload_selected)
        up_btns.addWidget(b_up_sel)
        b_up_all = QPushButton(tr("上传全部未上传"))
        b_up_all.clicked.connect(self._upload_all_pending)
        up_btns.addWidget(b_up_all)
        up_btns.addStretch(1)
        up_row.addLayout(up_btns)
        right.addLayout(up_row)

        self._upload_progress = QProgressBar()
        self._upload_progress.setRange(0, 100)
        self._upload_progress.setValue(0)
        right.addWidget(self._upload_progress)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumHeight(150)
        right.addWidget(self._log_view)
        body.addLayout(right, 1)

        self.resize(980, 640)
        self._refresh_upload_list()

    def _wire_pipeline(self):
        p = self._pipeline
        p.recording_started.connect(self._on_recording_started)
        p.recording_finished.connect(self._on_recording_finished)
        p.recording_aborted.connect(self._on_recording_aborted)
        p.duration_changed.connect(self._on_duration)
        p.recording_log.connect(self._log)
        p.error_occurred.connect(
            lambda sid, msg: self._log(tr("[错误] {}", msg)))

    def _wire_upload(self):
        u = self._upload_manager
        u.task_added.connect(
            lambda tid: self._log(tr("☁ 上传已入队: {}", tid)))
        u.task_started.connect(
            lambda tid: self._log(tr("☁ 开始上传: {}", tid)))
        u.task_completed.connect(self._on_upload_done)
        u.task_failed.connect(self._on_upload_failed)
        u.task_progress.connect(self._on_upload_progress)
        u.all_completed.connect(self._refresh_upload_list)

    # ── 日志 ──────────────────────────────────────────

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._log_view.append(f"[{ts}] {msg}")

    # ── 设备扫描 ──────────────────────────────────────

    def _rescan(self):
        if self._scan_busy:
            return
        self._scan_busy = True
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            d435 = _list_d435_devices()
            usb = _list_usb_glove_devices()
            ble = _list_ble_devices()
            uvc = _list_uvc_devices()
            self._scan_result.emit(d435, usb, ble, uvc)
        finally:
            self._scan_busy = False

    def _on_scan_result(self, d435: List[DeviceInfo],
                        usb: List[DeviceInfo], ble: List[DeviceInfo],
                        uvc: List[DeviceInfo]):
        """扫描结果回填设备下拉框（保留当前选中项）。"""
        for kind, devs in (("d435", d435), ("usb", usb), ("ble", ble),
                           ("uvc", uvc)):
            grp = self._dev_groups[kind]
            combo = grp["combo"]
            prev_key = combo.currentData().key if combo.currentData() else ""
            combo.clear()
            for dev in devs:
                label = dev.label or dev.display_name
                if dev.serial:
                    label = f"{label} ({dev.serial})"
                combo.addItem(label, dev)
            if prev_key:
                for i in range(combo.count()):
                    if combo.itemData(i).key == prev_key:
                        combo.setCurrentIndex(i)
                        break
            grp["btn"].setText(
                tr("关闭") if getattr(self, self._open_key_attr(kind))
                else tr("开启"))

    def _open_key_attr(self, kind: str) -> str:
        return {"d435": "_d435_key", "uvc": "_uvc_key",
                "usb": "_usb_key", "ble": "_ble_key"}[kind]

    # ── 设备开关 ──────────────────────────────────────

    def _toggle_group(self, kind: str):
        attr = self._open_key_attr(kind)
        if getattr(self, attr):
            self._close_device(kind)
            return
        grp = self._dev_groups[kind]
        dev = grp["combo"].currentData()
        if not dev:
            QMessageBox.information(self, tr("提示"),
                                    tr("列表中没有设备。请检查连接后点「重新扫描」。"))
            return
        if self._pipeline.is_recording:
            QMessageBox.warning(self, tr("无法操作"),
                                tr("录制中不能开关设备。"))
            return
        ok = {"d435": self._open_d435,
              "uvc": self._open_uvc,
              "usb": self._open_usb_glove,
              "ble": self._open_ble_glove}[kind](dev)
        if ok:
            grp["btn"].setText(tr("关闭"))
            self._log(tr("[设备] 已开启 {}", dev.label))

    def _close_device(self, kind: str):
        attr = self._open_key_attr(kind)
        key = getattr(self, attr)
        entry = self._workers.get(key)
        if not entry:
            setattr(self, attr, "")
            return
        if kind == "d435":
            self._d435_manager.close(entry)
            self._pipeline.unregister_external_source(entry["rgb_slot"])
            self._pipeline.clear_depth_camera(entry["depth_slot"])
            self._pipeline.clear_device_calibration(key)
            for sid in entry["slots"]:
                self._ring.pop(sid, None)
            for sid in entry["slots"]:
                self._recover_preview_source(sid)
        elif kind == "uvc":
            pump = entry["pump"]
            try:
                pump.stop()
            except Exception:
                pass
            self._pipeline.unregister_external_source(entry["slots"][0])
            self._ring.pop(entry["slots"][0], None)
            self._recover_preview_source(entry["slots"][0])
        else:
            pump = entry["pump"]
            try:
                pump.stop()
            except Exception:
                pass
            self._pipeline.unregister_sensor(entry["role"])
            if kind == "ble":
                set_ble_scan_suppressed(False)
        self._workers.pop(key, None)
        setattr(self, attr, "")
        self._dev_groups[kind]["btn"].setText(tr("开启"))
        self._log(tr("[设备] 已关闭 {}", entry.get("label") or key))

    # ── D435 ──────────────────────────────────────────

    def _open_d435(self, dev: DeviceInfo) -> bool:
        """开启 D435（精简版 main_window._open_d435：固定槽名、无多机、
        无曝光、热力图 EMA 关闭）。测试注入 fake worker 类时跳过硬件
        复查。成功即记 _d435_key（_toggle_group 与直接调用口径一致）。"""
        if dev.key in self._workers:
            self._d435_key = dev.key
            return True   # 已开启（幂等）
        # 面板条目来自 2s 轮询，点击瞬间设备可能已被拔走 → 按 serial 复查
        if self._d435_worker_cls is D435Worker:
            live = {s for _, s in (list_d400_devices() or [])}
            if not live or dev.serial not in live:
                self._log(tr("[错误] 未检测到 RealSense 设备"))
                return False

        prof = settings.realsense_profile(dev.display_name)
        rgb_slot, depth_slot = settings.D435_SLOT_RGB, settings.D435_SLOT_DEPTH
        fps = prof["fps"]
        rgb_h, rgb_w = prof["rgb_resolution"][1], prof["rgb_resolution"][0]
        depth_h, depth_w = (prof["depth_resolution"][1],
                            prof["depth_resolution"][0])
        near_mm, far_mm = prof["depth_near_mm"], prof["depth_far_mm"]
        smooth_k = prof.get("heatmap_smooth_k", 0)

        self._log(tr("正在启动深度双目摄像机 ({})…", f"{dev.label} {dev.serial}"))
        # RGB 注册为外部帧源（录制时写入 videos/d435_rgb/ MP4）
        self._pipeline.register_external_source(rgb_slot, (rgb_h, rgb_w),
                                                fps=fps)
        # 深度伪相机：热力图 EMA 关闭（temporal_alpha=0 → 不建
        # DepthHeatmapSmoother，省显示开销；存储码值不受影响）
        self._pipeline.set_depth_camera(depth_slot, (depth_h, depth_w),
                                        fps=fps, master_slot=rgb_slot,
                                        heatmap_near_mm=near_mm,
                                        heatmap_far_mm=far_mm,
                                        heatmap_smooth_k=smooth_k,
                                        heatmap_temporal_alpha=0.0)
        entry = self._d435_manager.new_entry(
            dev.label, dev.serial, rgb_slot, depth_slot,
            near_mm, far_mm, smooth_k, 0.0)
        self._workers[dev.key] = entry
        self._d435_manager.spawn(dev.key, entry, dev.display_name, prof,
                                 exposure=None, worker_cls=self._d435_worker_cls)
        self._d435_key = dev.key
        self._prefer_preview_source(rgb_slot)
        self._log(tr("深度双目摄像机已启动: {}", f"{dev.label} {dev.serial}"))
        return True

    def _on_d435_frames(self, slot_id: str, frame: np.ndarray,
                        hardware_ns: int = 0, imu_samples: list = None,
                        dev_key: str = None):
        """D435 帧信号（主线程执行）：帧处理口径（calib 首帧注入/热力图/
        录制写入）全在 d435_manager，lite 只把显示帧丢预览 ring。"""
        entry = self._workers.get(dev_key) if dev_key else None
        if not entry or entry.get("kind") != "d435":
            return
        display, _is_depth = self._d435_manager.process_frame(
            entry, slot_id, frame, hardware_ns, dev_key)
        self._ring[slot_id] = display   # last-write-wins，防积压

    def _preview_frame(self):
        """预览源当前显示帧（LitePreview 15fps 刷新时取用）。"""
        src = self._src_combo.currentData()
        return self._ring.get(src)

    def _prefer_preview_source(self, slot: str):
        """当前预览源无画面时切到 slot（开哪台看哪台）。"""
        if self._ring.get(self._src_combo.currentData()) is None:
            idx = self._src_combo.findData(slot)
            if idx >= 0:
                self._src_combo.setCurrentIndex(idx)

    def _recover_preview_source(self, popped_slot: str):
        """关闭的正是当前预览源 → 切到仍有画面的源。"""
        if self._src_combo.currentData() != popped_slot:
            return
        for i in range(self._src_combo.count()):
            s = self._src_combo.itemData(i)
            if s != popped_slot and self._ring.get(s) is not None:
                self._src_combo.setCurrentIndex(i)
                return

    # ── UVC ──────────────────────────────────────────

    def _open_uvc(self, dev: DeviceInfo) -> bool:
        """开启 UVC 摄像头（固定槽 uvc_rgb，1 台上限）。
        成功即记 _uvc_key（与 _toggle_group 口径一致）。"""
        if dev.key in self._workers:
            self._uvc_key = dev.key
            return True
        self._pipeline.register_external_source(
            LITE_UVC_SLOT, (LITE_UVC_H, LITE_UVC_W), fps=LITE_UVC_FPS)
        pump = LiteUvcPump(dev, capture=self._uvc_capture)
        pump.frames_ready.connect(
            lambda frame: self._on_uvc_frame(LITE_UVC_SLOT, frame))
        pump.error_occurred.connect(
            lambda msg: self._log(tr("[UVC错误] {}", msg)))
        self._workers[dev.key] = {"kind": "uvc", "label": dev.label,
                                  "serial": dev.serial or "",
                                  "slots": [LITE_UVC_SLOT], "pump": pump}
        self._uvc_key = dev.key
        self._prefer_preview_source(LITE_UVC_SLOT)
        pump.start()
        self._log(tr("[UVC] 已开启 {} → 槽 {}", dev.label, LITE_UVC_SLOT))
        return True

    def _on_uvc_frame(self, slot_id: str, frame: np.ndarray):
        """UVC 帧信号（主线程执行）：预览 ring + 录制写帧。"""
        self._ring[slot_id] = frame   # last-write-wins，防积压
        if self._pipeline.is_recording:
            self._pipeline.write_external_frame(slot_id, frame, hardware_ns=0)

    # ── 手套 ──────────────────────────────────────────

    def _open_ble_glove(self, dev: DeviceInfo) -> bool:
        """开启 BLE 手套 → 分配传感器列 + 注册 + 数据泵。
        成功即记 _ble_key（与 _toggle_group 口径一致）。"""
        if dev.key in self._workers:
            self._ble_key = dev.key
            return True
        prefer = {"l": "left_glove", "r": "right_glove"}.get(
            (dev.display_name or "").strip().lower(), "")
        role = settings.assign_glove_sensor_role(dev.key, prefer)
        self._pipeline.register_sensor(role)
        engine = SensorBLEEngine()
        pump = LiteGlovePump(role, engine, on_log=self._log)
        pump.set_pipeline(self._pipeline)
        pump.start(dev.address)
        set_ble_scan_suppressed(True)   # 手套连接中防扫描挤占数据吞吐
        self._workers[dev.key] = {"kind": "ble_glove", "label": dev.label,
                                  "pump": pump, "engine": engine, "role": role}
        self._ble_key = dev.key
        self._log(tr("[手套] BLE {} → 传感器列 {}", dev.label, role))
        return True

    def _open_usb_glove(self, dev: DeviceInfo) -> bool:
        """开启 USB-C 手套（串口引擎，触觉 + IMU；无骨架解算）。
        成功即记 _usb_key（与 _toggle_group 口径一致）。"""
        if dev.key in self._workers:
            self._usb_key = dev.key
            return True
        if not _USB_GLOVE_AVAILABLE:
            self._log(tr("[错误] USB 手套依赖缺失（pyserial）"))
            return False
        prefer = usb_glove_prefer_side(dev.serial or "")
        role = settings.assign_glove_sensor_role(dev.key, prefer)
        self._pipeline.register_sensor(role)
        engine = UsbGloveEngine(dev.address)
        pump = LiteGlovePump(role, engine, on_log=self._log)
        pump.set_pipeline(self._pipeline)
        pump.start(dev.address)
        self._workers[dev.key] = {"kind": "usb_glove", "label": dev.label,
                                  "pump": pump, "engine": engine, "role": role}
        self._usb_key = dev.key
        self._log(tr("[USB手套] {} → 传感器列 {}",
                     dev.serial or dev.label, role))
        return True

    # ── 录制控制 ──────────────────────────────────────

    def _device_meta(self) -> List[dict]:
        """当前已开设备的录制元数据（写入 info.json devices 段）。"""
        meta = []
        for key, e in self._workers.items():
            if e.get("kind") == "d435":
                meta.append({"key": key, "kind": "d435", "name": e["label"],
                             "serial": e["serial"], "slots": e["slots"]})
            elif e.get("kind") == "uvc":
                meta.append({"key": key, "kind": "uvc", "name": e["label"],
                             "serial": e.get("serial", ""),
                             "slots": e["slots"]})
            else:
                meta.append({"key": key, "kind": e["kind"],
                             "name": e["label"], "serial": "",
                             "slots": [e["role"]]})
        return meta

    def _start_recording(self):
        if self._pipeline.is_recording:
            return
        # pipeline.py:445 无源守卫 —— 无视频源无法录制，前置提示
        # （D435 优先；只开 UVC 时 UVC 作主视频源）
        if not self._d435_key and not self._uvc_key:
            QMessageBox.warning(
                self, tr("无法录制"),
                tr("请先开启 D435 深度相机或 UVC 摄像头。\n"
                   "录制必须有视频源。"))
            return
        master = (settings.D435_SLOT_RGB if self._d435_key
                  else LITE_UVC_SLOT)
        self._pipeline.start_recording(
            master,
            task_name=self._task_edit.text().strip(),
            batch_index=0,
            device_meta=self._device_meta())

    def _stop_recording(self):
        if self._pipeline.is_recording:
            self._pipeline.finish_recording("")

    def _abort_recording(self):
        if self._pipeline.is_recording:
            self._pipeline.abort_recording("")

    def _on_recording_started(self, slot_id: str):
        self._rec_status.setText(tr("⏺ 录制中"))
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_abort.setEnabled(True)
        self._set_device_buttons_enabled(False)

    def _on_recording_finished(self, slot_id: str, session_path: str):
        self._rec_status.setText(tr("待机"))
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_abort.setEnabled(False)
        self._set_device_buttons_enabled(True)
        self._last_session_path = session_path
        self._refresh_upload_list()

        episode_index = getattr(self._pipeline, "last_episode_index", 0) or 0
        frames = sum(self._pipeline.last_recording_frames.values())
        drops = self._pipeline.last_drop_stats
        total_drops = sum(v for k, v in drops.items() if k != "imu_overflow")
        self._log(tr("[录制] ■ 完成: {}（{} 帧 / 丢帧 {} / episode {}）",
                     os.path.basename(session_path) if session_path else "-",
                     frames, total_drops, episode_index))

        # ── 自动上传（对照 main_window._on_recording_finished 口径）──
        if session_path and settings.UPLOAD_ENABLED:
            if not settings.UPLOAD_AUTO_SYNC:
                self._log(tr("☁ 自动上传未开启，请手动上传: {}",
                             os.path.basename(session_path)))
            elif UploadManager.get_upload_status(
                    session_path, episode_index) != "completed":
                self._upload_manager.project_id = \
                    settings.load_upload_project_id()
                task_id = self._upload_manager.add_task(
                    session_path, episode_index=episode_index)
                self._upload_task_map[task_id] = (session_path, episode_index)
                self._log(tr("☁ 已自动加入上传队列: {}",
                             os.path.basename(session_path)))

    def _on_recording_aborted(self, slot_id: str):
        self._rec_status.setText(tr("待机"))
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_abort.setEnabled(False)
        self._set_device_buttons_enabled(True)
        self._log(tr("[录制] ⛔ 已丢弃本次录制"))

    def _on_duration(self, slot_id: str, seconds: float):
        self._rec_status.setText(tr("⏺ 录制中 {}", format_duration(seconds)))

    def _set_device_buttons_enabled(self, on: bool):
        for grp in self._dev_groups.values():
            grp["btn"].setEnabled(on)
        self._btn_rescan.setEnabled(on)

    # ── 服务器 / 上传 ─────────────────────────────────

    def _on_url_changed(self):
        url = self._url_edit.text().strip()
        settings.save_server_url(url)          # merge-write，与主程序共用
        self._upload_manager.server_url = url

    def _test_connection(self):
        url = self._url_edit.text().strip() or settings.load_server_url()
        ok = APIClient(url).health_check()
        self._log(tr("☁ 服务器连接{}: {}", tr("成功") if ok else tr("失败"),
                     url))

    def _on_auto_toggled(self, on: bool):
        settings.save_upload_auto_sync(bool(on))
        settings.UPLOAD_AUTO_SYNC = bool(on)   # 运行时立即生效

    def _on_delete_toggled(self, on: bool):
        settings.save_upload_delete_after(bool(on))
        settings.UPLOAD_DELETE_AFTER = bool(on)

    def _episode_max(self, task_dir: str) -> int:
        """任务目录内已录制的最大 episode 号（0 = 无 parquet，按整目录上传）。"""
        best = 0
        for p in glob.glob(os.path.join(task_dir, "data", "chunk-*",
                                        "episode-*.parquet")):
            m = re.search(r"episode-(\d+)\.parquet$", os.path.basename(p))
            if m:
                best = max(best, int(m.group(1)))
        return best

    def _refresh_upload_list(self):
        """待上传列表 = data/recordings 下的任务目录 + 上传状态。"""
        self._upload_list.clear()
        rec_dir = settings.RECORDING_DIR
        if not os.path.isdir(rec_dir):
            return
        for name in sorted(os.listdir(rec_dir)):
            path = os.path.join(rec_dir, name)
            if not os.path.isdir(path):
                continue
            ep = self._episode_max(path)
            status = UploadManager.get_upload_status(path, ep)
            status_cn = {"pending": tr("未上传"), "uploading": tr("上传中"),
                         "completed": tr("已上传"), "failed": tr("失败")}
            item = QListWidgetItem(
                tr("{} —— {} (ep {})", name,
                   status_cn.get(status, status), ep))
            item.setData(Qt.UserRole, (path, ep))
            self._upload_list.addItem(item)

    def _upload_selected(self):
        item = self._upload_list.currentItem()
        if not item:
            return
        path, ep = item.data(Qt.UserRole)
        self._enqueue_uploads([(path, ep)])

    def _upload_all_pending(self):
        items = []
        for i in range(self._upload_list.count()):
            path, ep = self._upload_list.item(i).data(Qt.UserRole)
            if UploadManager.get_upload_status(path, ep) != "completed":
                items.append((path, ep))
        self._enqueue_uploads(items)

    def _enqueue_uploads(self, items: list):
        if not items:
            self._log(tr("☁ 没有待上传的录制"))
            return
        self._upload_manager.project_id = settings.load_upload_project_id()
        ids = self._upload_manager.add_tasks(items)
        for i, (path, ep) in enumerate(items):
            self._upload_task_map[ids[i]] = (path, ep)
        self._log(tr("☁ 已加入上传队列 {} 项", len(ids)))
        self._refresh_upload_list()

    def _on_upload_progress(self, task_id: str, progress: float):
        # 串行上传（UPLOAD_MAX_CONCURRENT=1），进度条直接映射当前任务
        self._upload_progress.setValue(int(progress * 100))

    def _on_upload_done(self, task_id: str):
        pair = self._upload_task_map.pop(task_id, ("", 0))
        path, ep = pair
        name = os.path.basename(path) if path else task_id
        if ep > 0:
            name = f"{name} (ep {ep})"
        self._log(tr("☁ 上传完成: {}", name))
        if path and settings.UPLOAD_DELETE_AFTER:
            threading.Thread(target=self._delete_after_upload,
                             args=(path, ep), daemon=True).start()
        else:
            self._refresh_upload_list()
        self._upload_progress.setValue(0)

    def _delete_after_upload(self, session_path: str, episode_index: int):
        """后台删除本地会话（ep>0 只删该 episode 文件组，否则整目录）。"""
        if episode_index > 0:
            delete_pooled_episode(session_path, episode_index)
        elif os.path.isdir(session_path):
            shutil.rmtree(session_path, ignore_errors=True)

    def _on_upload_failed(self, task_id: str, error: str):
        pair = self._upload_task_map.pop(task_id, ("", 0))
        name = os.path.basename(pair[0]) if pair[0] else task_id
        self._log(tr("☁ 上传失败: {} — {}", name, error))
        self._upload_progress.setValue(0)
        self._refresh_upload_list()

    # ── 生命周期 ──────────────────────────────────────

    def closeEvent(self, event):
        self._scan_timer.stop()
        for kind in ("d435", "uvc", "usb", "ble"):
            attr = self._open_key_attr(kind)
            if getattr(self, attr):
                try:
                    self._close_device(kind)
                except Exception:
                    pass
        try:
            self._upload_manager.stop()
        except Exception:
            pass
        event.accept()
