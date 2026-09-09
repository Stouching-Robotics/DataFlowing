#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夹爪力矩阵回放演示（单文件自包含，可直接发给客户）

用法:
    python gripper_force_matrix_demo.py [episode-NNN.parquet | 任务目录]
    不带参数则启动后点"打开"选择数据文件

读什么（主程序 v1.3.0+ 池化录制布局）:
    <task>/data/chunk-NNN/episode-FFF.parquet   每行 = 一个录制帧
    <task>/meta/info.json                       fps

    触觉力矩阵列（稀疏变长：本帧没有新样本时为空列表，回放按"保持上一帧"
    处理，与实时画面行为一致）:
        observation.gripper_{left,right}_force_matrix            夹爪 1
        observation.gripper_2_gripper_{left,right}_force_matrix  夹爪 2
    int16 行差分编码（info.json features.encoding = row_diff_quantized）:
        编码: 每行 250×3 个元素横向差分，mod 2^16 有符号补码可逆
        反解: np.frombuffer(...).reshape(-1, 750) → cumsum(axis=1)
              → reshape(250, 250, 3) → [fx, fy, fz] 三力平面
    力总值列（float32 mN，SDK 同帧输出，可选）:
        observation.{prefix}gripper_{left,right}_force  [fx, fy, fz]

显示什么（与主程序触觉面板一致）:
    每侧 = 热力图（上，可滚轮缩放/拖拽/双击复位）+ 力曲线（下）
    热力图 = core/gripper/tactile_process_worker.pressure_to_heatmap 原样移植:
        fz 平面 → max(0) → 3×3 高斯(σ=0.8) → p10/p98 百分位归一化
        → JET → resize(320, 240)；无接触帧全零 → JET 深蓝
    力曲线 = ui/gripper_widgets.ForceCurveWidget 原样移植:
        ±3000mN 对称量程、Fx/Fy/Fz 三线、300 点滚动窗口
    只显示 fz 平面（主程序口径）。矩阵里的 fx/fy 平面在现有录制中逐点值
    均 < 1，int16 取整后整平面为全零，切过去只会是空画面，故不提供切换。

注意（编码有损，回放热力图比实时画面粗）:
    encode_gripper_force_matrix 把 float32 矩阵 clip 到 ±32767 后直接
    astype(int16)（截断取整，无缩放因子）。SDK 逐点力值多为小数，
    落盘只保留整数部分 —— 空间分布与录制时一致，梯度细节是台阶状。
    力总值列（float32）不受影响，曲线与录制时一致。

内存: 打开一份 episode 会把两侧矩阵列整体读进内存（≈ 帧数 × 375KB/侧，
    696 帧双触觉约 490MB）；逐帧解码后只保留当帧矩阵。

