#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GloveKeypointSolver 单测（假 toolkit，不依赖真实工具包/手套）:

    venv/bin/python tools/tests/test_glove_keypoint_solver.py

覆盖:
  1. 工具包目录缺失 → available()=False、process 不崩、错误回调一次
  2. 导入失败 → 同上
  3. warmup 门控: warmup_completed=False → None；完成后 → (21,3)
  4. 非有限关节 / 解算异常 / 输入形状错误 → None 不崩
  5. left/right 标定文件路径映射
退出码 0 = 全部通过。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import numpy as np

import core.glove_keypoint_solver as gks

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def make_fake_toolkit(root: str, with_sdk: bool = True,
                      suffix: str = "fake") -> str:
    """构造假工具包目录（可缺 glove_sdk 以模拟导入失败）。"""
    tk = os.path.join(root, f"stouch_glove_toolkit_{suffix}")
    os.makedirs(os.path.join(tk, "calibration"))
    os.makedirs(os.path.join(tk, "assets", "hand_geometry"))
    # 默认标定载荷须通过 pick_calibration 的 _calibration_ok 校验
    # （side 匹配 + param_inst_calib dict；generated_at 取旧日期，
    # 保证任何用户标定都比它新）
    for side in ("right", "left"):
        with open(os.path.join(tk, "calibration",
                               f"imu_calibration_{side}_default.json"),
                  "w") as f:
            f.write('{"side": "%s", "param_inst_calib": {},'
                    ' "generated_at": "2026-01-01T00:00:00"}' % side)
    with open(os.path.join(tk, "assets", "hand_geometry",
                           "hand_measured_runtime_v1.json"), "w") as f:
        f.write("{}")
    if with_sdk:
        sdk = os.path.join(tk, "glove_sdk")
        os.makedirs(os.path.join(sdk, "interfaces"))
        for d in (sdk, os.path.join(sdk, "interfaces")):
            with open(os.path.join(d, "__init__.py"), "w") as f:
                f.write("")
        with open(os.path.join(sdk, "types.py"), "w") as f:
            f.write(TYPES_SRC)
        with open(os.path.join(sdk, "interfaces", "solver.py"), "w") as f:
            f.write(SOLVER_SRC)
    return tk


TYPES_SRC = '''\
"""假 RawImuFrame（记录构造参数，不做数组规整）。"""


class RawImuFrame:
    def __init__(self, sequence, device_timestamp_us, host_timestamp_us,
                 quaternions_xyzw, present_mask, valid_mask):
        self.sequence = sequence
        self.device_timestamp_us = device_timestamp_us
        self.host_timestamp_us = host_timestamp_us
        self.quaternions_xyzw = quaternions_xyzw
        self.present_mask = present_mask
        self.valid_mask = valid_mask
'''

SOLVER_SRC = '''\
"""假 HandSolver：按模块级脚本返回结果。"""
import numpy as np


class HandStatus:
    def __init__(self, details):
        self.details = details


class KeypointFrame:
    def __init__(self, joints, warmup):
        self.joints_m = np.asarray(joints, np.float32)
        self.status = HandStatus({"warmup_completed": warmup})


class HandSolver:
    mode = "cold"          # cold | ok | nan | raise
    calls = 0
    last_args = None

    def __init__(self, side, calibration, geometry=None):
        HandSolver.last_args = (side, str(calibration), str(geometry))

    def process(self, frame):
        HandSolver.calls += 1
        if HandSolver.mode == "raise":
            raise RuntimeError("boom")
        joints = (np.zeros((21, 3), np.float32) if HandSolver.mode in
                  ("ok", "nan") else None)
        if HandSolver.mode == "nan":
            joints[7] = np.nan
        return KeypointFrame(joints, warmup=HandSolver.mode != "cold")
'''


def reset_import_state():
    """清掉进程级懒导入缓存与已注入的假模块。"""
    gks._HAND_SOLVER_CLS = None
    gks._RAW_FRAME_CLS = None
    gks._IMPORT_ERROR = None
    sys.modules.pop("glove_sdk", None)
    sys.modules.pop("glove_sdk.types", None)
    sys.modules.pop("glove_sdk.interfaces", None)
    sys.modules.pop("glove_sdk.interfaces.solver", None)


