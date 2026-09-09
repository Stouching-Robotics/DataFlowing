"""UMI 夹爪触觉显示控件（P2）：单侧热力图 + 力曲线竖排。

热力图直接复用 ZoomableVideoWidget（BGR 帧、滚轮缩放/拖拽），力曲线为
自绘 QPainter 控件：±3000mN 量程、Fx/Fy/Fz 三线、10Hz 定时重绘、
deque(300) 滚动窗口。后续 P4 的力值落盘复用本控件的 force 快照。
"""

from __future__ import annotations

from collections import deque

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from config import settings
from config.i18n import tr
from ui.camera_widget import ZoomableVideoWidget

_FORCE_RANGE_MN = 3000.0
_CURVE_POINTS = 300
_CURVE_COLORS = {
    "fx": QColor("#4fc3f7"),  # 蓝
    "fy": QColor("#aed581"),  # 绿
    "fz": QColor("#ff8a65"),  # 橙
}


class ForceCurveWidget(QWidget):
    """Fx/Fy/Fz 力曲线（mN），±3000mN 对称量程。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series = {axis: deque(maxlen=_CURVE_POINTS)
                        for axis in ("fx", "fy", "fz")}
        self._timer = QTimer(self)
        self._timer.setInterval(100)  # 10Hz
        self._timer.timeout.connect(self.update)
        self._timer.start()
        self.setMinimumHeight(140)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(),
                           self.sizePolicy().verticalPolicy())

    def push(self, force: tuple):
        """force=(fx, fy, fz) mN；非法值跳过。"""
        try:
            fx, fy, fz = (float(v) for v in force[:3])
        except (TypeError, ValueError, IndexError):
            return
        if any(abs(v) > _FORCE_RANGE_MN * 4 for v in (fx, fy, fz)):
            return
        self._series["fx"].append(fx)
        self._series["fy"].append(fy)
        self._series["fz"].append(fz)

    def reset(self):
        for axis in self._series.values():
            axis.clear()
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101418"))
        w, h = self.width(), self.height()
        mid_y = h / 2
        # 网格 + 零轴
        grid_pen = QPen(QColor("#2a3038"), 1)
        painter.setPen(grid_pen)
        for frac in (-0.5, 0.0, 0.5):
            y = mid_y - frac * h * 0.92
            painter.drawLine(0, int(y), w, int(y))
        zero_pen = QPen(QColor("#55606d"), 1)
        painter.setPen(zero_pen)
        painter.drawLine(0, int(mid_y), w, int(mid_y))
        # 三线（最新点在右缘）
        for axis, series in self._series.items():
            if not series:
                continue
            pen = QPen(_CURVE_COLORS[axis], 1.6)
            painter.setPen(pen)
            step = w / max(1, _CURVE_POINTS - 1)
            for index in range(1, len(series)):
                x0 = w - (len(series) - index) * step
                x1 = w - (len(series) - index - 1) * step
                y0 = mid_y - series[index - 1] / _FORCE_RANGE_MN * h * 0.46
                y1 = mid_y - series[index] / _FORCE_RANGE_MN * h * 0.46
                painter.drawLine(int(x0), int(y0), int(x1), int(y1))
        painter.end()


class GripperSideWidget(QWidget):
    """单侧触觉：热力图（上）+ 力曲线（下）。"""

    def __init__(self, side: str, parent=None):
        super().__init__(parent)
        self.side = side
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self.video_widget = ZoomableVideoWidget(self)
        self.video_widget.set_status_text(
            tr("触觉 {}（等待 SDK 结果）").format(
                "L" if side == "left" else "R"))
        layout.addWidget(self.video_widget, 3)
        curve_box = QHBoxLayout()
        curve_box.setSpacing(4)
        caption = QLabel(tr("力 (mN)"), self)
        caption.setStyleSheet(
            f"color:{settings.COLOR_TEXT_SECONDARY}; font-size:11px;")
        caption.setFixedWidth(52)
        curve_box.addWidget(caption)
        self.force_curve = ForceCurveWidget(self)
        curve_box.addWidget(self.force_curve, 1)
        layout.addLayout(curve_box, 1)

    def update_tactile(self, heatmap: np.ndarray, force: tuple):
        self.video_widget.set_frame(heatmap, flip_vertical=False)
        self.force_curve.push(force)

    def reset(self):
        self.force_curve.reset()
        self.video_widget.set_status_text(
            tr("触觉 {}（等待 SDK 结果）").format(
                "L" if self.side == "left" else "R"))