依赖: numpy, pyarrow, opencv-python, PyQt5
"""

import argparse
import json
import os
import re
import sys

import cv2
import numpy as np
import pyarrow.parquet as pq

from PyQt5.QtCore import QLibraryInfo, QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# opencv-python 打包了自家 Qt 插件并在 import cv2 时写入
# QT_QPA_PLATFORM_PLUGIN_PATH，会让 PyQt5 去加载 ABI 不匹配的 xcb 插件
# 而崩溃 —— 在这里指回 PyQt5 自带的插件目录
os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = QLibraryInfo.location(
    QLibraryInfo.PluginsPath)

MATRIX_DIM = 250                 # 触觉力矩阵边长（settings.GRIPPER_FORCE_MATRIX_DIM）
ROW_LEN = MATRIX_DIM * 3         # 行差分以整行 250×3 为一组
FZ_PLANE = 2                     # 显示平面 = fz（主程序触觉面板口径）

_FORCE_RANGE_MN = 3000.0         # 力曲线量程（主程序同值）
_CURVE_POINTS = 300              # 力曲线滚动窗口点数（主程序同值）
_CURVE_COLORS = {
    "fx": QColor("#4fc3f7"),     # 蓝
    "fy": QColor("#aed581"),     # 绿
    "fz": QColor("#ff8a65"),     # 橙
}

_MATRIX_COL_RE = re.compile(
    r"^observation\.(?P<prefix>.*?)gripper_(?P<side>left|right)_force_matrix$")
_EPISODE_RE = re.compile(r"^episode-(\d+)\.parquet$")


# ══════════════════════════════════════════════════════════════════
# 1. 力矩阵反解 + 热力图（与主程序逐行一致）
# ══════════════════════════════════════════════════════════════════

def decode_force_matrix(encoded) -> np.ndarray | None:
    """int16 行差分 → (250,250,3) 三力平面。

    与 ui/main_window.encode_gripper_force_matrix 的反解口径一致：
    reshape(-1,750) → cumsum(axis=1) → reshape(250,250,3)。
    cumsum 走 int32 再截断回 int16（mod 2^16 补码回绕），与编码端
    的 int16 差分算术可逆。
    """
    arr = np.asarray(encoded, dtype=np.int16).ravel()
    if arr.size == 0 or arr.size % ROW_LEN:
        return None                      # 空样本或长度不符（损坏行）
    rows = np.cumsum(arr.reshape(-1, ROW_LEN).astype(np.int32), axis=1)
    return rows.astype(np.int16).reshape(MATRIX_DIM, MATRIX_DIM, 3)


def pressure_to_heatmap(pressure_matrix, device_type="bevel", scale=None):
    """压力平面 → 320×240 RGB 热力图。

    移植自 core/gripper/tactile_process_worker.pressure_to_heatmap
    （0902 定标 JET 显示，不再逐传感器/逐帧归一化），输出 RGB（主程序在
    _on_gripper_tactile 里做 RGB→BGR 再交给 BGR 约定控件，本 demo 直接按
    RGB 上屏，净效果一致）。
    """
    if pressure_matrix is None:
        return None
    if scale is None:
        scale = 200.0 if device_type == "curved" else 50.0
    fz = np.asarray(pressure_matrix, dtype=np.float32)
    scaled = np.clip(fz * float(scale), 0, 255).astype(np.uint8)
    heatmap_bgr = cv2.applyColorMap(scaled, cv2.COLORMAP_JET)
    heatmap_rgb = cv2.cvtColor(heatmap_bgr, cv2.COLOR_BGR2RGB)
    return cv2.resize(heatmap_rgb, (320, 240))


# ══════════════════════════════════════════════════════════════════
# 2. 控件（力曲线 / 热力图 / 单侧面板）
# ══════════════════════════════════════════════════════════════════

class ForceCurveWidget(QWidget):
    """Fx/Fy/Fz 力曲线（mN），±3000mN 对称量程。

    移植自 ui/gripper_widgets.ForceCurveWidget（配色/量程/窗口点数一致）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series = {axis: [] for axis in ("fx", "fy", "fz")}
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def push(self, force):
        """force=(fx, fy, fz) mN；非法值跳过。"""
        try:
            fx, fy, fz = (float(v) for v in force[:3])
        except (TypeError, ValueError, IndexError):
            return
        if any(abs(v) > _FORCE_RANGE_MN * 4 for v in (fx, fy, fz)):
            return
        for axis, value in zip(("fx", "fy", "fz"), (fx, fy, fz)):
            series = self._series[axis]
            series.append(value)
            if len(series) > _CURVE_POINTS:
                del series[0]
        self.update()

    def reset(self):
        for series in self._series.values():
            series.clear()
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101418"))
        w, h = self.width(), self.height()
        mid_y = h / 2
        # 网格 + 零轴
        painter.setPen(QPen(QColor("#2a3038"), 1))
        for frac in (-0.5, 0.0, 0.5):
            y = mid_y - frac * h * 0.92
            painter.drawLine(0, int(y), w, int(y))
        painter.setPen(QPen(QColor("#55606d"), 1))
        painter.drawLine(0, int(mid_y), w, int(mid_y))
        # 三线（最新点在右缘）
        for axis, series in self._series.items():
            if not series:
                continue
            painter.setPen(QPen(_CURVE_COLORS[axis], 1.6))
            step = w / max(1, _CURVE_POINTS - 1)
            for index in range(1, len(series)):
                x0 = w - (len(series) - index) * step
                x1 = w - (len(series) - index - 1) * step
                y0 = mid_y - series[index - 1] / _FORCE_RANGE_MN * h * 0.46
                y1 = mid_y - series[index] / _FORCE_RANGE_MN * h * 0.46
                painter.drawLine(int(x0), int(y0), int(x1), int(y1))
        painter.end()


