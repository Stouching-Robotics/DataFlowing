"""PoseViewQt 轨迹常驻回归测试（离屏渲染）。

背景：Tk 版 PoseView 的 canvas 图元是持久的，慢路径（世界轴/原点/轨迹）
画一次即常驻；Qt 的 paintEvent 是立即模式、每帧全量重画。移植时把
_draw_static 留在了 `if full_redraw:` 分支里，导致轨迹只在缩放/改尺寸
（连续触发 full_redraw）时闪现，其余时间隐藏。

本测试用离屏 QImage 像素断言：
1. 首次绘制（初始化 full_redraw）后静态层存在；
2. 快速路径重绘（无缩放/尺寸变化/新轨迹点）后静态层仍然存在；
3. 新轨迹点到来（超过节流间隔）触发缓存重算，轨迹点数增加。
4. 换帧（v1.3.13：发布帧改为 +X下/+Y左/+Z前）在视图里**看得见**：投影不含
   任何补偿，同一物理点换成新帧坐标就落在不同像素；世界轴标签按发布帧合成，
   指向物理下/左/前。
5. 帧写在画面上（轴标签的方向词 + 标题下那行帧名），且两条设置下画面差异
   **只**落在文字上——纯标注、不动几何。
"""
import math
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QImage, QPainter
from PyQt5.QtWidgets import QApplication

from ui.pose_view_qt import (
    BG, CAM_AXES, DISPLAY_TRAJECTORY_MAX_POINTS, PoseViewQt,
)


def _rgb_array(image: QImage) -> np.ndarray:
    """QImage → (h, w, 4) uint8 数组（BGRx 序，与 Qt 的 RGB32 一致）。"""
    image = image.convertToFormat(QImage.Format_RGB32)
    ptr = image.bits()
    ptr.setsize(image.byteCount())
    raw = np.frombuffer(ptr, np.uint8).reshape(
        image.height(), image.bytesPerLine())
    return raw[:, :image.width() * 4].reshape(
        image.height(), image.width(), 4).copy()

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
    check_frame_transform()
    check_frame_legibility()
    check_frame_line_not_clipped()
    check_gripper_model_frame_invariant()
    check_main_window_quat_to_matrix()
    check_frame_words_drawn()
    check_gripper_triad_directions()

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


def _project_pre_frame(widget, point, span, cx, cy, unit):
    """v1.3.13 换帧之前那次投影：公式与现在逐字相同，喂的坐标不同。

    留着它是为说清两件事：①现在 _project 里没有任何换帧换算（喂同一坐标
    必须落同一像素）；②同一条物理轨迹换成新帧坐标后像素确实变了 —— 也就是
    换帧在主程序里**看得见**。
    """
    x, y, z = (float(v) for v in np.asarray(point, dtype=float).reshape(3))
    cos_a, sin_a = math.cos(widget._azimuth), math.sin(widget._azimuth)
    cos_e, sin_e = math.cos(widget._elevation), math.sin(widget._elevation)
    x1 = x * cos_a - y * sin_a
    y1 = (x * sin_a + y * cos_a) * cos_e + z * sin_e
    return QPointF(cx + (x1 + widget._pan_x) / span * unit,
                   cy - (y1 + widget._pan_y) / span * unit)


