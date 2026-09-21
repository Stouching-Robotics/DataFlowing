#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pooled_viewer_demo 离屏自检（无 pytest 依赖，直接运行）:

    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_pooled_viewer_demo.py

覆盖:
  1. 合成池化会话（parquet + mp4 + info.json）→ 加载 / 逐帧渲染 / 跳帧 / 播放
  2. 降级路径：无触觉/IMU/视频的 parquet → 面板隐藏不崩
  3. 真实录制（data/recordings 下任一含视频的 episode）→ 加载并渲染一帧
退出码 0 = 全部通过。
"""

from __future__ import annotations

import glob
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

DEMO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "demos", "pooled_viewer_demo", "pooled_viewer_demo.py")

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def make_synthetic_session(root: str, n: int = 30,
                           tactile_zero: bool = False,
                           right_only: bool = False,
                           both_hands: bool = False) -> str:
    """合成含触觉 + IMU + 骨架 + RGB/深度视频的池化 episode，返回 parquet 路径。

    tactile_zero: 触觉列全零（覆盖"触觉列存在但无数据"占位提示路径）。
    right_only: 只有右手触觉列（覆盖"单面板回退：无左手数据时显示右手
    并渲染矩阵、面板不得隐藏"的回归路径）。
    both_hands: 左右手齐全（左手也有 IMU 与非零骨架，与真实双套录制一致；
    覆盖"双手触觉/IMU/骨架各一个面板并排、左手在左"）。
    """
    task = os.path.join(root, "synthetic_task" if not tactile_zero
                        else "synthetic_task_zero_tactile")
    if right_only:
        task += "_right_only"
    if both_hands:
        task += "_both_hands"
    os.makedirs(os.path.join(task, "data", "chunk-000"))
    os.makedirs(os.path.join(task, "videos", "chunk-000", "d435_rgb"))
    os.makedirs(os.path.join(task, "videos", "chunk-000", "d435_depth"))
    os.makedirs(os.path.join(task, "meta"))

    rng = np.random.default_rng(7)
    rows = []
    for i in range(n):
        # 触觉: 两个高斯斑点在 16x16 上移动（保证 vmax > 0）
        base = np.zeros((16, 16), np.float32)
        if not tactile_zero:
            for c, amp in ((i % 16, 0.9), (15 - i % 16, 0.5)):
                for r in range(16):
                    for cc in range(16):
                        base[r, cc] += amp * float(
                            np.exp(-((r - 7) ** 2 + (cc - c) ** 2) / 4.0))
        # 骨架: 右手展手模板绕 Z 旋转（非零 21x3），左手全零占位
        ang = 2 * np.pi * i / n
        c, s = np.cos(ang), np.sin(ang)
        rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        kpts = np.array([[0.0, 0.0, 0.0]] + [
            [0.05 * j, 0.012 * k, 0.0]
            for k in range(1, 6) for j in range(1, 5)], np.float32)
        kpts = kpts @ rot.T
        row = {
            "episode_index": 1,
            "frame_index": i,
            "timestamp": float(i) / 30.0,
            "observation.right_glove": base.reshape(-1).tolist(),
            "observation.right_glove_imu_quat": (np.tile(
                [0.0, 0.0, 0.0, 1.0], 16) +
                rng.normal(0, 0.02, 64)).tolist(),
            "observation.right_glove_imu_valid": (
                np.where(np.arange(16) % 5 == 0, 0.0, 1.0)).tolist(),
            "observation.right_hand_pose": kpts.reshape(-1).tolist(),
            # 左手骨架默认全零占位（恒写列，覆盖"占位列被过滤"）
            "observation.left_hand_pose": [0.0] * 63,
        }
        if both_hands:
            # 左手 IMU：绕 X 转 90° 的定值姿态（与右手的单位姿态可区分）
            row["observation.left_glove_imu_quat"] = (np.tile(
                [math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)], 16) +
                rng.normal(0, 0.02, 64)).tolist()
            row["observation.left_glove_imu_valid"] = (
                np.where(np.arange(16) % 3 == 0, 0.0, 1.0)).tolist()
            # 左手骨架：右手模板镜像 X（手性不同，与右手面板可区分）
            row["observation.left_hand_pose"] = (
                kpts * np.array([-1.0, 1.0, 1.0], np.float32)
            ).reshape(-1).tolist()
        rows.append(row)
    cols = {
        "episode_index": pa.array([r["episode_index"] for r in rows],
                                  pa.int64()),
        "frame_index": pa.array([r["frame_index"] for r in rows], pa.int64()),
        "timestamp": pa.array([r["timestamp"] for r in rows], pa.float64()),
        "observation.right_glove": pa.array(
            [r["observation.right_glove"] for r in rows],
            pa.list_(pa.float32(), 256)),
        "observation.right_glove_imu_quat": pa.array(
            [r["observation.right_glove_imu_quat"] for r in rows],
            pa.list_(pa.float32(), 64)),
        "observation.right_glove_imu_valid": pa.array(
            [r["observation.right_glove_imu_valid"] for r in rows],
            pa.list_(pa.float32(), 16)),
        # 右手骨架非零、左手全零占位（覆盖"有骨架 + 占位列过滤"组合）
        "observation.right_hand_pose": pa.array(
            [r["observation.right_hand_pose"] for r in rows],
            pa.list_(pa.float32(), 63)),
        "observation.left_hand_pose": pa.array(
            [r["observation.left_hand_pose"] for r in rows],
            pa.list_(pa.float32(), 63)),
    }
    # 左手套默认只有触觉、无 IMU（覆盖"两手：触觉有 IMU 无"的组合路径），
    # both_hands 时才算齐全
    if not right_only:
        cols["observation.left_glove"] = pa.array(
            [r["observation.right_glove"] for r in rows],
            pa.list_(pa.float32(), 256))
        info_left = {"observation.left_glove": {"dtype": "float32",
                                                "shape": [16, 16]}}
    else:
        info_left = {}
    if both_hands:
        cols["observation.left_glove_imu_quat"] = pa.array(
            [r["observation.left_glove_imu_quat"] for r in rows],
            pa.list_(pa.float32(), 64))
        cols["observation.left_glove_imu_valid"] = pa.array(
            [r["observation.left_glove_imu_valid"] for r in rows],
            pa.list_(pa.float32(), 16))
        info_left["observation.left_glove_imu_quat"] = {
            "dtype": "float32", "shape": [16, 4]}
        info_left["observation.left_glove_imu_valid"] = {
            "dtype": "float32", "shape": [16]}
    parquet_path = os.path.join(task, "data", "chunk-000", "episode-000.parquet")
    pq.write_table(pa.table(cols), parquet_path)

    info = {
        "format": "pooled_episodes_v1", "fps": 30.0,
        "cameras": {"d435_rgb": {"width": 320, "height": 240},
                    "d435_depth": {"width": 320, "height": 240}},
        "features": {
            "observation.right_glove": {"dtype": "float32", "shape": [16, 16]},
            "observation.right_glove_imu_quat": {"dtype": "float32",
                                                  "shape": [16, 4]},
            "observation.right_glove_imu_valid": {"dtype": "float32",
                                                   "shape": [16]},
            "observation.right_hand_pose": {"dtype": "float32",
                                            "shape": [21, 3]},
            "observation.left_hand_pose": {"dtype": "float32",
                                           "shape": [21, 3]},
            **info_left,
        },
        "video_extensions": {"d435_rgb": "mp4", "d435_depth": "mp4"},
    }
    with open(os.path.join(task, "meta", "info.json"), "w",
              encoding="utf-8") as f:
        json.dump(info, f)

    mp4 = os.path.join(task, "videos", "chunk-000", "d435_rgb",
                       "episode-000.mp4")
    vw = cv2.VideoWriter(mp4, cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (320, 240))
    assert vw.isOpened(), "VideoWriter 打不开"
    for i in range(n):
        img = np.zeros((240, 320, 3), np.uint8)
        img[:, :, 0] = int(255 * i / n)      # B 渐变，帧号可见
        cv2.putText(img, f"{i}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (255, 255, 255), 2)
        vw.write(img)
    vw.release()

    # 深度流：近景椭圆 ~320mm + 远背景 ~900mm → 12-bit 对数码 →
    # gray12le HEVC MP4（与主程序 core/depth_codec 同式；x265 不可用
    # 时回落 FFV1 gray16le MKV 存毫米 —— demo 两条读取路径都要能跑）
    def _find_ffmpeg():
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and os.path.isfile(exe):
                return exe
        except Exception:
            pass
        return shutil.which("ffmpeg")

    def _quantize(depth_mm):
        codes = np.zeros(depth_mm.shape, dtype="<u2")
        valid = depth_mm > 0
        codes[valid] = np.clip(
            np.rint((np.log(depth_mm[valid].astype(np.float64))
                     - math.log(100.0))
                    * (4095.0 / (math.log(5000.0) - math.log(100.0)))),
            0, 4095).astype("<u2")
        return codes

    ffmpeg = _find_ffmpeg()
    assert ffmpeg, "no ffmpeg"
    yy, xx = np.mgrid[0:240, 0:320]
    mm_frames, code_frames = [], []
    for i in range(n):
        depth = np.full((240, 320), 900.0, np.float64)
        depth += rng.normal(0, 8, (240, 320))
        cx = 160 + 75 * np.sin(2 * np.pi * i / n)
        cy = 120 + 45 * np.cos(2 * np.pi * i / n)
        mask = ((xx - cx) ** 2 / 45.0 ** 2 + (yy - cy) ** 2 / 35.0 ** 2) <= 1.0
        depth[mask] = 320.0
        mm_frames.append(depth.astype("<u2"))
        code_frames.append(_quantize(depth))
    depth_path = os.path.join(task, "videos", "chunk-000",
                              "d435_depth", "episode-000.mp4")
    cmd = [ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "gray12le",
           "-s", "320x240", "-r", "30", "-i", "-",
           "-c:v", "libx265", "-pix_fmt", "gray12le", "-tag:v", "hvc1",
           "-preset", "fast", "-x265-params", "qp=6:range=full", depth_path]
    proc = subprocess.run(cmd, input=np.stack(code_frames).tobytes(),
                          capture_output=True, timeout=300)
    if proc.returncode != 0:
        # x265 不可用 → FFV1 gray16le MKV（存 uint16 毫米）
        depth_path = os.path.join(task, "videos", "chunk-000",
                                  "d435_depth", "episode-000.mkv")
        info["video_extensions"]["d435_depth"] = "mkv"
        with open(os.path.join(task, "meta", "info.json"), "w",
                  encoding="utf-8") as f:
            json.dump(info, f)
        cmd = [ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "gray16le",
               "-s", "320x240", "-r", "30", "-i", "-",
               "-c:v", "ffv1", depth_path]
        proc = subprocess.run(cmd, input=np.stack(mm_frames).tobytes(),
                              capture_output=True, timeout=300)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "ignore")[-400:]
    return parquet_path


def make_bare_session(root: str) -> str:
    """无观测列、无视频的 episode（降级路径）。"""
    task = os.path.join(root, "bare_task")
    os.makedirs(os.path.join(task, "data", "chunk-000"))
    tbl = pa.table({
        "episode_index": pa.array([1, 1, 1], pa.int64()),
        "frame_index": pa.array([0, 1, 2], pa.int64()),
        "timestamp": pa.array([0.0, 0.1, 0.2], pa.float64()),
    })
    p = os.path.join(task, "data", "chunk-000", "episode-000.parquet")
    pq.write_table(tbl, p)
    return p


def find_real_session(recordings_root: str):
    """data/recordings 下任一含视频 mp4 的 episode parquet（无则 None）。"""
    for mp4 in glob.glob(os.path.join(recordings_root, "**", "*.mp4"),
                         recursive=True)[:200]:
        base = os.path.basename(mp4)
        if not base.startswith(("episode-", "file-")):
            continue
        stem = base.rsplit(".", 1)[0]
        task = mp4
        for _ in range(4):                # videos/chunk-NNN/<key>/ 上四层 = task
            task = os.path.dirname(task)
        cdir = os.path.dirname(os.path.dirname(mp4))
        p = os.path.join(task, "data", os.path.basename(cdir),
                         stem + ".parquet")
        if os.path.isfile(p):
            return p
    return None


def main() -> int:
    # ── 导入 demo 模块 ──
    spec = importlib.util.spec_from_file_location("pooled_viewer_demo",
                                                  DEMO_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    print(f"demo 模块导入: {DEMO_PATH}")

    # ── 数据层单测（无 GUI）──
    with tempfile.TemporaryDirectory() as tmp:
        p = make_synthetic_session(tmp)
        s = mod.PooledSession(p)
        check("数据层: 帧数", s.n == 30, f"n={s.n}")
        check("数据层: episode_index", s.episode_index == 1)
        check("数据层: fps 来自 info.json", abs(s.fps - 30.0) < 1e-6)
        check("数据层: 触觉列(左+右)",
              {"left_glove", "right_glove"} <= set(s.tactile))
        check("数据层: IMU 列(仅右手)",
              ("right_glove" in s.imu_quats
               and "right_glove" in s.imu_valid
               and "left_glove" not in s.imu_quats))
        check("数据层: 触觉 vmax > 0",
              s.tactile_vmax["right_glove"] > 0
              and s.tactile_vmax["left_glove"] > 0)
        check("数据层: 视频 1 路", len(s.videos) == 1,
              f"videos={[v.name for v in s.videos]}")
        tf = s.tactile_frame("right_glove", 3)
        check("数据层: 触觉帧形状 16x16", tf.shape == (16, 16))
        quats, valid = s.imu_frame("right_glove", 3)
        check("数据层: IMU 帧形状", quats.shape == (16, 4) and valid.shape == (16,)
              and valid.sum() == 12, f"valid={valid.sum()}")
        check("数据层: 骨架列(右手非零, 左手占位被过滤)",
              s.has_keypoints() and list(s.keypoints) == ["right"])
        kpts = s.keypoints_frame("right", 3)
        check("数据层: 骨架帧形状 21x3", kpts.shape == (21, 3)
              and float(np.abs(kpts).max()) > 0.05)
        vf = s.videos[0][5]
        check("数据层: 视频顺序读", isinstance(vf, np.ndarray)
              and vf.shape == (240, 320, 3))
        vf2 = s.videos[0][3]        # 跳转 seek
        check("数据层: 视频跳转读", isinstance(vf2, np.ndarray))
        vf3 = s.videos[0][99]       # 越界 → 保持最后一帧
        check("数据层: 视频越界保持", vf3 is not None)

        # 深度流（gray12le HEVC mp4 / FFV1 mkv 回退两条路径共用同一读取面）
        check("数据层: 深度 1 路", s.has_depth() and len(s.depth_videos) == 1,
              f"depth_videos={[v.name for v in s.depth_videos]}")
        check("数据层: 深度流全部可解码", not s.skipped_depth,
              f"skipped={s.skipped_depth}")
        dv0 = s.depth_videos[0].read(0)
        mean_bgr = tuple(int(x) for x in dv0.mean(axis=(0, 1)))
        check("数据层: 深度帧 JET 伪彩", isinstance(dv0, np.ndarray)
              and dv0.shape == (240, 320, 3)
              and mean_bgr[0] != mean_bgr[1] != mean_bgr[2],
              f"mean BGR {mean_bgr}")
        check("数据层: 近/远区域颜色分层",
              tuple(dv0[165, 160]) != tuple(dv0[20, 20]),
              f"hand {tuple(dv0[165, 160])} vs bg {tuple(dv0[20, 20])}")
        dv_jump = s.depth_videos[0].read(10)     # 跳转 seek
        check("数据层: 深度跳转读", isinstance(dv_jump, np.ndarray))

        # 深度随机访问回归：后退/大跳必须返回目标帧（旧实现后退返回
        # 错帧——拖进度条时深度面板不动、与其它面板错帧的根因）
        dv = s.depth_videos[0]
        old_max = mod.DepthVideo._PULL_FWD_MAX
        mod.DepthVideo._PULL_FWD_MAX = 5   # 缩小阈值，让 30 帧夹具覆盖重建路径
        try:
            ref = mod.DepthVideo(dv.path)
            ref._open(5)
            f5_ref = ref.read(5)
            dv.read(0)
            dv.read(25)          # 大跳前进 → -ss 重建
            f5_back = dv.read(5)   # 后退 → 必须帧精确
            check("深度: 后退 seek 帧精确",
                  isinstance(f5_ref, np.ndarray) and isinstance(f5_back, np.ndarray)
                  and np.array_equal(f5_ref, f5_back))
            f7 = dv.read(7)        # 小步前进（顺序 pull 路径）
            check("深度: 小步前进帧精确",
                  isinstance(f7, np.ndarray) and not np.array_equal(f7, f5_back))
            check("深度: 越界返回 None", dv.read(s.n + 5) is None)
        finally:
            mod.DepthVideo._PULL_FWD_MAX = old_max
            ref.close()

        # 渲染函数单测（厂商 Glove-test V1.4 移植：只留分区网格，
        # 手形热图已按用户要求移除）
        imu_canvas = mod.render_imu_panel(quats, valid, w=640, h=200)
        check("渲染: IMU 面板", imu_canvas.shape == (200, 640, 3))
        base = s.tactile_baselines["right_glove"]
        check("数据层: 触觉中位基线 16x16", base.shape == (16, 16))
        grid = mod.render_tactile_grid(tf, side="right", baseline=base,
                                       use_baseline=True, w=780, h=560)
        check("渲染: 分区网格(右手)", grid.shape == (560, 780, 3))
        grid_l = mod.render_tactile_grid(tf, side="left", baseline=base,
                                         use_baseline=True, w=780, h=560)
        check("渲染: 分区网格(左手翻转)", grid_l.shape == (560, 780, 3)
              and not np.array_equal(grid, grid_l), "左右布局不同")
        grid_z = mod.render_tactile_grid(np.zeros((16, 16), np.float32),
                                         side="right", w=780, h=560)
        check("渲染: 全零矩阵不崩", grid_z.shape == (560, 780, 3))

        # 手指分区框要扣在"该手指的行落点"上：左手行序镜像（拇指 15-13），
        # 翻转后拇指框仍在画面左侧 —— 旧写法把左右手的框整体对调了
        def _leftmost(img, bgr):
            m = np.all(img == np.array(bgr, np.uint8), axis=-1)
            xs = np.nonzero(m.any(axis=0))[0]
            return int(xs.min()) if len(xs) else -1

        for _side in ("right", "left"):
            g = mod.render_tactile_grid(np.zeros((16, 16), np.float32),
                                        side=_side, w=780, h=560)
            _t = _leftmost(g, mod._TACTILE_FINGER_BGR[0])      # Thumb 红
            _p = _leftmost(g, mod._TACTILE_FINGER_BGR[4])      # Pinky 紫
            check(f"渲染: {_side} 拇指框在小指框左边（拇指统一朝左）",
                  0 <= _t < _p, f"thumb@x={_t} pinky@x={_p}")
        check("渲染: 手形热图函数已移除", not hasattr(mod, "render_tactile_hand"))
        check("渲染: 左右手判定", mod.glove_side_of("right_glove") == "right"
              and mod.glove_side_of("left_glove") == "left"
              and mod.glove_side_of("glove") == "right")
        skel = mod.render_skeleton(kpts, side="right", w=640, h=420)
        check("渲染: 骨架面板", skel.shape == (420, 640, 3)
              and np.count_nonzero(skel.max(axis=-1) >= 200) > 50,
              "有骨骼/坐标轴绘制内容")
        kpts_nan = kpts.copy()
        kpts_nan[7] = np.nan
        skel2 = mod.render_skeleton(kpts_nan, side="right", w=320, h=240)
        check("渲染: 骨架含 NaN 不崩", skel2.shape == (240, 320, 3))

        # ── GUI 离屏 ──
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)
        win = mod.DemoWindow()
        win.load(p)
        app.processEvents()
        check("GUI: 加载后帧数", win.n == 30)
        check("GUI: 触觉/骨架面板可见, IMU 默认隐藏(有骨架时兜底)",
              not win.lbl_hand.isHidden()
              and not win.lbl_skel.isHidden() and win.lbl_imu.isHidden()
              and not win.chk_imu.isHidden())
        check("GUI: 触觉基线勾选可见", not win.chk_tactile_baseline.isHidden()
              and win.chk_tactile_baseline.isChecked())
        check("GUI: 触觉左右手各一个面板（左手在左）",
              win.tactile_sensors == ["left_glove", "right_glove"],
              f"{win.tactile_sensors}")
        check("GUI: 骨架相机距离按整段数据一次拟合(绕腕)",
              abs(win._skel_dists["right"]
                  - mod._fit_dist(s.keypoints["right"].reshape(30, 21, 3)
                                  - s.keypoints["right"].reshape(30, 21, 3)[:, :1, :]))
              < 1e-9,
              f"dist={win._skel_dists.get('right'):.3f}")
        check("GUI: 骨架居中点按整段数据一次计算(相机固定)",
              "right" in win._skel_centers
              and np.isfinite(win._skel_centers["right"]).all())
        d_before = dict(win._skel_dists)
        win.render_frame(15)
        app.processEvents()
        check("GUI: 播放中骨架相机距离不变（不缩放）",
              win._skel_dists == d_before)
        win.chk_imu.setChecked(True)
        app.processEvents()
        check("GUI: 勾选显示 IMU 后面板可见", not win.lbl_imu.isHidden())
        win.chk_imu.setChecked(False)
        app.processEvents()
        win.render_frame(10)
        app.processEvents()
        check("GUI: 逐帧渲染", win.lbl_frame.text().startswith("11 / 30"))
        check("GUI: 深度面板可见", not win.lbl_depth.isHidden())
        win.seek(5)
        app.processEvents()
        check("GUI: 跳帧", win.idx == 5)

        # 帧时钟映射：各路视频按各自帧时钟对齐主时钟（帧率不同按比例）
        class _FakeStream:
            def __init__(self, fps=0.0, n=40):
                self.fps = fps
                self.n = n

            def __len__(self):
                return self.n

        check("GUI: 帧时钟映射(15fps 按比例)",
              win._video_frame_for(_FakeStream(15.0), 10) == 5)
        check("GUI: 帧时钟映射(夹紧尾帧)",
              win._video_frame_for(_FakeStream(15.0), 1000) == 39)
        check("GUI: 帧时钟映射(无 fps 恒等)",
              win._video_frame_for(_FakeStream(), 7) == 7)
        # 播放节奏 = 墙钟对齐（实时+掉帧保速）：无时间流逝时 next_frame
        # 不推进；把起点回拨 2 帧时间后一次 tick 应直接跳到目标帧
        win.toggle_play()
        win.next_frame()
        check("GUI: 播放推进: 无时间流逝不推进", win.idx == 5,
              f"idx={win.idx}")
        win._play_t0 -= 2.0 / win.play_fps
        win.next_frame()
        win.toggle_play()
        check("GUI: 播放推进: 落后时一次跳到目标帧", win.idx == 7,
              f"idx={win.idx}")

        # 播放中拖进度条：松手后应从新位置继续播放，而不是弹回原播放处
        win.seek(3)
        win.toggle_play()               # 播放中
        win._last_slider_render = 0.0
        win._on_slider_moved(20)
        win._on_slider_released()
        check("GUI: 播放中拖动松手落定新位置", win.idx == 20, f"idx={win.idx}")
        # 归零已流逝时间再 tick：上面的松手渲染本身要几十毫秒（深度 seek +
        # 两路触觉网格），真实墙钟会漏进"无时间流逝"里 —— 渲染恰好跨过一
        # 帧就假失败。与下面 -3.0/fps 是同一个套路：这条断言要测的是
        # "tick 不重设节奏起点"，不是渲染耗时。
        win._play_t0 = time.perf_counter() - win.idx / win.play_fps
        win.next_frame()                # 下一拍按新起点推进（无时间流逝→原地）
        check("GUI: 播放中拖动后不被弹回", win.idx == 20, f"idx={win.idx}")
        win._play_t0 -= 3.0 / win.play_fps
        win.next_frame()
        check("GUI: 播放中拖动后按新位置继续播放", win.idx == 23,
              f"idx={win.idx}")
        win.toggle_play()
        win.close()
        app.processEvents()

        # ── 降级路径 ──
        p2 = make_bare_session(tmp)
        s2 = mod.PooledSession(p2)
        check("降级: 空会话可加载", s2.n == 3 and not s2.has_tactile()
              and not s2.has_imu())
        win2 = mod.DemoWindow()
        win2.load(p2)
        app.processEvents()
        check("降级: 无传感器面板隐藏",
              win2.lbl_hand.isHidden() and win2.lbl_imu.isHidden()
              and win2.lbl_skel.isHidden() and win2.chk_imu.isHidden())
        check("降级: 无深度流面板隐藏", win2.lbl_depth.isHidden())
        win2.render_frame(1)
        app.processEvents()
        check("降级: 无视频帧渲染不崩", True)
        win2.close()

        # ── 触觉列全零占位（有深度流）──
        p3 = make_synthetic_session(tmp, tactile_zero=True)
        s3 = mod.PooledSession(p3)
        check("占位: 触觉列存在但全零",
              not s3.has_tactile() and s3.tactile and s3.has_depth())
        win3 = mod.DemoWindow()
        win3.load(p3)
        app.processEvents()
        check("占位: 触觉说明可见、深度可见",
              not win3.lbl_hand.isHidden()
              and win3.lbl_hand.text().startswith("触觉列存在但全为零")
              and not win3.lbl_depth.isHidden())
        win3.render_frame(0)
        app.processEvents()
        check("占位: 渲染不崩", True)
        win3.close()

        # ── 仅右手触觉（单面板回退：无左手数据时显示右手）──
        p4 = make_synthetic_session(tmp, right_only=True)
        s4 = mod.PooledSession(p4)
        check("仅右手触觉: 数据层归类", set(s4.tactile) == {"right_glove"}
              and s4.has_tactile())
        win4 = mod.DemoWindow()
        win4.load(p4)
        app.processEvents()
        check("仅右手触觉: 回退显示右手且面板可见",
              not win4.lbl_hand.isHidden()
              and win4.tactile_sensors == ["right_glove"])
        win4.render_frame(0)
        app.processEvents()
        check("仅右手触觉: 渲染像素图（非说明文字占位）",
              win4.lbl_hand.pixmap() is not None
              and not win4.lbl_hand.pixmap().isNull())
        win4.close()

        # ── 左右手齐全（新数据形态：两只手套都有触觉 + IMU + 骨架）──
        p5 = make_synthetic_session(tmp, both_hands=True)
        s5 = mod.PooledSession(p5)
        check("双手: 数据层两手齐全",
              set(s5.tactile) == {"left_glove", "right_glove"}
              and set(s5.imu_quats) == {"left_glove", "right_glove"}
              and set(s5.keypoints) == {"left", "right"})
        q5, v5 = s5.imu_frame("left_glove", 0)
        check("渲染: IMU 标题带传感器名（左右手可区分）",
              not np.array_equal(
                  mod.render_imu_panel(q5, v5, w=640, h=240,
                                       label="left_glove"),
                  mod.render_imu_panel(q5, v5, w=640, h=240,
                                       label="right_glove")),
              "同一份四元数、只有标题不同 → 画布必须不同")
        win5 = mod.DemoWindow()
        # IMU 面板的渲染闸门是 isVisible()（窗口 show 过才为真），别的
        # 用例只查 isHidden() 所以不用 show；这条要真渲染 IMU 就得 show
        win5.show()
        app.processEvents()
        win5.load(p5)
        app.processEvents()
        check("双手: 触觉左右各一个面板（左手在左）",
              win5.tactile_sensors == ["left_glove", "right_glove"])
        # 截下 _show_image 的入参：面板数直接看拼出来的画布宽度，比数
        # pixmap 可靠（pixmap 是缩放后的，看不出几块）
        grabbed = {}
        orig_show = mod.DemoWindow.__dict__["_show_image"]
        mod.DemoWindow._show_image = staticmethod(
            lambda lbl, bgr: grabbed.__setitem__(lbl, np.asarray(bgr)))
        try:
            win5.render_frame(0)
            app.processEvents()
            win5.chk_imu.setChecked(True)      # 有骨架时 IMU 默认隐藏
            app.processEvents()
        finally:
            mod.DemoWindow._show_image = orig_show
        hand_img = grabbed[win5.lbl_hand]
        pw = max(680, (win5.lbl_hand.width() - 24) // 2)
        check("双手: 触觉画布 = 左右两块并排 + 24px 间隙",
              hand_img.shape[1] == 2 * pw + 24,
              f"{hand_img.shape[1]} vs {2 * pw + 24}")
        sw = max(480, (win5.lbl_skel.width() or 1280) // 2)
        check("双手: 骨架画布 = 左右两块并排 + 8px 间隙",
              grabbed[win5.lbl_skel].shape[1] == 2 * sw + 8,
              f"{grabbed[win5.lbl_skel].shape[1]} vs {2 * sw + 8}")
        iw = max(((win5.lbl_imu.width() or 880) - 4) // 2, 480)
        check("双手: IMU 画布 = 左右两块并排 + 4px 间隙",
              grabbed[win5.lbl_imu].shape[1] == 2 * iw + 4,
              f"{grabbed[win5.lbl_imu].shape[1]} vs {2 * iw + 4}")
        win5.close()

    # ── 真实录制（有则测）──
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    real = find_real_session(os.path.join(repo, "data", "recordings"))
    if real:
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)
        s = mod.PooledSession(real)
        print(f"  真实会话: {real}  (n={s.n}, videos={len(s.videos)}, "
              f"tactile={list(s.tactile)})")
        win = mod.DemoWindow()
        win.load(real)
        app.processEvents()
        win.render_frame(min(5, win.n - 1))
        app.processEvents()
        check("真实会话: 加载+渲染", True, f"n={s.n}")
        win.close()
    else:
        print("  真实会话: data/recordings 下无含视频的 episode，跳过")

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 — {_FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
