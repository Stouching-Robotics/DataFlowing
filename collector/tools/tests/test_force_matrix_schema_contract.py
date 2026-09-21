#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""力矩阵列**落盘契约**：把「泵交给 writer 的东西」从 Python list 换成 ndarray
（v1.3.11 L1-1），列本身的 schema 与值必须一个字节都不变。

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_force_matrix_schema_contract.py

**为什么要有这份测试**：`pa.array([list, list, ...], pa.list_(int16))` 对
Python list 是逐值装箱——185 帧 × 187500 点实测 **538ms 全程持 GIL**，正好
把原始流接收线程挡在 `recv()` 外面；服务端 16 包队列≈0.53s 一满就主动
close（2026-09-18 实测：55 次「完成」里 54 次收尾断链，而中止路径不写
parquet ⇒ 0/11）。改走 numpy 缓冲后同一列 **13.5ms / 最长停摆 3.6ms**
（噪声底 0.9ms）。但这条列是**数据主路径**，改错了就是静默数据损坏：

  * `_note_matrix_dtype` 用 `isinstance(value[0], float)` 定型，而
    `isinstance(np.float32(x), float)` 是 **False** ⇒ float32 矩阵会被
    判成 int16，列类型错 + 精度真丢（不是报错，是悄悄截断）；
  * `if not value:` 对真数组直接 **抛 ValueError**（numpy≥1.25 的
    「truth value of an empty array is ambiguous」）⇒ 每帧都炸；
  * offsets 写错 ⇒ **行错位**，而总行数、列类型全都对得上，肉眼看不出来。

所以本测试**不引用新实现**：基准是冻结的旧表达式（`pa.array(list_of_lists)`），
把 writer 真写出的 parquet 与它逐字段、逐元素比。旧写法不认识 ndarray 时
本测试必须变红（先红后绿的改造纪律）。

覆盖：int16 档 / float32 档 / 两种入参（list 与 ndarray）/ 逐行混用 /
空行 / 缺键行 / 变长行 / 单帧 / 定型判定 / features 描述 / 停顿回归（软断言）。
退出码 0 = 全部通过。
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from core.egodata_writer import EgoDataWriter                # noqa: E402
from core.gripper_codec import (                             # noqa: E402
    encode_gripper_force_matrix, encode_gripper_force_matrix_array)

MATRIX_KEY = "gripper_left_force_matrix"
COLUMN = f"observation.{MATRIX_KEY}"
DIM = 250 * 250 * 3                     # 187500：一小帧力矩阵的元素数

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


# ── 基准：冻结的旧实现（改动前 writer 用的就是这一行）────────────
def legacy_column(values, item):
    """`pa.array([r.get(name, []) for r in rows], pa.list_(item))` 的等价冻结版。

    刻意不复用被测代码：新旧要能互相对照，基准必须自己站着。
    """
    return pa.array(list(values), pa.list_(item))


def item_of(dtype: str):
    return pa.float32() if dtype == "float32" else pa.int16()


# ── fixture：一列的全部边界形状 ────────────────────────────────
def contact_matrix(value: float) -> np.ndarray:
    m = np.zeros((250, 250, 3), np.float32)
    m[105:135, 105:135, 2] = value
    m[105:135, 105:135, 0] = value * 0.25
    return m


