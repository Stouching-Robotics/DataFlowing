#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
池化存储查看器演示（单文件自包含，可直接发给客户）

用法:
    python pooled_viewer_demo.py [episode-NNN.parquet]
    不带参数则启动后点"打开文件"选择数据 parquet

读取主程序 v1.1.2+ 池化录制布局（与 core/helpers.py 路径约定一致）:
    <task>/meta/info.json                        # fps / features / cameras
    <task>/data/chunk-NNN/episode-FFF.parquet    # 每行 = 一个 30fps 写入帧
    <task>/videos/chunk-NNN/<image_key>/episode-FFF.mp4   # 每路视频一个文件

画面（自上而下）:
    1) D435 等相机 RGB 视频与深度伪彩视频左右并排（RGB 多路竖排拼接，
       帧号对齐逐帧播放；深度 12-bit 灰度 HEVC MP4 / FFV1 MKV →
       反量化毫米 → 码值 → JET 热力图，与主程序显示口径一致；
       无深度流时 RGB 占满整行）
    2) 手套触觉面板（厂商 Glove-test V1.4 glove_qt_visualizer.py 的
       PressureMatrixCanvas 的 OpenCV 移植：只留 16x16 分区网格，
       手形热图 PressureHandCanvas 移植已按用户要求移除；左右手各一个
       面板并排、左手在左，面板角上标传感器名；会话只有单手数据时
       就只剩那一个面板；可选"触觉基线校正"（每传感器中位基线，
       默认开）。与骨架面板在同一行左右并排（触觉左、骨架右））
    3) 手部骨架面板（工具包 apps/rendering/replay.py 的 MANO 21 关键点
       骨骼渲染，observation.{left,right}_hand_pose，左 | 右；
       视角 = 标定预览初始视角（calibration_pose_preview 的
       _PREVIEW_*：yaw=186.8°/elev=-44.3°/roll=1.5°），每侧绕腕显示
       旋转烘焙进本文件，手背面向观众（右手拇指在画面右侧、左手拇指
       在画面左侧）；相机距离与居中点按整段数据一次计算，播放中
       相机完全不动）
    4) 手套 IMU 面板（16 个 BNO055 的四元数姿态，每格画旋转后的 XYZ 轴；
       每传感器一个面板左右并排、标题带传感器名；有骨架数据时默认隐藏，
       "显示 IMU 四元数"勾选可切出）

parquet 稀疏列约定（与 core/egodata_writer 一致）:
    observation.<sensor>            fixed_size_list<float32,256>  16x16 触觉
    observation.<sensor>_imu_quat   fixed_size_list<float32,64>   16x4 四元数 XYZW
    observation.<sensor>_imu_valid  fixed_size_list<float32,16>   有效掩码
    observation.<side>_hand_pose    fixed_size_list<float32,63>   21x3 骨架（米，
                                   恒写列；全零 = 无骨架数据）
时间对齐: 主时钟 = parquet 行序（录制时视频与传感器同 30fps 写入节拍）。
行序 i 对应时刻 t=i/fps，各路视频按各自帧时钟取 round(t×fps) 帧
（帧率一致即按帧号读；不一致时按比例映射，保证两路画面时间一致）。
RGB 顺序读/跳转 seek、帧数不足保持尾帧；深度小步前进顺序解、后退/
大跳 -ss 重建流（帧精确）；拖动进度条节流渲染、松手落定。

依赖 (见 requirements.txt):
    numpy, pyarrow, opencv-python, PyQt5
"""

import sys
import os
import re
import math
import time
import shutil
import argparse
import json
import subprocess

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import cv2

from PyQt5.QtCore import Qt, QTimer, QLibraryInfo
from PyQt5.QtGui import QColor, QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QSlider, QPushButton,
    QHBoxLayout, QVBoxLayout, QGridLayout, QFileDialog, QSizePolicy,
    QCheckBox,
)

# opencv-python 打包了自家 Qt 插件并在 import cv2 时写入 QT_QPA_PLATFORM_PLUGIN_PATH，
# 会让 PyQt5 去加载 ABI 不匹配的 xcb 插件而崩溃 —— 在这里指回 PyQt5 自带的插件目录
os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = QLibraryInfo.location(
    QLibraryInfo.PluginsPath)

CHUNK_SIZE = 1000          # 每 chunk 1000 个 episode（与主程序一致）


# ══════════════════════════════════════════════════════════════════
# 1. 池化会话数据层
# ══════════════════════════════════════════════════════════════════

_EPISODE_RE = re.compile(r"^(?:episode|file)-(\d{3,})\.parquet$")
_CHUNK_RE = re.compile(r"^chunk-(\d{3,})$")
_DEPTH_KEY_RE = re.compile(r"(^|_)depth$")     # d435_depth / head_depth 等深度流


class Mp4Stream:
    """单路 MP4 视频按帧访问（顺序读快、跳转 seek + 跳转帧缓存）。

    与 hdf5_demo 的 RenderedVideo 相同策略：连续播放走顺序 read
    （~5ms/帧），拖进度条才 seek；解码失败/越界返回最后一帧。
    """

    _CACHE_MAX = 30

    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(
            os.path.dirname(path)))[0]
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise ValueError(f"cannot open video: {path}")
        self.n = max(0, int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)))
        self.fps = float(self.cap.get(cv2.CAP_PROP_FPS)) or 0.0
        self._cache = {}
        self._seq_next = 0
        self._last = None

    def __len__(self):
        return self.n

    def __getitem__(self, i):
        i = int(i)
        if i in self._cache:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            self.cap.read()
            self._seq_next = i + 1
            return self._cache[i]
        if i == self._seq_next:
            ok, frame = self.cap.read()
        else:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, frame = self.cap.read()
            if ok:
                self._cache[i] = frame
                if len(self._cache) > self._CACHE_MAX:
                    self._cache.pop(next(iter(self._cache)))
        self._seq_next = i + 1
        if ok and frame is not None:
            self._last = frame
            return frame
        return self._last

    def close(self):
        try:
            self.cap.release()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# 1.5 深度流读取（主程序 core/depth_codec + depth_reader 的轻量移植）
# ══════════════════════════════════════════════════════════════════
# 深度视频为 12-bit 对数深度码（gray12le HEVC MP4，lerobot v3 同款）或
# 旧 FFV1 gray16le MKV（uint16 毫米）。cv2 直读 gray12le 的 8-bit 转换
# 不可靠，统一走 ffmpeg CLI 解 rawvideo；显示口径 = 码值 → JET（与
# 实时显示同构：mm → log 码 → JET，near/far 线性色标已废弃）。

_DEPTH_MIN_MM = 100.0
_DEPTH_MAX_MM = 5000.0
_DEPTH_QMAX = 4095
_DEPTH_LOG_LO = math.log(_DEPTH_MIN_MM)
_DEPTH_LOG_STEP = _DEPTH_QMAX / (math.log(_DEPTH_MAX_MM) - _DEPTH_LOG_LO)


def _find_ffmpeg():
    """可用的 ffmpeg 二进制（imageio 静态 → PATH），找不到返回 None。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    p = shutil.which("ffmpeg")
    return p if p else None


