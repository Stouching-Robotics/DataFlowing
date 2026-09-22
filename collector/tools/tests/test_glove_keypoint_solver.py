#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GloveKeypointSolver 单测（假 SDK，不依赖真实 SDK/手套/Python 3.10）:

    venv/bin/python tools/tests/test_glove_keypoint_solver.py

**为什么能在任何解释器下跑**：解算链的 import 由 core.glove_sdk_boot 负责，
本测试把 `glove_sdk_boot.solver_parts` 换成假实现（`_ensure_imported` 内部
是 `from core.glove_sdk_boot import solver_parts`，即调用时 getattr，所以
直接给模块属性赋值就能拦住）。真 SDK 的装配与注入另有
`test_glove_sdk_boot.py` 管，那份需要真 SDK 与 3.10。

覆盖:
  1. SDK 目录缺失 → available()=False、process 不崩、错误回调一次
  2. 导入失败 → 同上
  3. warmup 门控（**含 SDK 2.1.0 换键名后的三种新形态与认不出时的放行**）
  4. 非有限关节 / 解算异常 / 输入形状错误 → None 不崩
  5. left/right 标定文件路径映射
  6. pick_calibration 优先级
退出码 0 = 全部通过。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import numpy as np

import core.glove_keypoint_solver as gks
import core.glove_sdk_boot as boot

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


# ── 假 SDK（替代真 sdk.solver.HandSolver / common.types.RawImuFrame）──

class FakeRawImuFrame:
    def __init__(self, sequence, device_timestamp_us, host_timestamp_us,
                 quaternions_xyzw, present_mask, valid_mask):
        self.sequence = sequence
        self.device_timestamp_us = device_timestamp_us
        self.host_timestamp_us = host_timestamp_us
        self.quaternions_xyzw = quaternions_xyzw
        self.present_mask = present_mask
        self.valid_mask = valid_mask


class FakeHandStatus:
    def __init__(self, details):
        self.details = details


class FakeKeypointFrame:
    def __init__(self, joints, details):
        self.joints_m = np.asarray(joints, np.float32)
        self.status = FakeHandStatus(details)


class FakeHandSolver:
    """按模块级脚本返回结果。details 决定 warmup 判据长什么样。"""
    mode = "cold"          # cold | ok | nan | raise
    calls = 0
    last_args = None
    details = {"warmup_completed": False}

    def __init__(self, side, calibration, geometry=None):
        FakeHandSolver.last_args = (side, str(calibration), str(geometry))

    def process(self, frame):
        FakeHandSolver.calls += 1
        if FakeHandSolver.mode == "raise":
            raise RuntimeError("boom")
        joints = (np.zeros((21, 3), np.float32) if FakeHandSolver.mode in
                  ("ok", "nan") else None)
        if FakeHandSolver.mode == "nan":
            joints[7] = np.nan
        return FakeKeypointFrame(joints, FakeHandSolver.details)


_TRUE_SOLVER_PARTS = boot.solver_parts


def install_fake_sdk(ok: bool = True, message: str = ""):
    """把 boot 的解算链换成假实现；ok=False 模拟导入失败（可指定报错文本）。"""
    if ok:
        boot.solver_parts = lambda: (FakeHandSolver, FakeRawImuFrame)
    else:
        def _boom():
            raise ImportError(message or "No module named 'sdk.solver'")
        boot.solver_parts = _boom


def reset_import_state():
    """清掉进程级懒导入缓存（与 solver 内部缓存同口径）。"""
    gks._HAND_SOLVER_CLS = None
    gks._RAW_FRAME_CLS = None
    gks._IMPORT_ERROR = None
    install_fake_sdk(True)


def make_fake_sdk_dir(root: str, suffix: str = "fake") -> str:
    """构造假 SDK 目录 —— 只放 solve 需要的两样：标定与几何。"""
    sdk = os.path.join(root, f"glove_sdk_{suffix}")
    os.makedirs(os.path.join(sdk, "calibration"))
    os.makedirs(os.path.join(sdk, "assets", "hand_geometry"))
    # 默认标定载荷须通过 pick_calibration 的 _calibration_ok 校验
    # （side 匹配 + param_inst_calib dict；generated_at 取旧日期，
    # 保证任何用户标定都比它新）
    for side in ("right", "left"):
        with open(os.path.join(sdk, "calibration",
                               f"imu_calibration_{side}_default.json"),
                  "w") as f:
            f.write('{"side": "%s", "param_inst_calib": {},'
                    ' "generated_at": "2026-01-01T00:00:00"}' % side)
    with open(os.path.join(sdk, "assets", "hand_geometry",
                           "hand_measured_runtime_v1.json"), "w") as f:
        f.write("{}")
    return sdk