class Rows:
    """一列的行值序列；`as_` 决定每行交付给 writer 的形态。

    同一份逻辑内容、三种交付形态（list / ndarray / 混用），
    落盘结果必须**完全一致**——这正是「换入参类型不改契约」的定义。
    """

    def __init__(self):
        self.raw = []            # (kind, payload)
        for i in range(6):
            self.raw.append(("mat", contact_matrix(3.0 + i)))
        self.raw.append(("empty", None))          # 空行：有键、值为空
        self.raw.append(("missing", None))        # 缺键行
        self.raw.append(("short", contact_matrix(9.0)))
        self.raw.append(("empty", None))

    def encoded(self, spec: str, as_: str):
        out = []
        for kind, payload in self.raw:
            if kind == "missing":
                out.append(None)
                continue
            if kind == "empty":
                out.append(np.zeros(0, np.float32) if as_ == "ndarray" else [])
                continue
            enc = (encode_gripper_force_matrix_array(payload, spec)
                   if as_ == "ndarray" else encode_gripper_force_matrix(payload, spec))
            if kind == "short":
                # 变长行：截短到 750（一行=一个力平面）——offsets 最容易错的地方
                enc = enc[:750]
            out.append(enc)
        return out

    def row_dicts(self, spec: str, as_: str):
        rows = []
        for enc in self.encoded(spec, as_):
            rows.append({} if enc is None else {COLUMN: enc})
        return rows

    def reference(self, spec: str):
        """旧实现的基准列：逐行缺键 → 空列表（**不是 null**）。"""
        return legacy_column([r.get(COLUMN, []) for r in self.row_dicts(spec, "list")],
                             item_of(spec))


def write_episode(task_dir: str, row_dicts, default: str = ""):
    writer = EgoDataWriter()
    writer._task_dir = task_dir
    writer._episode_index = 0
    for i, row in enumerate(row_dicts):
        gripper = {}
        key = MATRIX_KEY if COLUMN in row else None
        if key:
            gripper[key] = row[COLUMN]
        writer.write_frame_row(i, i / 30.0, gripper=gripper)
    writer._write_data_parquet()
    return writer, os.path.join(task_dir, "data", "chunk-000",
                                "episode-000.parquet")


def read_column(path: str):
    return pq.read_table(path, columns=[COLUMN]).column(COLUMN).combine_chunks()


# ── ① 两种档位 × 三种入参：schema 与值逐元素一致 ───────────────
def test_schema_and_values(tmp: str):
    for spec in ("int16", "float32"):
        ref = Rows().reference(spec)
        for as_ in ("list", "ndarray", "mixed"):
            sub = os.path.join(tmp, f"{spec}_{as_}")
            r = Rows()
            if as_ == "mixed":
                # 逐行混用：偶数行 list、奇数行 ndarray（历史调用方与新调用方
                # 同时存在时的形状；两边的 dtype 判定必须给出同一结论）
                a = r.row_dicts(spec, "list")
                b = r.row_dicts(spec, "ndarray")
                rowdicts = [a[i] if i % 2 == 0 else b[i]
                            for i in range(len(a))]
            else:
                rowdicts = r.row_dicts(spec, as_)
            _, path = write_episode(sub, rowdicts)
            col = read_column(path)
            tag = f"{spec}/{as_}"
            check(f"{tag}: parquet 列类型 = list<{spec}>",
                  pq.read_schema(path).field(COLUMN).type == ref.type,
                  str(pq.read_schema(path).field(COLUMN).type))
            check(f"{tag}: 行数一致",
                  len(col) == len(ref), f"{len(col)} vs {len(ref)}")
            check(f"{tag}: 值与基准逐元素相等",
                  col.to_pylist() == ref.to_pylist())
            check(f"{tag}: 无 null 元素（空行落成空 list）",
                  col.null_count == 0 and all(v is not None for v in col.to_pylist()))
            # 行错位最隐蔽：单独再钉一次每行长度的序列
            check(f"{tag}: 行长序列一致",
                  [len(v) for v in col.to_pylist()]
                  == [len(v) for v in ref.to_pylist()],
                  str([len(v) for v in col.to_pylist()]))


