"""
Intel RealSense D400 系列（D435/D435I/D405…）深度双目采集模块 —— 进程内 QObject worker。

D400 系列是稳定 UVC 用户态设备（pyrealsense2 自带 librealsense2 .so + libusb 后端），
无需像 S80M 那样隔离到子进程。后台线程跑 rs2.pipeline，对外输出两路
（RGB 彩色 / 深度），经 Qt 信号跨线程发回主线程，复用
MainWindow 双目帧消费模式（显示 + 录制外部源链路）。
左红外流仅内部启用（时间戳元数据），不对外输出；右红外不再开流
（基线改从 depth sensor 的 stereo_baseline 选项直读）。

多设备支持：worker 按 serial 锁定具体设备（config.enable_device），
面板点击哪台就开哪台；serial 为空时自动取第一台 D400。

信号签名与 MainWindow.stereo_frame_ready 一致:
    frames_ready(str slot_id, np.ndarray frame, int hardware_ns, list imu_samples,
                 dict extra)
        - "d435_rgb": BGR 三通道彩色图；hardware_ns = **该彩色帧**的设备时钟 ns
        - "d435_depth": uint16 毫米深度图（D435/D405 无 IMU，imu_samples 恒为 []）；
          hardware_ns = **该深度帧**的设备时钟 ns
        - extra: 逐帧诊断 {depth_ns, rgb_dup, rgb_gap, depth_dup, depth_gap,
          ts_src, host_ns}，见 D435Worker._stamp_frame（v1.3.11 时间戳取证）；
          槽函数可以只接前 4 个参数

左红外流仅内部启用（时间戳元数据），不对外输出、不显示、不落盘；
右红外不再开流——其唯一用途是算基线，现改从 depth sensor 的
stereo_baseline 选项直读。双 RealSense 同 hub 共享带宽时多一条
IR2 会让 D405 掉到 ~19fps（实测，与开启顺序无关），去掉后两台满帧。
深度本身由红外双目内部计算，无需暴露红外画面。

用法:
    from core.d435_camera import D435Worker, d435_available, list_d400_devices
    if d435_available():
        worker = D435Worker(width=848, height=480, fps=30, serial="…")
        worker.frames_ready.connect(on_frames)
        worker.start()
        ...
        worker.stop()

亦可独立运行做无头自检:
    python core/d435_camera.py
"""

from __future__ import annotations
import os
import sys

# 脚本直跑(如 python core/d435_camera.py)时 sys.path[0] 是 core/,
# 需把项目根插入 sys.path 才能 import config —— 仅在直跑场景生效
if os.path.dirname(os.path.abspath(__file__)) == sys.path[0]:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
import time
from collections import deque
from typing import Optional, List

import cv2
import numpy as np
from PyQt5.QtCore import QObject, QMutex, QMutexLocker, pyqtSignal

from config import settings

# pyrealsense2 延迟失败:未安装时本模块仍可 import(d435_available 返回 False)
try:
    import pyrealsense2 as rs
except ImportError:  # pragma: no cover
    rs = None


# d435_available 结果缓存(探测一次,避免重复打开设备上下文)
_d435_avail_cache: Optional[bool] = None


def d435_available(force: bool = False) -> bool:
    """检测 pyrealsense2 可用且存在 D400 系列设备。结果模块级缓存。

    force=True 绕过缓存（热插拔后点击路径的活体复查）。
    """
    global _d435_avail_cache
    if not force and _d435_avail_cache is not None:
        return _d435_avail_cache
    _d435_avail_cache = bool(list_d400_devices())
    return _d435_avail_cache


def list_d400_devices() -> List[tuple]:
    """返回已连接的 D400 系列设备 [(name, serial), …]（无设备/未安装 → []）。"""
    out: List[tuple] = []
    if rs is None:
        return out
    try:
        ctx = rs.context()
        for d in ctx.query_devices():
            try:
                if "D400" not in d.get_info(rs.camera_info.product_line):
                    continue
                out.append((d.get_info(rs.camera_info.name),
                            d.get_info(rs.camera_info.serial_number)))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _frameset_timestamp_us(frames, rs_mod) -> float:
    """帧时间戳逐级回退（µs）—— **仅作兼容保留**，新代码走 _frame_ts_us()。

    见 _frame_ts_us / D435Worker._stamp_frame：**逐路取值**（RGB 行用彩色帧戳、
    深度用深度帧戳）才是一列一源的正解。本函数保留给外部调用方，语义为
    「帧组里深度帧的采集时刻」，取不到时回退帧组戳。
    """
    v = _frame_ts_us(frames.get_depth_frame(), rs_mod) if frames else None
    if v is None:
        v = _frame_ts_us(frames.get_infrared_frame(1), rs_mod) if frames else None
    if v is not None:
        return v
    return frames.get_timestamp() * 1000