def main():
    orig_find = gks.find_toolkit_dir

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1. 工具包目录缺失 ──
        reset_import_state()
        gks.find_toolkit_dir = lambda: ""
        errors = []
        sol = gks.GloveKeypointSolver("right", on_error=errors.append)
        check(not sol.available(), "目录缺失: available()=False")
        check(len(errors) == 1 and "未找到工具包目录" in errors[0],
              f"目录缺失: 错误回调一次: {errors}")
        check(sol.process(np.zeros(64), np.ones(16), 1) is None,
              "目录缺失: process → None 不崩")

        # ── 2. 导入失败（目录在但没有 glove_sdk）──
        reset_import_state()
        tk_bad = make_fake_toolkit(tmp, with_sdk=False, suffix="bad")
        gks.find_toolkit_dir = lambda: tk_bad
        errors.clear()
        sol = gks.GloveKeypointSolver("right", on_error=errors.append)
        check(not sol.available(), "导入失败: available()=False")
        check(len(errors) == 1 and "骨架解算不可用" in errors[0],
              f"导入失败: 错误回调一次: {errors}")

        # ── 3. warmup 门控 + 正常解算 ──
        reset_import_state()
        tk = make_fake_toolkit(tmp, with_sdk=True)
        gks.find_toolkit_dir = lambda: tk
        errors.clear()
        warmups = []
        sol = gks.GloveKeypointSolver("right", on_error=errors.append,
                                      on_warmup=lambda: warmups.append(1))
        check(sol.available(), "正常导入: available()=True")
        check(not errors, f"正常导入: 无错误回调: {errors}")
        from glove_sdk.interfaces.solver import HandSolver as FakeSolver
        FakeSolver.mode = "cold"
        FakeSolver.calls = 0
        quats = np.tile([0.0, 0.0, 0.0, 1.0], 16).astype(np.float32)
        valid = np.ones(16, np.float32)
        check(sol.process(quats, valid, 1000) is None,
              "cold: warmup 前返回 None")
        check(not warmups, "cold: on_warmup 未触发")
        FakeSolver.mode = "ok"
        k = sol.process(quats, valid, 1000)
        check(k is not None and k.shape == (21, 3),
              "ok: 返回 (21,3)")
        check(warmups == [1], "ok: on_warmup 触发一次")
        k2 = sol.process(quats, valid, 2000)
        check(k2 is not None and len(warmups) == 1,
              "ok: 后续帧持续解算, on_warmup 不重复")
        side, calib, geom = FakeSolver.last_args
        check(side == "right"
              and calib.endswith("imu_calibration_right_default.json")
              and geom.endswith("hand_measured_runtime_v1.json"),
              f"right: 标定/几何路径映射: {calib}")
        check(FakeSolver.calls == 3, f"ok: process 调用次数: {FakeSolver.calls}")

        # ── 4. left 映射 + 非有限/异常/坏输入 ──
        FakeSolver.mode = "nan"
        check(sol.process(quats, valid, 3000) is None,
              "nan: 关节非有限 → None")
        FakeSolver.mode = "raise"
        check(sol.process(quats, valid, 4000) is None,
              "raise: 解算异常 → None 不崩")
        check(sol.process(np.zeros(10), valid, 5000) is None,
              "坏输入: 形状不符 → None")
        sol_l = gks.GloveKeypointSolver("left")
        check(FakeSolver.last_args[0] == "left"
              and FakeSolver.last_args[1].endswith(
                  "imu_calibration_left_default.json"),
              f"left: 标定路径映射: {FakeSolver.last_args[1]}")

        # ── 5. pick_calibration 优先级 ──
        # 最新 generated_at 优先；2d / 无效载荷 / _default 排除；全无 → ""
        cal = os.path.join(tk, "calibration")
        def write_cal(name, side, ts, param_valid=True):
            payload = {"side": side,
                       "param_inst_calib": {} if param_valid else None,
                       "generated_at": ts}
            with open(os.path.join(cal, name), "w") as f:
                f.write(json.dumps(payload))

        import json
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
        picked = gks.pick_calibration(tk, "right")
        check(picked.endswith("imu_calibration_right_20260827_101530.json"),
              f"pick: 最新 generated_at 胜出: {picked}")
        os.unlink(os.path.join(cal,
                               "imu_calibration_right_20260827_101530.json"))
        picked = gks.pick_calibration(tk, "right")
        check(picked.endswith("imu_calibration_right.json"),
              f"pick: 次新用户标定接替: {picked}")
        for name in ("imu_calibration_right.json",
                     "imu_calibration_right_2d_20260828.json",
                     "imu_calibration_right_broken.json"):
            os.unlink(os.path.join(cal, name))
        picked = gks.pick_calibration(tk, "right")
        check(picked.endswith("imu_calibration_right_default.json"),
              f"pick: 无用户标定回落默认: {picked}")
        os.unlink(os.path.join(cal, "imu_calibration_right_default.json"))
        check(gks.pick_calibration(tk, "right") == "",
              "pick: 全无可用 → \"\"")
        check(gks.pick_calibration(tk, "left") != "",
              "pick: 左侧仍有默认标定不受影响")

    gks.find_toolkit_dir = orig_find
    reset_import_state()

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: GloveKeypointSolver 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