def check_frame_transform():
    """换帧在视图里看得见：gui 坐标系 == 轨迹系，投影不做任何补偿。

    v1.3.13 第一版在这里左乘了一个 V = Mᵀ 想让"画面逐像素不变"，结果 V·M = I
    把换帧在视图里整个抵消掉，用户在主程序里看到的就是换帧前的画面，判"坐标系
    还是没改"。所以现在的契约是反的：**同一物理点换成发布帧坐标后必须落在不同
    像素**。第 1、2 条分别钉住"没补偿"和"看得见"，第 3 条钉住标签的物理含义。
    """
    from core.gripper.slam.protocol import (
        PoseSample, relabel_published_frame)
    import ui.pose_view_qt as pv

    # M 的列 = 重标对三个中间帧单位向量的作用。**不另抄一份 M**：抄一份
    # 就等于拿常量跟自己比，M 写错时测试照样绿。
    M = np.stack([
        relabel_published_frame(PoseSample(
            position=tuple(float(v) for v in axis), rotation=(0, 0, 0, 1),
            timestamp=0.0)).position
        for axis in np.eye(3)], axis=1)
    assert not hasattr(pv, "WORLD_TO_VIEW"), (
        "视图里又出现了 WORLD_TO_VIEW：它会把换帧在视图内抵消掉，"
        "主程序就看不见坐标系改过了")

    widget = PoseViewQt()
    widget._azimuth, widget._elevation = 0.62, -0.31
    widget._pan_x, widget._pan_y = 0.03, -0.017
    span, cx, cy, unit = 0.5, 340.0, 210.0, 200.0

    def same_pixel(now, old, why):
        assert abs(now.x() - old.x()) < 1e-9 and \
            abs(now.y() - old.y()) < 1e-9, why

    # 1. 投影 = 换帧老公式（老公式就是没有 V 的那版）：喂什么坐标画什么像素，
    #    视图对发布帧不做任何额外换算。
    rng = np.random.default_rng(7)
    for p in rng.uniform(-1.0, 1.0, (200, 3)):
        same_pixel(widget._project(p, span, cx, cy, unit),
                   _project_pre_frame(widget, p, span, cx, cy, unit),
                   f"投影里多了换帧换算（画面又被抵消）@{p}")

    # 2. 看得见：同一个物理动作（夹爪往下走 0.4m）在中间帧坐标是 (0,0,-0.4)、
    #    在发布帧坐标是 M·(0,0,-0.4) = (0.4,0,0)=+X 下。老代码画前者、新代码
    #    画后者，两者必须落在明显不同的像素上——一旦有人把 V=Mᵀ 加回来，
    #    这两点会重合，本条立刻红。
    down_mid = np.array([0.0, 0.0, -0.4])
    down_pub = M @ down_mid
    assert np.allclose(down_pub, [0.4, 0.0, 0.0], atol=1e-12), (
        f"物理下在发布帧应是 +X，实得 {down_pub}")
    moved = widget._project(down_pub, span, cx, cy, unit)
    was = _project_pre_frame(widget, down_mid, span, cx, cy, unit)
    delta = math.hypot(moved.x() - was.x(), moved.y() - was.y())
    assert delta > 20.0, (
        f"换帧没改变画面（同一下移只挪了 {delta:.1f}px）：视图把换帧抵消掉了")

    # 3. 世界轴标签按发布帧合成（tip[i] = e_i），含义与 protocol 的物理映射
    #    一致：X 箭头 = 物理下、Y = 左、Z = 前。
    assert [label for _, label in pv.WORLD_AXES] == ["X", "Y", "Z"], (
        f"世界轴标签顺序变了：{pv.WORLD_AXES}")
    physical = {"X": (0.0, 0.0, -1.0),    # 下
                "Y": (-1.0, 0.0, 0.0),    # 左
                "Z": (0.0, 1.0, 0.0)}     # 前
    for index, axis in enumerate(np.eye(3)):
        # 标签画在发布帧 +e_index 的轴上；这个物理方向（中间帧坐标）经
        # protocol 重标后必须正好落在 +e_index，否则屏幕上的 X 指的就不是下。
        name = "XYZ"[index]
        back = relabel_published_frame(PoseSample(
            position=physical[name], rotation=(0, 0, 0, 1),
            timestamp=0.0)).position
        assert np.allclose(back, axis, atol=1e-12), (
            f"世界 {name} 轴标签不等于物理 {name}（重标后 {back}）")
    print("  坐标帧检查: 投影无补偿、同一下移在画面上挪 "
          f"{delta:.0f}px（换帧看得见）；X/Y/Z 标签 = 物理下/左/前")