# ── 帧元数据安全读取（时间戳取证，v1.3.11） ─────────────
# 背景（2026-10 真机定因）：rs2::pipeline.wait_for_frames 的 syncer 在多流模式
# 下会给「本拍没到新帧」的那一路**补上一帧的旧帧**（frame_counter 不前进），
# 本拍到两帧时又会跳号。单流模式实测 1794 帧零补帧，多流模式按负载出现
# 0.28%~1.5% 的 补帧/跳号。
#
# 旧实现把 hardware_ns 一律取自**深度成员**，于是深度那一路的补帧/跳号被原样
# 盖到 RGB 行上：实测 81 次深度戳重复里有 54 次彩色帧其实是新帧 —— 戳在撒谎。
# 现在改成逐路取值 + frame_counter 判重，并把诊断落成 parquet 列。

TS_SRC_NONE = 0            # 无设备时钟：hardware_ns 写 0（0 = 未知，仓库既有约定）
TS_SRC_COLOR_META = 1      # 彩色帧 frame_timestamp 元数据（首选）
TS_SRC_FRAMESET_HW = 2     # 帧组 get_timestamp()×1000，域=硬件时钟（仍属设备钟）
TS_SRC_FRAMESET_HOST = 3   # 帧组戳域=宿主钟：**不写 hardware_ns**，只另存 host 列


def _frame_meta(frame, rs_mod, key):
    """安全读单帧元数据；不支持/读失败/无帧 → None（绝不抛）。"""
    try:
        if frame is None or not frame.supports_frame_metadata(key):
            return None
        v = frame.get_frame_metadata(key)
        return None if v is None else float(v)
    except Exception:
        return None


def _frame_ts_us(frame, rs_mod):
    """单帧采集时刻（µs，设备时钟）。

    RealSense 契约：RS2_FRAME_METADATA_FRAME_TIMESTAMP = 设备读出并开始传输时
    设备时钟的读数，**单位 µs**；而 frame.get_timestamp() 单位是 ms。两者不可
    混用 —— 本函数只返回元数据，量级自检见 _check_meta_unit()。
    """
    return _frame_meta(frame, rs_mod, rs_mod.frame_metadata_value.frame_timestamp)


def _frame_counter(frame, rs_mod):
    """该路流的自增帧序号（判「同一物理帧被投递两次」/「跳号」的唯一硬证据）。

    每路流各有一套序号：Δ=0 → 本拍这一路是上一帧的重复；Δ≥2 → 中间掉了帧。
    不支持则返回 None。
    """
    v = _frame_meta(frame, rs_mod, rs_mod.frame_metadata_value.frame_counter)
    return None if v is None else int(v)


