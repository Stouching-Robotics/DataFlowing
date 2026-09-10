#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repair_force_matrix_scale 的推断逻辑自检（无 pytorch/Qt 依赖，直接运行）:

    venv/bin/python tools/tests/test_repair_force_matrix_scale.py

覆盖:
  1. 四个档位都判得出：float32 / int16×10 / int16×1000 → 实测比值；未定标
     int16（逐点向零截断）→ 和明显小于 1 → 判 ×1
  2. 判不出的情形一律不写：无 force 列可比对、近零帧不算数、有效帧不足
  3. 补写只动 meta 行：data parquet 逐字节不变
  4. 已带倍率的段不重推（录制端写下的值是事实，推断只是补救手段）

判据本身（实测比值定档）有测试兜着，是因为这条逻辑一旦判错就是静默
放大/缩小 N 倍 —— 数值看着"正常"，只有和图对比才看得出来。
退出码 0 = 全部通过。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from core.egodata_writer import _episode_rows_table   # noqa: E402
from core.helpers import (                            # noqa: E402
    pooled_data_parquet_path, pooled_episodes_path)

COLUMN = "observation.gripper_left_force_matrix"
FORCE_COLUMN = "observation.gripper_left_force"
SIDE = 30                       # 接触方块边长 → 900 个受力点
VALUE = 5.75                    # 真值 mN；未定标 int16 档会被截成 5
N_FRAMES = 8                    # ≥ MIN_FRAMES(5)

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def load_repair_module():
    path = os.path.join(REPO_ROOT, "scripts", "repair_force_matrix_scale.py")
    spec = importlib.util.spec_from_file_location("repair_force_matrix_scale",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plane(value: float = VALUE) -> np.ndarray:
    m = np.zeros((250, 250, 3), np.float32)
    m[120:120 + SIDE, 110:110 + SIDE, 2] = value
    return m


def encode(matrix: np.ndarray, spec: str) -> list:
    """四个档位的落盘编码（与 ui.main_window.encode_gripper_force_matrix 同口径）。"""
    flat = np.asarray(matrix, np.float32).reshape(matrix.shape[0], -1)
    if spec == "float32":
        return flat.reshape(-1).tolist()
    n = {"int16": 1, "int16x10": 10, "int16x100": 100,
         "int16x1000": 1000}[spec]
    q = (np.clip(flat, -32767.0, 32767.0).astype(np.int16) if n == 1
         else np.clip(np.rint(flat * np.float32(n)),
                      -32767.0, 32767.0).astype(np.int16))
    diff = np.empty_like(q)
    diff[:, 0] = q[:, 0]
    diff[:, 1:] = q[:, 1:] - q[:, :-1]
    return np.frombuffer(diff.tobytes(), dtype=np.int16).tolist()


def make_task(root: str, name: str, spec: str, index: int = 1,
              with_force: bool = True, with_meta_specs: bool = False,
              active_frames: int = N_FRAMES) -> str:
    """合成一份「旧格式」任务：meta 行没有 force_matrix_specs（除非要求）。

    力列 fz = 矩阵平面 fz 之和 —— 这是标定档能还原到 1.000 的物理依据；
    未定标档因截断会小于它，正是判据要区分的那条。
    """
    task = os.path.join(root, name)
    m_true = plane()
    enc = encode(m_true, spec)
    # 前 active_frames 帧有样本，其余留空（稀疏列）
    rows = [enc] * active_frames + [[]] * (N_FRAMES - active_frames)
    cols = {"frame_index": pa.array(range(N_FRAMES), pa.int64()),
            COLUMN: pa.array(rows, pa.list_(
                pa.float32() if spec == "float32" else pa.int16()))}
    if with_force:
        # 力列给真值（矩阵真值之和），与档位无关
        total = float(m_true[:, :, 2].sum())
        cols[FORCE_COLUMN] = pa.array(
            [[0.0, 0.0, total]] * N_FRAMES, pa.list_(pa.float32(), 3))
    data_path = pooled_data_parquet_path(task, index)
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    pq.write_table(pa.table(cols), data_path)

    specs = {} if not with_meta_specs else {
        COLUMN: {"dtype": "float32" if spec == "float32" else "int16",
                 "shape": [250, 250, 3],
                 "encoding": ("row_flat_raw" if spec == "float32"
                              else "row_diff_quantized"),
                 "scale": 1}}
    meta_path = pooled_episodes_path(task, index)
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    pq.write_table(_episode_rows_table([{
        "episode_index": index, "task_index": 0, "start_frame_index": 0,
        "end_frame_index": N_FRAMES - 1, "length": N_FRAMES,
        "created_at": 0.0, "duration_sec": 0.2, "drop_stats": "{}",
        "video_codec": "{}", "calibration": "{}",
        "force_matrix_specs": json.dumps(specs),
    }]), meta_path)
    return task


def main() -> int:
    mod = load_repair_module()
    tmp = tempfile.mkdtemp(prefix="repair_scale_")
    try:
        print("[1] 各档位都判得出（实测比值定档）")
        for spec, want in (("float32", 1), ("int16x10", 10),
                           ("int16x100", 100), ("int16x1000", 1000),
                           ("int16", 1)):
            task = make_task(tmp, f"t_{spec}", spec)
            dpath = pooled_data_parquet_path(task, 1)
            got, why = mod.infer_scale(dpath, COLUMN, FORCE_COLUMN)
            check(f"{spec} → ×{want}",
                  got == want, f"判得 ×{got}：{why}")

        print("[2] 判不出的情形一律不写")
        no_force = make_task(tmp, "no_force", "int16x10", with_force=False)
        got, why = mod.infer_scale(pooled_data_parquet_path(no_force, 1),
                                   COLUMN, FORCE_COLUMN)
        check("无 force 列可比对 → 判不出", got is None, str(why))

        # 力列恒为 0（近零帧是分母噪声，不能拿来比）→ 有效帧不足
        zero = make_task(tmp, "zero_force", "int16x10")
        zpath = pooled_data_parquet_path(zero, 1)
        table = pq.read_table(zpath)
        table = table.set_column(table.schema.get_field_index(FORCE_COLUMN),
                                 FORCE_COLUMN,
                                 pa.array([[0.0, 0.0, 0.0]] * N_FRAMES,
                                          pa.list_(pa.float32(), 3)))
        pq.write_table(table, zpath)
        got, why = mod.infer_scale(zpath, COLUMN, FORCE_COLUMN)
        check("力列全零 → 有效帧不足、判不出", got is None, str(why))

        few = make_task(tmp, "few_frames", "int16x10", active_frames=2)
        got, why = mod.infer_scale(pooled_data_parquet_path(few, 1),
                                   COLUMN, FORCE_COLUMN)
        check("有效帧 < 5 → 判不出（不拿两帧中位数赌）", got is None, str(why))

        print("[3] 补写只动 meta 行")
        task = make_task(tmp, "apply_int16x10", "int16x10")
        dpath = pooled_data_parquet_path(task, 1)

        def recorded():
            raw = pq.read_table(pooled_episodes_path(task, 1),
                                columns=["force_matrix_specs"]).column(0)[0]
            return json.loads(raw.as_py() or "{}")

        before = open(dpath, "rb").read()
        check("预览模式不写", mod.repair(task, False) == 0 and recorded() == {},
              "预览后 meta 仍未记录")
        check("apply 写入", mod.repair(task, True) == 0)
        specs = recorded()
        check("写入 ×10（实测比值定档）",
              specs[COLUMN]["scale"] == 10 and specs[COLUMN]["dtype"] == "int16",
              str(specs.get(COLUMN)))
        check("data parquet 逐字节不变（只补 meta）",
              open(dpath, "rb").read() == before)

        print("[4] 已有权威倍率的段不重推")
        ws = make_task(tmp, "already_has", "int16x1000",
                       with_meta_specs=True)      # 记为 ×1（模拟旧值）
        check("plan_task 不把已记录的段列为待补",
              all(name != "episode-000.parquet"
                  for name, *_ in mod.plan_task(ws)),
              str([n for n, *_ in mod.plan_task(ws)]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 — {_FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