def main():
    orig_find = gks.find_sdk_dir

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1. SDK 目录缺失（boot.ensure_sdk 的报错原文）──
        reset_import_state()
        gks.find_sdk_dir = lambda: ""
        install_fake_sdk(False, f"未找到厂商 SDK 目录（{boot.sdk_relpath()}/）")
        errors = []
        sol = gks.GloveKeypointSolver("right", on_error=errors.append)
        check(not sol.available(), "目录缺失: available()=False")
        check(len(errors) == 1 and "未找到厂商 SDK 目录" in errors[0],
              f"目录缺失: 错误回调一次: {errors}")
        check(sol.process(np.zeros(64), np.ones(16), 1) is None,
              "目录缺失: process → None 不崩")

        # ── 2a. 目录在但导入失败（Python 版本不符 / SDK 损坏）──
        reset_import_state()
        sdk_bad = make_fake_sdk_dir(tmp, "bad")
        install_fake_sdk(False)
        gks.find_sdk_dir = lambda: sdk_bad
        errors.clear()
        sol = gks.GloveKeypointSolver("right", on_error=errors.append)
        check(not sol.available(), "导入失败: available()=False")
        check(len(errors) == 1 and "骨架解算不可用" in errors[0],
              f"导入失败: 错误回调一次: {errors}")

        # ── 2b. 导入成功但标定目录缺失 → 单独一条报错，别混成"SDK 不可用" ──
        reset_import_state()
        gks.find_sdk_dir = lambda: ""
        errors.clear()
        sol = gks.GloveKeypointSolver("right", on_error=errors.append)
        check(not sol.available(), "无标定: available()=False")
        check(len(errors) == 1 and "无 right 手标定文件" in errors[0],
              f"无标定: 错误回调一次: {errors}")

        # ── 3. warmup 门控 + 正常解算 ──
        reset_import_state()
        sdk = make_fake_sdk_dir(tmp, "ok")
        gks.find_sdk_dir = lambda: sdk
        errors.clear()
        warmups = []
        sol = gks.GloveKeypointSolver("right", on_error=errors.append,
                                      on_warmup=lambda: warmups.append(1))
        check(sol.available(), "正常导入: available()=True")
        check(not errors, f"正常导入: 无错误回调: {errors}")
        FakeHandSolver.mode = "cold"
        FakeHandSolver.details = {"warmup_completed": False}
        FakeHandSolver.calls = 0
        quats = np.tile([0.0, 0.0, 0.0, 1.0], 16).astype(np.float32)
        valid = np.ones(16, np.float32)
        check(sol.process(quats, valid, 1000) is None,
              "cold: warmup 前返回 None")
        check(not warmups, "cold: on_warmup 未触发")
        FakeHandSolver.mode = "ok"
        FakeHandSolver.details = {"warmup_completed": True}
        k = sol.process(quats, valid, 1000)
        check(k is not None and k.shape == (21, 3),
              "ok: 返回 (21,3)")
        check(warmups == [1], "ok: on_warmup 触发一次")
        k2 = sol.process(quats, valid, 2000)
        check(k2 is not None and len(warmups) == 1,
              "ok: 后续帧持续解算, on_warmup 不重复")
        side, calib, geom = FakeHandSolver.last_args
        check(side == "right"
              and calib.endswith("imu_calibration_right_default.json")
              and geom.endswith("hand_measured_runtime_v1.json"),
              f"right: 标定/几何路径映射: {calib}")
        check(FakeHandSolver.calls == 3,
              f"ok: process 调用次数: {FakeHandSolver.calls}")

        # ── 3b. warmup 判据：SDK 2.1.0 换键名后的各种形态 ──
        # 旧写法读 details["warmup_completed"]，而 2.1.0 给的是反极性的
        # warming_up —— 旧写法恒 False ⇒ 骨架列永久空白且零报错。这一组
        # 把新旧两套键都钉住，外加「认不出时必须放行」。
        def fresh_solver():
            FakeHandSolver.mode = "ok"
            return gks.GloveKeypointSolver("right", on_error=errors.append)

        cases = [
            ({"warmup_completed": False}, None, "旧键: 未出暖 → None"),
            ({"warmup_completed": True}, (21, 3), "旧键: 出暖 → (21,3)"),
            ({"warming_up": True, "warmup_remaining_s": 0.5,
              "warmup_timed_out": False}, None, "新键: warming_up → None"),
            ({"warming_up": False}, (21, 3), "新键: 出暖 → (21,3)"),
            ({"warming_up": True, "warmup_timed_out": True}, (21, 3),
             "新键: 暖机超时逃生口 → 放行"),
            ({}, (21, 3), "认不出的键集合 → 放行（不再静默空列）"),
            ({"warming_up": False, "warmup_static_rms_deg": 0.0}, (21, 3),
             "新键: 带统计量 → (21,3)"),
        ]
        for details, want, label in cases:
            reset_import_state()
            errors.clear()
            s = fresh_solver()
            FakeHandSolver.details = details
            got = s.process(quats, valid, 1000)
            shape = None if got is None else got.shape
            check(shape == want, f"warmup {label}: {shape}")

        # 认不出时只报一次警（不是每帧刷屏）
        reset_import_state()
        errors.clear()
        s = fresh_solver()
        FakeHandSolver.details = {}
        for _ in range(5):
            s.process(quats, valid, 1000)
        check(len(errors) == 1 and "认不出" in errors[0],
              f"认不出的键集合: 只报一次警: {errors}")

        # ── 4. left 映射 + 非有限/异常/坏输入 ──
        reset_import_state()
        errors.clear()
        sol = gks.GloveKeypointSolver("right", on_error=errors.append)
        FakeHandSolver.details = {"warmup_completed": True}
        FakeHandSolver.mode = "nan"
        check(sol.process(quats, valid, 3000) is None,
              "nan: 关节非有限 → None")
        FakeHandSolver.mode = "raise"
        check(sol.process(quats, valid, 4000) is None,
              "raise: 解算异常 → None 不崩")
        check(sol.process(np.zeros(10), valid, 5000) is None,
              "坏输入: 形状不符 → None")
        sol_l = gks.GloveKeypointSolver("left")
        check(FakeHandSolver.last_args[0] == "left"
              and FakeHandSolver.last_args[1].endswith(
                  "imu_calibration_left_default.json"),
              f"left: 标定路径映射: {FakeHandSolver.last_args[1]}")

        # ── 5. pick_calibration 优先级 ──
        # 最新 generated_at 优先；2d / 无效载荷 / _default 排除；全无 → ""
        cal = os.path.join(sdk, "calibration")

        def write_cal(name, side, ts, param_valid=True):
            payload = {"side": side,
                       "param_inst_calib": {} if param_valid else None,
                       "generated_at": ts}
            with open(os.path.join(cal, name), "w") as f:
                f.write(json.dumps(payload))

        write_cal("imu_calibration_right.json", "right",
                  "2026-08-26T10:00:00")           # 用户标定（较旧）
        write_cal("imu_calibration_right_20260827_101530.json", "right",
                  "2026-08-27T10:15:30")           # 最新的用户标定 → 应胜出
        write_cal("imu_calibration_right_2d_20260828.json", "right",
                  "2026-08-28T10:00:00")           # 2d 排除（即便更新）
        write_cal("imu_calibration_right_broken.json", "right",
                  "2026-09-01T10:00:00", param_valid=False)  # 无效载荷排除
        write_cal("imu_calibration_left.json", "left",
                  "2026-08-27T09:00:00")           # 另一侧不干扰
        picked = gks.pick_calibration(sdk, "right")
        check(picked.endswith("imu_calibration_right_20260827_101530.json"),
              f"pick: 最新 generated_at 胜出: {picked}")
        os.unlink(os.path.join(cal,
                               "imu_calibration_right_20260827_101530.json"))
        picked = gks.pick_calibration(sdk, "right")
        check(picked.endswith("imu_calibration_right.json"),
              f"pick: 次新用户标定接替: {picked}")
        for name in ("imu_calibration_right.json",
                     "imu_calibration_right_2d_20260828.json",
                     "imu_calibration_right_broken.json"):
            os.unlink(os.path.join(cal, name))
        picked = gks.pick_calibration(sdk, "right")
        check(picked.endswith("imu_calibration_right_default.json"),
              f"pick: 无用户标定回落默认: {picked}")
        os.unlink(os.path.join(cal, "imu_calibration_right_default.json"))
        check(gks.pick_calibration(sdk, "right") == "",
              "pick: 全无可用 → \"\"")
        check(gks.pick_calibration(sdk, "left") != "",
              "pick: 左侧仍有默认标定不受影响")

    gks.find_sdk_dir = orig_find
    boot.solver_parts = _TRUE_SOLVER_PARTS
    reset_import_state()

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: GloveKeypointSolver 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
