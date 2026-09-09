"""SLAM 3D 位姿与轨迹视图（QPainter 等距正交投影，PoseView 的 Qt 移植）。

无 OpenGL——与 Tk 版同一理由：避免 GLX/X11 BadWindow 致命错误。
主窗口按位姿节奏调用 update(position, rotation, trajectory)；paintEvent
全量重绘（位姿 ~30Hz 时每条线几十个点，QPainter 开销可忽略）。
轨迹显示降采样到 DISPLAY_TRAJECTORY_MAX_POINTS 点；左键拖动旋转视角、
右键/中键拖动平移、滚轮缩放、双击复位视角。
"""

from __future__ import annotations

import math
import time

import numpy as np
from PyQt5.QtCore import Qt, QPointF, QRectF
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import QWidget

DISPLAY_TRAJECTORY_MAX_POINTS = 400
TRAJECTORY_UPDATE_MIN_INTERVAL = 1.0 / 25.0
# 网格降采样：stride 只增不减（2 的幂）且锚定索引 0——旧顶点索引永不滑动，
# 新点只在落上网格时追加；stride 翻倍只发生在点数超过 400×stride 时
# （一次离散简化，顶点只减不挪，不再每帧整体重抽）。
# span 自适配：只在探索前沿超过当前 span 的 85% 时外扩一次（缓动 0.3s，
# 目标为前沿 ×1.35），其余时间完全固定——前沿位姿噪声不再牵动整幅画面。
SPAN_EXPAND_TRIGGER = 0.85
SPAN_EXPAND_TARGET = 1.35
SPAN_EXPAND_ANIM_SECONDS = 0.3
# 默认观测角（与旧固定视角一致）；用户拖动后可自由旋转
AZIMUTH_DEFAULT = math.radians(-150.0)
ELEVATION_DEFAULT = math.radians(30.0)

BG = QColor("#0c1a2b")
TITLE = QColor("#7d9bc0")
TRAJECTORY = QColor("#e0a840")
WORLD_AXES = (
    (QColor("#f06070"), "X"),
    (QColor("#50d890"), "Y"),
    (QColor("#5098f0"), "Z"),
)
CAM_AXES = (
    (QColor("#f06070"), "X"),
    (QColor("#50d890"), "Y"),
    (QColor("#5098f0"), "Z"),
)
PLATE_GRIP = QColor("#e8606c")
PLATE_OPEN = QColor("#58a8e8")
CONNECTOR = QColor("#4a6880")
CAM_ORIGIN = QColor("#d8e858")