class _TsUnwrap:
    """单路设备 µs 计数 → 会话内单调 ns（首帧归零 + 32 位回绕解绕）。

    **每路一个实例**：彩色/深度各有独立的计数器与回绕相位，共用一个实例会把
    两路的基差当成一次天文数字级的跳变（同 core/frame_gap.py 的「混源」坑）。
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.t0_us = None
        self.last_us = 0
        self.wrap_us = 0
        self.frames = 0

    def to_ns(self, raw_us):
        """返回 (ns, ok)；raw_us=None → (0, False)（该项未知，不冒充 0 时刻）。"""
        if raw_us is None:
            return 0, False
        raw = int(raw_us)
        if self.t0_us is None:
            self.t0_us = raw
            self.last_us = raw
            self.wrap_us = 0
            self.frames += 1
            return 0, True          # 与旧实现一致：本路首帧归零
        if raw < self.last_us - (1 << 31):
            self.wrap_us += 1 << 32
        self.last_us = raw
        self.frames += 1
        return (raw + self.wrap_us - self.t0_us) * 1000, True



def _apply_color_exposure(sensor, auto: bool, value: float) -> bool:
    """对 color 传感器应用曝光（µs）：auto=True 只开自动曝光；
    否则先关自动再写手动值。异常返回 False（不打断采集线程）。"""
    try:
        sensor.set_option(rs.option.enable_auto_exposure, 1 if auto else 0)
        if not auto:
            sensor.set_option(rs.option.exposure, float(value))
        return True
    except Exception:
        return False


class D435Worker(QObject):
    """D435 采集 worker:后台线程 rs2.pipeline 三路流,信号回主线程。"""

    # (slot_id, frame, hw_ns, imu_samples, extra)
    # ★ hw_ns 必须用 object:PyQt5 队列信号把 Python int 按 C++ qint32
    #   封送,超过 2^31(≈2.1s 纳秒)即静默截断为负数(S80M 已有数据受害)
    # ★ extra: 逐帧诊断 dict（time 戳来源/深度戳/补帧跳号），见 _stamp_frame；
    #   槽函数可以只接前 4 个参数（PyQt 允许槽参数少于信号）
    frames_ready = pyqtSignal(str, np.ndarray, object, list, object)
    error_occurred = pyqtSignal(str)                         # 错误消息
    status_changed = pyqtSignal(str)                         # "running" / "stopped"

    def __init__(self, width: int = None, height: int = None,
                 fps: int = None, parent=None, ts_log: str = None,
                 rgb_width: int = None, rgb_height: int = None,
                 serial: str = None, model_name: str = None,
                 rgb_slot: str = None, depth_slot: str = None,
                 exposure: dict = None):
        super().__init__(parent)
        self._width = width if width is not None else settings.D435_RESOLUTION[0]
        self._height = height if height is not None else settings.D435_RESOLUTION[1]
        self._fps = fps if fps is not None else settings.D435_FPS
        self._rgb_width = (rgb_width if rgb_width is not None
                           else settings.D435_RGB_RESOLUTION[0])
        self._rgb_height = (rgb_height if rgb_height is not None
                            else settings.D435_RGB_RESOLUTION[1])
        self._serial = serial            # 目标设备序列号（None → 第一台 D400）
        self._model_name = model_name    # 仅用于日志/诊断
        # 槽位名（多设备时由 MainWindow 消歧分配；None 回落 settings 常量）
        self._rgb_slot = rgb_slot or settings.D435_SLOT_RGB
        self._depth_slot = depth_slot or settings.D435_SLOT_DEPTH
        self._depth_unit_factor = 1.0    # 设备深度单位 → mm 换算系数（流启动时探测）
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._calibration = None        # StereoCalibration,首帧后可用
        self._frame_count = 0
        self._t0_us: Optional[int] = None   # 首个帧计数(µs,重连时重置)
        self._last_us: int = 0              # 上一帧原始计数(解绕用)
        self._wrap_us: int = 0              # 累计回绕补偿(µs)
        # ── 逐路时间戳取证（v1.3.11，见 _stamp_frame） ──
        # 每路独立解绕器：彩色/深度计数器各自归零，共用一个会把基差当跳变
        self._clk_color = _TsUnwrap()
        self._clk_depth = _TsUnwrap()
        self._clk_frameset = _TsUnwrap()    # 第 2 级回退（帧组戳）专用
        self._prev_cnt: Dict[str, Optional[int]] = {}   # 上一帧 frame_counter
        self._ts_domain: str = "unknown"    # frames.get_frame_timestamp_domain()
        self._ts_src: int = TS_SRC_NONE     # 本段实际采用的戳来源（落 parquet）
        self._ts_logged = False             # 来源只在首帧记一次日志
        self._meta_scale: float = 1.0       # 元数据→µs 换算（自检，正常恒 1.0）
        self._unit_probe = None             # 单位自检用的上一帧元数据
        self._dup_skipped = 0               # 本段跳过的重复帧计数（见 _stamp_frame）
        self._gap_skipped = 0               # 本段设备侧跳号累计
        self._ts_log = ts_log               # 诊断 CSV（时间戳取证列，仅调试）
        self._ts_fh = None                  # 首启失败时 _run 尾部清理仍需存在
        # 曝光设置（主线程只写 pending；采集线程在流启动后应用，
        # 每次重连新建管道须重放最近一次设置）
        self._exp_lock = QMutex()
        self._exp_auto: Optional[bool] = None
        self._exp_value: Optional[float] = None
        self._exp_pending = False
        self._exp_range: Optional[tuple] = None   # (min, max) µs，流启动后缓存
        # 「最一开始」曝光基线（开流时读回硬件，应用任何设置之前）
        self._original_exp: Optional[tuple] = None
        if exposure:
            self._exp_auto = bool(exposure.get("auto", True))
            self._exp_value = float(exposure.get("value", 0.0))
            self._exp_pending = True

    # ── 生命周期 ────────────────────────────────────────

    def start(self):
        """启动后台采集线程(幂等)。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="d435-capture")
        self._thread.start()

    def stop(self):
        """停止采集线程(阻塞至多约 3s;wait_for_frames 1s 超时保证退出)。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.status_changed.emit("stopped")

    # ── 标定(录制开始时由主线程读取) ────────────────────

    def get_calibration(self):
        """返回首帧后提取的 StereoCalibration;尚未就绪时为 None。"""
        return self._calibration

    # ── 曝光调节（主线程只写 pending；采集线程应用并缓存量程） ──

    def set_exposure(self, auto: bool, value: float):
        """设置 color 传感器曝光：auto=True 开自动；否则手动值（µs）。"""
        with QMutexLocker(self._exp_lock):
            self._exp_auto = bool(auto)
            self._exp_value = float(value)
            self._exp_pending = True

    def exposure_info(self) -> tuple:
        """返回 ((min,max)µs 或 None, auto, value)；流未启动时量程 None。"""
        with QMutexLocker(self._exp_lock):
            return (self._exp_range, self._exp_auto, self._exp_value)

    def original_exposure(self) -> tuple:
        """「最一开始」曝光基线 (auto, value)；未捕获返回 None。"""
        with QMutexLocker(self._exp_lock):
            return self._original_exp

    # ── 帧时间戳取证（v1.3.11） ─────────────────────────

    def _stamp_frame(self, frames, color, depthf) -> tuple:
        """给本拍两路帧各盖一个**自己的**设备时间戳，并给出逐帧诊断。

        返回 (rgb_ns, depth_ns, extra)。

        规则（一列一源）：
          * RGB 行 = 彩色帧自己的 frame_timestamp —— 这一行代表的就是这一张彩色
            画面；用深度成员会继承深度那一路的补帧/跳号（旧实现的病根）。
          * 深度轨 = 深度帧自己的 frame_timestamp。
          * 两路各自解绕、各自归零，互不污染。

        回退显式化（不再静默写宿主钟）：
          1) 本帧 frame_timestamp 元数据（µs，设备钟）—— 首选；
          2) 元数据不可用但帧组戳域是 hardware_clock → 帧组戳×1000（仍是设备钟）；
          3) 域是宿主钟（system_time/global_time）→ hardware_ns 写 0（0=未知，
             仓库既有约定），另存 host_ns，并只告警一次。
        """
        ftm_ok_color = _frame_ts_us(color, rs)
        depth_raw = _frame_ts_us(depthf, rs)
        if not self._ts_logged:
            self._resolve_ts_policy(frames, ftm_ok_color is not None)

        # 单位自检：元数据契约是 µs；若实测是 ms（量级差 1000）则换算并告警
        if ftm_ok_color is not None and self._meta_scale != 1.0:
            ftm_ok_color = ftm_ok_color * self._meta_scale
        if depth_raw is not None and self._meta_scale != 1.0:
            depth_raw = depth_raw * self._meta_scale

        rgb_ns, c_ok = self._clk_color.to_ns(ftm_ok_color)
        depth_ns, d_ok = self._clk_depth.to_ns(depth_raw)

        host_ns = 0
        if not c_ok:
            # 逐级回退（显式）：只有确定是设备钟时才写 hardware_ns
            if self._ts_domain == "hardware_clock":
                # 帧组戳（ms→µs）同样是设备钟，但要过**自己的**解绕器：
                # 直接返回绝对值会与前面的「首帧归零」相对量纲不一致
                fs_us = float(frames.get_timestamp()) * 1000.0
                rgb_ns, _ = self._clk_frameset.to_ns(fs_us)
                self._ts_src = TS_SRC_FRAMESET_HW
            else:
                rgb_ns = 0                      # 0 = 未知，不冒充设备钟
                host_ns = time.monotonic_ns()
                self._ts_src = TS_SRC_FRAMESET_HOST
        else:
            self._ts_src = TS_SRC_COLOR_META

        c_cnt = _frame_counter(color, rs)
        d_cnt = _frame_counter(depthf, rs)
        rgb_dup, rgb_gap = self._cnt_delta("rgb", c_cnt)
        depth_dup, depth_gap = self._cnt_delta("depth", d_cnt)
        self._dup_skipped += rgb_dup
        self._gap_skipped += rgb_gap

        extra = {
            # 采集侧宿主单调钟（本帧从 wait_for_frames 返回的瞬间）：
            # pipeline 用它算 timestamp，避免把录制队列积压算进时间戳
            "mono_ns": int(time.monotonic_ns()),
            "depth_ns": int(depth_ns),
            "rgb_dup": int(rgb_dup), "rgb_gap": int(rgb_gap),
            "depth_dup": int(depth_dup), "depth_gap": int(depth_gap),
            "ts_src": int(self._ts_src),
            "host_ns": int(host_ns),
        }
        return int(rgb_ns), int(depth_ns), extra

    def _cnt_delta(self, key: str, cnt: Optional[int]) -> tuple:
        """返回 (dup, gap)：dup=本拍该路是上一物理帧的重复；gap=中间跳过的帧数。

        frame_counter 是本项目唯一能**证明**「同一物理帧被投递两次」的依据：
        Δ=0 只能是补帧，Δ≥2 只能是设备侧跳号。取不到元数据的设备恒返回 (0,0)。
        """
        prev = self._prev_cnt.get(key)
        self._prev_cnt[key] = cnt
        if cnt is None or prev is None:
            return 0, 0
        d = cnt - prev
        if d <= 0:
            return 1, 0
        return 0, d - 1

    def _resolve_ts_policy(self, frames, color_meta_ok: bool):
        """首帧探一次时基域与元数据可用性，记日志；顺带做单位自检。"""
        self._ts_logged = True
        try:
            self._ts_domain = frames.get_frame_timestamp_domain().name
        except Exception:
            self._ts_domain = "unknown"
        if not color_meta_ok and self._ts_domain != "hardware_clock":
            self.error_occurred.emit(
                "[时间戳] 本设备不支持 frame_timestamp 元数据且帧组戳域="
                f"{self._ts_domain}（非设备钟）→ hardware_ns 将写 0，"
                "宿主时间另存 host_ns；请勿把 hardware_ns 当作设备时钟使用")
        else:
            self.error_occurred.emit(
                f"[时间戳] 源=逐帧 frame_timestamp(µs) 域={self._ts_domain} "
                f"彩色元数据={'可用' if color_meta_ok else '不可用→回退帧组戳'}")

    def _check_meta_unit(self, frames, color):
        """开流后第二帧做一次量级自检：元数据是 µs 还是 ms。

        契约是 µs；若实测相邻帧差 ≈33（而非 ≈33000），说明该固件给的是 ms，
        置 _meta_scale=1000 换算并告警 —— 杜绝「靠注释约定」的 1000 倍风险。
        """
        try:
            if self._unit_probe is None:
                self._unit_probe = _frame_ts_us(color, rs)
                return
            cur = _frame_ts_us(color, rs)
            if cur is None or self._unit_probe is None:
                return
            d = abs(cur - self._unit_probe)
            self._unit_probe = cur
            if 5.0 < d < 200.0:              # 33 量级 → ms
                self._meta_scale = 1000.0
                self.error_occurred.emit(
                    f"[时间戳] frame_timestamp 实测单位疑似 ms（相邻帧差={d:.1f}）"
                    "→ 已按 ×1000 换算为 µs")
            elif 5000.0 < d < 200000.0:      # 33000 量级 → µs（正常）
                self._meta_scale = 1.0
        except Exception:
            pass

    # ── 后台线程 ────────────────────────────────────────

    def _run(self):
        if rs is None:
            self.error_occurred.emit("pyrealsense2 未安装")
            return

        while not self._stop_event.is_set():
            # 每次重连新建管道:复用同一 rs.pipeline 在"流停滞"后 stop→start
            # 无法复位 UVC 通道,同一对象会无限循环"无帧→重连"(实测停滞机
            # 新管道立即恢复出帧——新管道等价于新进程打开设备)
            pipe = rs.pipeline()
            try:
                config = rs.config()
                # 多设备时按序列号锁定目标设备；不指定则用第一台 D400
                if self._serial:
                    config.enable_device(self._serial)
                # 对外: RGB 彩色 + 深度两路
                config.enable_stream(rs.stream.color,
                                     self._rgb_width, self._rgb_height,
                                     rs.format.rgb8, self._fps)
                config.enable_stream(rs.stream.depth,
                                     self._width, self._height,
                                     rs.format.z16, self._fps)
                # 内部: 左红外仅用于时间戳元数据,不对外输出。右红外不开流:
                # 其唯一用途是算基线,现从 stereo_baseline 选项直读——
                # 双 RealSense 同 hub 时多一条 IR2 会把 D405 挤到 ~19fps
                config.enable_stream(rs.stream.infrared, 1,
                                     self._width, self._height,
                                     rs.format.y8, self._fps)
                profile = pipe.start(config)
                # 设备深度单位探测（D435=1mm，D405=0.1mm）→ mm 换算系数。
                # 帧统一归一化到 mm 输出：全链路（热力图/无损深度流/metadata
                # "unit:mm"/depth_scale 0.001）都按 mm 约定。
                try:
                    dev_unit = float(profile.get_device()
                                     .first_depth_sensor().get_depth_scale())
                except Exception:
                    dev_unit = float(settings.DEPTH_SCALE)
                self._depth_unit_factor = dev_unit / float(settings.DEPTH_SCALE)
                # 曝光：流启动后缓存 color 传感器量程并应用最近设置。
                # 重连 = 新管道 = 新传感器句柄，相机端设置会丢，须每次重放；
                # 消费 pending 同样在本线程完成（主线程只写 pending）。
                try:
                    color = profile.get_device().first_color_sensor()
                    rng = color.get_option_range(rs.option.exposure)
                    with QMutexLocker(self._exp_lock):
                        self._exp_range = (float(rng.min), float(rng.max))
                        # 「最一开始」曝光基线：应用任何设置前读回硬件
                        # 当前值（重连 = 新管道，读的是本次开流的硬件状态）
                        try:
                            oa = float(color.get_option(
                                rs.option.enable_auto_exposure))
                            ov = float(color.get_option(rs.option.exposure))
                            self._original_exp = (oa > 0.5, ov)
                        except Exception:
                            self._original_exp = None
                        auto, value = self._exp_auto, self._exp_value
                        self._exp_pending = False
                    if auto is not None:
                        _apply_color_exposure(color, auto,
                                              value if value is not None else 0.0)
                except Exception:
                    pass
                if self._calibration is None:
                    self._calibration = _build_calibration(
                        profile, self._fps, (self._width, self._height))
                    # 帧已归一化为 mm，标定里 depth_scale 写输出格式刻度
                    # （设备原生单位仅影响内部换算，不进入落盘标定）
                    self._calibration.depth_scale = float(settings.DEPTH_SCALE)
                self._t0_us = None  # 设备计数随流启动重置,重连后重新归零
                # 逐路解绕/判重状态同样随新管道重置（计数器从 0 重新开始）
                self._clk_color.reset()
                self._clk_depth.reset()
                self._clk_frameset.reset()
                self._prev_cnt.clear()
                self._ts_logged = False
                self._unit_probe = None
                self._last_us = 0
                self._wrap_us = 0
                self._ts_fh = open(self._ts_log, "w") if self._ts_log else None
                if self._ts_fh:
                    self._ts_fh.write(
                        "i,fs_ts_ms,color_ts_us,ts_src,rgb_dup,rgb_gap,"
                        "depth_dup,depth_gap\n")
                self.status_changed.emit("running")

                # 看门狗一: 停滞——连续无帧秒数(双 RealSense 第二台开流偶发
                # 静默停滞——管道已启动但不再出帧,wait_for_frames 持续超时,
                # 无异常可捕获;累计超过阈值则抛给外层走重连)
                # 看门狗二: 帧率——半死状态(零星出帧不报错但画面极卡),
                # 10s 窗口内帧数低于期望一半 → 同样抛给外层走重连
                stall_s = 0
                fps_win: deque = deque()          # 帧到达时刻(monotonic 秒)
                fps_win_start: Optional[float] = None   # 重连后首个帧时刻
                while not self._stop_event.is_set():
                    try:
                        frames = pipe.wait_for_frames(timeout_ms=1000)
                    except RuntimeError:
                        # 超时无帧(拔线初期/流停滞) → 计数,超阈值触发重连
                        stall_s += 1
                        if stall_s >= settings.D435_STALL_TIMEOUT_S:
                            raise RuntimeError(
                                f"流停滞({stall_s}s 无帧),触发重连")
                        continue
                    stall_s = 0
                    # 消费 pending 曝光设置（主线程 set_exposure 下发）
                    with QMutexLocker(self._exp_lock):
                        if self._exp_pending and self._exp_auto is not None:
                            auto, value = self._exp_auto, self._exp_value
                            self._exp_pending = False
                        else:
                            auto = None
                    if auto is not None:
                        try:
                            color = profile.get_device().first_color_sensor()
                            _apply_color_exposure(
                                color, auto, value if value is not None else 0.0)
                        except Exception:
                            pass
                    now_s = time.monotonic()
                    fps_win.append(now_s)
                    while (fps_win
                           and now_s - fps_win[0] > settings.D435_LOW_FPS_WINDOW_S):
                        fps_win.popleft()
                    if fps_win_start is None:
                        fps_win_start = now_s
                    elif (now_s - fps_win_start >= settings.D435_LOW_FPS_WINDOW_S
                          and len(fps_win) < (settings.D435_LOW_FPS_FRACTION
                                              * self._fps
                                              * settings.D435_LOW_FPS_WINDOW_S)):
                        raise RuntimeError(
                            f"帧率过低({len(fps_win)}帧/"
                            f"{settings.D435_LOW_FPS_WINDOW_S}s,期望≥"
                            f"{settings.D435_LOW_FPS_FRACTION * self._fps:.0f}fps),"
                            f"触发重连")

                    # ── 帧时间戳：逐路取证（v1.3.11，见 _stamp_frame） ──
                    # 一列一源：RGB 行用彩色帧自己的戳、深度用深度帧自己的戳，
                    # 各自解绕归零。与 frame_counter 一起给出「补帧/跳号」诊断，
                    # 随行落 parquet（rgb_dup/rgb_gap/depth_dup/depth_gap/ts_src）。
                    color_f = frames.get_color_frame()
                    depth_f = frames.get_depth_frame()
                    self._check_meta_unit(frames, color_f)
                    hw_ns, depth_ns, extra = self._stamp_frame(
                        frames, color_f, depth_f)
                    if self._ts_fh:
                        self._ts_fh.write(
                            f"{self._frame_count},{frames.get_timestamp()},"
                            f"{int(_frame_ts_us(color_f, rs) or 0)},"
                            f"{extra['ts_src']},{extra['rgb_dup']},"
                            f"{extra['rgb_gap']},{extra['depth_dup']},"
                            f"{extra['depth_gap']}\n")
                        self._ts_fh.flush()

                    # 显式 copy:rs2 帧缓冲由 C++ 持有,frameset 一释放即失效;
                    # 深度帧经排队信号延迟消费,必须脱离底层缓冲(848×480×2≈0.8MB)
                    depth = np.asanyarray(depth_f.get_data()).copy()  # 设备原生单位
                    if self._depth_unit_factor != 1.0:
                        # 归一化到 mm（D405 原生 0.1mm/单位；×0.1 不会溢出 uint16）
                        depth = (depth.astype(np.float32)
                                 * self._depth_unit_factor + 0.5).astype(np.uint16)

                    # RGB8 → BGR 三通道(显示 + MP4 录制链路要求 BGR24)
                    rgb = np.asanyarray(color_f.get_data())
                    rgb_bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

                    self.frames_ready.emit(self._rgb_slot,
                                           rgb_bgr, hw_ns, [], extra)
                    # 深度槽带自己的戳（深度轨对齐用），extra 同 RGB 一份
                    self.frames_ready.emit(self._depth_slot,
                                           depth, depth_ns, [], extra)
                    self._frame_count += 1

            except RuntimeError as e:
                # 拔线 / 设备异常 → 每 2s 重连(仿 CameraWorker 重连策略)
                if not self._stop_event.is_set():
                    self.error_occurred.emit(
                        f"RealSense {self._model_name or ''} "
                        f"{self._serial or ''} 设备异常,2s 后重连: {e}".replace("  ", " "))
                    self.status_changed.emit("error")
                try:
                    pipe.stop()
                except Exception:
                    pass
                self._stop_event.wait(settings.CAMERA_RECONNECT_INTERVAL_MS / 1000.0)
            except Exception as e:
                if not self._stop_event.is_set():
                    self.error_occurred.emit(f"RealSense 采集异常: {e}")
                try:
                    pipe.stop()
                except Exception:
                    pass
                self._stop_event.wait(settings.CAMERA_RECONNECT_INTERVAL_MS / 1000.0)

        try:
            pipe.stop()
        except Exception:
            pass
        if self._ts_fh:
            try:
                self._ts_fh.close()
            except Exception:
                pass
            self._ts_fh = None
        # 收尾汇报逐帧判重结果（明细在 parquet 的 rgb_dup/rgb_gap 列）
        if self._dup_skipped or self._gap_skipped:
            self.error_occurred.emit(
                f"[时间戳] 本段彩色帧补帧 {self._dup_skipped} 次、"
                f"跳号 {self._gap_skipped} 帧（syncer 补帧，非丢帧）；"
                f"hardware_ns 由录制管线统一归一为均匀递增，"
                f"逐行明细见 parquet rgb_dup/rgb_gap/depth_dup/depth_gap")


# ── 标定提取 ───────────────────────────────────────────

def _build_calibration(profile, fps: int, resolution: tuple):
    """从 rs2 pipeline profile 提取左右红外内参/深度内参/基线 → StereoCalibration。"""
    from core.calibration import StereoCalibration, CameraIntrinsics

    def _ir_intrinsics(idx: int) -> CameraIntrinsics:
        try:
            prof = profile.get_stream(rs.stream.infrared, idx)
        except RuntimeError:
            prof = None   # 未启用的流: get_stream 抛异常而非返回 None
        if prof is None:
            # IR2 已不开流:右目与左目是同一传感器模组、几何一致,
            # 内参镜像左目(基线另从 stereo_baseline 选项读取)
            return _ir_intrinsics(1)
        intr = prof.as_video_stream_profile().get_intrinsics()
        return CameraIntrinsics(
            intrinsic=[intr.fx, intr.fy, intr.ppx, intr.ppy],
            distortion=list(intr.coeffs),   # Inverse Brown Conrady 系数,原样存储
        )

    def _depth_intrinsics() -> CameraIntrinsics:
        prof = profile.get_stream(rs.stream.depth)
        intr = prof.as_video_stream_profile().get_intrinsics()
        return CameraIntrinsics(
            intrinsic=[intr.fx, intr.fy, intr.ppx, intr.ppy],
            distortion=list(intr.coeffs),
        )

    def _baseline() -> float:
        # 直读 depth sensor 的 stereo_baseline 选项(单位 mm→m),无需 IR2 流。
        # 与旧 IR1→IR2 外参算法实测一致(D435 50.15mm / D405 17.98mm)
        try:
            return float(profile.get_device().first_depth_sensor()
                         .get_option(rs.option.stereo_baseline)) / 1000.0
        except Exception:
            # 无此选项的罕见设备:IR2 若仍在流中则回落外参算法
            # (未启用时 get_stream 抛异常而非返回 None)
            try:
                ir2 = profile.get_stream(rs.stream.infrared, 2)
            except RuntimeError:
                ir2 = None
            if ir2 is not None:
                ir1 = profile.get_stream(
                    rs.stream.infrared, 1).as_video_stream_profile()
                trans = ir1.get_extrinsics_to(
                    ir2.as_video_stream_profile()).translation
                return float(np.linalg.norm(trans))   # D435 约 0.050 m
            return float(settings.STEREO_BASELINE)

    def _depth_scale() -> float:
        try:
            sensor = profile.get_device().first_depth_sensor()
            return float(sensor.get_depth_scale())
        except Exception:
            return float(settings.DEPTH_SCALE)

    return StereoCalibration(
        resolution=[resolution[0], resolution[1]],
        fps=float(fps),
        baseline=_baseline(),
        left_camera=_ir_intrinsics(1),
        right_camera=_ir_intrinsics(2),
        depth_camera=_depth_intrinsics(),
        depth_scale=_depth_scale(),
        cam_imu_timeshift=0.0,   # D435 无 IMU
    )


# ── 无头自检 ───────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os
    # 脚本直跑时把项目根加入 sys.path(config/core 均为包)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from PyQt5.QtCore import QCoreApplication

    app = QCoreApplication(sys.argv)

    if not d435_available():
        print("未检测到 D435 设备(pyrealsense2 或设备缺失)")
        sys.exit(1)

    worker = D435Worker()
    stats = {"t0": time.time(), "count": 0, "saved": False,
             "sample_dir": "/tmp/d435_selftest"}

    def on_frames(slot_id, frame, hw_ns, imu_samples, extra=None):
        stats["count"] += 1
        # 无头自检不弹窗口(cv2.imshow 与 PyQt5 同进程会冲突);
        # 各抽一帧存 /tmp 供人工查看
        if not stats["saved"]:
            os.makedirs(stats["sample_dir"], exist_ok=True)
            if slot_id == settings.D435_SLOT_DEPTH:
                cv2.imwrite(os.path.join(stats["sample_dir"], "depth.png"), frame)
            else:
                cv2.imwrite(os.path.join(stats["sample_dir"],
                                         f"{slot_id}.png"), frame)
            if os.path.isfile(os.path.join(stats["sample_dir"], "depth.png")) \
                    and os.path.isfile(os.path.join(stats["sample_dir"],
                                                    f"{settings.D435_SLOT_RGB}.png")):
                stats["saved"] = True

    worker.frames_ready.connect(on_frames)
    worker.error_occurred.connect(lambda m: print("[error]", m))
    worker.start()

    print(f"D435 自检: RGB {worker._rgb_width}x{worker._rgb_height} + "
          f"深度 {worker._width}x{worker._height}@{worker._fps}, 运行 5s...")
    t_end = time.time() + 5.0
    while time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)

    elapsed = time.time() - stats["t0"]
    print(f"  帧率: {stats['count'] / max(elapsed, 1e-6):.1f} 信号/秒")
    print(f"  抽样帧: {stats['sample_dir']}/ (depth.png, d435_rgb.png)")

    calib = worker.get_calibration()
    if calib is not None:
        print("── 标定 ──")
        print(f"  baseline     = {calib.baseline:.6f} m")
        print(f"  depth_scale  = {calib.depth_scale}")
        print(f"  left  IR 内参 = {calib.left_camera.intrinsic}")
        print(f"  right IR 内参 = {calib.right_camera.intrinsic}")
        print(f"  depth 内参   = {calib.depth_camera.intrinsic}")

    worker.stop()
