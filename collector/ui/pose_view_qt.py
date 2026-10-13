"""SLAM 3D 位姿与轨迹视图（QPainter 等距正交投影，PoseView 的 Qt 移植）。

无 OpenGL——与 Tk 版同一理由：避免 GLX/X11 BadWindow 致命错误。
主窗口按位姿节奏调用 update(position, rotation, trajectory)；paintEvent
全量重绘（位姿 30Hz，轨迹点数随会话线性增长，绘制用一次 drawPolyline）。
轨迹显示降采样到 DISPLAY_TRAJECTORY_MAX_POINTS 点；左键拖动旋转视角、
右键/中键拖动平移、滚轮缩放、双击复位视角。

坐标帧：进来的位姿是世界**发布帧**（默认 +X=下 / +Y=左 / +Z=前，
见 protocol.finish_publish_pose）。本视图不做任何换帧补偿，直接把它当数据帧
画——gui 坐标系与轨迹系是同一个坐标系。换帧后画面随之整体改变（场景相对
画布转约 120°，世界轴三箭头屏幕位置不动、含义改成下/左/前），不是逐像素相同。

帧**写在画面上**：标题下第二行是 `坐标帧 +X下 +Y左 +Z前`，三支世界轴箭头的
标签也各带方向词（X 下 / Y 左 / Z 前）。这两处都从 settings 派生（跟
KSQ_GRIPPER_SLAM_FRAME 开关走），换帧或回落旧约定时逐字变化——因为世界轴
三箭头的**屏幕位置**换帧前后完全相同（tip 是常数 e_i，不含数据），能承载
"这一屏画的是哪一帧"的只有文字。详见 _frame_axis_word。

**跟随夹爪的那三支箭头**（_draw_frame_axes，画的是发布姿态的三列 = 夹爪
**体轴**）用的是同一张词表：发布侧给体轴也改了名（protocol.relabel_
published_frame 姿态那个右乘 Mᵀ），所以原点那一瞬发布姿态 = M·Mᵀ = 单位
矩阵，这三支箭头与世界三箭头**重合**，标签写同样的 下/左/前。夹爪的**物理
朝向**一点没变：_draw_gripper 用同一张 M（_BODY_RELABEL）把模型顶点从旧体轴
坐标搬到新体轴坐标，position + R·(M·v) == position + R_旧·v（R_旧 = 改前那次
**发布**的姿态，已含发布侧左乘的 M，不是夹爪的相对姿态 R_rel），逐像素与改前
相同。★只改发布侧不改这里（或反过来）夹爪会被画歪 120°，两种改法各自都
"看起来自洽"——别只动一处。
"""

from __future__ import annotations

import math
import time

import numpy as np
from PyQt5.QtCore import Qt, QPointF, QRectF
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import QWidget

from config import settings
from core.gripper.slam.protocol import PUBLISH_RELABEL_MATRIX

