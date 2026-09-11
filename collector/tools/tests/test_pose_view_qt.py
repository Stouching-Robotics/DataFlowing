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

from ui.pose_view_qt import DISPLAY_TRAJECTORY_MAX_POINTS, PoseViewQt

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
    # update_pose 必须按引用透传轨迹：桥接侧只在新增点时重建元组，
    # _maybe_refresh_trajectory 的 `is` 判等靠这个 identity 命中。这里若被
    # 拷贝过，判等恒为假，每个位姿都在主线程深拷贝整条轨迹（点数随会话线性
    # 增长）→ 轨迹帧率随时间下降。注意此处的旧实现是自己构造新对象，
    # 与传进来的 traj 不是同一个。
    assert widget._rendered_trajectory is traj, (
        "update_pose 未按引用透传轨迹（深拷贝让 `is` 判等失效）")

    # 新轨迹点（超过 40ms 节流）→ dirty 标志触发缓存重算
    time.sleep(0.06)
    traj2 = traj + ((1.5, 0.6, 0.3), (2.0, 0.8, 0.4))
    widget.update_pose((0.4, 0.2, 0.15), np.eye(3), traj2)
    third = widget.grab().toImage()
    n_third = count_color(third, ORIGIN_RGB)
    assert n_third > 20, f"新轨迹点绘制后静态层缺失: {n_third}"
    assert widget._trajectory_point_count == 5, (
        f"轨迹点未刷新: {widget._trajectory_point_count}")

    check_decimation()
    check_projection_equivalence()

    print("PASS: PoseViewQt 轨迹常驻回归测试通过 "
          f"(origin 像素 {n_first}/{n_second}/{n_third})")


def make_path(n):
    """一段平滑的螺旋路径，模拟手部运动。"""
    t = np.linspace(0.0, 6.0 * np.pi, n)
    return np.stack([0.30 * np.sin(t), 0.30 * np.cos(t),
                     0.10 * np.sin(2.0 * t)], axis=1)


def check_decimation():
    """显示上限决定画出顶点数——这正是「20 秒后轨迹变有棱有角」的根因。

    降采样 stride 是 2 的幂，所以只要点数越过上限，画出顶点数立刻从
    cap 掉到 cap/2，折线每段跨度翻倍。上限曾是 400：30Hz 下 13 秒就
    越界，顶点数 400→201，每段跨 67ms，看起来就像 SLAM 出点变慢，
    而数据其实一直是满 30Hz。
    """
    widget = PoseViewQt()
    widget.resize(680, 420)

    def drawn_vertices(n):
        traj = tuple(map(tuple, make_path(n)))
        widget._rendered_trajectory = traj
        widget._display_stride = 1          # 每个规模都从头算 stride
        widget._view_dirty = True
        widget._refresh_cached(680, 420)
        return len(widget._cached_trajectory_points)

    cap = DISPLAY_TRAJECTORY_MAX_POINTS
    for n in (cap // 2, cap, cap + 1, 2 * cap, 4 * cap):
        v = drawn_vertices(n)
        assert v >= cap // 2, (
            f"{n} 点只画出 {v} 个顶点（下限 {cap // 2}）：轨迹会显出折角")

    # 曾经的上限 400 会在这里塌成 201 —— 固定住这个数量级，防止回退
    assert drawn_vertices(401) >= 400, (
        "刚越过 400 点时顶点数不应腰斩（旧的 400 上限行为）")

    # 走样上界：画出顶点相对真实路径的最大偏离必须远小于路径尺度
    traj = make_path(2000)
    widget._rendered_trajectory = tuple(map(tuple, traj))
    widget._display_stride = 1
    widget._refresh_cached(680, 420)
    assert len(widget._cached_trajectory_points) == len(traj), "全分辨率不应降采样"
    print(f"  降采样检查: 上限 {cap}，2000 点全量画出，401 点画出 "
          f"{drawn_vertices(401)} 顶点")


def check_projection_equivalence():
    """_project_many 必须与逐点 _project 结果一致（批量化的正确性）。"""
    widget = PoseViewQt()
    widget._azimuth, widget._elevation = 0.7, 0.35
    widget._pan_x, widget._pan_y = 0.01, -0.02
    pts = make_path(50)
    batch = widget._project_many(pts, 0.5, 340.0, 210.0, 200.0)
    for i, p in enumerate(pts):
        one = widget._project(p, 0.5, 340.0, 210.0, 200.0)
        assert abs(batch[i].x() - one.x()) < 1e-9, f"x 不一致 @{i}"
        assert abs(batch[i].y() - one.y()) < 1e-9, f"y 不一致 @{i}"
    print("  投影检查: _project_many 与逐点 _project 逐点一致")


if __name__ == "__main__":
    main()