# ── ② 定型判定：numpy 标量必须与 Python 标量同判 ────────────────
def test_dtype_classification(tmp: str):
    for spec in ("int16", "float32"):
        seen = {}
        for as_ in ("list", "ndarray"):
            sub = os.path.join(tmp, f"cls_{spec}_{as_}")
            w, path = write_episode(sub, Rows().row_dicts(spec, as_))
            seen[as_] = w._matrix_dtype.get(MATRIX_KEY)
            check(f"{spec}/{as_}: 定型 = {spec}",
                  seen[as_] == spec, str(seen[as_]))
            # info.json 的 dtype/encoding 是读取端唯一依据，必须跟着一致
            w._write_info_json()
            feats = json.load(open(os.path.join(sub, "meta", "info.json"),
                                   encoding="utf-8"))["features"][COLUMN]
            want_enc = "row_flat_raw" if spec == "float32" else "row_diff_quantized"
            check(f"{spec}/{as_}: features dtype/encoding 正确",
                  feats["dtype"] == spec and feats["encoding"] == want_enc,
                  str(feats))
        check(f"{spec}: 两种入参定型一致（np.float32 不是 float！）",
              seen["list"] == seen["ndarray"],
              f"list={seen['list']} ndarray={seen['ndarray']}")

    # 空 ndarray 首帧不定型（`if not value:` 对真数组会抛 ValueError）
    w = EgoDataWriter()
    w._task_dir = os.path.join(tmp, "empty_first")
    w._episode_index = 0
    w.write_frame_row(0, 0.0, gripper={MATRIX_KEY: np.zeros(0, np.int16)})
    check("空 ndarray 首帧不抛异常且不定型",
          w._matrix_dtype.get(MATRIX_KEY) is None, str(w._matrix_dtype))
    w.write_frame_row(1, 1 / 30.0, gripper={
        MATRIX_KEY: encode_gripper_force_matrix_array(contact_matrix(4.0), "int16")})
    check("空 ndarray 之后仍能定型",
          w._matrix_dtype.get(MATRIX_KEY) == "int16", str(w._matrix_dtype))

    # 混型仍要显式报错（历史契约：不让 pyarrow 抛难懂的转换异常）
    w2 = EgoDataWriter()
    w2._task_dir = os.path.join(tmp, "mixed_spec")
    w2._episode_index = 0
    w2.write_frame_row(0, 0.0, gripper={
        MATRIX_KEY: encode_gripper_force_matrix_array(contact_matrix(4.0), "int16")})
    raised = None
    try:
        w2.write_frame_row(1, 0.5, gripper={
            MATRIX_KEY: encode_gripper_force_matrix_array(
                contact_matrix(4.0), "float32")})
    except ValueError as exc:
        raised = exc
    check("ndarray 混型同样被拒绝（ValueError）", raised is not None,
          str(raised)[:70] if raised else "未报错")


# ── ③ 整列无矩阵 / 单帧 / 全空行 ───────────────────────────────
def test_degenerate(tmp: str):
    # 整段一帧都没有矩阵：列不存在（与旧行为一致）
    w, path = write_episode(os.path.join(tmp, "none"), [{}, {}, {}])
    check("整段无矩阵 → 不出现该列",
          COLUMN not in pq.read_schema(path).names,
          str(pq.read_schema(path).names))
    # 单帧
    _, path1 = write_episode(os.path.join(tmp, "single"),
                             Rows().row_dicts("int16", "ndarray")[:1])
    ref1 = legacy_column([Rows().encoded("int16", "list")[0]], pa.int16())
    check("单帧列与基准一致",
          read_column(path1).to_pylist() == ref1.to_pylist())
    # 全空行：列在（键出现过），每行都是空 list
    rowdicts = [{COLUMN: np.zeros(0, np.int16)} for _ in range(4)]
    _, pathe = write_episode(os.path.join(tmp, "allempty"), rowdicts)
    col = read_column(pathe)
    check("全空行 → 列存在且每行为空 list",
          COLUMN in pq.read_schema(pathe).names
          and col.to_pylist() == [[], [], [], []],
          str(col.to_pylist()))
    check("全空行无 null",
          col.null_count == 0)