class PoseViewQt(QWidget):
    """Own the 3D pose view and all of its drawing state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._localization_name = "ORB-SLAM3"
        self._zoom = 1.0
        self._azimuth = AZIMUTH_DEFAULT    # 观测方位角（弧度）
        self._elevation = ELEVATION_DEFAULT  # 观测仰角（弧度）
        self._pan_x = 0.0                  # 平移偏移（世界单位，视图平面）
        self._pan_y = 0.0
        self._drag_last = None             # 拖动起点；None=未在拖动
        self._drag_button = None
        self._view_dirty = False           # 视角已变，投影缓存待重算
        self._view_initialized = False
        self._trajectory_point_count = 0
        self._rendered_trajectory = None
        self._cached_span = 0.5
        self._cached_trajectory_points = []
        self._trajectory_dirty = False   # 新轨迹点已存但投影缓存未重算
        self._last_trajectory_update = 0.0
        self._last_size = (0, 0)
        self._last_zoom = 1.0
        self._position = np.zeros(3)
        self._rotation = np.eye(3)
        self._grip_pct = None
        self._grip_gripped = False
        self._display_stride = 1      # 网格降采样步长，只增不减（2 的幂）
        self._fit_span = 0.5          # 世界单位 span（未除 zoom），单调外扩
        self._span_target = None      # 外扩缓动目标；None=无缓动进行中
        self._span_anim_from = 0.5
        self._span_anim_start = 0.0
        self.setMinimumSize(280, 200)

    # ---- 外部接口 ----
    def update_pose(self, position, rotation, trajectory=()):
        """位姿主路径：更新相机/夹爪绘制并触发重绘。"""
        self._position = np.asarray(position, dtype=float).reshape(3)
        self._rotation = np.asarray(rotation, dtype=float).reshape(3, 3)
        if trajectory:
            self._maybe_refresh_trajectory(tuple(
                tuple(point) for point in trajectory))
        self.update()

    def set_grip_state(self, percentage, gripped):
        """夹爪开合快照（None=未连接不绘制夹爪板）。"""
        self._grip_pct = percentage
        self._grip_gripped = bool(gripped)
        self.update()

    def set_localization_backend(self, backend):
        self._localization_name = (
            "AIKit" if str(backend).strip().lower() == "aikit"
            else "ORB-SLAM3")
        self._view_initialized = False
        self.update()

    def reset(self):
        self._zoom = 1.0
        self._azimuth = AZIMUTH_DEFAULT
        self._elevation = ELEVATION_DEFAULT
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._view_dirty = False
        self._view_initialized = False
        self._trajectory_point_count = 0
        self._rendered_trajectory = None
        self._cached_span = 0.5
        self._cached_trajectory_points = []
        self._trajectory_dirty = False
        self._last_trajectory_update = 0.0
        self._last_zoom = 1.0
        self._display_stride = 1
        self._fit_span = 0.5
        self._span_target = None
        self._span_anim_from = 0.5
        self._span_anim_start = 0.0
        self.update()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            return
        factor = 1.15 if delta > 0 else 1.0 / 1.15
        self._zoom = max(0.2, min(10.0, self._zoom * factor))
        self.update()

    # ---- 视角交互 ----
    def mousePressEvent(self, event):
        if event.button() in (Qt.LeftButton, Qt.RightButton,
                              Qt.MiddleButton):
            self._drag_last = event.pos()
            self._drag_button = event.button()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_last is None:
            return
        delta = event.pos() - self._drag_last
        self._drag_last = event.pos()
        if self._drag_button == Qt.LeftButton:
            # 抓取式旋转：向右拖→方位角增（场景随指针转向），
            # 向上拖→仰角增（视点升高）；仰角限 ±89° 防极点翻转
            self._azimuth += math.radians(delta.x() * 0.5)
            self._elevation -= math.radians(delta.y() * 0.5)
            self._elevation = min(math.radians(89.0),
                                  max(math.radians(-89.0),
                                      self._elevation))
        elif self._drag_button in (Qt.RightButton, Qt.MiddleButton):
            width = float(self.width()) or 680.0
            height = float(self.height()) or 420.0
            unit = min(width, height) / 2.0 * 0.78
            scale = self._cached_span / unit if unit > 0.0 else 1.0
            self._pan_x += delta.x() * scale
            self._pan_y -= delta.y() * scale
            # 平移限幅：不让场景彻底拖出视野（reset/双击可复位）
            limit = self._fit_span * 6.0
            self._pan_x = min(limit, max(-limit, self._pan_x))
            self._pan_y = min(limit, max(-limit, self._pan_y))
        self._view_dirty = True
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_last = None
        self._drag_button = None
        self.unsetCursor()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        # 双击复位视角（角度/平移/缩放；轨迹数据不动）
        self._azimuth = AZIMUTH_DEFAULT
        self._elevation = ELEVATION_DEFAULT
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._zoom = 1.0
        self._view_dirty = True
        self.update()
        event.accept()

    # ---- 轨迹缓存 ----
    def _maybe_refresh_trajectory(self, trajectory):
        now = time.perf_counter()
        if (trajectory is self._rendered_trajectory
                or (now - self._last_trajectory_update)
                < TRAJECTORY_UPDATE_MIN_INTERVAL):
            return
        self._rendered_trajectory = trajectory
        self._trajectory_point_count = len(trajectory)
        self._last_trajectory_update = now
        self._trajectory_dirty = True   # 投影缓存需在下一次绘制时重算

    # ---- 绘制 ----
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), BG)
        painter.setRenderHint(QPainter.Antialiasing)
        width = float(self.width()) or 680.0
        height = float(self.height()) or 420.0

        size_changed = (int(width), int(height)) != self._last_size
        zoom_changed = abs(self._zoom - self._last_zoom) > 0.001
        full_redraw = (
            size_changed or zoom_changed or self._view_dirty
            or not self._view_initialized
            or self._trajectory_dirty
            or self._span_target is not None   # 外扩缓动期间每帧重算投影
            or self._rendered_trajectory is not None
            and self._trajectory_point_count > 0
            and len(self._cached_trajectory_points) == 0
        )
        if full_redraw:
            self._last_size = (int(width), int(height))
            self._last_zoom = self._zoom
            self._trajectory_dirty = False
            self._view_dirty = False
            self._refresh_cached(width, height)

        cx = width / 2.0
        cy = height / 2.0
        unit = min(width, height) / 2.0 * 0.78
        span = self._cached_span

        # 静态层（世界轴/原点/轨迹/标题）每帧必画：Tk 版 canvas 图元持久、
        # 慢路径画一次即常驻；Qt 立即模式每次 paintEvent 全量重画，若仍
        # 只在 full_redraw 时绘制，轨迹/世界轴只会在缩放或改尺寸时闪现。
        self._draw_static(painter, width, height, cx, cy, unit, span)
        self._draw_frame_axes(painter, cx, cy, unit, span)
        self._draw_gripper(painter, cx, cy, unit, span)
        self._view_initialized = True

    def _refresh_cached(self, width, height):
        trajectory = self._rendered_trajectory
        if trajectory:
            display_traj = self._decimated_trajectory(trajectory)
        else:
            display_traj = self._position.reshape(1, 3)
        all_points = np.vstack([
            display_traj,
            self._position.reshape(1, 3),
            np.zeros((1, 3)),
        ])
        self._update_fit_span(float(np.max(np.abs(all_points))))
        self._cached_span = self._fit_span / self._zoom
        cx = width / 2.0
        cy = height / 2.0
        unit = min(width, height) / 2.0 * 0.78
        self._cached_trajectory_points = [
            self._project(point, self._cached_span, cx, cy, unit)
            for point in display_traj]

    def _decimated_trajectory(self, trajectory):
        """锚定索引 0 的固定网格降采样，已绘制顶点索引永不滑动。

        与每帧 linspace 整体重抽不同：stride 只在点数超过
        ``400 × stride`` 时翻倍（2 的幂，新网格是旧网格的子集，
        顶点只减不挪）；末尾始终补上最新点，让轨迹尾部跟着实时位姿。
        """
        length = len(trajectory)
        while length > DISPLAY_TRAJECTORY_MAX_POINTS * self._display_stride:
            self._display_stride *= 2
        if self._display_stride == 1:
            return np.asarray(trajectory, dtype=float)
        indices = list(range(0, length, self._display_stride))
        if indices[-1] != length - 1:
            indices.append(length - 1)
        return np.asarray(
            [trajectory[i] for i in indices], dtype=float)

    def _update_fit_span(self, frontier):
        """span 只外扩不收缩：前沿超过当前 span 的 85% 才触发一次缓动外扩。

        旧实现每次刷新都把 span 精确重拟合到 max(|轨迹|, |当前位姿|)，
        前沿移动时整幅画面每帧跟着缩放呼吸；现在 span 平时固定，
        位姿噪声与前沿抖动不再牵动已绘制的轨迹。
        """
        if self._span_target is None:
            if frontier > self._fit_span * SPAN_EXPAND_TRIGGER:
                self._span_anim_from = self._fit_span
                self._span_target = max(
                    frontier * SPAN_EXPAND_TARGET,
                    self._fit_span * 1.3)
                self._span_anim_start = time.perf_counter()
            return
        elapsed = time.perf_counter() - self._span_anim_start
        if elapsed >= SPAN_EXPAND_ANIM_SECONDS:
            self._fit_span = self._span_target
            self._span_target = None
            return
        eased = 1.0 - (1.0 - elapsed / SPAN_EXPAND_ANIM_SECONDS) ** 3
        self._fit_span = (
            self._span_anim_from
            + (self._span_target - self._span_anim_from) * eased)

    def _project(self, point, span, cx, cy, unit):
        """等距正交投影：绕 Z 转方位角、再绕视图 X 转仰角，加平移偏移。"""
        x, y, z = (float(value) for value in point)
        cos_a, sin_a = math.cos(self._azimuth), math.sin(self._azimuth)
        cos_e, sin_e = math.cos(self._elevation), math.sin(self._elevation)
        x1 = x * cos_a - y * sin_a
        y1 = (x * sin_a + y * cos_a) * cos_e + z * sin_e
        return QPointF(
            cx + (x1 + self._pan_x) / span * unit,
            cy - (y1 + self._pan_y) / span * unit)

    def _draw_static(self, painter, width, height, cx, cy, unit, span):
        def project(point):
            return self._project(point, span, cx, cy, unit)

        # 世界坐标轴 + 标签
        world_length = span * 0.3
        for index, (color, label) in enumerate(WORLD_AXES):
            tip = np.zeros(3)
            tip[index] = world_length
            p0 = project((0, 0, 0))
            p1 = project(tip)
            painter.setPen(QPen(color, 2.4))
            painter.drawLine(p0, p1)
            label_point = project(tip * 1.3)
            painter.setPen(QPen(color, 1.0))
            painter.drawText(
                QRectF(label_point.x() - 12, label_point.y() - 12,
                       24, 24), Qt.AlignCenter, label)
        # 原点
        radius = max(2.0, unit * 0.012)
        origin = project((0, 0, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#50d890"))
        painter.drawEllipse(origin, radius, radius)
        # 轨迹线
        if len(self._cached_trajectory_points) >= 2:
            painter.setPen(QPen(TRAJECTORY, 1.6))
            path_points = self._cached_trajectory_points
            for start, end in zip(path_points, path_points[1:]):
                painter.drawLine(start, end)
        # 标题
        painter.setPen(TITLE)
        display_count = len(self._cached_trajectory_points)
        text = (
            f"{self._localization_name} Pose"
            f"  |  Zoom:{self._zoom:.2f}x"
            f"  |  Traj:{self._trajectory_point_count}"
            + (f"→{display_count}"
               if 0 < display_count < self._trajectory_point_count else "")
            + "pts"
        )
        painter.drawText(
            QRectF(0, 2, width, 16), Qt.AlignHCenter, text)

    def _draw_frame_axes(self, painter, cx, cy, unit, span):
        def project(point):
            return self._project(point, span, cx, cy, unit)

        origin = self._position
        rotation = self._rotation
        # 位姿数据已在 ingestion 处统一旋转（正对=+Y，
        # protocol.rotate_pose_z90，真机实测校准），帧轴直接按旋转
        # 矩阵列绘制即与轨迹/世界轴一致：X(红)=右、Y(绿)=夹爪正对、
        # Z(蓝)=上
        directions = (
            rotation[:, 0],
            rotation[:, 1],
            rotation[:, 2],
        )
        length = span * 0.15
        for index, (direction, (color, label)) in enumerate(
                zip(directions, CAM_AXES)):
            tip = origin + direction * length
            p0 = project(origin)
            p1 = project(tip)
            painter.setPen(QPen(color, 2.0))
            painter.drawLine(p0, p1)
            # 箭头
            delta = np.array([p1.x() - p0.x(), p1.y() - p0.y()])
            norm = max(1.0, math.hypot(*delta))
            heading = delta / norm
            perp = np.array([-heading[1], heading[0]])
            head = max(3.0, length / span * unit * 0.18)
            left = QPointF(
                p1.x() - heading[0] * head + perp[0] * head * 0.5,
                p1.y() - heading[1] * head + perp[1] * head * 0.5)
            right = QPointF(
                p1.x() - heading[0] * head - perp[0] * head * 0.5,
                p1.y() - heading[1] * head - perp[1] * head * 0.5)
            painter.drawLine(p1, left)
            painter.drawLine(p1, right)
            # 标签
            tip_point = project(tip + direction * length * 0.15)
            painter.setPen(QPen(color, 1.0))
            painter.drawText(
                QRectF(tip_point.x() - 10, tip_point.y() - 10, 20, 20),
                Qt.AlignCenter, label)
        radius = max(2.0, unit * 0.012)
        origin_p = project(origin)
        painter.setPen(Qt.NoPen)
        painter.setBrush(CAM_ORIGIN)
        painter.drawEllipse(origin_p, radius, radius)

    def _draw_gripper(self, painter, cx, cy, unit, span):
        def project(point):
            return self._project(point, span, cx, cy, unit)

        if self._grip_pct is None:
            return
        percentage = self._grip_pct
        gripped = self._grip_gripped
        plate_color = PLATE_GRIP if gripped else PLATE_OPEN
        plate_length = 0.20
        plate_width = 0.05
        maximum_half_gap = 0.06
        base_x = 0.10
        half_gap = maximum_half_gap * (1.0 - percentage / 100.0)
        position = self._position
        rotation = self._rotation
        # 局部系 = 新约定（正对=+Y、X=右）：基座沿 +Y 前伸，板长沿 Y，
        # 两板沿 X（左右）分开
        base = position + rotation @ np.array([0, base_x, 0])
        relative_corners = np.array([
            [0, -plate_length / 2, -plate_width / 2],
            [0, plate_length / 2, -plate_width / 2],
            [0, plate_length / 2, plate_width / 2],
            [0, -plate_length / 2, plate_width / 2],
        ])
        painter.setBrush(Qt.NoBrush)
        for sign in (1, -1):
            plate_center = base + rotation @ np.array(
                [sign * half_gap, 0, 0])
            polygon = QPolygonF([
                project(plate_center + rotation @ corner)
                for corner in relative_corners])
            painter.setPen(QPen(plate_color, 1.8))
            painter.drawPolygon(polygon)
            painter.setPen(QPen(plate_color, 0.6))
            painter.drawLine(
                project(plate_center + rotation @ np.array(
                    [0, -plate_length / 2, 0])),
                project(plate_center + rotation @ np.array(
                    [0, plate_length / 2, 0])),
            )
            painter.setPen(QPen(CONNECTOR, 1.0))
            for corner_y in (-plate_length / 2, plate_length / 2):
                plate_point = plate_center + rotation @ np.array(
                    [0, corner_y, 0])
                base_point = base + rotation @ np.array(
                    [0, corner_y, sign * half_gap])
                painter.drawLine(project(plate_point), project(base_point))
        crossbar = np.array([
            base + rotation @ np.array([-maximum_half_gap, 0, 0]),
            base + rotation @ np.array([maximum_half_gap, 0, 0]),
        ])
        painter.setPen(QPen(CONNECTOR, 1.2))
        painter.drawLine(project(crossbar[0]), project(crossbar[1]))
        base_p = project(base)
        radius = max(2.0, unit * 0.010)
        painter.setPen(Qt.NoPen)
        painter.setBrush(plate_color)
        painter.drawEllipse(base_p, radius, radius)