class HeatmapView(QWidget):
    """热力图画布：适应窗口 + 滚轮缩放 + 拖拽平移 + 双击复位。

    交互对齐主程序 ZoomableVideoWidget（1.08×/级、0.25×~8×、双击还原）。
    """

    ZOOM_MIN = 0.25
    ZOOM_MAX = 8.0
    ZOOM_STEP = 1.08

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._zoom = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._drag_from = None
        self._status_text = "无触觉数据"
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background:#000000;")
        self.setCursor(Qt.OpenHandCursor)

    def set_heatmap(self, rgb: np.ndarray):
        """传入 320×240 RGB 热力图（None 保持当前画面，与实时行为一致）。"""
        if not isinstance(rgb, np.ndarray) or rgb.size == 0 or rgb.ndim != 3:
            return
        if not rgb.flags.c_contiguous:
            rgb = np.ascontiguousarray(rgb)
        h, w = rgb.shape[:2]
        # Format_RGB888 直接包住 RGB 数据；fromImage 立即拷贝，rgb 可释放
        qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg)
        self.update()

    def reset_view(self):
        self._zoom = 1.0
        self._offset = QPointF(0.0, 0.0)
        self.update()

    def clear(self):
        """清空画面（换夹爪/换 episode 时避免残留上一份数据）。"""
        self._pixmap = None
        self.reset_view()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._pixmap is None:
            painter.setPen(QColor("#666666"))
            painter.drawText(self.rect(), Qt.AlignCenter, self._status_text)
            painter.end()
            return
        pw, ph = self._pixmap.width(), self._pixmap.height()
        fit = min(self.width() / pw, self.height() / ph)
        scale = fit * self._zoom
        w, h = pw * scale, ph * scale
        cx = self.width() / 2 + self._offset.x()
        cy = self.height() / 2 + self._offset.y()
        target = QRectF(cx - w / 2, cy - h / 2, w, h)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, self._zoom > 1.5)
        painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
        painter.end()

    def wheelEvent(self, event):
        if self._pixmap is None:
            return
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        factor = self.ZOOM_STEP ** steps
        self._zoom = max(self.ZOOM_MIN,
                         min(self.ZOOM_MAX, self._zoom * factor))
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._pixmap is not None:
            self._drag_from = (event.pos(), QPointF(self._offset))
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag_from is None:
            return
        start_pos, start_offset = self._drag_from
        delta = event.pos() - start_pos
        self._offset = QPointF(start_offset.x() + delta.x(),
                               start_offset.y() + delta.y())
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_from = None
            self.setCursor(Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, _event):
        self.reset_view()