def check_frame_legibility():
    """帧必须**写在画面上**，而且只动文字、不动几何。

    世界轴三箭头的屏幕位置换帧前后逐像素相同（tip 是常数 e_i，投影公式不吃
    数据），所以这一屏能承载"画的是哪一帧"的只有文字：轴标签后的方向词
    （X 下 / Y 左 / Z 前）、标题下那行 `坐标帧 ...`，以及**跟随夹爪那三支
    箭头的标签**（体轴跟世界轴共用一张词表，换帧时一起变）。v1.3.13 第一版
    正是把整个变化在视图里补偿掉了，用户看到的还是旧画面、判"坐标系还是没改"
    （见 ui/pose_view_qt.py 文件头的教训），所以这条测试钉的是"看得见"本身。

    两条设置下的差异必须**全部**落在文字区（标题两行 + 六个轴标签周围）：
    这同时钉住了"纯标注"——有人以后再往几何里加补偿，这里立刻红。
    """
    from config import settings
    import ui.pose_view_qt as pv

    app = QApplication.instance() or QApplication([])
    env_key = settings.GRIPPER_SLAM_FRAME_ENV
    saved_env = os.environ.get(env_key)
    span = 0.6174          # 钉死 span：_update_fit_span 有 0.3s 壁钟缓动，
    #                        不钉的话两张图会落在不同缩放上，量到的差异不是标注引起的
    traj = tuple(map(tuple, make_path(40)))
    expected = {
        None: (settings.GRIPPER_SLAM_FRAME_NEW,
               {"X": "下", "Y": "左", "Z": "前"}),
        settings.GRIPPER_SLAM_FRAME_LEGACY:
            (settings.GRIPPER_SLAM_FRAME_LEGACY,
             {"X": "右", "Y": "正对", "Z": "上"}),
    }
    shots = {}
    try:
        for frame in expected:
            if frame is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = frame
            name, words = expected[frame]
            widget = PoseViewQt()
            widget.resize(680, 420)
            widget.show()
            widget.set_grip_state(50.0, False)
            widget.update_pose(tuple(traj[-1]), np.eye(3), traj)
            widget._span_target = None
            widget._fit_span = span
            widget._cached_span = span
            widget._trajectory_dirty = True
            app.processEvents()
            assert widget._frame_name == name, (
                f"视图认的帧成了 {widget._frame_name}，settings 解析出的是 {name}")
            assert widget._frame_words == words, (
                f"轴方向词 {widget._frame_words} != {words}（帧={name}）")
            assert widget._frame_text == " ".join(
                f"+{a}{words[a]}" for a in "XYZ"), widget._frame_text
            # 完整帧名（down_left_forward）太长，塞标题会顶出框，放在 tooltip：
            # 想核对 info.json 的 frame 字段的人悬停就能拿到。
            assert name in widget.toolTip(), widget.toolTip()
            img = widget.grab().toImage().convertToFormat(QImage.Format_RGB32)
            ptr = img.bits()
            ptr.setsize(img.byteCount())
            raw = np.frombuffer(ptr, np.uint8).reshape(
                img.height(), img.bytesPerLine())
            shots[frame] = (
                widget,
                raw[:, :img.width() * 4].reshape(
                    img.height(), img.width(), 4).copy())
    finally:
        if saved_env is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = saved_env

    new_widget, new_img = shots[None]
    _, legacy_img = shots[settings.GRIPPER_SLAM_FRAME_LEGACY]
    diff = np.any(new_img != legacy_img, axis=2)
    assert diff.sum() > 100, (
        f"换帧/回落旧约定在画面上只差 {diff.sum()}px —— 帧标注没画出来"
        "（或又被补偿掉了）")

    # 允许差异的区域：标题两行、三个世界轴标签、以及**跟随夹爪那三支箭头
    # 的标签**周围 40px。
    #
    # ⚠ 老实说这条比它自称的弱：40px 的框同时盖住了夹爪**模型**。本用例给
    # 两种帧喂的是同一个 np.eye(3)，可是 _body_relabel 按帧取值（新帧 M、
    # 旧帧 I），于是模型那一块像素并不相同（实测 1339px，全落在这些框里）——
    # 也就是说"差异 100% 落在文字区"并不是"只动了文字"，其中夹爪模型那部分
    # 是**同一个合成姿态在两种换基下**画出来的、跨帧本就不同，属合理差异，
    # 恰好被框吞掉了。所以：
    #   * 真正钉住"标签上有方向词"的是 check_frame_words_drawn（对词做变异）；
    #   * 真正钉住"模型逐像素不变"的是 check_gripper_model_frame_invariant；
    #   * 这条只负责"跨帧差异没有散落到别处去"（几何大动会把它撞红）。
    height, width = diff.shape
    text_zone = np.zeros_like(diff)
    text_zone[:50, :] = True
    cx, cy = width / 2.0, height / 2.0
    unit = min(width, height) / 2.0 * 0.78
    label_points = []
    for index in range(3):
        tip = np.zeros(3)
        tip[index] = span * 0.3
        label_points.append(new_widget._project(tip * 1.3, span, cx, cy, unit))
    # 夹爪三箭头标签的位置照抄 _draw_frame_axes 的算法（tip ×1.15）
    origin = new_widget._position
    length = span * 0.15
    for index in range(3):
        direction = new_widget._rotation[:, index]
        label_points.append(new_widget._project(
            origin + direction * (length * 1.15), span, cx, cy, unit))
    for point in label_points:
        y0, y1 = int(point.y()) - 40, int(point.y()) + 41
        x0, x1 = int(point.x()) - 40, int(point.x()) + 41
        text_zone[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    outside = int((diff & ~text_zone).sum())
    assert outside == 0, (
        f"轴方向词/帧名改了 {outside}px 文字区之外的像素 —— 标注不是纯标注，"
        "几何被动了（补偿式的实现正是这么开始的）")
    # 而且轴标签那一处**自己**也得变（不能只靠标题行那行字）。
    # ⚠ 这句断言和它的说明词对不上：它并不区分"标签变了"与"夹爪模型变了"，
    # 而后者恒真（见上面那段说明），所以哪怕把两处标签的方向词全删掉它也绿
    # （变异实测）。这里只留"世界轴标签框内确有变化"这个弱结论，强结论交给
    # check_frame_words_drawn —— 那边是对词本身做变异，删词必红。
    axis_zone = text_zone.copy()
    axis_zone[:50, :] = False
    world_label_zone = np.zeros_like(diff)
    for point in label_points[:3]:
        y0, y1 = int(point.y()) - 40, int(point.y()) + 41
        x0, x1 = int(point.x()) - 40, int(point.x()) + 41
        world_label_zone[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    assert (diff & world_label_zone).sum() > 20, (
        "三条世界轴标签的位置上换帧前后没有任何变化（只有标题行变了）")
    print(f"  帧标注检查: 新帧 {expected[None][0]} 轴词 {expected[None][1]}；"
          f"旧约定 {expected[settings.GRIPPER_SLAM_FRAME_LEGACY][0]} 轴词 "
          f"{expected[settings.GRIPPER_SLAM_FRAME_LEGACY][1]}；"
          f"两者画面差 {int(diff.sum())}px 且 100% 落在文字上")


def _render_gripper_layer(widget, rotation, body_relabel=None,
                          size=(680, 420), span=0.6174):
    """只画夹爪模型那一层，返回 (h, w, 4) 像素数组。

    不画世界轴/轨迹/标签：那几个换帧时本来就该变，混进来就分不清"模型
    动了"还是"标注动了"。

    body_relabel=None ⇒ 用 widget 自己那份（= 走真正的接线，能抓到
    "视图忘了按帧取换基矩阵"这种漏；显式传值只用来构造对照物）。
    """
    width, height = size
    cx, cy = width / 2.0, height / 2.0
    unit = min(width, height) / 2.0 * 0.78
    widget._rotation = np.asarray(rotation, dtype=float)
    if body_relabel is not None:
        widget._body_relabel = np.asarray(body_relabel, dtype=float)
    widget.set_grip_state(50.0, False)
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor("#0c1a2b"))
    painter = QPainter(image)
    try:
        widget._draw_gripper(painter, cx, cy, unit, span)
    finally:
        painter.end()
    ptr = image.bits()
    ptr.setsize(image.byteCount())
    raw = np.frombuffer(ptr, np.uint8).reshape(height, image.bytesPerLine())
    return raw[:, :width * 4].reshape(height, width, 4).copy()


def _quat_to_matrix(qx, qy, qz, qw):
    """标准 (x,y,z,w) → 3×3，供上面那条用例当独立参照。"""
    return np.array([
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw),
         2 * (qx * qz + qy * qw)],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz),
         2 * (qy * qz - qx * qw)],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw),
         1 - 2 * (qx * qx + qy * qy)],
    ])