# ── ④ 编码器契约：新函数返回 ndarray，旧函数仍返回 list ────────
def test_encoder_contract():
    m = contact_matrix(5.75)
    for spec in ("int16", "int16x10", "int16x1000", "float32"):
        arr = encode_gripper_force_matrix_array(m, spec)
        lst = encode_gripper_force_matrix(m, spec)
        contiguous = isinstance(arr, np.ndarray) and bool(arr.flags["C_CONTIGUOUS"])
        check(f"{spec}: 新函数返回 ndarray 且连续",
              isinstance(arr, np.ndarray) and contiguous,
              f"{type(arr).__name__} contiguous={contiguous}")
        check(f"{spec}: 新函数 dtype 家族正确",
              arr.dtype == (np.float32 if spec == "float32" else np.int16),
              str(arr.dtype))
        check(f"{spec}: 旧函数仍返回 list（历史调用方契约）",
              isinstance(lst, list), type(lst).__name__)
        check(f"{spec}: 两函数逐位同值",
              arr.tolist() == lst)
    # 返回的必须是**自己的副本**：矩阵来自 SDK 的单槽缓冲，若交出去的是
    # 视图，录制中每帧都会被下一帧改写 ⇒ 落盘全是最后一帧（静默损坏）
    src = contact_matrix(7.0)
    arr = encode_gripper_force_matrix_array(src, "float32")
    before = arr.copy()
    src[:] = -1.0
    check("新函数返回值不被源缓冲改写（是副本不是视图）",
          np.array_equal(arr, before))


# ── ⑤ 停顿回归（软断言）：ndarray 入参不得再逐值装箱 ────────────
def max_stall(fn) -> float:
    """跑 fn 的同时用心跳线程量「最长多久拿不到 GIL」（= 接收线程最长
    进不去 recv() 的时间）。每 0.2ms 采一次，自己不抢 GIL。"""
    stop = False
    worst = [0.0]

    def beat():
        last = time.perf_counter()
        while not stop:
            time.sleep(0.0002)
            now = time.perf_counter()
            worst[0] = max(worst[0], now - last)
            last = now

    th = threading.Thread(target=beat, daemon=True)
    th.start()
    t0 = time.perf_counter()
    fn()
    dt = time.perf_counter() - t0
    stop = True
    th.join()
    return dt, worst[0]


def test_no_boxing(tmp: str):
    rows, n = 30, 185000          # 30×185000=5.55M 元素，够看出量级
    rng = np.random.default_rng(7)
    arrs = [rng.integers(-4000, 4000, n, dtype=np.int16) for _ in range(rows)]
    lists = [a.tolist() for a in arrs]
    gc.disable()
    try:
        dt_new, stall_new = max_stall(
            lambda: legacy_column(arrs, pa.int16()))
        dt_old, stall_old = max_stall(
            lambda: legacy_column(lists, pa.int16()))
    finally:
        gc.enable()
    print(f"      （同一列 {rows}×{n}：ndarray {dt_new*1000:.0f}ms/"
          f"停顿 {stall_new*1000:.0f}ms；list {dt_old*1000:.0f}ms/"
          f"停顿 {stall_old*1000:.0f}ms）")
    check("软断言：ndarray 建列比 list 快 ≥4×（实测约 40×）",
          dt_new * 4 < dt_old, f"{dt_new*1000:.0f}ms vs {dt_old*1000:.0f}ms")
    check("软断言：ndarray 建列最长停顿 <100ms（服务端队列≈530ms 的量级）",
          stall_new < 0.1, f"{stall_new*1000:.0f}ms")
    del lists, arrs


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="force_matrix_contract_")
    try:
        print("[1] 两档 × list/ndarray/混用：schema 与值")
        test_schema_and_values(tmp)
        print("[2] 定型判定（numpy 标量 / 空数组 / 混型）")
        test_dtype_classification(tmp)
        print("[3] 退化形状（无列 / 单帧 / 全空行）")
        test_degenerate(tmp)
        print("[4] 编码器契约（返回类型、副本语义）")
        test_encoder_contract()
        print("[5] 停顿回归（软断言）")
        test_no_boxing(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项")
        for f in _FAILS:
            print("   -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