class TactileSidePanel(QWidget):
    """单侧触觉面板：热力图（上）+ 力曲线（下）。

    布局对齐主程序 ui/gripper_widgets.GripperSideWidget。
    """

    def __init__(self, side: str, parent=None):
        super().__init__(parent)
        self.side = side
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        title = QLabel("触觉 {}".format("L" if side == "left" else "R"), self)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color:#9aa4b0; font-size:12px;")
        layout.addWidget(title)
        self.view = HeatmapView(self)
        layout.addWidget(self.view, 3)
        curve_box = QHBoxLayout()
        curve_box.setSpacing(4)
        caption = QLabel("力 (mN)", self)
        caption.setStyleSheet("color:#7f8a96; font-size:11px;")
        caption.setFixedWidth(52)
        curve_box.addWidget(caption)
        self.force_curve = ForceCurveWidget(self)
        curve_box.addWidget(self.force_curve, 1)
        layout.addLayout(curve_box, 1)

    def show_tactile(self, heatmap, force):
        self.view.set_heatmap(heatmap)
        if force is not None:
            self.force_curve.push(force)

    def reset(self):
        self.force_curve.reset()
        self.view.clear()


# ══════════════════════════════════════════════════════════════════
# 3. 数据层
# ══════════════════════════════════════════════════════════════════

def _list_column_to_numpy(column):
    """list<int16> 列 → (offsets int64[n+1], values int16[flat])。

    走 pyarrow 缓冲区转 numpy：整段矩阵是上亿个 int16，走 to_pylist
    会生成同等数量的 Python 对象（几 GB），必须绕开。
    """
    offsets, values, base = [], [], 0
    for chunk in column.chunks:
        offs = np.asarray(chunk.offsets, dtype=np.int64)
        vals = np.asarray(chunk.values, dtype=np.int16)
        offsets.append(offs[:-1] + base)
        values.append(vals)
        base += int(vals.size)
    if not offsets:
        return np.zeros(1, np.int64), np.zeros(0, np.int16)
    all_offsets = np.concatenate(offsets + [np.array([base], np.int64)])
    all_values = (np.concatenate(values) if values
                  else np.zeros(0, np.int16))
    return all_offsets, all_values


def _read_fps(parquet_path: str) -> float:
    """从 <task>/meta/info.json 读 fps；缺失回退 30。"""
    task_dir = os.path.dirname(os.path.dirname(os.path.dirname(parquet_path)))
    try:
        with open(os.path.join(task_dir, "meta", "info.json"),
                  "r", encoding="utf-8") as fh:
            return float(json.load(fh).get("fps") or 30.0)
    except (OSError, ValueError, TypeError):
        return 30.0


class TactileStream:
    """单侧触觉流：力矩阵稀疏列（保持上一帧）+ 力总值列。"""

    def __init__(self, table, prefix: str, side: str, n_frames: int):
        self.prefix = prefix
        self.side = side
        name = f"observation.{prefix}gripper_{side}_force_matrix"
        self.offsets, self.values = _list_column_to_numpy(table.column(name))
        lengths = np.diff(self.offsets)
        # 稀疏列 → 每帧实际显示的样本行：本帧无新样本则沿用上一帧
        # （实时画面同样保持上一次 SDK 结果不动），首个样本之前 = -1
        self.hold = np.maximum.accumulate(
            np.where(lengths > 0, np.arange(n_frames, dtype=np.int64), -1))
        force_name = f"observation.{prefix}gripper_{side}_force"
        self.force = None
        if force_name in table.schema.names:
            self.force = np.asarray(table.column(force_name).to_pylist(),
                                    dtype=np.float32)
        self._cache_row = -1
        self._cache_matrix = None

    def matrix(self, idx: int):
        """第 idx 帧显示的三力平面 (250,250,3) int16；无数据返回 None。"""
        if not 0 <= idx < self.hold.size:
            return None
        row = int(self.hold[idx])
        if row < 0:
            return None
        if row == self._cache_row:
            return self._cache_matrix
        start, end = int(self.offsets[row]), int(self.offsets[row + 1])
        matrix = decode_force_matrix(self.values[start:end])
        self._cache_row, self._cache_matrix = row, matrix
        return matrix

    def force_at(self, idx: int):
        """第 idx 帧的 [fx, fy, fz] mN；力列缺失时回退三平面求和。"""
        if self.force is not None and 0 <= idx < len(self.force):
            return tuple(float(v) for v in self.force[idx][:3])
        matrix = self.matrix(idx)
        if matrix is None:
            return None
        return tuple(float(v) for v in
                     matrix.astype(np.float32).sum(axis=(0, 1)))