def _render_axes_layer(widget, rotation, size=(680, 420), span=0.6174):
    """只画「跟随夹爪那三支箭头」那一层，返回 (h, w, 4) 像素数组。

    与 _render_gripper_layer 同理：把世界轴/轨迹/标题都排除掉，剩下的像素
    才只跟这一个函数有关。
    """
    width, height = size
    cx, cy = width / 2.0, height / 2.0
    unit = min(width, height) / 2.0 * 0.78
    widget._rotation = np.asarray(rotation, dtype=float)
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor("#0c1a2b"))
    painter = QPainter(image)
    try:
        widget._draw_frame_axes(painter, cx, cy, unit, span)
    finally:
        painter.end()
    ptr = image.bits()
    ptr.setsize(image.byteCount())
    raw = np.frombuffer(ptr, np.uint8).reshape(height, image.bytesPerLine())
    return raw[:, :width * 4].reshape(height, width, 4).copy()


# 词清空 -> 两次渲染必须不同。判据与 _draw_frame_axes 的落笔位置绑定。
def _label_anchors(widget, span, size=(680, 420)):
    """六个轴标签的投影锚点：前三个是世界轴，后三个是夹爪轴。"""
    width, height = size
    cx, cy = width / 2.0, height / 2.0
    unit = min(width, height) / 2.0 * 0.78
    points = []
    for index in range(3):
        tip = np.zeros(3)
        tip[index] = span * 0.3
        points.append(widget._project(tip * 1.3, span, cx, cy, unit))
    origin = widget._position
    length = span * 0.15
    for index in range(3):
        points.append(widget._project(
            origin + widget._rotation[:, index] * (length * 1.15),
            span, cx, cy, unit))
    return points