# 显示上限。这个值直接决定轨迹看起来光不光滑：降采样按 stride 翻倍，
# 画出顶点数始终落在 [cap/2, cap] —— 上限是 400 时，只要会话超过 400 点
# （30Hz 下 13 s）顶点数就从 400 掉到 201，每段跨 67 ms，折线开始明显切角，
# 且随 stride 每翻一倍继续变粗（801 点→133 ms/段，1601 点→267 ms/段）。
# 观感像「SLAM 出点变慢」，实际数据一直是满 30Hz，是绘制侧丢的。
# 6000 是实测代价选出来的：矢量投影 + drawPolyline 下 6000 点约
# 2ms/帧绘制 + 3ms/次刷新，而顶点数下限 3000 已让折线远密于像素。
DISPLAY_TRAJECTORY_MAX_POINTS = 6000
TRAJECTORY_UPDATE_MIN_INTERVAL = 1.0 / 25.0
# 网格降采样：stride 只增不减（2 的幂）且锚定索引 0——旧顶点索引永不滑动，
# 新点只在落上网格时追加；stride 翻倍只发生在点数超过上限×stride 时
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
# 夹爪**体轴**换基：发布帧给体轴也改了名（protocol.relabel_published_frame
# 姿态那个右乘 Mᵀ 就是这件事），画模型时得把顶点从旧体轴坐标搬到新体轴坐标，
# 否则夹爪会被画歪 120°。
#
# 建模约定只写一份，就是"夹爪自己的那套"（体前 = 局部 +Y、体左 = 局部 −X、
# 体下 = 局部 −Z）——下面所有顶点常量都按这套写。哪个发布帧用哪套名字，在这
# 张表里查：新约定体轴跟世界轴共用一套词（+X=体下/+Y=体左/+Z=体前），换基
# 矩阵就是 M；旧约定（中间帧）体轴本来就叫右/正对/上，不换。
#
# ★ 只换名字不动物理朝向：rotation 已经被发布侧右乘了 Mᵀ，这里再左乘一次 M，
# 画出来 position + R·(M·v) == position + R_旧·v —— 逐点与换帧前相同。
# ⚠ R_旧 指的是**改前那次发布的姿态**（= M·R_rel，里头已经含了发布侧那道左乘
# M），不是夹爪自己的相对姿态 R_rel。拿 R_rel 去代会差一个 M、算出"夹爪歪了
# 0.3–0.4m"的假警报 —— 这个坑我自己踩过一次（两处都改时残差只在浮点噪声量级，
# 只改一半就是 0.3–0.4m 的实偏差）。
# 两处必须同进同退：只改发布侧不改这里、或只改这里不改发布侧，夹爪都会歪
# 120°，而且两种改法各自都"看起来自洽"，很难从画面上看出是谁漏了。
_BODY_RELABEL = {
    settings.GRIPPER_SLAM_FRAME_NEW: np.asarray(PUBLISH_RELABEL_MATRIX,
                                                dtype=float),
    settings.GRIPPER_SLAM_FRAME_LEGACY: np.eye(3),
}
# 世界轴标签上要写的物理方向词。**别改上面两个常量** ——
# tools/tests/test_pose_view_qt.py 断言 WORLD_AXES 的标签恰好是 ["X","Y","Z"]
# （那是"轴顺序"的契约）；方向词在绘制处派生，见 _draw_static。
#
# 词表**只在 settings 里有一份**（GRIPPER_SLAM_FRAME_AXES_CN），视图不自己
# 抄一份：屏幕上写的字和落进 meta/info.json 的 frame_axes 必须是同一张表
# 出来的，否则哪天改词就只剩一边动，界面和落盘各说各话。翻回旧约定时这三支
# 箭头自己变成"右/正对/上"，否则界面在说谎。
#
# 旧约定的 +Y 写"正对"而不是"前"——用户自己的叫法就是「X=右 / Y=正对 / Z=上」，
# 照抄。**不是**因为两种 forward 指向不同物理方向：M 把中间帧 +Y 映到发布帧 +Z，
# 旧 +Y「正对」与新 +Z「前」就是同一处。会骗人的是**轴字母**（旧 +Y 与新 +Y 差
# 90°），跨帧对齐看方向词、别看字母。

# 标题那一行的顶。画布上两行字（标题、帧）都从这个顶开始往下排，行高取
# **字体实测高度**，不写死像素 —— 写死 16px 时 21px 的中文行会被 drawText
# 按矩形裁掉底部笔画，而帧那一行正是"一眼确认坐标帧改没改"的入口。
TITLE_TOP = 2.0


def _frame_axis_word(frame, axis):
    """frame 这一帧下 +axis 的物理方向词。

    中文优先（settings.GRIPPER_SLAM_FRAME_AXES_CN），未收录的帧回落
    settings 的英文词。回落是刻意的：将来加第三种帧约定时，界面上宁可显示
    英文方向，也不能显示别的帧的方向词，更不能在 paintEvent 里 KeyError
    把整个视图打崩。
    """
    word = settings.GRIPPER_SLAM_FRAME_AXES_CN.get(frame, {}).get("+" + axis)
    if word:
        return word
    return settings.GRIPPER_SLAM_FRAME_AXES.get(frame, {}).get("+" + axis, "")
# 这里**没有**视图侧换帧：发布帧（+X=下 / +Y=左 / +Z=前）就是本视图的数据
# 帧，投影公式直接吃它。gui 坐标系与轨迹系是同一个坐标系，换帧时两者一起
# 变——这正是用户要的"看得见"。
#
# 教训（v1.3.13 第一版，别改回去）：当时为了"画面逐像素不变"在 _project /
# _project_many 里左乘了一个 V = Mᵀ（M 是发布侧那道世界重标）。V·M = I 确实
# 让轨迹/夹爪/自动 span 全部原样不动——但也把换帧在视图里**整个抵消**了：
# 用户在主程序里看到的就是换帧前的画面，只有原点那三支细箭头换了指向
# （实测整幅 0.6% 像素、全落在原点 140×140px 内），于是他判"坐标系还是没改"。
# 补偿掉的不是噪声，是这次改动本身。

