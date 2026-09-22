"""手套传感器控件 —— 触觉分区网格画面，直接嵌入主网格（统一设备体系）。

替代旧底部传感器 dock：面板开关打开手套 → 主网格出现 16×16 分区网格
渲染（厂商 PressureMatrixCanvas 移植，与回放页/查看器同一实现），
录制时数据经 pipeline.write_sensor 写入 parquet 对应传感器列。
"""

from __future__ import annotations
from typing import Optional
import time

import numpy as np
from PyQt5.QtCore import QObject, QTimer

from config.i18n import tr
from core.ble_engine import SensorBLEEngine
from core.glove_keypoint_solver import GloveKeypointSolver
from core.render_engine import (
    render_skeleton, fit_skeleton_dist, render_tactile_grid, glove_side_of,
)
from ui.camera_widget import CameraWidget


class GloveWidget(CameraWidget):
    """触觉分区网格实时画面（固定 tactile 渲染模式，复用 CameraWidget 覆盖条）。"""

    # 固定渲染画布尺寸（与旧仿生手掌面板一致；网格几何按此等比自适应）
    _RENDER_W = 1280
    _RENDER_H = 720

    def __init__(self, slot_id: str, address: str, sensor_column: str,
                 label: str = "", parent=None, engine=None, on_log=None):
        super().__init__(slot_id, label or sensor_column, parent)
        self.address = address
        self.sensor_column = sensor_column

        # engine 注入：None → BLE 手套引擎（默认）；USB 手套传
        # UsbGloveEngine（信号/属性口径一致，见 core.usb_glove_engine）
        self._engine: Optional[SensorBLEEngine] = engine
        self._running = False
        self._pipeline = None

        # 骨架叠加（USB 手套）：连接时建解算器，渲染循环内解算 + 小窗叠加
        self._solver = None          # GloveKeypointSolver（BLE 手套恒 None）
        self._kpts = None            # 最近一帧关键点 (21,3) float32
        self._skel_dist = 0.0        # 骨架相机距离 EMA（防抖）
        self._on_log = on_log or (lambda msg: None)   # 主窗口日志（USB 路径传）
        self._fps_logged = False     # 硬件帧率只报一次（与 GloveDataPump 同口径）

        # 左右手区分：网格布局按传感器列名判手（glove_side_of）。两只手**共用
        # 一套坐标**（拇指恒朝左，谁都不翻）—— 固件的左手帧是规范系的
        # `[::-1, ::-1].T`，由 canonical_pressure_matrix 在取整前归位；别再在这
        # 儿加翻转（2026-09-21 那次左手整体转 90° 就是"左手=右手整块镜像"这个
        # 假设造的，契约见 core/render_engine.canonical_pressure_matrix）。
        # 仿生手掌那套 config 映射（左/右不同配置文件）随 render_hand 一起
        # 只留在回放页的模式下拉里。
        self.side = glove_side_of(sensor_column)

        # 渲染定时器（30ms ≈ 30fps；未连接时 tick 直接返回）
        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._render_tick)
        self._render_timer.start(30)

    # ── 连接控制 ──────────────────────────────────────

    def start(self, address: str = ""):
        """连接手套设备并开始渲染（BLE / USB 引擎共用同一启动路径）。"""
        if address:
            self.address = address
        self.video_widget.set_status_text(tr("连接中…"))
        if self._engine is None:
            self._engine = SensorBLEEngine()
        self._engine.connected.connect(self._on_connected)
        self._engine.disconnected.connect(self._on_disconnected)
        self._engine.fps_updated.connect(self._on_fps)
        self._engine.calibration_progress.connect(self._on_calib_progress)
        self._engine.error_occurred.connect(self._on_error)
        self._engine.connect_device(self.address)

    def stop(self):
        """断开连接、停止渲染。"""
        if self._engine:
            self._engine.disconnect()
        self._running = False
        self.video_widget.set_status_text(tr("已断开"))

    def set_pipeline(self, pipeline):
        """由主窗口注入/清除当前录制管线引用。"""
        self._pipeline = pipeline

    # ── 引擎信号 ──────────────────────────────────────

    def _on_connected(self, addr: str):
        self._running = True
        self.video_widget.set_status_text(tr("已连接: {}…", addr[:12]))
        self._on_log(tr("[手套] {} 已连接: {}", self.sensor_column,
                        str(addr)[:16]))
        if self._pipeline:
            self._pipeline.record_event(self.sensor_column, "connected")
        # USB 手套：连接时创建骨架解算器（scipy 首次导入 ~1s，别拖到录制中途）
        if hasattr(self._engine, "latest_imu") and self._solver is None:
            side = self.sensor_column.split("_")[0]
            self._solver = GloveKeypointSolver(
                side,
                on_error=lambda msg: self._on_log(tr("[手套] {}", msg)),
                on_warmup=lambda: self._on_log(
                    tr("[手套] {} 骨架解算就绪", self.sensor_column)))
            if self._solver.available():
                self._on_log(tr("[手套] {} 骨架解算已启用（标定: {}）",
                                self.sensor_column,
                                self._solver.calibration_name))

    def _on_disconnected(self):
        self._running = False
        self.video_widget.set_status_text(tr("已断开"))
        self._on_log(tr("[手套] {} 已断开", self.sensor_column))
        if self._pipeline:
            self._pipeline.record_event(self.sensor_column, "disconnected")

    def _on_error(self, msg: str):
        """引擎错误显示到画面状态栏（失败原因不再只有控制台可见）。"""
        self.video_widget.set_status_text(tr("⚠ {}", msg))
        self._on_log(tr("[手套] {} 错误: {}", self.sensor_column, msg))

    def _on_fps(self, fps: float):
        self.fps_label.setText(f"HW: {fps:.0f}")
        if not self._fps_logged:
            self._fps_logged = True
            self._on_log(tr("[手套] {} 硬件帧率: {:.0f} fps",
                            self.sensor_column, fps))

    def _on_calib_progress(self, progress: int):
        self.video_widget.set_status_text(
            tr("校准完成") if progress >= 100 else tr("校准中… {}%", progress))
        if progress >= 100:
            self._on_log(tr("[手套] {} 校准完成", self.sensor_column))

    # ── 渲染循环（仿生手掌，固定模式） ──────────────────

    def _render_tick(self):
        """定时器触发：处理一帧并更新画面（逻辑平移自 SensorPanel）。"""
        if self._engine is None or not self._running:
            return

        processed, _max_signal = self._engine.process_frame()

        if processed is None:
            if self._engine.is_calibrating:
                self.video_widget.set_status_text(tr("校准中…"))
            return

        # USB 手套附带 IMU 四元数（BLE 引擎无 latest_imu，hasattr 跳过）：
        # 录制与否都解算骨架（画面小窗叠加用）
        imu = None
        if hasattr(self._engine, "latest_imu"):
            imu = self._engine.latest_imu()
            if imu is not None:
                quats, valid, ts_us = imu
                self._kpts = self._solve_keypoints(quats, valid, ts_us)

        # 写入共享录制会话（→ parquet observation.<sensor_column>）
        if self._pipeline is not None and self._pipeline.is_recording:
            capture_ts = self._engine.latest_data_ts_us
            self._pipeline.write_sensor(processed, capture_ts,
                                        sensor_name=self.sensor_column)
            if imu is not None:
                quats, valid, _ts_us = imu
                self._pipeline.write_glove_imu(self.sensor_column,
                                               quats, valid)
                if self._kpts is not None:
                    self._pipeline.write_glove_keypoints(
                        self.sensor_column, self._kpts)

        try:
            # baseline=None + use_baseline=True 是刻意的，两件事各取所需：
            #   · 数值 —— 实时矩阵在引擎里已经扣过标定基线
            #     （ble/usb 引擎都做 max(0, smoothed - baseline_map)），
            #     这里再扣一次就重复了，所以 baseline 传 None；
            #   · 配色 —— 色带档位只看 use_baseline（校正档 333/666/999/
            #     1333/1666 vs 原始档 2000/2400/2800/3200/3600），实时值域
            #     就是校正后的，要的正是校正档。
            # 返回的是**共享面板画布本身**（同尺寸复用 + 逐格脏重画），
            # 下面还要往上叠骨架小窗和覆盖条 —— 先拷一份，别把覆盖物
            # 烙进缓存画布（下一帧同格不算脏、不会被重画，会一直留着）。
            frame = render_tactile_grid(
                processed, side=self.side, baseline=None, use_baseline=True,
                w=self._RENDER_W, h=self._RENDER_H,
            ).copy()
        except Exception:
            import traceback
            print(f"[{self.sensor_column}] render error:")
            traceback.print_exc()
            return

        self._overlay_skeleton(frame)
        self._display_frame(frame)

    # ── 骨架叠加（USB 手套：IMU → HandSolver → 小窗透视画面） ──

    # 骨架小窗：1280×720 画布右下空白区（换触觉网格后左下角不再空 —— 网格
    # 占 x 54..698 / y 54..698，图例文字在 x 716..~890 / y 68..338，右下
    # x 930..1270 / y 400..700 是唯一放得下 340×300 的位置）
    _SKEL_W, _SKEL_H = 340, 300
    _SKEL_POS = (930, 400)

    def _solve_keypoints(self, quats, valid, ts_us):
        """IMU 帧 → 骨架关键点 (21,3)；解算器不可用/未 warmup 返回 None。"""
        solver = self._solver
        if solver is None or not solver.available():
            return None
        return solver.process(quats, valid, ts_us)

    def _overlay_skeleton(self, frame: np.ndarray):
        """把骨架透视小窗贴到仿生手掌画面左下角（无关键点时跳过）。"""
        if self._kpts is None:
            return
        dist = fit_skeleton_dist(self._kpts)
        # 相机距离 EMA 防抖（首帧直接采用）
        self._skel_dist = (dist if self._skel_dist <= 0.0
                           else self._skel_dist * 0.9 + dist * 0.1)
        panel = render_skeleton(self._kpts, w=self._SKEL_W, h=self._SKEL_H,
                                dist=self._skel_dist,
                                label=tr("骨架 {}", self.sensor_column))
        x, y = self._SKEL_POS
        frame[y:y + self._SKEL_H, x:x + self._SKEL_W] = panel

    # 状态条：USB 三行（传感器名 / 帧率+帧龄 / IMU+触觉帧率）画在**面板下方**
    # 的独立带里 —— 面板上边距只有 54px，而三行条高 68px，画在 (0,0) 会切掉
    # 最上一行（y=15 指尖）左起约 6 格的顶部 14px。带高按三行文本留 68px，
    # 面板本身仍是 1280x720（几何与查看器逐像素一致，一点没动）。
    _STATUS_H = 68

    def _display_frame(self, frame: np.ndarray):
        """渲染好的 BGR 帧 → 画面（叠加传感器名 + 数据帧龄诊断）。"""
        import cv2
        now_us = int(time.time() * 1_000_000)
        age_ms = (now_us - self._engine.latest_data_ts_us) / 1000.0
        hw_fps = self._engine.hardware_fps
        # USB 手套：状态条追加 IMU 有效数 + 触觉帧率（BLE 引擎无此属性）
        has_imu = hasattr(self._engine, "imu_present_count")
        if has_imu:
            # 面板 + 下方状态带；整条带子黑底，不与任何格区重叠
            out = np.zeros((frame.shape[0] + self._STATUS_H, frame.shape[1], 3),
                           np.uint8)
            out[:frame.shape[0]] = frame
            top, box_h = frame.shape[0], self._STATUS_H
        else:
            # BLE 两行条只有 52px，够不着网格上边距（54px），保持原位
            out, top, box_h = frame, 0, 52
            cv2.rectangle(out, (0, top), (260, top + box_h), (0, 0, 0), -1)
        cv2.putText(out, self.sensor_column, (6, top + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(out, f'HW: {hw_fps:.0f} fps  Age: {age_ms:.0f} ms',
                    (6, top + 40), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (0, 255, 0) if age_ms < 100 else
                    (0, 255, 255) if age_ms < 300 else (0, 0, 255), 1)
        if has_imu:
            imu_ok = self._engine.imu_present_count
            tac_fps = getattr(self._engine, "tactile_fps", 0.0)
            cv2.putText(out, f'IMU: {imu_ok}/16  Tac: {tac_fps:.0f} fps',
                        (6, top + 60), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (0, 255, 0) if imu_ok >= 16 else (0, 255, 255), 1)
        self.video_widget.set_frame(out)


class GloveDataPump(QObject):
    """无画面手套数据泵 —— 只把数据送进录制管线，不占主网格画面。

    主程序 GUI 不再可视化手套数据（画面统一在
    tools/demos/pooled_viewer_demo 回放查看器）；此泵接管 GloveWidget
    的录制职责：30ms 轮询引擎 process_frame → pipeline.write_sensor
    （16x16 触觉）+ write_glove_imu（16x4 四元数）+ 骨架关键点
    （toolkit HandSolver → write_glove_keypoints，回填
    observation.{left,right}_hand_pose 列），连接/校准/错误状态经
    on_log 回主窗口日志。与 GloveWidget 同一条管线，BLE 手套仍走
    GloveWidget（历史行为不变）。
    """

    _TICK_MS = 30

    def __init__(self, slot_id: str, sensor_column: str, engine,
                 on_log=None, parent=None):
        super().__init__(parent)
        self.slot_id = slot_id
        self.sensor_column = sensor_column
        self._engine = engine
        self._on_log = on_log or (lambda msg: None)
        self._pipeline = None
        self._running = False
        self._fps_logged = False     # 帧率只报一次，避免每秒刷日志
        self._solver = None          # 骨架解算器（连接时创建，工具包缺失降级）
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(self._TICK_MS)

    # ── 连接控制 ──────────────────────────────────────

    def set_pipeline(self, pipeline):
        """由主窗口注入/清除当前录制管线引用。"""
        self._pipeline = pipeline

    def start(self, address: str = ""):
        """连接引擎并开始轮询（信号口径与 GloveWidget.start 一致）。"""
        if self._engine is None:
            return
        self._engine.connected.connect(self._on_connected)
        self._engine.disconnected.connect(self._on_disconnected)
        self._engine.fps_updated.connect(self._on_fps)
        self._engine.calibration_progress.connect(self._on_calib_progress)
        self._engine.error_occurred.connect(self._on_error)
        self._engine.connect_device(address)

    def stop(self):
        """断开连接（与 GloveWidget.stop 同口径，供 _close_glove 复用）。"""
        if self._engine:
            try:
                self._engine.disconnect()
            except Exception:
                pass
        self._running = False

    # ── 引擎信号 → 主窗口日志 ─────────────────────────

    def _on_connected(self, addr: str):
        self._running = True
        self._on_log(tr("[手套] {} 已连接: {}",
                        self.sensor_column, str(addr)[:16]))
        if self._pipeline:
            self._pipeline.record_event(self.sensor_column, "connected")
        # 骨架解算器在连接时创建（scipy 首次导入 ~1s，别拖到录制中途）
        if self._solver is None:
            side = self.sensor_column.split("_")[0]
            self._solver = GloveKeypointSolver(
                side,
                on_error=lambda msg: self._on_log(
                    tr("[手套] {}", msg)),
                on_warmup=lambda: self._on_log(
                    tr("[手套] {} 骨架解算就绪", self.sensor_column)))
            if self._solver.available():
                self._on_log(tr("[手套] {} 骨架解算已启用（标定: {}）",
                                self.sensor_column,
                                self._solver.calibration_name))

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
        """定时器触发：处理一帧并写入录制管线（逻辑平移自
        GloveWidget._render_tick 的录制分支）。"""
        if self._engine is None or not self._running:
            return

        processed, _max_signal = self._engine.process_frame()
        if processed is None:
            return

        if self._pipeline is not None and self._pipeline.is_recording:
            capture_ts = self._engine.latest_data_ts_us
            self._pipeline.write_sensor(processed, capture_ts,
                                        sensor_name=self.sensor_column)
            # USB 手套附带 IMU 四元数列（BLE 引擎无 latest_imu，hasattr 跳过）
            if hasattr(self._engine, "latest_imu"):
                imu = self._engine.latest_imu()
                if imu is not None:
                    quats, valid, ts_us = imu
                    self._pipeline.write_glove_imu(self.sensor_column,
                                                   quats, valid)
                    # 骨架关键点（工具包解算；未 warmup/异常时跳过，
                    # 对应 hand_pose 列保持零占位）
                    solver = self._solver
                    if solver is not None and solver.available():
                        kpts = solver.process(quats, valid, ts_us)
                        if kpts is not None:
                            self._pipeline.write_glove_keypoints(
                                self.sensor_column, kpts)