def _depth_to_heatmap_bgr(mm_or_codes, is_mm):
    """毫米/码值 → BGR JET 热力图（统一显示口径）。"""
    if is_mm:
        mm = np.asarray(mm_or_codes)
        valid = mm > 0
        codes = np.zeros(mm.shape, dtype="<u2")
        if valid.any():
            codes[valid] = np.clip(
                np.rint((np.log(mm[valid].astype(np.float64)) - _DEPTH_LOG_LO)
                        * _DEPTH_LOG_STEP), 0, _DEPTH_QMAX).astype("<u2")
    else:
        codes = np.asarray(mm_or_codes)
    # codes 是 uint16，直接 *255 会按 u2 回绕 —— 必须先升 int32
    c8 = ((np.clip(codes, 0, _DEPTH_QMAX).astype(np.int32) * 255)
          // _DEPTH_QMAX).astype(np.uint8)
    return cv2.applyColorMap(c8, cv2.COLORMAP_JET)


class DepthVideo:
    """单路深度视频随机访问（顺序读复用流，大跳 -ss 快进）。

    与 Mp4Stream 同口径：连续播放走顺序 pull，拖进度条才重建流；
    解码失败/越界返回 None（调用方画占位帧）。
    """

    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(
            os.path.dirname(path)))[0]
        self._ffmpeg = _find_ffmpeg()
        if not self._ffmpeg:
            raise ValueError(f"no ffmpeg binary for depth: {path}")
        self._kind, self._stream = self._probe_kind(path)
        if self._kind is None:
            raise ValueError(f"unsupported depth format: {path}")
        w = h = 0
        fps = 0.0
        total = 0
        try:
            cap = cv2.VideoCapture(path)
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
                total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            cap.release()
        except Exception:
            pass
        if w <= 0 or h <= 0:
            # cv2 探不开灰度 12-bit → ffmpeg -i stderr 兜底
            text = self._probe_text
            m = re.search(r"gray12le[^\n]*?(\d{3,5})x(\d{3,5})[^\n]*?([\d.]+)\s+fps",
                          text)
            if m:
                w, h, fps = int(m.group(1)), int(m.group(2)), float(m.group(3))
        if w <= 0 or h <= 0:
            raise ValueError(f"cannot probe depth size: {path}")
        self.width, self.height = int(w), int(h)
        self.fps = float(fps) if fps > 0 else 30.0
        if total <= 0:
            text = self._probe_text
            dm = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
            if dm and self.fps > 0:
                total = int((int(dm.group(1)) * 3600 + int(dm.group(2)) * 60
                             + float(dm.group(3))) * self.fps)
        self.total = max(1, int(total))
        self._proc = None
        self._buf = b""
        self._next_idx = 0
        self._frame_bytes = self.width * self.height * 2

    @property
    def _probe_text(self):
        if not hasattr(self, "_probe_text_cache"):
            try:
                r = subprocess.run(
                    [self._ffmpeg, "-hide_banner", "-i", self.path],
                    capture_output=True, timeout=30)
                self._probe_text_cache = (r.stderr or b"").decode(
                    "utf-8", "ignore")
            except Exception:
                self._probe_text_cache = ""
        return self._probe_text_cache

    def _probe_kind(self, path):
        text = self._probe_text
        if not text:
            return None, None
        if re.search(r"Video:\s*[^\n]*gray12le", text):
            return "mp4", None      # 12-bit 对数深度码（hevc Rext）
        if re.search(r"Video:\s*ffv1", text):
            # 双流件（v1.0.14）= 流0 热力图 h264 + 流1 FFV1；迁移单流件 = 流0
            idx = None
            for line in text.splitlines():
                m = re.match(r"\s*Stream #0:(\d+)(?:\([^)]*\))?:\s*Video:\s*ffv1",
                             line)
                if m:
                    idx = int(m.group(1))
                    break
            return "mkv", (idx if idx is not None else 0)
        return None, None

    def __len__(self):
        return self.total

    # 顺序拉帧上限：实测 848x480 HEVC 顺序 ~2ms/帧、-ss 重建 ~21-57ms，
    # 30 帧附近两者打平；超过或后退一律 -ss 重建（帧精确、O(1)）
    _PULL_FWD_MAX = 30

    def read(self, idx):
        """读取帧 idx（0-based）→ BGR JET 热力图；越界/EOF 返回 None。

        访问策略: 小步前进（≤_PULL_FWD_MAX）顺序 pull；后退/大跳 -ss
        重建流。旧实现只在进程死亡时重建——后退时顺序流已越过目标帧
        无法回卷，直接 _pull() 返回错帧（拖进度条后退时深度面板不动、
        与其它面板错帧的根因），前进大跳则逐帧解出数千帧（一次 11s+
        的卡死，进度条拖不动的根因）。
        """
        if idx >= self.total:
            return None
        if (self._proc is None or self._proc.poll() is not None
                or idx < self._next_idx
                or idx > self._next_idx + self._PULL_FWD_MAX):
            self._open(idx)
        while self._next_idx < idx:
            if self._pull() is None:
                return None
        return self._pull()

    def close(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=3)
            except Exception:
                pass
            self._proc = None
        self._buf = b""

    def _open(self, idx):
        self.close()
        if not self._ffmpeg:
            return
        cmd = [self._ffmpeg, "-hide_banner", "-nostdin"]
        if idx > 0:
            ss = max(0.0, (idx - 0.5) / self.fps)
            cmd += ["-ss", f"{ss:.6f}"]
        if self._kind == "mp4":
            pix_fmt = "gray12le"
            cmd += ["-i", self.path, "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]
        else:
            cmd += ["-i", self.path, "-map", f"0:{self._stream}",
                    "-f", "rawvideo", "-pix_fmt", "gray16le", "-"]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception:
            self._proc = None
            return
        self._next_idx = idx

    def _pull(self):
        if self._proc is None:
            return None
        try:
            while len(self._buf) < self._frame_bytes:
                chunk = self._proc.stdout.read(65536)
                if not chunk:
                    return None
                self._buf += chunk
            raw = self._buf[:self._frame_bytes]
            self._buf = self._buf[self._frame_bytes:]
            self._next_idx += 1
        except Exception:
            return None
        codes = np.frombuffer(raw, "<u2").reshape(self.height, self.width)
        if self._kind == "mp4":
            # mp4 存的就是 12-bit 码值，直接映射（勿先解出毫米再当码值）
            return _depth_to_heatmap_bgr(codes, is_mm=False)
        return _depth_to_heatmap_bgr(codes, is_mm=True)


def _col_to_matrix(col, n_rows):
    """pyarrow 列 → (n_rows, D) float32 矩阵（失败返回 None）。"""
    try:
        arr = col.combine_chunks()
        if pa.types.is_fixed_size_list(arr.type):
            child = arr.values.to_numpy(zero_copy_only=False)
        else:
            child = arr.to_numpy(zero_copy_only=False)
        m = np.asarray(child)
        if m.ndim == 1:
            m = m.reshape(n_rows, -1)
        return m.astype(np.float32, copy=False)
    except Exception:
        return None


class PooledSession:
    """一个池化 episode 的数据层：parquet 行 + 视频流 + info.json 元数据。

    Attributes:
        n                : 主时钟帧数（= parquet 行数）
        frame_indices    : (N,) int64
        timestamps       : (N,) float64 秒
        fps              : info.json fps → 视频 fps → 30
        tactile          : {sensor: (N,256) float32}  16x16 触觉矩阵列
        imu_quats        : {sensor: (N,64) float32}   → (16,4) XYZW
        imu_valid        : {sensor: (N,16) float32}
        tactile_vmax     : {sensor: float}  全局最大值（色标基准）
        tactile_baselines: {sensor: (16,16)} 逐格中位基线（基线校正）
        videos           : [Mp4Stream]  RGB 视频流
        depth_videos     : [DepthVideo]  深度流（伪彩热力图）
        skipped_depth    : [str] 无法解码的深度流 key
        task_dir / episode_index / info
    """

    def __init__(self, parquet_path):
        self.parquet_path = os.path.abspath(parquet_path)
        if not os.path.isfile(self.parquet_path):
            raise ValueError(f"file not found: {parquet_path}")
        m = _EPISODE_RE.match(os.path.basename(self.parquet_path))
        if not m:
            raise ValueError(
                f"文件名需为 episode-NNN.parquet（或旧 file-NNN.parquet）: "
                f"{os.path.basename(self.parquet_path)}")
        file_index = int(m.group(1))
        chunk_dir = os.path.basename(os.path.dirname(self.parquet_path))
        cm = _CHUNK_RE.match(chunk_dir)
        chunk_index = int(cm.group(1)) if cm else 0
        # data/chunk-NNN/episode-FFF.parquet → task_dir 为上三级
        self.task_dir = os.path.dirname(os.path.dirname(
            os.path.dirname(self.parquet_path)))
        self.episode_index = chunk_index * CHUNK_SIZE + file_index + 1
        self.stem = f"episode-{file_index:03d}"
        self.chunk_index = chunk_index

        # ── info.json（可选）──
        self.info = {}
        info_path = os.path.join(self.task_dir, "meta", "info.json")
        if os.path.isfile(info_path):
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    self.info = json.load(f) or {}
            except (OSError, json.JSONDecodeError):
                self.info = {}
        if not isinstance(self.info, dict):
            self.info = {}
        self.fps = 30.0
        try:
            if float(self.info.get("fps") or 0) > 0:
                self.fps = float(self.info["fps"])
        except (TypeError, ValueError):
            pass

        # ── data parquet ──
        tbl = pq.read_table(self.parquet_path)
        self.n = len(tbl)
        if self.n == 0:
            raise ValueError("parquet 为空: " + self.parquet_path)
        schema = tbl.schema
        self.frame_indices = (
            tbl.column("frame_index").to_numpy(zero_copy_only=False).astype(np.int64)
            if schema.get_field_index("frame_index") >= 0
            else np.arange(self.n, dtype=np.int64))
        self.timestamps = (
            tbl.column("timestamp").to_numpy(zero_copy_only=False).astype(np.float64)
            if schema.get_field_index("timestamp") >= 0
            else np.zeros(self.n, np.float64))

        self.tactile, self.imu_quats, self.imu_valid = {}, {}, {}
        self.keypoints = {}
        for field in schema:
            name = field.name
            if not name.startswith("observation."):
                continue
            sensor = name[len("observation."):]
            mat = _col_to_matrix(tbl.column(name), self.n)
            if mat is None or mat.shape[1] == 0:
                continue
            if name.endswith("_imu_quat"):
                if mat.shape[1] == 64:
                    self.imu_quats[sensor[:-len("_imu_quat")]] = mat
            elif name.endswith("_imu_valid"):
                if mat.shape[1] == 16:
                    self.imu_valid[sensor[:-len("_imu_valid")]] = mat
            elif name.endswith("_hand_pose"):
                # 恒写占位列：全零 = 无骨架数据（旧录制/未 warmup），过滤掉
                if (mat.shape[1] == 63 and self.n
                        and float(np.nanmax(np.abs(mat))) > 1e-6):
                    self.keypoints[sensor[:-len("_hand_pose")]] = mat
            elif mat.shape[1] == 256:
                self.tactile[sensor] = mat
        # 触觉全局最大值（热力图/手掌归一化基准，全零传感器视为未触碰）
        self.tactile_vmax = {}
        for sensor, mat in self.tactile.items():
            v = float(np.nanmax(mat)) if self.n else 0.0
            self.tactile_vmax[sensor] = v if v > 0 else 0.0
        # 触觉中位基线（每传感器 16x16 逐格中位数，厂商 Glove-test 工具的
        # 基线校正等价物；"触觉基线校正"勾选时网格显示 值-基线）
        self.tactile_baselines = {}
        for sensor, mat in self.tactile.items():
            self.tactile_baselines[sensor] = np.median(
                mat.reshape(self.n, 16, 16), axis=0).astype(np.float32)

        # ── 视频 ──
        self.videos, self.depth_videos, self.skipped_depth = [], [], []
        vroot = os.path.join(self.task_dir, "videos", f"chunk-{chunk_index:03d}")
        if os.path.isdir(vroot):
            for key in sorted(os.listdir(vroot)):
                kd = os.path.join(vroot, key)
                if not os.path.isdir(kd):
                    continue
                for fn in sorted(os.listdir(kd)):
                    if not fn.startswith(self.stem + "."):
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in (".mp4", ".mkv", ".avi"):
                        continue
                    if _DEPTH_KEY_RE.search(key) or ext == ".mkv":
                        # 深度流：12-bit gray12le HEVC MP4 / 旧 FFV1 gray16le
                        # MKV → DepthVideo（ffmpeg 解码 → 伪彩热力图）
                        try:
                            self.depth_videos.append(
                                DepthVideo(os.path.join(kd, fn)))
                        except ValueError:
                            self.skipped_depth.append(key)
                        continue
                    try:
                        self.videos.append(Mp4Stream(os.path.join(kd, fn)))
                    except ValueError:
                        pass
        # fps 兜底：视频实际帧率优先于 info.json 缺失值
        for v in self.videos:
            if v.fps > 0:
                self.fps = v.fps
                break

    def has_tactile(self):
        return any(v > 0 for v in self.tactile_vmax.values())

    def has_depth(self):
        return bool(self.depth_videos)

    def has_imu(self):
        return bool(self.imu_quats)

    def has_keypoints(self):
        return bool(self.keypoints)

    def keypoints_frame(self, side, idx):
        return self.keypoints[side][idx].reshape(21, 3)

    def tactile_frame(self, sensor, idx):
        return self.tactile[sensor][idx].reshape(16, 16)

    def imu_frame(self, sensor, idx):
        quats = self.imu_quats[sensor][idx].reshape(16, 4).copy()
        valid = (self.imu_valid[sensor][idx].reshape(16) > 0.5
                 if sensor in self.imu_valid
                 else np.ones(16, dtype=bool))
        return quats, valid

    def close(self):
        for v in self.videos:
            v.close()
        self.videos = []
        for dv in self.depth_videos:
            dv.close()
        self.depth_videos = []


# ══════════════════════════════════════════════════════════════════
# 2. 渲染工具（手套触觉面板 / IMU 姿态面板）
# ══════════════════════════════════════════════════════════════════
# 手套触觉可视化移植自厂商 Glove-test V1.4 工具
# glove_qt_visualizer.py 的 PressureMatrixCanvas
# （QPainter → OpenCV，QColor → BGR，中文字串 → 英文，布局/配色照搬）:
#   render_tactile_grid —— 16x16 分区网格（手指区 y=12…15 / 手掌区 y=3…11 /
#                         空值区 y≤2，左右手行翻转；行列号 + 逐格数值 + 图例）
#   （2026-09-04 起按用户要求移除手形热图 PressureHandCanvas 移植，只留矩阵）
# 触觉矩阵映射（2026-09-04 与实机录制数据逐格核对）:
#   行 = 手指（右手 拇指 1-3/食指 4-6/中指 7-9/无名 10-12/小指 13-15；
#             左手镜像 拇指 15-13 … 小指 3-1），行 0 为空；
#   列 12-15 = 指骨，列序 [14,12,13,15] = 根→尖；列 3-11 = 掌心
#   （右手传感列 [10,9,8,6,4] / 左手 [10,9,8,6]），列 0-2 为空。
# 面板内拇指统一朝左：网格 x 轴 = 矩阵行，左手翻转、右手不翻
# （与厂商网格恰好对调，因左手行序与厂商 README 假定相反）。
# 手指分区框与图例里的 x 区间也按各手的行序给（左手 拇指 x=13..15、
# 小指 x=1..3），所以两只手的拇指框都落在画面左侧 —— 2026-09-20 修正：
# 旧写法按「两只手拇指都在行 1-3」摆框，左手的五个框整体对调了
# （分区框/图例与 Spare 框、掌心框口径不一致）。

_TACTILE_FINGER_NAMES = ("Thumb", "Index", "Middle", "Ring", "Pinky")
# 手指区分区框颜色（厂商 _FINGER_COLORS，BGR）
_TACTILE_FINGER_BGR = ((68, 68, 239), (11, 158, 245), (94, 197, 34),
                       (246, 130, 59), (247, 85, 168))
# 压力色带（厂商 PRESSURE_COLOR_BANDS_CORRECTED / _RAW，BGR）
_TACTILE_BANDS_CORRECTED = ((333, (95, 58, 30)), (666, (199, 134, 22)),
                            (999, (94, 197, 34)), (1333, (21, 204, 250)),
                            (1666, (22, 115, 249)), (float("inf"), (68, 68, 239)))
_TACTILE_BANDS_RAW = ((2000, (95, 58, 30)), (2400, (199, 134, 22)),
                      (2800, (94, 197, 34)), (3200, (21, 204, 250)),
                      (3600, (22, 115, 249)), (float("inf"), (68, 68, 239)))
_TACTILE_BG = (25, 16, 8)          # #081019
_TACTILE_CELL_IDLE = (49, 33, 18)  # #122131（无数据格底色）
_TACTILE_EMPTY_OVERLAY = (48, 38, 30)   # (30,38,48) 斜线覆盖
_TACTILE_GRID_BORDER = (239, 225, 210)  # (210,225,239,105) 简化实线
_TACTILE_TEXT = (245, 232, 220)    # #dce8f5
_TACTILE_TEXT_DIM = (210, 189, 169)     # #a9bdd2
_TACTILE_TEXT_FAINT = (185, 148, 148)   # #94a3b8
_TACTILE_EMPTY_LINE = (184, 163, 148)   # #94a3b8
_TACTILE_SPARE_LINE = (219, 213, 209)   # #d1d5db
_TACTILE_PALM_BLUE = (248, 189, 56)     # #38bdf8
_TACTILE_WHITE = (252, 250, 248)        # #f8fafc


def glove_side_of(sensor: str) -> str:
    """传感器列名 → 左右手（默认右手）。"""
    low = (sensor or "").lower()
    return "left" if "left" in low else "right"


def _tactile_heat_color(value, use_baseline):
    bands = _TACTILE_BANDS_CORRECTED if use_baseline else _TACTILE_BANDS_RAW
    for upper, bgr in bands:
        if value <= upper:
            return bgr
    return bands[-1][1]


def render_tactile_grid(matrix, side="right", baseline=None,
                        use_baseline=True, w=780, h=560):
    """16x16 压力矩阵 → 分区确认网格（厂商 PressureMatrixCanvas 移植）。

    网格行 = 矩阵列（y=15 在顶 = 指尖），网格列 = 矩阵行（拇指朝左：
    左手翻转、右手不翻）；baseline 为 16x16 中位基线（可 None）。
    """
    m = np.asarray(matrix, np.float32).reshape(16, 16)
    # 网格 (x, y) = (矩阵行, 矩阵列) —— 厂商网格即直接读 m[x, y]，不转置
    img = np.full((h, w, 3), _TACTILE_BG, np.uint8)
    left_margin, top_margin, bottom_margin = 54.0, 54.0, 22.0
    legend_width = 250.0
    flip = (side == "left")                     # 左手行序反转 → 网格 x 翻转
    cell = max(10.0, min((w - left_margin - legend_width) / 16.0,
                         (h - top_margin - bottom_margin) / 16.0))
    grid_left, grid_top = left_margin, top_margin
    if baseline is not None:
        base = np.asarray(baseline, np.float32).reshape(16, 16)

    def display_value(x, y):
        value = float(m[x, y])
        if use_baseline and baseline is not None:
            value = max(0.0, value - float(base[x, y]))
        return int(round(value))

    cv2.putText(img, "X -> 0..15" if not flip else "X -> 15..0",
                (int(grid_left) + 16 * int(cell) // 2 - 70, 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, _TACTILE_TEXT_DIM,
                1, cv2.LINE_AA)
    for display_x in range(16):
        x = display_x if not flip else 15 - display_x
        cv2.putText(img, str(x),
                    (int(grid_left + display_x * cell) + int(cell) // 2 - 5,
                     int(grid_top) - 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_DIM,
                    1, cv2.LINE_AA)
    for display_row in range(16):
        y = 15 - display_row
        row_top = grid_top + display_row * cell
        cv2.putText(img, f"y={y}",
                    (6, int(row_top + cell / 2 + 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_DIM,
                    1, cv2.LINE_AA)
        for display_x in range(16):
            x = display_x if not flip else 15 - display_x
            value = display_value(x, y)
            x0 = int(grid_left + display_x * cell)
            y0 = int(row_top)
            x1 = int(grid_left + (display_x + 1) * cell)
            y1 = int(row_top + cell)
            fill = _tactile_heat_color(value, use_baseline)
            cv2.rectangle(img, (x0, y0), (x1, y1), fill, -1)
            is_empty = y <= 2
            is_spare = y >= 3 and x == 0
            if is_empty or is_spare:
                cv2.rectangle(img, (x0, y0), (x1, y1),
                              _TACTILE_EMPTY_OVERLAY, -1)
                cv2.line(img, (x0, y0), (x1, y1), _TACTILE_EMPTY_LINE, 1)
                cv2.line(img, (x1, y0), (x0, y1), _TACTILE_EMPTY_LINE, 1)
            cv2.rectangle(img, (x0, y0), (x1, y1), _TACTILE_GRID_BORDER, 1)
            lum = (0.299 * fill[2] + 0.587 * fill[1] + 0.114 * fill[0])
            text_color = (7, 16, 25) if lum > 145 else (255, 246, 238)
            if cell >= 11:
                cv2.putText(img, str(value), (x0 + 2, y0 + int(cell) - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.28, text_color,
                            1, cv2.LINE_AA)

    finger_height = 4.0 * cell
    for group in range(5):
        # 框要扣在该手指的行落点上：右手行序 1..15（拇指 1-3），左手镜像
        # （拇指 15-13）→ 翻转后第 g 组落在显示列 3g..3g+2，拇指仍在画面
        # 左侧。与下面 Spare 框（行 0）、掌心框（行 1..15）同一口径。
        display_start_x = 3 * group if flip else 1 + group * 3
        x0 = int(grid_left + display_start_x * cell)
        y0 = int(grid_top)
        cv2.rectangle(img, (x0 + 2, y0 + 2),
                      (x0 + int(3 * cell) - 2, y0 + int(finger_height) - 2),
                      _TACTILE_FINGER_BGR[group], 2)
    spare_x = 0 if not flip else 15
    x0 = int(grid_left + spare_x * cell)
    cv2.rectangle(img, (x0 + 1, int(grid_top) + 1),
                  (x0 + int(cell) - 1, int(grid_top + finger_height) - 1),
                  _TACTILE_SPARE_LINE, 1)
    palm_left = grid_left + (cell if not flip else 0.0)
    x0 = int(palm_left)
    y0 = int(grid_top + 4.0 * cell)
    cv2.rectangle(img, (x0 + 2, y0 + 2),
                  (x0 + int(15 * cell) - 2, y0 + int(9 * cell) - 2),
                  _TACTILE_PALM_BLUE, 2)
    cv2.line(img, (x0, y0), (x0 + int(15 * cell), y0), _TACTILE_WHITE, 2)

    legend_left = int(grid_left + 16 * cell + 18.0)
    if legend_left < w - 10:
        cv2.putText(img, "Fingers  y=12..15", (legend_left, int(grid_top) + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, _TACTILE_TEXT, 1, cv2.LINE_AA)
        for group in range(5):
            top = int(grid_top + 28.0 + group * 24.0)
            cv2.rectangle(img, (legend_left, top),
                          (legend_left + 15, top + 15),
                          _TACTILE_FINGER_BGR[group], -1)
            row_lo = 13 - 3 * group if flip else 1 + 3 * group
            cv2.putText(img, f"{_TACTILE_FINGER_NAMES[group]}: x={row_lo}..{row_lo+2}",
                        (legend_left + 23, top + 13),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT, 1, cv2.LINE_AA)
        cv2.putText(img, "Spare col: x=0, y=3..15",
                    (legend_left, int(grid_top + 166.0)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_FAINT,
                    1, cv2.LINE_AA)
        cv2.putText(img, "Palm  x=1..15, y=3..11",
                    (legend_left, int(grid_top + 208.0)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_PALM_BLUE,
                    1, cv2.LINE_AA)
        cv2.putText(img, "Empty zone: y=0..2",
                    (legend_left, int(grid_top + 232.0)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT, 1, cv2.LINE_AA)
        cv2.putText(img, "Top of view = y=15..0",
                    (legend_left, int(grid_top + 258.0)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_FAINT,
                    1, cv2.LINE_AA)
        cv2.putText(img, "fingers point upward",
                    (legend_left, int(grid_top + 280.0)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_FAINT,
                    1, cv2.LINE_AA)
    return img


def put_label(img, text, pos, color=(255, 255, 255)):
    """叠加标签文字（带黑底）。"""
    if (not isinstance(img, np.ndarray) or img.dtype == np.object_
            or img.size == 0 or img.ndim != 3 or img.shape[2] != 3):
        return
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
    x, y = pos
    cv2.rectangle(img, (x - 4, y - th - 6), (x + tw + 4, y + 4), (0, 0, 0), -1)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, color, 2, cv2.LINE_AA)


# ── IMU 四元数姿态面板 ──────────────────────────────────

_VIEW_DIR = np.array([0.6, -0.7, 1.0], dtype=np.float64)
_VIEW_DIR /= np.linalg.norm(_VIEW_DIR)
_WORLD_UP = np.array([0.0, 1.0, 0.0], dtype=np.float64)
_E1 = np.cross(_VIEW_DIR, _WORLD_UP)
_E1 /= np.linalg.norm(_E1)
_E2 = np.cross(_VIEW_DIR, _E1)      # 屏幕 x = dot(p, e1), 屏幕 y = dot(p, e2)
_AXIS_BGR = ((60, 60, 255),         # X 红
             (60, 220, 60),         # Y 绿
             (255, 120, 40))        # Z 蓝
_AXIS_NAMES = ("X", "Y", "Z")


def _quat_to_rotmat(q):
    """四元数 XYZW → 3x3 旋转矩阵。"""
    x, y, z, w = (float(v) for v in q)
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return np.array([
        [1 - 2 * (yy + zz), 2 * (xy - wz),     2 * (xz + wy)],
        [2 * (xy + wz),     1 - 2 * (xx + zz), 2 * (yz - wx)],
        [2 * (xz - wy),     2 * (yz + wx),     1 - 2 * (xx + yy)],
    ], dtype=np.float64)


def _project(p3):
    """3D 点 → 2D 屏幕坐标（斜视正交投影）。"""
    return float(np.dot(p3, _E1)), float(np.dot(p3, _E2))


def _arrow(img, p0, p1, color, thick=2):
    """带箭头的线段（p0→p1，三角箭头）。"""
    cv2.line(img, tuple(p0), tuple(p1), color, thick, cv2.LINE_AA)
    d = np.array(p1, np.float64) - np.array(p0, np.float64)
    n = np.linalg.norm(d)
    if n < 1e-6:
        return
    t = d / n
    nrm = np.array([-t[1], t[0]])
    tip = np.array(p1, np.float64)
    cv2.fillConvexPoly(img, np.array([
        tip + t * 5,
        tip - t * 3 + nrm * 4,
        tip - t * 3 - nrm * 4,
    ], np.int32), color, cv2.LINE_AA)


def render_imu_panel(quats, valid, w=880, h=240, label=""):
    """手套 IMU 姿态面板：16 个 BNO055 各画一格，格内为旋转后的 XYZ 轴。

    Args:
        quats: (16,4) float XYZW
        valid: (16,) bool
        label: 传感器名（左右手各一个面板时用于区分，空则不显示）
    Returns:
        BGR 画布
    """
    canvas = np.full((h, w, 3), 18, np.uint8)
    n_valid = int(np.count_nonzero(valid))
    title = "Glove IMU 四元数姿态"
    if label:
        title += f" [{label}]"
    cv2.putText(canvas, f"{title} ({n_valid}/16 sensors)",
                (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (220, 220, 220), 1, cv2.LINE_AA)

    cols, rows = 4, 4
    top, gap = 30, 6
    cw = max(40, (w - 2 * gap) // cols)
    ch = max(40, (h - top - gap) // rows)
    for i in range(16):
        cx0 = gap + (i % cols) * cw
        cy0 = top + (i // cols) * ch
        x1, y1 = cx0 + cw - gap, cy0 + ch - gap
        ok = bool(valid[i]) and np.isfinite(quats[i]).all()
        cv2.rectangle(canvas, (cx0, cy0), (x1, y1),
                      (60, 160, 60) if ok else (60, 60, 60),
                      1 if ok else 1)
        cv2.putText(canvas, f"{i}", (cx0 + 3, cy0 + 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                    (120, 200, 120) if ok else (110, 110, 110),
                    1, cv2.LINE_AA)
        c = (cx0 + x1) // 2, (cy0 + y1) // 2
        if not ok:
            cv2.line(canvas, (c[0] - 6, c[1] - 6), (c[0] + 6, c[1] + 6),
                     (90, 90, 90), 1, cv2.LINE_AA)
            cv2.line(canvas, (c[0] - 6, c[1] + 6), (c[0] + 6, c[1] - 6),
                     (90, 90, 90), 1, cv2.LINE_AA)
            continue
        try:
            R = _quat_to_rotmat(quats[i])
        except Exception:
            continue
        scale = min(cw, ch) * 0.30
        for k in range(3):
            tip = _project(R @ np.eye(3)[k] * scale)
            _arrow(canvas,
                   (int(c[0]), int(c[1])),
                   (int(c[0] + tip[0]), int(c[1] - tip[1])),
                   _AXIS_BGR[k], 2)
        cv2.putText(canvas, "XYZ", (x1 - 34, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.3, (140, 140, 140),
                    1, cv2.LINE_AA)
    return canvas


# ══════════════════════════════════════════════════════════════════
# 2.5 手部骨架渲染（工具包 apps/rendering/replay.py 的 MANO 21 关键点
#     骨骼渲染器移植：BONES / FINGER_BGR / Camera / _fit_dist / _bg /
#     _grid / _draw_hand，画布尺寸参数化以适配面板，像素口径一致）
# ══════════════════════════════════════════════════════════════════

# MANO 21 关键点骨骼连接 + 分组: 0 拇指 1 食指 2 中指 3 无名指 4 小指 5 掌骨
BONES = [
    (0, 1, 0), (1, 2, 0), (2, 3, 0), (3, 4, 0),                # thumb
    (0, 5, 1), (5, 6, 1), (6, 7, 1), (7, 8, 1),                # index
    (0, 9, 2), (9, 10, 2), (10, 11, 2), (11, 12, 2),           # middle
    (0, 13, 3), (13, 14, 3), (14, 15, 3), (15, 16, 3),         # ring
    (0, 17, 4), (17, 18, 4), (18, 19, 4), (19, 20, 4),         # pinky
    (5, 9, 5), (9, 13, 5), (13, 17, 5),                        # metacarpal
]
# 每根手指分组颜色（BGR）
FINGER_BGR = [
    (60, 80, 255),     # 0 拇指 红
    (60, 255, 255),    # 1 食指 黄
    (60, 220, 60),     # 2 中指 绿
    (255, 210, 60),    # 3 无名指 青
    (255, 120, 255),   # 4 小指 紫
    (210, 210, 210),   # 5 掌骨 白
]

_SKEL_FOV_DEG = 50.0


# 骨架显示旋转（绕腕部施加，数值烘焙自 stouch_glove_toolkit 的
# calibration_pose_preview 初始视角）:
#   canonical = hand_display_basis(中性右手).T（预览注释: 中性手放在世界 XY
#   网格上，指尖朝 +Y、手背法线朝 +Z），左手先做 align_left_to_right_reference。
#   该 canonical 视角下相机在 -Z 侧望向 +Z，看到的是掌心；再叠加绕 Y 轴
#   180°（diag(-1,1,-1)，det=1 的旋转）把手背翻向 -Z 正对相机 → 观众看到
#   手背（右手拇指在画面右侧、左手拇指在画面左侧，与真实手背一致）。
#   渲染约定 rotated = (kpts - wrist) @ R（与 scipy 的
#   R.from_matrix(R.T) apply 逐点等价，已数值验证，max 误差 ~2e-8）。
_SKEL_ROT_LEFT = np.array([
    [0.16005944656801546, 0.9859625704810917, 0.04752665751616468],
    [0.07954678139330758, -0.06087436335953418, 0.9949706636155343],
    [0.983896988070553, -0.1554738611339206, -0.08817366596555032],
], np.float64)
_SKEL_ROT_RIGHT = np.array([
    [0.16005944656801546, -0.9859625704810917, -0.04752665751616468],
    [-0.07954678139330756, -0.06087436335953418, 0.9949706636155343],
    [-0.983896988070553, -0.15547386113392053, -0.08817366596555033],
], np.float64)

# 标定预览初始视角相机角（calibration_pose_preview 的 _PREVIEW_*）
_SKEL_YAW_DEG = 186.80125157005767
_SKEL_ELEV_DEG = -44.31525254354176
_SKEL_ROLL_DEG = 1.524153207538752


class SkeletonCamera:
    """球坐标相机 + 透视投影（照工具包 replay.py 的 Camera，画布尺寸参数化）。

    pan_px: 图像空间平移（照工具包 Camera，用于把手居中，等价预览里
    pan = (w - cx, h - cy) 的构图逻辑）。
    """

    def __init__(self, yaw_deg, elev_deg, dist, img_w, img_h, roll_deg=0.0,
                 pan_px=(0.0, 0.0)):
        yaw, elev = np.deg2rad(yaw_deg), np.deg2rad(elev_deg)
        roll = np.deg2rad(roll_deg)
        cp = np.array([dist * np.cos(elev) * np.sin(yaw),
                       dist * np.sin(elev),
                       dist * np.cos(elev) * np.cos(yaw)])
        self.pos = cp
        self.fwd = -cp / np.linalg.norm(cp)
        self.right = np.cross(self.fwd, [0, 1, 0])
        self.right /= np.linalg.norm(self.right)
        self.up = np.cross(self.right, self.fwd)
        cos_r, sin_r = np.cos(roll), np.sin(roll)
        r0, u0 = self.right, self.up
        self.right = r0 * cos_r + u0 * sin_r
        self.up = -r0 * sin_r + u0 * cos_r
        self.w, self.h = img_w, img_h
        self.f = (self.h / 2) / np.tan(np.deg2rad(_SKEL_FOV_DEG) / 2)
        self.pan_px = [float(pan_px[0]), float(pan_px[1])]

    def project(self, pts3d):
        """(N,3) → (N,2) 图像坐标 + (N,) 深度（相机前方为正）。"""
        v = pts3d - self.pos
        z = v @ self.fwd
        x = v @ self.right
        y = v @ self.up
        u = self.f * x / z + self.w / 2 + self.pan_px[0]
        vv = self.h / 2 - self.f * y / z + self.pan_px[1]
        return np.stack([u, vv], -1), z


def _fit_dist(kpts):
    """按手部空间尺度定相机距离（照工具包 replay.py）。"""
    v = kpts[np.isfinite(kpts).all(axis=-1)]
    if len(v) == 0:
        return 0.5
    r = float(np.abs(v).max())
    return max(0.25, r * 2.6 + 0.1)


def _skel_bg(w, h):
    img = np.empty((h, w, 3), np.uint8)
    ramp = (10 + (10 * np.arange(h) // h)).astype(np.uint8)
    img[:] = ramp[:, None, None]
    return img


def _skel_grid(img, cam, r):
    """z=0 平面网格 + 三色坐标轴（照工具包 replay.py 的 _grid）。"""
    x = [(i / 6) * r for i in range(-6, 7)]
    lines = ([([v, -r, 0.0], [v, r, 0.0]) for v in x]
             + [([-r, v, 0.0], [r, v, 0.0]) for v in x])
    for p0, p1 in lines:
        pts, z = cam.project(np.array([p0, p1], np.float32))
        if (z > 0).all():
            cv2.line(img, tuple(pts[0].astype(int)), tuple(pts[1].astype(int)),
                     (40, 44, 52), 1, cv2.LINE_AA)
    origin = np.zeros((3,), np.float32)
    for axis, col in [(np.array([r, 0.0, 0.0]), (40, 60, 255)),    # X 红
                      (np.array([0.0, r, 0.0]), (40, 255, 60)),    # Y 绿
                      (np.array([0.0, 0.0, r]), (255, 60, 40))]:   # Z 蓝
        pts, z = cam.project(np.stack([origin, axis]))
        if (z > 0).all():
            cv2.line(img, tuple(pts[0].astype(int)), tuple(pts[1].astype(int)),
                     col, 2, cv2.LINE_AA)


def _draw_skeleton_hand(img, cam, kpts):
    """深度着色的骨骼连线 + 白色关节点（照工具包 replay.py 的 _draw_hand）。"""
    pts, z = cam.project(kpts)
    vis = np.isfinite(kpts).all(axis=1) & (z > 0)
    # 近亮远暗的深度着色；手指比掌骨略亮
    zmin, zmax = 0.05, 0.6
    lum = np.clip((zmax - z) / (zmax - zmin), 0.35, 1.0)
    for a, b, g in BONES:
        if vis[a] and vis[b]:
            k = 0.5 * (lum[a] + lum[b])
            col = tuple(int(c * k) for c in FINGER_BGR[g])
            cv2.line(img, tuple(pts[a].astype(int)), tuple(pts[b].astype(int)),
                     col, 3, cv2.LINE_AA)
    for p, ok in zip(pts, vis):
        if ok:
            cv2.circle(img, tuple(p.astype(int)), 2, (255, 255, 255), -1,
                       cv2.LINE_AA)


def render_skeleton(kpts, side="right", w=640, h=420, dist=None, center=None,
                    label=""):
    """单手骨架面板：手背面向观众（标定预览初始视角的相机角 + 绕腕显示旋转）。

    视角 = calibration_pose_preview 的初始视角相机
    （_PREVIEW_YAW/ELEV/ROLL：yaw=186.80°/elev=-44.32°/roll=1.52°），
    每侧手先绕腕部施加烘焙好的显示旋转（中性手 → 指尖 +Y、手背翻向
    相机，矩阵见 _SKEL_ROT_LEFT/_SKEL_ROT_RIGHT；约定
    rotated = (kpts - wrist) @ R），再像预览一样按手部投影中心平移居中
    （pan = (w/2 - cx, h/2 - cy)），手腕落在原点、手指朝上、
    观众看到手背（右手拇指在画面右侧、左手拇指在画面左侧）。

    Args:
        kpts: (21,3) float 关键点（米，MANO 顺序 0=腕）
        side: "left" / "right" 选用哪侧的预览旋转矩阵
        dist: 相机距离（None → 按当前帧 _fit_dist；DemoWindow 传入
              load 时按整段数据一次拟合的固定距离，播放中不缩放）
        center: 居中用的 3D 手部中心（None → 按当前帧包围盒中心；
                DemoWindow 传入 load 时按整段数据一次计算的固定中心，
                播放中相机完全不动、画面不抖）
    Returns:
        BGR 画布
    """
    if dist is None:
        dist = _fit_dist(kpts)
    rot = _SKEL_ROT_LEFT if side == "left" else _SKEL_ROT_RIGHT
    finite = np.isfinite(kpts).all(axis=-1)
    wrist = kpts[0] if finite[0] else np.zeros(3, np.float64)
    rotated = (kpts - wrist) @ rot
    img = _skel_bg(w, h)
    # 先无平移投影一次求手部中心，再按 (w/2-cx, h/2-cy) 平移居中（照预览）
    probe = SkeletonCamera(yaw_deg=_SKEL_YAW_DEG, elev_deg=_SKEL_ELEV_DEG,
                           roll_deg=_SKEL_ROLL_DEG, dist=dist,
                           img_w=w, img_h=h)
    if center is None:
        ok = np.isfinite(rotated).all(axis=-1)
        center_pts = rotated[ok] if ok.any() else np.zeros((1, 3), np.float64)
        center = 0.5 * (center_pts.min(axis=0) + center_pts.max(axis=0))
    pc, _ = probe.project(center[None])
    cam = SkeletonCamera(yaw_deg=_SKEL_YAW_DEG, elev_deg=_SKEL_ELEV_DEG,
                         roll_deg=_SKEL_ROLL_DEG, dist=dist,
                         img_w=w, img_h=h,
                         pan_px=(w / 2 - float(pc[0, 0]),
                                 h / 2 - float(pc[0, 1])))
    _skel_grid(img, cam, dist * 0.5)
    _draw_skeleton_hand(img, cam, rotated)
    if label:
        cv2.putText(img, label, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (240, 240, 240), 2, cv2.LINE_AA)
    return img


# ══════════════════════════════════════════════════════════════════
# 3. 主窗口
# ══════════════════════════════════════════════════════════════════
class DemoWindow(QMainWindow):
    def __init__(self, parquet_path=None):
        super().__init__()
        self.setWindowTitle("Pooled Data Viewer")
        self.resize(1500, 920)
        self.data = None
        self.idx = 0
        self.playing = False
        self._play_t0 = 0.0   # 播放实时节奏起点（toggle_play 时按当前帧折算）
        self._pending_seek = None      # 拖动进度条期间的待渲染目标帧
        self._last_slider_render = 0.0  # 上次拖动渲染时刻（节流用）
        self._skel_dists = {}    # 骨架相机距离（每侧，整段数据一次拟合后固定）
        self._skel_centers = {}  # 骨架居中点（每侧，整段数据一次计算后固定）
        self.tactile_sensors = []  # 触觉面板显示的传感器（优先左手，无左手回退右手）

        central = QWidget()
        # 暗色底：面板被隐藏时，空网格单元不露出白色窗口背景。
        # 用调色板+自动填充而不是 QSS —— QSS 无选择器规则会级联到
        # 所有子控件，把底部控制条的按钮/滑条/勾选框也染成深底深字
        pal = central.palette()
        pal.setColor(central.backgroundRole(), QColor("#111111"))
        central.setPalette(pal)
        central.setAutoFillBackground(True)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.lbl_video = QLabel("Video: D435 RGB streams")
        self.lbl_depth = QLabel("Depth (Pseudo-color)")
        self.lbl_hand = QLabel("Glove Tactile Matrix Panel")
        self.lbl_skel = QLabel("Hand Skeleton (MANO 21 keypoints)")
        self.lbl_imu = QLabel("IMU Quaternions")
        for lb in (self.lbl_video, self.lbl_depth, self.lbl_hand,
                   self.lbl_skel, self.lbl_imu):
            lb.setAlignment(Qt.AlignCenter)
            lb.setMinimumSize(1, 1)
            lb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            lb.setStyleSheet("background:#111; color:#888; font-size:14px;")

        grid = QGridLayout()
        grid.addWidget(self.lbl_video, 0, 0)
        grid.addWidget(self.lbl_depth, 0, 1)
        grid.addWidget(self.lbl_hand, 1, 0)
        grid.addWidget(self.lbl_skel, 1, 1)
        grid.addWidget(self.lbl_imu, 2, 0, 1, 2)
        grid.setRowStretch(0, 3)
        grid.setRowStretch(1, 2)
        grid.setRowStretch(2, 0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid, 1)
        self.grid = grid
        self.lbl_imu.setFixedHeight(260)

        # 控制条：独立浅色容器（调色板填充，不用 QSS），
        # 按钮/滑条/勾选框保持原生渲染，深色面板底上依然清晰可读
        barw = QWidget()
        pal2 = barw.palette()
        pal2.setColor(barw.backgroundRole(), QColor("#d9d9d9"))
        barw.setPalette(pal2)
        barw.setAutoFillBackground(True)
        bar = QHBoxLayout(barw)
        self.btn_open = QPushButton("打开 Parquet")
        self.btn_play = QPushButton("播放")
        self.btn_play.setEnabled(False)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.lbl_frame = QLabel("- / -")
        self.btn_prev = QPushButton("|<")
        self.btn_next = QPushButton(">|")
        for b in (self.btn_prev, self.btn_next):
            b.setEnabled(False)
        self.chk_imu = QCheckBox("显示 IMU 四元数")
        self.chk_imu.setVisible(False)
        self.chk_tactile_baseline = QCheckBox("触觉基线校正")
        self.chk_tactile_baseline.setChecked(True)
        self.chk_tactile_baseline.setVisible(False)
        bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_play)
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.btn_next)
        bar.addWidget(self.chk_tactile_baseline)
        bar.addWidget(self.chk_imu)
        bar.addWidget(self.lbl_frame)
        root.addWidget(barw)

        self.btn_open.clicked.connect(self.open_file_dialog)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_prev.clicked.connect(lambda: self.seek(self.idx - 1))
        self.btn_next.clicked.connect(lambda: self.seek(self.idx + 1))
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.chk_imu.toggled.connect(self._on_imu_toggle)
        self.chk_tactile_baseline.toggled.connect(
            lambda _c: self.data is not None and self.render_frame(self.idx))

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)   # 节奏计时器要精确节拍
        self.timer.setInterval(33)   # 30fps；加载后按数据 fps 重设
        self.timer.timeout.connect(self.next_frame)

        if parquet_path:
            self.load(parquet_path)

    # ── 文件 ──
    def open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择池化录制 parquet", "",
            "Parquet (*.parquet);;All files (*)")
        if path:
            self.load(path)

    def load(self, path):
        try:
            if self.data:
                self.data.close()
            self.data = PooledSession(path)
        except Exception as e:
            self.statusBar().showMessage(f"打开失败: {e}", 8000)
            return
        self.n = self.data.n
        self.idx = 0
        self._skel_dists = {}
        self._skel_centers = {}
        self.has_tactile = self.data.has_tactile()
        self.has_depth = self.data.has_depth()
        self.has_imu = self.data.has_imu()
        self.has_kpts = self.data.has_keypoints()
        # 触觉面板：左手套 + 右手套各一个并排（左手在左，与骨架面板
        # 同序）；会话只有单手数据时就只剩那一个面板
        self.tactile_sensors = sorted(
            self.data.tactile,
            key=lambda s: (0 if glove_side_of(s) == "left" else 1, s))
        self._update_panel_visibility()
        # 骨架相机：距离照工具包 replay.py 按整段数据空间尺度一次拟合、
        # 居中点按整段数据各帧包围盒中心的中位数一次计算，播放中全部
        # 固定（相机不动，画面不抖）；渲染前绕腕部做显示旋转，拟合时
        # 同样先减去腕部
        self._skel_dists = {}
        self._skel_centers = {}
        for side, mat in self.data.keypoints.items():
            k = mat.reshape(self.n, 21, 3)
            wk = k - k[:, :1, :]
            self._skel_dists[side] = _fit_dist(wk)
            rot = _SKEL_ROT_LEFT if side == "left" else _SKEL_ROT_RIGHT
            rotated = wk @ rot
            ok = np.isfinite(rotated).all(axis=-1)
            centers = [0.5 * (rotated[f][ok[f]].min(axis=0)
                              + rotated[f][ok[f]].max(axis=0))
                       for f in range(self.n) if ok[f].any()]
            if centers:
                self._skel_centers[side] = np.median(
                    np.asarray(centers), axis=0)
        # 触觉列存在但全零（传感器无数据/未采集）→ 占位说明，不再静默隐藏
        if not self.has_tactile and self.tactile_sensors:
            self.lbl_hand.setVisible(True)
            self.lbl_hand.setText(
                "触觉列存在但全为零\n"
                "（手套触觉传感器无数据，检查硬件/连接后重新录制）")
        elif self.tactile_sensors:
            sides = "+".join(glove_side_of(s).upper()
                             for s in self.tactile_sensors)
            self.lbl_hand.setText(f"Glove Tactile Matrix Panel ({sides})")
        else:
            self.lbl_hand.setText("Glove Tactile Matrix Panel")
        self.chk_imu.setVisible(self.has_imu and self.has_kpts)
        self.chk_tactile_baseline.setVisible(bool(self.tactile_sensors))
        # 深度流缺失时 RGB 占满整行；有深度时两者左右并排
        self.grid.addWidget(self.lbl_video, 0, 0,
                            1, 1 if self.has_depth else 2)
        self.grid.addWidget(self.lbl_depth, 0, 1)
        self.grid.setRowStretch(
            1, 2 if (self.tactile_sensors or self.data.tactile
                      or self.has_kpts) else 0)
        self.play_fps = max(self.data.fps, 1.0)
        self.timer.setInterval(max(1, int(round(1000 / self.play_fps))))
        self.slider.setRange(0, max(0, self.n - 1))
        self.slider.setEnabled(True)
        self.btn_play.setEnabled(True)
        self.btn_prev.setEnabled(True)
        self.btn_next.setEnabled(True)
        self.setWindowTitle(
            f"Pooled Data Viewer - {os.path.basename(path)} "
            f"({self.n} 帧 @ {self.play_fps:g}fps, episode {self.data.episode_index})")
        self.render_frame(0)
        bits = [f"{self.n} 帧", f"fps {self.play_fps:g}",
                f"RGB 视频 {len(self.data.videos)} 路",
                f"深度 {len(self.data.depth_videos)} 路"]
        if self.data.skipped_depth:
            bits.append(f"深度流无法解码: {', '.join(self.data.skipped_depth)}")
        bits.append(f"触觉 {', '.join(self.data.tactile) or '无'}")
        bits.append(f"骨架 {', '.join(self.data.keypoints) or '无'}"
                    if self.has_kpts else "骨架 无")
        bits.append(f"IMU {', '.join(self.data.imu_quats) or '无'}")
        self.statusBar().showMessage(" | ".join(bits), 10000)

    def _update_panel_visibility(self):
        """骨架/IMU 面板可见性：有骨架时骨架优先，IMU 降为勾选兜底。

        触觉面板显示 self.tactile_sensors（左右手各一个，左手在左）。
        """
        self.lbl_depth.setVisible(self.has_depth)
        # 有触觉列就保留面板（正常渲染网格；全零时显示说明占位），
        # 完全没有触觉列才隐藏
        self.lbl_hand.setVisible(
            bool(self.tactile_sensors) or bool(self.data.tactile))
        self.lbl_skel.setVisible(self.has_kpts)
        self.lbl_imu.setVisible(
            self.has_imu and (not self.has_kpts or self.chk_imu.isChecked()))

    def _on_imu_toggle(self, _checked):
        self._update_panel_visibility()
        if self.data is not None:
            self.render_frame(self.idx)

    # ── 播放 ──
    def toggle_play(self):
        self.playing = not self.playing
        self.btn_play.setText("暂停" if self.playing else "播放")
        if self.playing:
            # 实时节奏起点：当前帧对应时刻 t=idx/fps（暂停后从原地续播）
            self._play_t0 = time.perf_counter() - self.idx / self.play_fps
            self.timer.start()
        else:
            self.timer.stop()

    def next_frame(self):
        if self.data is None:
            return
        # 拖动进度条时不推进播放（拖动是拖拽刷帧，节流渲染跟着拇指走；
        # 播放 tick 插进来会按节奏起点把画面抢走，与拖拽打架）
        if self.slider.isSliderDown():
            return
        # 实时节奏：目标帧 = 起点 + 已过时间×fps（取模循环，与原行为
        # 一致）。渲染慢时直接跳到目标帧——掉帧保速，而不是像旧的自
        # 适应写法那样把播放时钟拉慢（5 分钟数据播 6 分钟的根因）。
        # keep_clock=True：追赶 tick 不得重设节奏起点，否则每次 tick
        # 都把时钟锚到当前帧，绝对时钟追赶失效——快机器上 33ms 定时器
        # 对 33.3ms 帧间隔 int(0.99)=0 每两拍才进 1 帧（≈2 倍慢），
        # 渲染慢的机器上每拍只进 1 帧（播放速度 = 每帧渲染耗时，即
        # 一秒视频播一点几秒的根因）。
        target = int((time.perf_counter() - self._play_t0)
                     * self.play_fps) % self.n
        if target != self.idx:
            self.seek(target, keep_clock=True)

    def seek(self, i, keep_clock=False):
        if self.data is None:
            return
        self.idx = max(0, min(self.n - 1, int(i)))
        # 播放中由用户主动跳转（拖进度条/键盘/按钮）→ 重设节奏起点，
        # 从新位置继续播放；不重设的话 next_frame 会按旧起点把画面
        # 弹回原播放处。自动播放追赶（keep_clock=True）不重设。
        if self.playing and not keep_clock:
            self._play_t0 = time.perf_counter() - self.idx / self.play_fps
        self.render_frame(self.idx)

    def _video_frame_for(self, stream, idx):
        """主时钟行序 idx → 该路视频帧号（按各自帧时钟对齐）。

        行序 i 对应时刻 t = i/play_fps，该路视频取 round(t×fps) 帧：
        帧率一致时即按帧号读；深度等流帧率不同或帧数不足时按比例
        映射并夹紧，保证两路画面时间一致（不是简单按帧号对齐）。
        """
        if getattr(stream, "fps", 0) > 0 and self.play_fps > 0:
            f = int(round(idx * stream.fps / self.play_fps))
        else:
            f = idx
        return min(max(f, 0), len(stream) - 1)

    # 拖动进度条：sliderMoved 高频触发，深度 seek 每次 ~20-60ms，
    # 逐事件整帧渲染会积压卡死 —— 节流到 ≥100ms 一次；松手后必定
    # 落定到最终位置（所有面板一致，不会有的变有的不变）
    def _on_slider_moved(self, i):
        self._pending_seek = int(i)
        now = time.perf_counter()
        if now - self._last_slider_render >= 0.1:
            self._last_slider_render = now
            self.seek(self._pending_seek)

    def _on_slider_released(self):
        self._last_slider_render = time.perf_counter()
        if self._pending_seek is not None:
            i, self._pending_seek = self._pending_seek, None
            self.seek(i)
        else:
            self.seek(self.slider.value())

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Space:
            self.toggle_play()
        elif ev.key() == Qt.Key_Left:
            self.seek(self.idx - 1)
        elif ev.key() == Qt.Key_Right:
            self.seek(self.idx + 1)
        elif ev.key() == Qt.Key_PageUp:
            self.seek(self.idx + max(1, int(round(5 * self.play_fps))))
        elif ev.key() == Qt.Key_PageDown:
            self.seek(self.idx - max(1, int(round(5 * self.play_fps))))
        elif ev.key() == Qt.Key_Home:
            self.seek(0)
        elif ev.key() == Qt.Key_End:
            self.seek(self.n - 1)
        else:
            super().keyPressEvent(ev)

    # ── 渲染 ──
    def render_frame(self, idx):
        # 拖动中不 setValue：会把拇指弹回已渲染位置，与用户拖拽打架
        # （拖动一次渲染一次时表现为进度条拖不动）
        if not self.slider.isSliderDown():
            self.slider.setValue(idx)
        ts = self.data.timestamps[idx]
        self.lbl_frame.setText(
            f"{idx + 1} / {self.n}  t={ts:.3f}s")

        # 1) 视频面板：多路 RGB 竖排拼接（按各自帧时钟对齐主时钟）
        if self.data.videos:
            w = self.lbl_video.width() or 1280
            panels = []
            for v in self.data.videos:
                frame = v[self._video_frame_for(v, idx)]
                if frame is None:
                    frame = np.full((360, 640, 3), 15, np.uint8)
                frame = np.asarray(frame)
                if frame.dtype == np.object_ or frame.ndim != 3:
                    frame = np.full((360, 640, 3), 15, np.uint8)
                if frame.shape[1] != w:
                    hh = max(1, int(round(frame.shape[0] * w / frame.shape[1])))
                    frame = cv2.resize(frame, (w, hh),
                                       interpolation=cv2.INTER_AREA)
                put_label(frame, v.name, (12, 26), (0, 255, 255))
                panels.append(frame)
            if len(panels) > 1:
                pad = np.full((6, w, 3), 10, np.uint8)
                top = panels[0]
                for p in panels[1:]:
                    top = np.vstack([top, pad, p])
            else:
                top = panels[0]
            self._show_image(self.lbl_video, top)
        else:
            self.lbl_video.setText("无 RGB 视频文件\n"
                                   "(videos/chunk-NNN/<key>/episode-FFF.mp4)")

        # 1.5) 深度伪彩面板：多路深度流竖排拼接（按各自帧时钟对齐主时钟，
        #      JET 热力图）
        if self.has_depth and self.data.depth_videos:
            w = self.lbl_video.width() or 1280
            panels = []
            for dv in self.data.depth_videos:
                frame = dv.read(self._video_frame_for(dv, idx))
                if frame is None:
                    frame = np.full((360, 640, 3), 15, np.uint8)
                frame = np.asarray(frame)
                if frame.ndim != 3:
                    frame = np.full((360, 640, 3), 15, np.uint8)
                if frame.shape[1] != w:
                    hh = max(1, int(round(frame.shape[0] * w / frame.shape[1])))
                    frame = cv2.resize(frame, (w, hh),
                                       interpolation=cv2.INTER_AREA)
                put_label(frame, dv.name + " (depth, JET)", (12, 26),
                          (0, 255, 255))
                panels.append(frame)
            if len(panels) > 1:
                pad = np.full((6, w, 3), 10, np.uint8)
                top = panels[0]
                for p in panels[1:]:
                    top = np.vstack([top, pad, p])
            else:
                top = panels[0]
            self._show_image(self.lbl_depth, top)

        # 2) 手套触觉面板（厂商 Glove-test 移植：只留分区网格，手形热图
        #    已按用户要求移除；左右手各一个面板并排（左手在左），面板角
        #    上标传感器名；全零数据时不渲染、保留 load 里的占位说明文字）
        sensors = self.tactile_sensors
        if self.has_tactile and sensors:
            h = max(self.lbl_hand.height() or 560, 240)
            w = self.lbl_hand.width() or 1280
            use_baseline = self.chk_tactile_baseline.isChecked()
            per_w = max(680, (w - 24 * (len(sensors) - 1)) // len(sensors))
            panels = []
            for sn in sensors:
                side = glove_side_of(sn)
                mat = self.data.tactile_frame(sn, idx)
                baseline = (self.data.tactile_baselines.get(sn)
                            if use_baseline else None)
                panels.append(render_tactile_grid(
                    mat, side=side, baseline=baseline,
                    use_baseline=use_baseline, w=int(per_w), h=h))
            pad24 = np.full((h, 24, 3), 15, np.uint8)
            bhv = panels[0]
            for p in panels[1:]:
                bhv = np.hstack([bhv, pad24, p])
            # 传感器名另起一条顶栏：两个网格左右手单看分不出是谁的，
            # 但网格本身画得满（左上角压着坐标数字、中上是 "X -> …"），
            # 名字写进面板里必然盖掉东西 —— 往上加带子，标签区还有余量
            head = np.full((26, bhv.shape[1], 3), 15, np.uint8)
            for k, sn in enumerate(sensors):
                cv2.putText(head, sn, (k * (int(per_w) + 24) + 10, 19),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255),
                            1, cv2.LINE_AA)
            self._show_image(self.lbl_hand, np.vstack([head, bhv]))

        # 3) 手部骨架面板（MANO 21 关键点，每侧一个面板横排；视角 =
        #    标定预览初始视角 yaw=186.8°/elev=-44.3°/roll=1.5° + 每侧
        #    绕腕显示旋转，手背面向观众；相机距离与居中点均为 load 时
        #    按整段数据一次计算的固定值，播放中相机不动）
        kpt_sides = sorted(self.data.keypoints)
        if self.has_kpts and kpt_sides:
            h = max(self.lbl_skel.height() or 420, 360)
            w = self.lbl_skel.width() or 1280
            panel_w = max(480, w // len(kpt_sides))
            panels = []
            for side in kpt_sides:
                kpts = self.data.keypoints_frame(side, idx)
                panels.append(render_skeleton(
                    kpts, side=side, w=panel_w, h=h,
                    dist=self._skel_dists.get(side),
                    center=self._skel_centers.get(side),
                    label=side.capitalize()))
            if len(panels) > 1:
                pad = np.full((h, 8, 3), 15, np.uint8)
                out = panels[0]
                for p in panels[1:]:
                    out = np.hstack([out, pad, p])
            else:
                out = panels[0]
            self._show_image(self.lbl_skel, out)

        # 4) IMU 姿态面板（每传感器一个面板，左右并排，标题带传感器名；
        #    有骨架时默认隐藏。IMU 面板是宽扁的，两个竖排会被面板高度
        #    压到看不清，横排刚好铺满）
        imu_sensors = sorted(self.data.imu_quats)
        if self.has_imu and imu_sensors and self.lbl_imu.isVisible():
            h = max(self.lbl_imu.height() or 240, 200)
            w = self.lbl_imu.width() or 880
            per_w = max((w - 4 * (len(imu_sensors) - 1)) // len(imu_sensors),
                        480)
            panels = []
            for sn in imu_sensors:
                quats, valid = self.data.imu_frame(sn, idx)
                panels.append(render_imu_panel(quats, valid, w=int(per_w),
                                               h=h, label=sn))
            if len(panels) > 1:
                pad = np.full((h, 4, 3), 10, np.uint8)
                out = panels[0]
                for p in panels[1:]:
                    out = np.hstack([out, pad, p])
            else:
                out = panels[0]
            self._show_image(self.lbl_imu, out)

    @staticmethod
    def _show_image(label, bgr):
        if (not isinstance(bgr, np.ndarray) or bgr.dtype == np.object_
                or bgr.size == 0 or bgr.ndim != 3 or bgr.shape[2] != 3):
            return
        h, w = bgr.shape[:2]
        if not bgr.flags.c_contiguous:
            bgr = np.ascontiguousarray(bgr)
        # Format_BGR888 直接包住 BGR 数据，免掉每帧全图 cvtColor；
        # 缩放到标签尺寸用 FastTransformation（Smooth 每面板每帧数
        # 毫秒，四个面板叠起来是播放掉速的主因之一）
        qimg = QImage(bgr.data, w, h, 3 * w, QImage.Format_BGR888)
        pix = QPixmap.fromImage(qimg)
        label.setPixmap(pix.scaled(
            label.size(), Qt.KeepAspectRatio, Qt.FastTransformation))

    def closeEvent(self, ev):
        self.timer.stop()
        if self.data:
            self.data.close()
        super().closeEvent(ev)


# ══════════════════════════════════════════════════════════════════
# 4. 入口
# ══════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(
        description="池化存储查看器（D435 视频 + 手套触觉/IMU 可视化）")
    ap.add_argument("parquet", nargs="?",
                    help="episode-NNN.parquet 路径（可选，用界面打开）")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    win = DemoWindow(args.parquet)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