PLATE_GRIP = QColor("#e8606c")
PLATE_OPEN = QColor("#58a8e8")
CONNECTOR = QColor("#4a6880")
CAM_ORIGIN = QColor("#d8e858")


class PoseViewQt(QWidget):
    """Own the 3D pose view and all of its drawing state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._localization_name = "ORB-SLAM3"
        # 这一屏画的是哪个坐标帧：发布侧每次会话解析一次（process_controller
        # 同样只解析一次），视图也在构造时定一次，别放进 paintEvent 逐帧读
        # 环境变量——30Hz 下那是白读，而且会话中途改环境变量本就不生效。
        self._frame_name = settings.resolve_gripper_slam_frame()
        self._frame_words = {
            axis: _frame_axis_word(self._frame_name, axis) for axis in "XYZ"
        }
        self._frame_text = " ".join(
            f"+{axis}{self._frame_words[axis]}" for axis in "XYZ")
        # 体轴换基矩阵同样会话内定一次。回落取 DEFAULT 那一档（= 会重标），
        # 与 process_controller._publish_relabel 的判据保持一致：那边也是
        # "只有明确回落到 LEGACY 才不重标"，认不出的取值一律按新约定走。
        # 两边判据不一致的话，认不出的帧名会让位姿重标了、模型却没换基，
        # 夹爪歪 120° 而画面看不出是谁的错。
        self._body_relabel = _BODY_RELABEL.get(
            self._frame_name,
            _BODY_RELABEL[settings.GRIPPER_SLAM_FRAME_DEFAULT])
        # 方向词那一行很短（measured 194px），而槽最窄能到 280px，放得下；
        # 完整帧名（down_left_forward）太长，塞标题会把标题顶出框，改放
        # tooltip——想核对 info.json 里 frame 字段的人悬停即可。
        self.setToolTip(
            f"坐标帧 {self._frame_name}\n"
            + "\n".join(f"+{a} = {self._frame_words[a]}" for a in "XYZ")
            + f"\n改 {settings.GRIPPER_SLAM_FRAME_ENV}="
            f"{settings.GRIPPER_SLAM_FRAME_LEGACY} 可回落旧约定（重启生效）")
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
        """位姿主路径：更新相机/夹爪绘制并触发重绘。

        trajectory 按引用透传：桥接侧只在新增轨迹点时重建元组（见
        bridge.py 的 _trajectory_points 文档），_maybe_refresh_trajectory
        靠 `is` 判等命中「没有新点」的绝大多数调用。这里若再拷一份，判等
        恒为假，等于每个位姿都在 Qt 主线程深拷贝整条轨迹——点数随会话线性
        增长，正是「画一段时间后帧率变低」的原因。
        """
        self._position = np.asarray(position, dtype=float).reshape(3)
        self._rotation = np.asarray(rotation, dtype=float).reshape(3, 3)
        if trajectory:
            self._maybe_refresh_trajectory(trajectory)
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
        # 直接用发布帧坐标量 span：换帧只把三个分量带符号重排（M 每行恰好
        # 一个 ±1），max|坐标| 不变，所以自动 span / 默认缩放不跟着换帧跳。
        self._update_fit_span(float(np.max(np.abs(all_points))))
        self._cached_span = self._fit_span / self._zoom
        cx = width / 2.0
        cy = height / 2.0
        unit = min(width, height) / 2.0 * 0.78
        self._cached_trajectory_points = self._project_many(
            display_traj, self._cached_span, cx, cy, unit)

    def _decimated_trajectory(self, trajectory):
        """锚定索引 0 的固定网格降采样，已绘制顶点索引永不滑动。

        与每帧 linspace 整体重抽不同：stride 只在点数超过
        ``DISPLAY_TRAJECTORY_MAX_POINTS × stride`` 时翻倍（2 的幂，新网格是旧网格的子集，
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
        """等距正交投影：发布帧坐标直接绕 Z 转方位角、绕 X 转仰角，加平移偏移。

        入参就是发布帧（+X=下 / +Y=左 / +Z=前），没有任何换帧换算——所以
        _draw_static 按 tip[i] = e_i 合成的世界轴尖天然指向下/左/前。
        """
        x, y, z = (float(value) for value in point)
        cos_a, sin_a = math.cos(self._azimuth), math.sin(self._azimuth)
        cos_e, sin_e = math.cos(self._elevation), math.sin(self._elevation)
        x1 = x * cos_a - y * sin_a
        y1 = (x * sin_a + y * cos_a) * cos_e + z * sin_e
        return QPointF(
            cx + (x1 + self._pan_x) / span * unit,
            cy - (y1 + self._pan_y) / span * unit)

    def _project_many(self, points, span, cx, cy, unit):
        """批量投影成 QPolygonF，供一次 drawPolyline 画完整条轨迹。

        与逐点调 _project 结果逐点相同，只是把三角函数提到循环外并整体
        向量化（几千点时省掉大部分开销）。整条轨迹必须作为**一条**折线交给
        Qt：逐段 drawLine 是每次 paintEvent 上万次 Python→Qt 调用，这正是
        显示上限要卡在 DISPLAY_TRAJECTORY_MAX_POINTS 这个量级的原因
        （该常量的取值依据见文件头）。
        """
        pts = np.asarray(points, dtype=float).reshape(-1, 3)
        cos_a, sin_a = math.cos(self._azimuth), math.sin(self._azimuth)
        cos_e, sin_e = math.cos(self._elevation), math.sin(self._elevation)
        x1 = pts[:, 0] * cos_a - pts[:, 1] * sin_a
        y1 = (pts[:, 0] * sin_a + pts[:, 1] * cos_a) * cos_e + pts[:, 2] * sin_e
        return QPolygonF([
            QPointF(cx + (x + self._pan_x) / span * unit,
                    cy - (y + self._pan_y) / span * unit)
            for x, y in zip(x1, y1)])

    def _draw_static(self, painter, width, height, cx, cy, unit, span):
        def project(point):
            return self._project(point, span, cx, cy, unit)

        # 世界坐标轴 + 标签（tip 在**发布帧**里合成，tip[index] = length ——
        # 红色 X 标签就是发布帧 +X=下，不是画布的某个方向）
        #
        # 标签带物理方向词（X 下 / Y 左 / Z 前，随 settings 走）。这三支箭头的
        # 屏幕位置换帧前后逐像素相同（tip 是常数 e_i，投影公式不吃数据），所以
        # 换帧和"回落旧约定"在画面上唯一能读出区别的地方就是这几个字。
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
            word = self._frame_words[label]
            text = f"{label} {word}" if word else label
            # 框按**实际字体**量，不写死尺寸：字从"X"变成"X 正对"宽了一倍
            # 多，写死的框在字体比 DejaVu 12pt 大时会把这几个字裁掉
            # （drawText 给了 rect 又不换行就是硬裁，不会省成省略号）。
            # 框中心仍是箭尖 ×1.3 那个点，字不动位置。
            ink = painter.fontMetrics().boundingRect(text)
            painter.drawText(
                QRectF(label_point.x() - ink.width() / 2.0,
                       label_point.y() - ink.height() / 2.0,
                       ink.width(), ink.height()),
                Qt.AlignCenter, text)
        # 原点
        radius = max(2.0, unit * 0.012)
        origin = project((0, 0, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#50d890"))
        painter.drawEllipse(origin, radius, radius)
        # 轨迹线：整条一次画完（逐段 drawLine 的 Python 开销随点数线性增长，
        # 会把能显示的点数压到几百，轨迹因此显出折角）
        if len(self._cached_trajectory_points) >= 2:
            painter.setPen(QPen(TRAJECTORY, 1.6))
            painter.drawPolyline(self._cached_trajectory_points)
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
        # 两行的框都按**实际字体量**，不写死像素。写死 16px 时 drawText 会
        # 按矩形硬裁：默认字体下这一行 17px 的墨只剩 11px，实测丢 31%
        # （左/前/下/帧 的下横全没了）。用 height() 也不够 —— 中文墨迹会低
        # 到 height() 之下（DejaVu 12pt：height 21、boundingRect 25），
        # 所以取 boundingRect，它才是这一串字真正的框。
        # AlignHCenter 不带垂直标志 ⇒ 字从框顶开始，加高框不会挪动字。
        metrics = painter.fontMetrics()
        title_box = metrics.boundingRect(text)
        painter.drawText(
            QRectF(0, TITLE_TOP, width, title_box.height()),
            Qt.AlignHCenter, text)
        # 帧那一行：这一屏画的是哪个坐标帧。它是"一眼确认"的入口——世界轴三
        # 箭头的屏幕位置换帧前后逐像素相同，光看箭头分不出来（见文件头注释）。
        # 字串只有 15 字（实测 ~194px），最窄 280px 的槽也放得下；完整帧名在
        # tooltip。
        frame_text = f"坐标帧 {self._frame_text}"
        painter.drawText(
            QRectF(0, TITLE_TOP + title_box.height(), width,
                   metrics.boundingRect(frame_text).height()),
            Qt.AlignHCenter, frame_text)

    def _draw_frame_axes(self, painter, cx, cy, unit, span):
        def project(point):
            return self._project(point, span, cx, cy, unit)

        origin = self._position
        rotation = self._rotation
        # 跟随夹爪的三支箭头 = 发布姿态的三列 = **夹爪体轴**。
        #
        # 体轴跟世界轴共用一套词（protocol.relabel_published_frame 里姿态
        # 那个右乘 Mᵀ 干的就是这件事），所以原点那一瞬发布姿态 = 单位矩阵，
        # 这三支箭头与世界三箭头**重合**；标签也用世界轴同一张词表
        # （X 下 / Y 左 / Z 前），一眼能看出"跟随夹爪的轴也改了"。
        #
        # 旧版这里只左乘 M、体轴不改名：原点那瞬是常数 M，三支箭头与世界
        # 三箭头差约 120°，标签又是光秃秃的 X/Y/Z —— 用户的原话是"还有
        # 一个跟随夹爪移动的坐标轴你还没改"。
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
            # 标签：跟世界轴同一张词表、同一种度量方式（框按实际字体量，
            # 不然中文方向词会被 drawText 按矩形硬裁掉下半截）
            tip_point = project(tip + direction * length * 0.15)
            painter.setPen(QPen(color, 1.0))
            word = self._frame_words[label]
            text = f"{label} {word}" if word else label
            ink = painter.fontMetrics().boundingRect(text)
            painter.drawText(
                QRectF(tip_point.x() - ink.width() / 2.0,
                       tip_point.y() - ink.height() / 2.0,
                       ink.width(), ink.height()),
                Qt.AlignCenter, text)
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
        # 局部系 = 夹爪**体轴**，顶点一律按"夹爪自己的那套"写：
        #   +Y = 夹爪正前方（基座沿 Y 前伸、板长沿 Y）
        #   ±X = 夹爪左右（两块板沿 X 分开）
        #   ±Z = 夹爪上下（板的厚度方向）
        # 画之前统一过一道 body()，把它搬到**当前发布帧**的体轴叫法上去：
        # 新约定体轴跟世界轴同名（+X=体下/+Y=体左/+Z=体前），旧约定不动。
        # 几何只有这一份，换帧时既不用改顶点、也不会两处各写一份改漏一处。
        relabel = self._body_relabel

        def body(vector):
            return relabel @ np.asarray(vector, dtype=float)

        base = position + rotation @ body([0, base_x, 0])
        relative_corners = np.array([
            [0, -plate_length / 2, -plate_width / 2],
            [0, plate_length / 2, -plate_width / 2],
            [0, plate_length / 2, plate_width / 2],
            [0, -plate_length / 2, plate_width / 2],
        ])
        painter.setBrush(Qt.NoBrush)
        for sign in (1, -1):
            plate_center = base + rotation @ body([sign * half_gap, 0, 0])
            polygon = QPolygonF([
                project(plate_center + rotation @ body(corner))
                for corner in relative_corners])
            painter.setPen(QPen(plate_color, 1.8))
            painter.drawPolygon(polygon)
            painter.setPen(QPen(plate_color, 0.6))
            painter.drawLine(
                project(plate_center + rotation @ body(
                    [0, -plate_length / 2, 0])),
                project(plate_center + rotation @ body(
                    [0, plate_length / 2, 0])),
            )
            painter.setPen(QPen(CONNECTOR, 1.0))
            for corner_y in (-plate_length / 2, plate_length / 2):
                plate_point = plate_center + rotation @ body([0, corner_y, 0])
                base_point = base + rotation @ body(
                    [0, corner_y, sign * half_gap])
                painter.drawLine(project(plate_point), project(base_point))
        crossbar = np.array([
            base + rotation @ body([-maximum_half_gap, 0, 0]),
            base + rotation @ body([maximum_half_gap, 0, 0]),
        ])
        painter.setPen(QPen(CONNECTOR, 1.2))
        painter.drawLine(project(crossbar[0]), project(crossbar[1]))
        base_p = project(base)
        radius = max(2.0, unit * 0.010)
        painter.setPen(Qt.NoPen)
        painter.setBrush(plate_color)
        painter.drawEllipse(base_p, radius, radius)
