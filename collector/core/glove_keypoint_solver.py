#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手套骨架解算封装：16×BNO055 四元数 → MANO 21 关键点（米）。

复用厂商 SDK（tools/glove_sdk/）的 HandSolver：
运行时懒导入，SDK 目录缺失 / 导入或构造失败时 available()=False，调用方
跳过骨架列即可（触觉/IMU 录制不受影响）。

present_mask 近似取 valid_mask：本工程传输层不区分"物理在位"位域，
缺失传感器必为无效样本（valid=False），工具链按有效样本求解。
"""
import json
import os
import time

import numpy as np

# 进程内共享的懒导入结果（首次 import scipy 链需要 1s 左右）
_HAND_SOLVER_CLS = None
_RAW_FRAME_CLS = None
_IMPORT_ERROR = None

# 工具包标定选择器拒绝的标定类型（与 apps/gui/calibration_selector.py
# 的 discover_calibration_files 过滤口径一致）
_DIRECT_FK_KINEMATICS = {"direct_fk", "fitted_direct_fk"}
_DIRECT_FK_TOOLS = {"direct_fk_calibrate_cli", "hand_fk_fit_cli"}


def _calibration_ok(path: str, side: str) -> bool:
    """标定文件是否可用于该手侧（载荷解析 + 过滤口径）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return False
    if str(payload.get("side") or "").lower() != side:
        return False
    if not isinstance(payload.get("param_inst_calib"), dict):
        return False
    if payload.get("kinematics") in _DIRECT_FK_KINEMATICS:
        return False
    return str(payload.get("tool") or "") not in _DIRECT_FK_TOOLS