class TactileEpisode:
    """一份 episode parquet：发现全部夹爪/左右触觉流并提供逐帧取数。"""

    def __init__(self, parquet_path: str):
        self.path = os.path.abspath(parquet_path)
        # 只读触觉相关列：整段矩阵本就占内存，别再捎带手套/骨架等列
        schema = pq.read_schema(parquet_path)
        matrix_names = [name for name in schema.names
                        if _MATRIX_COL_RE.match(name)]
        wanted = list(matrix_names)
        for name in matrix_names:
            match = _MATRIX_COL_RE.match(name)
            force_name = "observation.{}gripper_{}_force".format(
                match.group("prefix"), match.group("side"))
            if force_name in schema.names:
                wanted.append(force_name)
        if not wanted:          # 无夹爪列：仍要读出帧数供界面显示
            wanted = [name for name in ("frame_index",) if name in schema.names]
        self.table = pq.read_table(parquet_path, columns=wanted)
        self.n_frames = self.table.num_rows
        self.fps = _read_fps(parquet_path)
        self.rigs = {}          # prefix → {"left": TactileStream, ...}
        for name in self.table.schema.names:
            match = _MATRIX_COL_RE.match(name)
            if not match:
                continue
            prefix = match.group("prefix")
            side = match.group("side")
            self.rigs.setdefault(prefix, {})[side] = TactileStream(
                self.table, prefix, side, self.n_frames)

    def rig_label(self, prefix: str) -> str:
        match = re.match(r"^gripper_(\d+)_$", prefix or "")
        return f"夹爪 {match.group(1)}" if match else "夹爪 1"

    def default_prefix(self) -> str:
        """默认选数据最多的那台（双夹爪录制里 rig1 可能只有零星样本）。"""
        best, best_count = None, -1
        for prefix, sides in self.rigs.items():
            count = sum(int(np.count_nonzero(np.diff(s.offsets)))
                        for s in sides.values())
            if count > best_count:
                best, best_count = prefix, count
        return best or ""

    def matrix_bytes(self) -> int:
        return sum(int(s.values.nbytes) for sides in self.rigs.values()
                   for s in sides.values())


def _resolve_episodes(path: str):
    """入参可以是 episode parquet、chunk 目录或任务目录 → 返回 episode 列表。"""
    if os.path.isdir(path):
        found = []
        for root, _dirs, files in os.walk(path):
            for name in files:
                if _EPISODE_RE.match(name):
                    found.append(os.path.join(root, name))
        return sorted(found)
    target = os.path.abspath(path)
    chunk_dir = os.path.dirname(target)
    try:
        siblings = sorted(
            os.path.join(chunk_dir, name) for name in os.listdir(chunk_dir)
            if _EPISODE_RE.match(name))
    except OSError:
        siblings = []
    if target not in siblings:      # 文件名非 episode-NNN 时也保证可加载
        siblings.insert(0, target)
    return siblings


# ══════════════════════════════════════════════════════════════════
# 4. 窗口
# ══════════════════════════════════════════════════════════════════

