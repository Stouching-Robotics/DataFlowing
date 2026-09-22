"""触觉分区网格测试 —— 主程序 vs 查看器逐像素一致 + 录制/回放两页接线。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_tactile_grid_render.py

覆盖:
  1. 逐像素一致：core.render_engine.render_tactile_grid 与查看器
     tools/demos/pooled_viewer_demo 的同名实现在（尺寸 × 左右手 × 基线档 ×
     值分布）组合下输出完全相同 —— 这是"主程序效果和 demo 一样"的机械化
     判据；两份实现是既定模式（demo 单文件自包含、不 import 主程序），
     改任何一份都必须同步另一份，否则这里当场红
  2. 增量重画：逐格脏重画与全量重画逐像素一致
  3. 录制页（GloveWidget）：矩阵真画进画面、**左右手共用一套坐标**（同一根
     手指在两只手上亮在**同一格**）、覆盖条不烙进共享画布
  4. 回放页（PlaybackDialog）：默认触觉矩阵、中位基线口径、勾选框换档
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import cv2
import pyarrow as pa
import pyarrow.parquet as pq
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication

import core.render_engine as re

DEMO_PY = os.path.join(ROOT, "tools", "demos", "pooled_viewer_demo",
                       "pooled_viewer_demo.py")

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def _pixmap_to_bgr(pm):
    """QPixmap → BGR ndarray（set_frame 存进去的是 RGB）。

    ⚠️ `toImage()` 回的是 **Format_RGB32**（每像素 4 字节，bpl = w*4），
    必须显式转 RGB888 再按 3 字节跨距取 —— 直接按 `w*3` 切会在第二个
    像素起逐字节错位（glove_widget_test 里那段只在 `.max()` 上用，错位也
    看不出来）。
    """
    qimg = pm.toImage().convertToFormat(QImage.Format_RGB888)
    ptr = qimg.constBits()
    ptr.setsize(qimg.byteCount())
    arr = np.frombuffer(ptr, np.uint8).reshape(
        qimg.height(), qimg.bytesPerLine())[:, :qimg.width() * 3].reshape(
        qimg.height(), qimg.width(), 3)
    return arr[:, :, ::-1].copy()


def _boundary_matrix():
    """色带边界值矩阵：LUT 构造（上界补一格 + clip）最容易在移植时走样。"""
    edges = [0, 1, 333, 334, 666, 667, 999, 1000, 1333, 1334, 1666, 1667,
             2000, 2400, 2800, 3200, 3600, 3666, 5000, 999999]
    return np.resize(np.array(edges, np.float32), 256).reshape(16, 16).copy()


SIZES = [(1280, 720),    # 录制页画布
         (640, 400),     # 回放页画布
         (780, 560),     # 查看器窗口（与 demo 默认一致）
         (400, 300),     # cell 被夹到 10：不出数值、不画图例
         (200, 150)]     # 极小：图例区放不下


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    # 查看器（单文件自包含，按路径加载，不 import 主程序）
    spec = importlib.util.spec_from_file_location("pvd_ref", DEMO_PY)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)

    rng = np.random.RandomState(20260921)
    base_r = np.median(rng.rand(30, 16, 16).astype(np.float32) * 1200, axis=0)
    cases = [
        ("随机 0..5000", (rng.rand(16, 16) * 5000).astype(np.float32)),
        ("全零", np.zeros((16, 16), np.float32)),
        ("全满量程", np.full((16, 16), 999999.0, np.float32)),
        ("色带边界值", _boundary_matrix()),
    ]

    # ── 1. 主程序实现 vs 查看器实现：逐像素一致 ──
    print("── 1. 主程序 vs 查看器：逐像素一致 ──")
    total = bad = 0
    for tag, mat in cases:
        for (w, h) in SIZES:
            for side in ("right", "left"):
                for use in (True, False):
                    for bl in ((base_r, None) if use else (base_r, None, "zero")):
                        baseline = (np.zeros((16, 16), np.float32)
                                    if isinstance(bl, str) else bl)
                        a = re.render_tactile_grid(
                            mat, side=side, baseline=baseline,
                            use_baseline=use, w=w, h=h).copy()
                        b = demo.render_tactile_grid(
                            mat, side=side, baseline=baseline,
                            use_baseline=use, w=w, h=h).copy()
                        total += 1
                        if not np.array_equal(a, b):
                            bad += 1
                            print(f"    差异: {tag} {w}x{h} {side} "
                                  f"use_baseline={use} baseline="
                                  f"{'有' if baseline is not None else '无'}")
    check(bad == 0, f"逐像素一致（{total - bad}/{total} 组相同）")

    # 尺寸不同 → 面板不同：同一矩阵在录制/回放画布上不该同图
    g1 = re.render_tactile_grid(cases[0][1], side="right", w=1280, h=720)
    g2 = re.render_tactile_grid(cases[0][1], side="right", w=640, h=400)
    check(g1.shape == (720, 1280, 3) and g2.shape == (400, 640, 3)
          and not np.array_equal(g1, g2), "画布按尺寸自适应且互不串图")

    # 回放画布尺寸策略：按显示区反推"数字不被裁"的画布。
    # 数值文字是固定 0.28 字号（四位 "2015" 宽 21px）—— 格子小于 ~26px 时
    # 四位数铺满整格、右边被裁（回放页原来的 640x400 就是这样：cell 20.25）。
    print("── 1b. tactile_canvas_for 尺寸策略 ──")
    four = cv2.getTextSize("2015", cv2.FONT_HERSHEY_SIMPLEX, 0.28, 1)[0][0]
    check(four == 21, f"四位数文本宽 {four}px（阈值 28 的依据）")
    bad_cell, bad_ratio, bad_shape = [], [], []
    for (dw, dh) in ((589, 223), (911, 326), (640, 400), (320, 240), (1920, 1080),
                     (1400, 800), (500, 500)):
        cw, ch = re.tactile_canvas_for(dw, dh)
        panel = re._TactilePanel(cw, ch, "right")
        if panel.cell < 28:
            bad_cell.append((dw, dh, panel.cell))
        if abs(cw / ch - dw / dh) > 0.02:
            bad_ratio.append((dw, dh, cw, ch))
        # 不小于显示区（只放大不缩小），且四位数字连左边距 2px 也放得下
        if cw < dw or ch < dh or panel.cell < four + 4:
            bad_shape.append((dw, dh, cw, ch))
    check(not bad_cell, f"任何显示区下格子 ≥28px（够 21px 的四位数留边距）: {bad_cell}")
    check(not bad_ratio, f"画布与显示区同宽高比（缩放不换束缚维度）: {bad_ratio}")
    check(not bad_shape, f"只放大不缩小、格子容得下数字+左边距: {bad_shape}")
    # 极小/异常显示区回退 demo 默认画布（别算出 0 或负尺寸）
    check(re.tactile_canvas_for(200, 150) == (780, 560)
          and re.tactile_canvas_for(0, 0) == (780, 560),
          f"显示区太小回退 780x560: {re.tactile_canvas_for(200, 150)}")
    # 显示区本身够大时不再放大（1:1，省一次重采样）
    check(re.tactile_canvas_for(1920, 1080) == (1920, 1080),
          f"够大的显示区按 1:1 渲染: {re.tactile_canvas_for(1920, 1080)}")

    # ── 2. 逐格脏重画 == 全量重画 ──
    print("── 2. 增量重画与全量重画一致 ──")
    mismatched = 0
    for side in ("right", "left"):
        for use in (True, False):
            panel = re._TactilePanel(780, 560, side)
            cur = cases[0][1].copy()
            for i in range(25):
                if i:
                    cur = np.clip(cur + rng.normal(0, 900, (16, 16)),
                                  0, 5000).astype(np.float32)
                    if i % 7 == 0:
                        cur = np.zeros((16, 16), np.float32)   # 整块归零
                a = panel.paint(cur, base_r, use).copy()
                b = re._TactilePanel(780, 560, side).paint(cur, base_r, use)
                if not np.array_equal(a, b):
                    mismatched += 1
    check(mismatched == 0, f"脏格重画与全量重画逐像素一致"
                           f"（{mismatched}/100 帧不一致）")

    # ── 3. 录制页：GloveWidget 真把矩阵画进画面 ──
    print("── 3. 录制页（GloveWidget）──")
    import ui.glove_widget as gw

    mat = np.zeros((16, 16), np.float32)
    mat[3, 14] = 1200.0        # 规范系 (行 3, 列 14) = 拇指靠食指那侧、近指尖
                               # （行 1–3 拇指 / 4–6 食指 / …；列 14 ≈ 沿指 0.82）
                               # 校正档 999<1200≤1333 → 青

    class StubEngine:
        def __init__(self, m):
            self.m = m
            self.hardware_fps = 30.0
            self.latest_data_ts_us = int(time.time() * 1_000_000)
            self.is_calibrating = False

        def process_frame(self):
            return self.m, float(self.m.max())

    wgt = gw.GloveWidget("sensor:ble:X", "AA:BB:CC:DD:EE:FF", "right_glove",
                         "右手套", engine=StubEngine(mat))
    wgt._running = True
    wgt._render_tick()
    frame = _pixmap_to_bgr(wgt.video_widget._pixmap)
    check(frame.shape == (720, 1280, 3), f"录制页画布 1280×720: {frame.shape}")

    p = re._TACTILE_PANELS[(1280, 720, "right")]
    # 矩阵 (x=3, y=14) → 显示格 (行=1, 列=3)。取样点取"格内水平居中、数字基线
    # 以下"（数字无下延，基线以下必无字迹）—— 数值文字会被格子裁掉，四位数
    # 能铺满整格，取格心会踩到字上（AA 混合色，不是纯色带）
    cx = int(p.grid_left + (3 + 0.5) * p.cell)
    cy = int(p.grid_top + (1 + 0.95) * p.cell)
    check(tuple(int(v) for v in frame[cy, cx]) == (21, 204, 250),
          f"录制页画出了矩阵值（格内像素 {tuple(int(v) for v in frame[cy, cx])} "
          f"= 1200 的校正档青色）")

    # 左手：**同一份物理刺激**要以固件原始帧的形式喂进去。固件的左手帧是规范
    # 系的 `[::-1, ::-1].T`（该变换与自身互为逆），所以同一根手指在左右手上的
    # 原始矩阵长得完全不同 —— 这正是"左右手共用一套坐标"的可判据形式。
    # （契约见 core/render_engine.canonical_pressure_matrix 的 docstring。）
    left_raw = np.ascontiguousarray(mat[::-1, ::-1].T)
    wl = gw.GloveWidget("sensor:ble:Y", "11:22:33:44:55:66", "left_glove",
                        "左手套", engine=StubEngine(left_raw))
    wl._running = True
    wl._render_tick()
    frame_l = _pixmap_to_bgr(wl.video_widget._pixmap)
    pl = re._TACTILE_PANELS[(1280, 720, "left")]
    lx = int(pl.grid_left + (3 + 0.5) * pl.cell)
    ly = int(pl.grid_top + (1 + 0.95) * pl.cell)
    check(tuple(int(v) for v in frame_l[ly, lx]) == (21, 204, 250),
          f"左手喂固件原始帧后，同一根手指亮在**同一格**（格内像素 "
          f"{tuple(int(v) for v in frame_l[ly, lx])}）")

    # 反证：**不**转就直接当左手帧喂 —— 必须亮在别处（否则上面那条恒真）。
    # 这里按**青色像素的重心**判格，不取固定采样点：反证那一格正下方紧挨着
    # 掌区蓝框的上边线（显示行 3 的底边 = 蓝框顶），固定点会踩到框线上。
    wb = gw.GloveWidget("sensor:ble:W", "22:33:44:55:66:77", "left_glove",
                        "左手套", engine=StubEngine(mat))
    wb._running = True
    wb._render_tick()
    frame_b = _pixmap_to_bgr(wb.video_widget._pixmap)
    cyan = np.all(frame_b == np.array([21, 204, 250], np.uint8), axis=-1)
    bys, bxs = np.nonzero(cyan)
    brow = (bys.mean() - pl.grid_top) / pl.cell if len(bys) else -1.0
    bcol = (bxs.mean() - pl.grid_left) / pl.cell if len(bxs) else -1.0
    check(3.0 <= brow < 4.0 and 1.0 <= bcol < 2.0,
          f"反证：不转就喂会亮到 (行 3, 列 1) 而不是 (行 1, 列 3)（实测 "
          f"行{brow:.2f}, 列{bcol:.2f}，{len(bys)} 个青像素）⇒ 上面不是恒真")

    # 覆盖条/骨架是画在**拷贝**上的：覆盖条那块（0,0)-(260,68) 会先铺一层
    # 纯黑，而 chrome 里根本没有纯黑像素（底色 25,16,8；文字/格线都是叠加色）
    # —— 所以"这块里出现 (0,0,0)"就是烙进去的判据，与图例/坐标文字无关。
    box = p.canvas[0:68, 0:260]
    check(not np.any(np.all(box == 0, axis=-1)),
          "覆盖条没烙进共享面板画布（返回的是画布本身，调用方须拷贝）")

    # ── 4. 回放页：默认模式 + 中位基线 + 勾选框换档 ──
    print("── 4. 回放页（PlaybackDialog）──")
    from ui.playback_dialog import PlaybackDialog

    root = tempfile.mkdtemp(prefix="tactile_grid_")
    try:
        session = _make_session(root)
        dlg = PlaybackDialog()
        dlg._load_session(session)
        deadline = time.time() + 30
        while time.time() < deadline and not dlg._sensor_widgets:
            app.processEvents()
            time.sleep(0.02)
        app.processEvents()
        check(dlg._sensor_modes == ["tactile"], f"默认模式 = 触觉矩阵: "
                                                f"{dlg._sensor_modes}")
        cell_w = dlg._sensor_cells[0]
        check(cell_w.mode_combo.currentIndex() == 5
              and "tactile" in str(cell_w.mode_combo.currentText()).lower(),
              f"模式下拉默认选中触觉矩阵: {cell_w.mode_combo.currentText()}")

        dlg._seek(0)
        app.processEvents()
        pm = dlg._sensor_widgets[0]._pixmap
        got = None if pm is None else _pixmap_to_bgr(pm)
        # 画布尺寸 = tactile_canvas_for(显示区)：断言与"按显示区反推"一致，
        # 而不是钉死某个像素数（显示区随窗口/分割条变）
        vid = dlg._sensor_widgets[0]
        cw, ch = re.tactile_canvas_for(vid.width(), vid.height())
        check(got is not None and got.shape == (ch, cw, 3),
              f"_update_sensor 出了画面且画布跟随显示区 "
              f"{vid.width()}x{vid.height()} → {cw}x{ch}"
              f"（None = 渲染分支抛异常被 try/except 吞掉）: "
              f"{None if got is None else got.shape}")
        if got is None:
            raise SystemExit(1)

        bl = dlg._tactile_baseline("right_glove")
        check(bl is not None and bl.shape == (16, 16)
              and float(bl[_X, _Y]) == _VAL and float(bl.sum()) == _VAL,
              f"中位基线 = 整段逐格中位（仅 ({_X},{_Y}) = {float(bl[_X, _Y])}）")
        want = re.render_tactile_grid(
            _matrix(), side="right", baseline=bl, use_baseline=True,
            w=cw, h=ch)
        check(np.array_equal(got, want),
              "回放帧 == 同参数直接渲染（矩阵/左右手/基线/尺寸都接对了）")

        # 勾选框换档：2200 扣掉基线 2200 → 0（校正档首档深红），不扣 → 用的是
        # 原始档，2200 落在 2000..2400（橙）—— 两档都换，颜色必须不同
        dlg._tactile_baseline_cb.setChecked(False)
        app.processEvents()
        got2 = _pixmap_to_bgr(dlg._sensor_widgets[0]._pixmap)
        pp = re._TACTILE_PANELS[(cw, ch, "right")]
        bx = int(pp.grid_left + (_X + 0.5) * pp.cell)
        by = int(pp.grid_top + ((15 - _Y) + 0.95) * pp.cell)   # 数字基线以下
        got_px = tuple(int(v) for v in got[by, bx])
        got2_px = tuple(int(v) for v in got2[by, bx])
        check(not np.array_equal(got, got2)
              and got_px == (95, 58, 30) and got2_px == (199, 134, 22),
              f"基线勾选框换档（扣基线 {got_px} → 不扣 {got2_px}）")
        dlg.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PASS")


# 合成数据只点亮一格：矩阵 (x=_X, y=_Y)（右手食指末节）。取值 2200 是挑过的 ——
# 扣掉基线 2200 后是 0（校正档首档 深红），不扣则落进原始档 2000..2400（橙），
# 两档颜色不同，勾选框换档才看得出来（取值若 <2000，两个档都是首档同色）。
_X, _Y, _VAL = 3, 14, 2200.0


def _matrix():
    """**规范系**的 (16,16)（`[x, y]`：x=手指行 1–15 拇指→小指，y=列）。

    固件原始帧与它的关系：右手帧就是它本身，左手帧是 `[::-1, ::-1].T`。
    """
    mat = np.zeros((16, 16), np.float32)
    mat[_X, _Y] = _VAL
    return mat


def _flat():
    """落盘形态：parquet 存的是 ravel() 的 C 序，flat = x*16 + y。"""
    flat = np.zeros(256, np.float32)
    flat[_X * 16 + _Y] = _VAL
    return flat


def _make_session(root: str) -> str:
    """合成会话：1 路摄像机 + right_glove 传感器（60 帧，结构同生产）。"""
    d = os.path.join(root, "20260921_120000_synth")
    os.makedirs(os.path.join(d, "meta"))
    vdir = os.path.join(d, "videos", "cam_a")
    os.makedirs(vdir)
    vw = cv2.VideoWriter(os.path.join(vdir, "chunk_000000.mp4"),
                         cv2.VideoWriter_fourcc(*"mp4v"), 30, (64, 48))
    for i in range(60):
        vw.write(np.full((48, 64, 3), i % 255, np.uint8))
    vw.release()

    pdir = os.path.join(d, "data", "right_glove", "chunk-0000")
    os.makedirs(pdir)
    n = 60
    mats = [_flat() for _ in range(n)]      # 每帧同值 → 中位基线 = 该值本身
    tbl = pa.table({
        "episode_index": pa.array(np.zeros(n, np.int64)),
        "frame_index": pa.array(np.arange(n, dtype=np.int64)),
        "timestamp": pa.array(np.arange(n, dtype=np.float64) / 30.0),
        "observation.right_glove": pa.array(mats, type=pa.list_(pa.float32(), 256)),
    })
    pq.write_table(tbl, os.path.join(pdir, "chunk_000000.parquet"))

    info = {
        "task_name": "synth",
        "fps": 30,
        "cameras": {"cam_a": {"fps": 30}},
        "sensors": ["right_glove"],
        "devices": [
            {"key": "uvc:a", "kind": "uvc", "name": "桌面摄像头",
             "slots": ["cam_a"]},
            {"key": "ble:AA:11:22:33:44:55", "kind": "data_ble",
             "name": "右手手套", "slots": [], "sensor_column": "right_glove"},
        ],
        "device_names": {"cam_a": "桌面摄像头"},
    }
    with open(os.path.join(d, "meta", "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    return d


if __name__ == "__main__":
    main()