def _calibration_ts(path: str) -> float:
    """标定生成时间（payload generated_at → epoch；解析失败用文件 mtime）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        ts = payload.get("generated_at")
        if ts:
            from datetime import datetime
            return datetime.fromisoformat(str(ts)).timestamp()
    except (OSError, ValueError, TypeError, ImportError):
        pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def pick_calibration(sdk_dir: str, side: str) -> str:
    """该手侧要用的标定 JSON 路径；全无可用时返回 ""。

    优先级（对应「连接手套后先用标定 GUI 做标定」的工作流:
      GUI 每次保存带时间戳的新文件 → 该手套最新的标定自动被采用）:
      1) calibration/ 下按 generated_at 最新的该侧标定（跳过
         _default 与 2d 标定 —— 口径见 calibration_selector）
      2) calibration/imu_calibration_{side}_default.json（随包默认标定）

    刻意不用 SDK 自带的 `sdk.calibration.find_latest_hand2mm_calibration`：
    它是厂商另一套口径（只看 `param_inst_calib` 与 direct-FK 标记，按 mtime
    取最新），而我们的选择器还要认 generated_at、跳过 2d 标定。标定选择是
    我们自己的策略，保留。

    注意: 标定文件只按手侧区分、不含 USB 序列号 —— 换用新手套后
    若未重新标定，会沿用旧手套的标定（日志会标明实际采用的文件）。
    """
    cal_dir = os.path.join(sdk_dir, "calibration")
    if not os.path.isdir(cal_dir):
        return ""
    best, best_ts = "", 0.0
    try:
        names = sorted(os.listdir(cal_dir))
    except OSError:
        names = []
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        if name == f"imu_calibration_{side}_default.json":
            continue
        if "2d" in name.lower():
            continue
        path = os.path.join(cal_dir, name)
        if not _calibration_ok(path, side):
            continue
        ts = _calibration_ts(path)
        if ts > best_ts:
            best, best_ts = path, ts
    if best:
        return best
    dflt = os.path.join(cal_dir, f"imu_calibration_{side}_default.json")
    return dflt if _calibration_ok(dflt, side) else ""


def _warmup_done(details: dict):
    """KeypointFrame 是否已过 warmup 期 → True / False / None（认不出）。

    ⚠️ 这条判据在 SDK 2.1.0 上**换过键名**，而换法会让旧写法静默失效：
    旧工具包给的是 `details["warmup_completed"]`（出暖才置 True），SDK 2.1.0
    给的是一组**反极性**的键 —— 实测 `process()` 返回
    `['warming_up', 'warmup_static_rms_deg', 'warmup_remaining_s',
    'warmup_timed_out']`，暖机期间 `warming_up=True`。

    旧代码读 `details.get("warmup_completed", False)` ⇒ 恒 False ⇒ `_warm`
    永假 ⇒ `process()` 永远返回 None ⇒ **骨架列永久空白且零报错**（2026-09-21
    排查确认；joints_m 其实从第 1 帧就是有效有限值）。

    新判据 = 反极性 + 超时兜底：
      · `warming_up` 转 False —— 正常出暖；
      · `warmup_timed_out` —— 手一直在动、静止判据永不满足时的逃生口，
        否则骨架会永久不落盘（SDK 自己设了约 0.6s 的窗口，超时即不再等）。

    **认不出时返回 None，由调用方开着门放行** —— 见 process() 里的理由。
    """
    if "warming_up" in details:
        return bool(details.get("warmup_timed_out")) or not bool(details["warming_up"])
    if "warmup_completed" in details:        # 旧工具包键名，兼容保留
        return bool(details["warmup_completed"])
    return None


def find_sdk_dir() -> str:
    """`tools/glove_sdk` 目录（委托 core.glove_sdk_boot，口径唯一）。"""
    from core.glove_sdk_boot import find_sdk_dir as _find
    return _find()


def _ensure_imported() -> str:
    """确保 SDK 解算链已导入；返回错误描述（成功返回 ""）。"""
    global _HAND_SOLVER_CLS, _RAW_FRAME_CLS, _IMPORT_ERROR
    if _HAND_SOLVER_CLS is not None:
        return ""
    if _IMPORT_ERROR is not None:
        return _IMPORT_ERROR
    try:
        from core.glove_sdk_boot import solver_parts
        _HAND_SOLVER_CLS, _RAW_FRAME_CLS = solver_parts()
        return ""
    except Exception as exc:  # SDK 缺失/缺依赖/版本不兼容等，全部降级
        _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        return _IMPORT_ERROR


class GloveKeypointSolver:
    """单只手套的 21 关键点解算器（GloveDataPump 在连接时创建）。

    Args:
        side: "left" / "right"（决定标定文件，见 pick_calibration：优先该
              手套自己的标定，其次最新标定，最后随包默认标定）
        on_error: 工具包不可用/初始化失败时回调一次（接主窗口日志）
        on_warmup: 解算 warmup 完成时回调一次（骨架列开始有有效数据）
    """

    def __init__(self, side: str, on_error=None, on_warmup=None):
        self.side = side if side in ("left", "right") else "right"
        self._on_error = on_error or (lambda msg: None)
        self._on_warmup = on_warmup or (lambda: None)
        self._solver = None
        self._warm = False
        self._warmup_schema_warned = False   # 「认不出 warmup 键名」只报一次
        self._seq = 0
        self.calibration_name = ""   # 实际采用的标定文件名（日志用）
        self._load()

    # ── 初始化 ──────────────────────────────────────

    def _load(self):
        err = _ensure_imported()
        if err:
            self._on_error(f"骨架解算不可用: {err}")
            return
        sdk_dir = find_sdk_dir()
        calibration = pick_calibration(sdk_dir, self.side)
        if not calibration:
            self._on_error(f"骨架解算不可用: 无 {self.side} 手标定文件")
            return
        self.calibration_name = os.path.basename(calibration)
        geometry = os.path.join(
            sdk_dir, "assets", "hand_geometry",
            "hand_measured_runtime_v1.json")
        try:
            self._solver = _HAND_SOLVER_CLS(self.side, calibration, geometry)
        except Exception as exc:
            self._solver = None
            self._on_error(f"骨架解算初始化失败: {type(exc).__name__}: {exc}")

    def available(self) -> bool:
        return self._solver is not None

    @property
    def warmup_completed(self) -> bool:
        return self._warm

    # ── 解算 ────────────────────────────────────────

    def process(self, quats, valid, device_ts_us: int = 0):
        """→ (21,3) float32 关键点（米）；未就绪/未 warmup/异常返回 None。

        Args:
            quats: 64×float32 展平的 16×4 XYZW 四元数
            valid: 16×bool/float32 有效掩码（展平）
            device_ts_us: 帧的设备时间戳（微秒），0 时用宿主时钟
        """
        if self._solver is None:
            return None
        try:
            q = np.asarray(quats, dtype=np.float64).reshape(16, 4)
            v = np.asarray(valid).reshape(16) > 0.5
        except ValueError:
            return None
        if not np.isfinite(q).all():
            return None
        self._seq += 1
        host_us = time.time_ns() // 1_000
        frame = _RAW_FRAME_CLS(
            sequence=self._seq,
            # 无设备时间戳时用宿主时钟占位（恒等映射，避免时间轴跳变）
            device_timestamp_us=int(device_ts_us) if device_ts_us else host_us,
            host_timestamp_us=host_us,
            quaternions_xyzw=q,
            present_mask=v,
            valid_mask=v,
        )
        try:
            kf = self._solver.process(frame)
        except Exception as exc:
            if self._warm:
                return None
            self._on_error(f"骨架解算异常: {type(exc).__name__}: {exc}")
            return None
        if not self._warm:
            details = getattr(getattr(kf, "status", None), "details", {}) or {}
            state = _warmup_done(details)
            if state is None:
                # 认不出的 schema：**开着门放行**，只报一次警。
                # 宁可录进一段可能偏噪的骨架，也不要再来一次「骨架列永久
                # 空白且零报错」—— 2026-09-21 那次就是这么被咬的，前后
                # 折腾很久才定位到是键名换了。
                if not self._warmup_schema_warned:
                    self._warmup_schema_warned = True
                    self._on_error(
                        f"骨架 warmup 判据认不出（status.details 键: "
                        f"{sorted(details)}），已按「已出暖」放行")
                state = True
            self._warm = state
            if self._warm:
                self._on_warmup()
        if not self._warm:
            return None
        # ⚠️ `joints_m` 是**原始**解算输出，全链无平滑 —— 落盘列 `hand_pose`
        # 要的就是它。SDK 另有一处 `sdk/glove.py` 的 `_JointSmoother`
        # （35ms EMA），那是 `Glove` 引擎层的东西，我们不走那条路：EMA 会给
        # 位姿叠加**不可逆的相位滞后**，且平滑输出与 tick 时序耦合、无法做
        # 后端逐位对比。改这里等于改数据集语义，别顺手"优化"。
        joints = np.asarray(kf.joints_m, dtype=np.float32)
        if joints.shape != (21, 3) or not np.isfinite(joints).all():
            return None
        return joints