class DemoWindow(QMainWindow):

    def __init__(self, path=None):
        super().__init__()
        self.setWindowTitle("夹爪力矩阵回放 · Force Matrix Viewer")
        self.resize(1280, 760)
        self.data = None
        self.idx = 0
        self.playing = False
        self._prefix = ""

        central = QWidget()
        # 暗色底：面板未出图时空区域不露白色窗口背景（调色板填充，
        # 不用 QSS —— 无选择器规则会级联到控制条按钮/滑条把字染暗）
        palette = central.palette()
        palette.setColor(central.backgroundRole(), QColor("#111111"))
        central.setPalette(palette)
        central.setAutoFillBackground(True)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        panels = QHBoxLayout()
        panels.setContentsMargins(6, 6, 6, 6)
        panels.setSpacing(6)
        self.panels = {"left": TactileSidePanel("left"), "right": TactileSidePanel("right")}
        for panel in self.panels.values():
            panels.addWidget(panel, 1)
        root.addLayout(panels, 1)

        # 控制条：独立浅色容器（原生控件在深色面板底上依然清晰）
        bar_widget = QWidget()
        bar_palette = bar_widget.palette()
        bar_palette.setColor(bar_widget.backgroundRole(), QColor("#d9d9d9"))
        bar_widget.setPalette(bar_palette)
        bar_widget.setAutoFillBackground(True)
        bar = QHBoxLayout(bar_widget)
        bar.setContentsMargins(6, 4, 6, 4)

        self.btn_open = QPushButton("打开 Parquet")
        self.btn_play = QPushButton("播放")
        self.btn_play.setEnabled(False)
        self.btn_prev = QPushButton("|<")
        self.btn_next = QPushButton(">|")
        for btn in (self.btn_prev, self.btn_next):
            btn.setEnabled(False)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.combo_episode = QComboBox()
        self.combo_episode.setEnabled(False)
        self.combo_rig = QComboBox()
        self.combo_rig.setEnabled(False)
        self.chk_loop = QCheckBox("循环")
        self.chk_loop.setChecked(True)
        self.lbl_frame = QLabel("- / -")

        bar.addWidget(self.btn_open)
        bar.addWidget(self.combo_episode)
        bar.addWidget(self.btn_play)
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.btn_next)
        bar.addWidget(self.combo_rig)
        bar.addWidget(self.chk_loop)
        bar.addWidget(self.lbl_frame)
        root.addWidget(bar_widget)

        self.btn_open.clicked.connect(self.open_file_dialog)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_prev.clicked.connect(lambda: self.seek(self.idx - 1))
        self.btn_next.clicked.connect(lambda: self.seek(self.idx + 1))
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.combo_episode.activated.connect(self._on_episode_chosen)
        self.combo_rig.activated.connect(self._on_rig_chosen)

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)   # 节奏计时器要精确节拍
        self.timer.setInterval(33)
        self.timer.timeout.connect(lambda: self.seek(self.idx + 1, advance=True))

        if path:
            self.load(path)

    # ── 加载 ──────────────────────────────────────────

    def open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 episode parquet 或任务目录", "",
            "Parquet (*.parquet);;All files (*)")
        if path:
            self.load(path)

    def load(self, path):
        episodes = _resolve_episodes(path)
        if not episodes:
            return
        # 指定文件就加载该文件；目录则取第一份（同目录其余进 episode 下拉）
        target = path if os.path.isfile(path) else episodes[0]
        try:
            data = TactileEpisode(target)
        except Exception as exc:            # 列缺失/文件损坏：界面不崩
            self.lbl_frame.setText(f"加载失败: {exc}")
            return
        self.timer.stop()
        self.playing = False
        self.btn_play.setText("播放")
        self.data = data
        self.idx = 0

        self.combo_episode.blockSignals(True)
        self.combo_episode.clear()
        for episode in episodes:
            self.combo_episode.addItem(os.path.basename(episode), episode)
        self.combo_episode.setCurrentIndex(episodes.index(data.path)
                                           if data.path in episodes else 0)
        self.combo_episode.blockSignals(False)
        self.combo_episode.setEnabled(len(episodes) > 1)

        self._prefix = data.default_prefix()
        self.combo_rig.blockSignals(True)
        self.combo_rig.clear()
        for prefix in sorted(data.rigs):
            self.combo_rig.addItem(data.rig_label(prefix), prefix)
        self.combo_rig.setCurrentIndex(
            list(sorted(data.rigs)).index(self._prefix))
        self.combo_rig.blockSignals(False)
        self.combo_rig.setEnabled(len(data.rigs) > 1)

        sides = data.rigs.get(self._prefix, {})
        for side, panel in self.panels.items():
            panel.setVisible(side in sides)
            panel.reset()

        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, data.n_frames - 1))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(data.n_frames > 1)
        self.btn_play.setEnabled(data.n_frames > 1)
        self.btn_prev.setEnabled(data.n_frames > 1)
        self.btn_next.setEnabled(data.n_frames > 1)
        self.timer.setInterval(max(1, int(round(1000.0 / data.fps))))
        self.setWindowTitle(
            "夹爪力矩阵回放 · {} · {:.0f}fps · {} 帧".format(
                os.path.basename(data.path), data.fps, data.n_frames))
        print("[力矩阵] {} 帧 {} 台夹爪，矩阵列常驻 {:.0f} MB".format(
            data.n_frames, len(data.rigs), data.matrix_bytes() / 1e6))
        self.render_frame(0)

    def _on_episode_chosen(self, index):
        if index >= 0:
            self.load(self.combo_episode.itemData(index))

    def _on_rig_chosen(self, index):
        if self.data is None or index < 0:
            return
        self._prefix = self.combo_rig.itemData(index)
        sides = self.data.rigs.get(self._prefix, {})
        for side, panel in self.panels.items():
            panel.setVisible(side in sides)
            panel.reset()
        self.render_frame(self.idx)

    # ── 播放控制 ──────────────────────────────────────

    def toggle_play(self):
        if self.data is None:
            return
        self.playing = not self.playing
        self.btn_play.setText("暂停" if self.playing else "播放")
        if self.playing:
            self.timer.start()
        else:
            self.timer.stop()

    def seek(self, index, advance=False):
        """跳转到第 index 帧；advance=True 时到尾部按循环开关决定回卷/停。"""
        if self.data is None:
            return
        last = self.data.n_frames - 1
        if index > last:
            if not (advance and self.chk_loop.isChecked()):
                if advance:
                    self.timer.stop()
                    self.playing = False
                    self.btn_play.setText("播放")
                index = last
            else:
                index = 0
        if index < 0:
            index = 0
        self.render_frame(index)

    def _on_slider_moved(self, value):
        # 拖动中实时渲染（热力图解码 ~1ms + 渲染 ~2ms，跟得上）
        self.render_frame(value)

    def _on_slider_released(self):
        self.render_frame(self.slider.value())

    def render_frame(self, index):
        if self.data is None:
            return
        index = max(0, min(index, self.data.n_frames - 1))
        self.idx = index
        for side, panel in self.panels.items():
            stream = self.data.rigs.get(self._prefix, {}).get(side)
            if stream is None:
                continue
            matrix = stream.matrix(index)
            if matrix is not None:
                # 稀疏帧返回 None 时不动画面：与实时画面保持上一次结果一致
                panel.show_tactile(
                    pressure_to_heatmap(matrix[:, :, FZ_PLANE]),
                    stream.force_at(index))
        self.slider.blockSignals(True)
        self.slider.setValue(index)
        self.slider.blockSignals(False)
        self.lbl_frame.setText("{} / {}  t={:.2f}s".format(
            index + 1, self.data.n_frames, index / self.data.fps))

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Space:
            self.toggle_play()
        elif key == Qt.Key_Left:
            self.seek(self.idx - 1)
        elif key == Qt.Key_Right:
            self.seek(self.idx + 1)
        elif key == Qt.Key_Home:
            self.seek(0)
        elif key == Qt.Key_End:
            self.seek(self.data.n_frames - 1 if self.data else 0)
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.timer.stop()
        self.data = None
        super().closeEvent(event)


# ══════════════════════════════════════════════════════════════════
# 5. 入口
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="夹爪力矩阵回放演示（录制数据 → 触觉热力图 + 力曲线）")
    parser.add_argument("path", nargs="?",
                        help="episode-NNN.parquet / chunk 目录 / 任务目录"
                             "（可选，用界面打开）")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = DemoWindow(args.path)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
