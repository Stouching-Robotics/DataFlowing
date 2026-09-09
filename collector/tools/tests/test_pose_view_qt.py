"""PoseViewQt 轨迹常驻回归测试（离屏渲染）。

背景：Tk 版 PoseView 的 canvas 图元是持久的，慢路径（世界轴/原点/轨迹）
画一次即常驻；Qt 的 paintEvent 是立即模式、每帧全量重画。移植时把
_draw_static 留在了 `if full_redraw:` 分支里，导致轨迹只在缩放/改尺寸
（连续触发 full_redraw）时闪现，其余时间隐藏。

本测试用离屏 QImage 像素断言：
1. 首次绘制（初始化 full_redraw）后静态层存在；
2. 快速路径重绘（无缩放/尺寸变化/新轨迹点）后静态层仍然存在；
3. 新轨迹点到来（超过节流间隔）触发缓存重算，轨迹点数增加。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication

from ui.pose_view_qt import PoseViewQt

# 静态层颜色：原点 #50d890（实心椭圆，保证有纯色像素）
ORIGIN_RGB = (0x50, 0xD8, 0x90)


def count_color(img: QImage, rgb) -> int:
    img = img.convertToFormat(QImage.Format_RGB32)
    ptr = img.bits()
    ptr.setsize(img.byteCount())
    arr = np.frombuffer(ptr, np.uint8).reshape(
        img.height(), img.bytesPerLine())
    arr = arr[:, :img.width() * 4].reshape(img.height(), img.width(), 4)
    r, g, b = rgb
    return int(((arr[:, :, 2] == r) & (arr[:, :, 1] == g)
                & (arr[:, :, 0] == b)).sum())


def main():
    app = QApplication.instance() or QApplication([])
    widget = PoseViewQt()
    widget.resize(680, 420)
    widget.show()
    app.processEvents()

    traj = ((0.0, 0.0, 0.0), (0.5, 0.2, 0.1), (1.0, 0.4, 0.2))
    widget.update_pose((0.2, 0.1, 0.05), np.eye(3), traj)
    first = widget.grab().toImage()
    n_first = count_color(first, ORIGIN_RGB)
    assert n_first > 20, f"首次绘制未见原点（静态层缺失）: {n_first}"

    # 同一轨迹、新位姿、立即重绘 → 走快速路径（无 full_redraw 条件）
    widget.update_pose((0.3, 0.15, 0.1), np.eye(3), traj)
    second = widget.grab().toImage()
    n_second = count_color(second, ORIGIN_RGB)
    assert n_second > 20, (
        f"快速路径重绘后静态层消失（轨迹常驻回归）: {n_second}")

    # 新轨迹点（超过 40ms 节流）→ dirty 标志触发缓存重算
    time.sleep(0.06)
    traj2 = traj + ((1.5, 0.6, 0.3), (2.0, 0.8, 0.4))
    widget.update_pose((0.4, 0.2, 0.15), np.eye(3), traj2)
    third = widget.grab().toImage()
    n_third = count_color(third, ORIGIN_RGB)
    assert n_third > 20, f"新轨迹点绘制后静态层缺失: {n_third}"
    assert widget._trajectory_point_count == 5, (
        f"轨迹点未刷新: {widget._trajectory_point_count}")

    print("PASS: PoseViewQt 轨迹常驻回归测试通过 "
          f"(origin 像素 {n_first}/{n_second}/{n_third})")


if __name__ == "__main__":
    main()