def check_frame_words_drawn():
    """六个轴标签（3 世界 + 3 夹爪）都必须**真的把方向词画出来**。

    ★ 这条替换掉了原来那句 `axis_zone` 断言（"三条世界轴标签的方向词没有随帧
    变化"）—— 它是假的：把两处标签词**一起**删掉，它照样通过。满足它的像素
    其实是**夹爪模型**的跨帧差异（新帧 _body_relabel=M、旧帧=I，同一个单位姿态
    画出来并不相同），不是任何标签。变异测试逮到的就是这个。

    这里改成对词本身做变异：把 widget._frame_words 清空（其它一字不动，标题那行
    用的是构造时定好的 _frame_text，不吃这张表），两次渲染必须不同，且差异要
    分别落在世界轴标签与夹爪轴标签的位置上 —— 任何一处不再把词写上去，立刻红。
    """
    app = QApplication.instance() or QApplication([])
    span = 0.6174
    widget = PoseViewQt()
    widget.resize(680, 420)
    widget.show()
    widget.set_grip_state(50.0, False)
    widget.update_pose((0.0, 0.0, 0.0), np.eye(3), ())
    app.processEvents()

    anchors = _label_anchors(widget, span)
    zone = np.zeros((420, 680), dtype=bool)
    for point in anchors:
        y0, y1 = int(point.y()) - 45, int(point.y()) + 46
        x0, x1 = int(point.x()) - 45, int(point.x()) + 46
        zone[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True

    real_words = dict(widget._frame_words)
    assert all(real_words.values()), (
        f"词表本身是空的（{real_words}）—— 本用例的前提就不成立")

    def render_static():
        canvas = QImage(680, 420, QImage.Format_RGB32)
        canvas.fill(QColor(BG))
        painter = QPainter(canvas)
        widget._draw_static(painter, 680, 420, 340.0, 210.0,
                            210.0 * 0.78, span)
        painter.end()
        return _rgb_array(canvas)

    def render_axes():
        return _render_axes_layer(widget, np.eye(3))

    try:
        for site, render in (("世界轴", render_static),
                             ("夹爪轴", render_axes)):
            with_words = render()
            widget._frame_words = {axis: "" for axis in "XYZ"}
            without_words = render()
            widget._frame_words = dict(real_words)
            diff = np.any(with_words[:, :, :3] != without_words[:, :, :3],
                          axis=2)
            assert diff.sum() > 20, (
                f"{site}标签删掉方向词后画面一字未变 —— 方向词根本没画出来"
                f"（差 {int(diff.sum())}px）")
            inside = int((diff & zone).sum())
            assert inside >= diff.sum() * 0.5, (
                f"{site}标签的方向词变了，但只有 {inside}/{int(diff.sum())}px "
                f"落在标签位置上 —— 改动不像只动了那几个字")
    finally:
        widget._frame_words = dict(real_words)
    print(f"  轴标签词检查: 世界轴/夹爪轴两处标签都把方向词画在标签框内"
          f"（删词立刻可见，{len(anchors)} 个标签位）")


def check_gripper_triad_directions():
    """跟随夹爪的三支箭头必须取发布姿态的**列**（= 体轴），不是行。

    ★ 变异测试逼出来的：原来所有用例喂给 _draw_frame_axes 的姿态都是
    np.eye(3)，而单位阵的行与列**恰好相等**（rotation[i,:] == rotation[:,i]），
    于是把 directions 写成 rotation[0,:]/[1,:]/[2,:] 这种错法整套测试都过
    （实测 exit 0）—— 夹爪体轴的方向根本没有回归保护。

    这里喂一个行列明显不同的姿态（绕 (1,2,3) 转 55°），再按颜色重心判箭头
    指向哪一边。列错成行时重心会整体跑到另一边，两条断言同时红。
    """
    app = QApplication.instance() or QApplication([])
    span = 0.6174
    axis = np.array([1.0, 2.0, 3.0])
    axis = axis / np.linalg.norm(axis)
    angle = math.radians(55.0)
    cross = np.array([[0.0, -axis[2], axis[1]],
                      [axis[2], 0.0, -axis[0]],
                      [-axis[1], axis[0], 0.0]])
    rotation = (np.eye(3) + math.sin(angle) * cross
                + (1.0 - math.cos(angle)) * (cross @ cross))
    assert not np.allclose(rotation, rotation.T, atol=1e-6), (
        "本用例需要一个行列不同的姿态，否则判不出列/行")

    widget = PoseViewQt()
    widget.resize(680, 420)
    widget.show()
    widget.update_pose((0.12, -0.08, 0.05), rotation, ())
    app.processEvents()

    layer = _render_axes_layer(widget, rotation)
    origin = widget._position
    length = span * 0.15
    cx, cy = 340.0, 210.0
    unit = 210.0 * 0.78
    for index, (color, label) in enumerate(CAM_AXES):
        rgb = color
        mask = ((layer[:, :, 2] == rgb.red()) & (layer[:, :, 1] == rgb.green())
                & (layer[:, :, 0] == rgb.blue()))
        assert mask.sum() > 10, (
            f"{label} 轴在夹爪层里找不到纯色像素（{int(mask.sum())}）"
            "—— 三支箭头没画出来")
        rows, cols = np.nonzero(mask)
        centroid = np.array([cols.mean(), rows.mean()])

        def projected(vector):
            point = widget._project(origin + vector * length,
                                    span, cx, cy, unit)
            return np.array([point.x(), point.y()])

        column = projected(rotation[:, index])
        row_dir = projected(rotation[index, :])
        separation = np.linalg.norm(column - row_dir)
        assert separation > 12.0, (
            f"{label} 轴：行与列在这个姿态下投影只差 {separation:.1f}px，"
            "判不出列/行 —— 换个姿态")
        d_column = np.linalg.norm(centroid - column)
        d_row = np.linalg.norm(centroid - row_dir)
        assert d_column * 2.0 < d_row, (
            f"{label} 轴箭头指向了**行**（{rotation[index, :]}）而不是列"
            f"（{rotation[:, index]}）：重心到列向 {d_column:.1f}px、到行向 "
            f"{d_row:.1f}px")
    print("  夹爪体轴方向检查: 三支箭头都取发布姿态的列（体轴），"
          "非对称姿态下行/列可分")


def check_main_window_quat_to_matrix():
    """从**发布四元数**到视图所画矩阵的那一跳，必须端到端钉住。

    ★ 这条补的是全链路唯一没测过的一环：发布出去的是四元数（落盘、
    orb_pose.json、bridge 信号都是它），而视图吃的是 3×3 —— 中间那次
    转换写在 ui/main_window.py 的 _on_gripper_pose 里（内联字面量），
    没有任何用例调过它（全 tools/ 里 grep 不到 _on_gripper_pose）。
    四元数约定写错（(w,x,y,z) 当 (x,y,z,w)、或转置）在这里的后果是
    夹爪整体转错，而协议侧所有测试都只比四元数，**全都看不见**。

    做法：拿真实协议函数算出"原点那一瞬"的发布位姿（四元数），用一个
    最小桩喂进真的 MainWindow._on_gripper_pose，再看视图里那份矩阵。
    顺带断言落盘的 7 元组与喂进视图的是同一份数（同源）。
    """
    app = QApplication.instance() or QApplication([])
    from ui.main_window import MainWindow

    from core.gripper.slam.protocol import (
        PoseSample, finish_publish_pose, rotate_pose_z90,
    )

    # 真实链路：native → 中间帧 → 相对化（锚点即首帧）→ 重标
    native = PoseSample(position=(0.1, 0.2, 0.3),
                        rotation=(0.0, 0.0, 0.0, 1.0), timestamp=1.0)
    mid = rotate_pose_z90(native)
    published = finish_publish_pose(mid, mid.rotation, relabel=True)
    assert np.allclose(published.rotation, (0.0, 0.0, 0.0, 1.0), atol=1e-12), (
        f"协议算出的锚点发布姿态应是单位四元数，实得 {published.rotation}")

    widget = PoseViewQt()
    widget.resize(680, 420)
    widget.show()
    app.processEvents()

    written = []

    class Pipeline:
        def write_slam_trajectory(self, row, prefix=None, timestamp=None,
                                  host_ns=None):
            written.append((tuple(row), prefix, timestamp, host_ns))

    class Window:
        _pipeline = Pipeline()

        def _gripper_entry(self, key):
            return {"pose_view": widget, "snapshot_prefix": "gripper_1_"}

    host_ns = 123456789
    MainWindow._on_gripper_pose(Window(), "k", published.position,
                                published.rotation, (),
                                published.timestamp, host_ns)

    got = np.asarray(widget._rotation, dtype=float)
    assert np.allclose(got, np.eye(3), atol=1e-9), (
        f"锚点发布姿态送进视图后应画成单位矩阵（体轴与世界轴重合），实得\n{got}"
        " —— _on_gripper_pose 里那次四元数→矩阵的转换把帧弄丢了")
    # 三支体轴箭头逐轴的世界方向（列）必须就是世界轴方向
    for index in range(3):
        want = np.eye(3)[:, index]
        assert np.allclose(got[:, index], want, atol=1e-9), (
            f"体轴 {index} 指向 {got[:, index]}，应指向 {want}")

    # 一般转动：转置/换序写错在这一条上会露馅
    q = (0.3, -0.5, 0.2, math.sqrt(1.0 - 0.09 - 0.25 - 0.04))
    MainWindow._on_gripper_pose(Window(), "k", (0.0, 0.0, 0.0), q, (),
                                2.0, host_ns)
    got = np.asarray(widget._rotation, dtype=float)
    assert np.allclose(got, _quat_to_matrix(*q), atol=1e-9), (
        f"四元数 {q} 送进视图得到\n{got}\n应为\n{_quat_to_matrix(*q)}"
        "（约定写错：x/y/z/w 顺序或转置）")

    row, prefix, timestamp, got_host = written[-1]
    assert np.allclose(row[3:], q, atol=1e-12), (
        f"落盘的四元数 {row[3:]} 与喂进视图的 {q} 不是同一份数")
    assert np.allclose(row[:3], (0.0, 0.0, 0.0), atol=1e-12)
    assert timestamp == 2.0 and got_host == host_ns, (
        f"时间基丢了：timestamp={timestamp} host_ns={got_host}")
    print("  四元数→矩阵检查: 真 _on_gripper_pose 驱动视图，"
          "锚点画成单位阵、一般姿态逐元素正确、落盘与显示同源")


def check_gripper_model_frame_invariant():
    """体轴改名（姿态右乘 Mᵀ + 模型顶点左乘 M）之后，夹爪画出来必须逐像素不变。

    v1.3.13 第二版起发布姿态是 M·R·Mᵀ，右边那次是给夹爪体轴改名（+X=体下/
    +Y=体左/+Z=体前）。改名不能改物理朝向 —— 画面上夹爪还是那个夹爪，只是
    它身上那三支箭头的名字跟世界轴统一了。这条用**纯像素**钉住"没动朝向"：

      新实现（rotation = M·R·Mᵀ，顶点过 M）= 旧实现（rotation = M·R，顶点不动）

    两处必须同进同退：只改一处，夹爪会被画歪 120°（实测 90°~120° 量级），
    而两种改法各自看起来都"自洽"，很难从画面上看出是谁漏了 —— 所以钉成
    逐像素相等，不靠肉眼。
    """
    from config import settings
    from core.gripper.slam.protocol import PUBLISH_RELABEL_MATRIX
    M = np.asarray(PUBLISH_RELABEL_MATRIX, dtype=float)
    identity = np.eye(3)
    app = QApplication.instance() or QApplication([])
    env_key = settings.GRIPPER_SLAM_FRAME_ENV
    saved_env = os.environ.get(env_key)

    def widget_for(frame):
        if frame is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = frame
        w = PoseViewQt()
        w.resize(680, 420)
        w.show()
        app.processEvents()
        w._position = np.zeros(3)
        return w

    try:
        # ① 接线：视图必须按**当前帧**取体轴换基矩阵。这一条单独钉，是因为
        #    下面的像素对照能查出"绘制没用换基"，查不出"视图取错了矩阵"。
        new_widget = widget_for(None)
        legacy_widget = widget_for(settings.GRIPPER_SLAM_FRAME_LEGACY)
        old_widget = widget_for(None)          # 旧实现的对照快件，见下
        assert np.allclose(new_widget._body_relabel, M, atol=1e-12), (
            f"新约定下视图的体轴换基矩阵成了 {new_widget._body_relabel}，应为 M")
        assert np.allclose(legacy_widget._body_relabel, identity, atol=1e-12), (
            f"旧约定下视图不该换基，实得 {legacy_widget._body_relabel}")
        assert not np.allclose(new_widget._body_relabel, identity, atol=1e-12), (
            "新约定下视图的换基矩阵是单位阵 —— 体轴没改名，夹爪会被画歪")

        # ② 绘制：新实现（发布姿态 M·R·Mᵀ + 顶点过 M）必须与旧实现
        #    （发布姿态 M·R，顶点不动）逐像素相同。注意 new_img 走的是
        #    widget **自己**那份换基矩阵——接线断了这里就会红。
        cases = {
            "原点那一瞬（发布姿态=M）": identity,
            "一般转动（发布姿态=M·R）": np.array(
                [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]),
        }
        for label, R in cases.items():
            new_img = _render_gripper_layer(new_widget, M @ R @ M.T)
            # 对照物 = 旧实现那一套（发布姿态 M·R、顶点不动）。专门用一个
            # 快件 widget 演它：_render_gripper_layer 会改写 widget 的换基
            # 矩阵，跟 new_widget 共用会把下一轮的 new_img 也污染掉。
            old_img = _render_gripper_layer(old_widget, M @ R, identity)
            ink = int(np.any(new_img[:, :, :3] != new_img[0, 0, :3],
                             axis=2).sum())
            assert ink > 200, (
                f"{label}: 夹爪模型没画出来（墨 {ink}px）——这一条会变成空断言")
            mismatch = int((new_img != old_img).any(axis=2).sum())
            assert mismatch == 0, (
                f"{label}: 体轴改名后夹爪模型动了 {mismatch}px —— 模型顶点该按 "
                "M 换基却没换（或视图取错了换基矩阵），夹爪被画歪了")

        # ③ 旧约定：换基是单位阵 ⇒ 走的就是改之前那条路，逐像素等于旧实现
        legacy_R = cases["一般转动（发布姿态=M·R）"]
        legacy_img = _render_gripper_layer(legacy_widget, M @ legacy_R)
        assert np.allclose(legacy_widget._body_relabel, identity, atol=1e-12)
        assert int((legacy_img != _render_gripper_layer(
            old_widget, M @ legacy_R, identity)).any(axis=2).sum()) == 0, (
            "旧约定下夹爪模型被改了 —— 回落旧约定必须一字不动")
    finally:
        for w in (new_widget, legacy_widget, old_widget):
            w.close()
        if saved_env is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = saved_env
    print("  夹爪体轴检查: 接线（新约定=M、旧约定=I）+ 模型逐像素不变"
          "（原点与一般转动两种姿态），回落旧约定一字不动")


def _image_to_ink(image: QImage) -> np.ndarray:
    """QImage → 逐像素 bool（相对左上角底色判"有墨"）。"""
    image = image.convertToFormat(QImage.Format_RGB32)
    ptr = image.bits()
    ptr.setsize(image.byteCount())
    raw = np.frombuffer(ptr, np.uint8).reshape(
        image.height(), image.bytesPerLine())
    rgb = raw[:, :image.width() * 4].reshape(
        image.height(), image.width(), 4)[:, :, :3].astype(int)
    return np.abs(rgb - rgb[0, 0]).sum(axis=2) > 12


def _ink_runs(rows):
    """把行号序列切成连续段，返回 [(起, 止), ...]。"""
    runs = []
    for row in rows:
        if runs and row == runs[-1][1] + 1:
            runs[-1][1] = row
        else:
            runs.append([row, row])
    return [(a, b) for a, b in runs]


def check_frame_line_not_clipped():
    """那行 `坐标帧 …` 必须**整字**画出来，不能被 drawText 按矩形裁掉底部。

    v1.3.13 第一版把标题两行都画进 16px 高的矩形。DejaVu 12pt 下这串中文的
    boundingRect 是 25px（height() 只有 21，中文墨会低到它之下），而 drawText
    给了 rect 又不换行就是硬裁：实测丢 31% 的墨，「左/前/下/帧」的下横全没了。
    —— 那行字正是用户验收"坐标帧改没改"时唯一能读的东西。

    上面那条帧标注检查看不见这个：它把 y<50 整条都当成允许差异区，字被裁、
    被画没、被画歪都照样绿。

    判据：同一串字、同一支笔、同一个顶，画进一个足够高的框当参照，两者墨迹
    相同。跟字体字号无关，客户机上换字体一样成立。顺带钉住"两行是分开的两
    行"——重合会并成一段，下面的段数断言立刻红。
    """
    app = QApplication.instance() or QApplication([])
    import ui.pose_view_qt as pv

    span = 0.6174
    width, height = 680, 420
    traj = tuple(map(tuple, make_path(40)))
    widget = PoseViewQt()
    widget.resize(width, height)
    widget.show()
    widget.set_grip_state(50.0, False)
    widget.update_pose(tuple(traj[-1]), np.eye(3), traj)
    widget._span_target = None
    widget._fit_span = span
    widget._cached_span = span
    widget._trajectory_dirty = True
    app.processEvents()

    canvas = QImage(width, height, QImage.Format_RGB32)
    canvas.fill(QColor(pv.BG))
    painter = QPainter(canvas)
    painter.setFont(widget.font())
    widget._draw_static(painter, width, height, width / 2.0, height / 2.0,
                        min(width, height) / 2.0 * 0.78, span)
    painter.end()

    ink = _image_to_ink(canvas)
    runs = _ink_runs(list(np.where(ink[:80].any(axis=1))[0]))
    assert len(runs) >= 2, (
        f"标题区只找到 {len(runs)} 段文字（应在标题之外还有一行 `坐标帧 …`）："
        f"{runs} —— 帧那一行没画出来，或者两行叠在一起了")

    top = runs[-1][0]
    band = 40
    text = "坐标帧 " + widget._frame_text
    # 参照：同一支笔、同一个顶，框高到不可能裁到字
    reference = QImage(width, height, QImage.Format_RGB32)
    reference.fill(QColor(pv.BG))
    painter = QPainter(reference)
    painter.setFont(widget.font())
    painter.setPen(pv.TITLE)
    painter.drawText(QRectF(0, top, width, 200.0), Qt.AlignHCenter, text)
    painter.end()
    want = int(_image_to_ink(reference)[top:top + band].sum())
    got = int(ink[top:top + band].sum())
    assert want > 0, f"参照渲染没画出 `{text}`（字体/落笔有问题）"
    assert got >= want, (
        f"帧那一行被裁：画面上 {got}px 墨，完整应是 {want}px（少 "
        f"{100 * (want - got) / want:.0f}%）—— drawText 的矩形框比字矮")

    print(f"  帧不裁检查: `{text}` 顶在第 {top} 行，墨 {got}px = 不裁参照的 "
          f"{want}px；标题与帧两行分成 {len(runs)} 段不叠")


if __name__ == "__main__":
    main()
