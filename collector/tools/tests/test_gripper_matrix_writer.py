#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""力矩阵落盘规格（int16 / float32）在 writer 侧的契约自检：

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_gripper_matrix_writer.py

覆盖:
  1. 元素类型定型：int 列表 → int16 行差分；float 列表 → float32 原值
  2. parquet 列类型随规格（list<int16> / list<float32>），且能按规格反解
  3. info.json features 的 dtype / encoding / scale 与规格一致
  4. 录制中改规格 → 显式报错（不让 pyarrow 抛难懂的转换异常）
  5. 空样本帧不定型（首帧为空也能在后续样本上定型）
  6. 定标档（×10/×100/×1000）：列类型仍是 int16、倍率靠 features.scale，
     未登记时回退 1（历史 episode 就是未定标的，行为必须不变）
退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from core.egodata_writer import (                      # noqa: E402
    EgoDataWriter, _EPISODE_SCHEMA, _read_episode_rows)
from core.helpers import pooled_episodes_path          # noqa: E402
from ui.main_window import encode_gripper_force_matrix  # noqa: E402

MATRIX_KEY = "gripper_left_force_matrix"
COLUMN = f"observation.{MATRIX_KEY}"

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def contact_matrix(value: float) -> np.ndarray:
    """250×250×3：中心 30×30 接触区，fz=value，fx=value/4。"""
    m = np.zeros((250, 250, 3), np.float32)
    m[105:135, 105:135, 2] = value
    m[105:135, 105:135, 0] = value * 0.25
    return m


def decode_int16(flat) -> np.ndarray:
    arr = np.asarray(flat, np.int16).reshape(-1, 750)
    return (np.cumsum(arr.astype(np.int32), axis=1).astype(np.int16)
            .reshape(250, 250, 3))


def build_writer(task_dir: str, matrix_lists, n_frames: int) -> EgoDataWriter:
    """喂 n_frames 帧（matrix_lists[i] 为第 i 帧的编码值，None=不写该帧）。"""
    writer = EgoDataWriter()
    writer._task_dir = task_dir
    writer._episode_index = 0
    for i in range(n_frames):
        gripper = {}
        if matrix_lists[i] is not None:
            gripper[MATRIX_KEY] = matrix_lists[i]
        writer.write_frame_row(i, i / 30.0, gripper=gripper)
    return writer


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="force_matrix_writer_")
    try:
        print("[1] int16 规格（元素为 int）")
        ref = contact_matrix(5.75)
        int_rows = [encode_gripper_force_matrix(ref)]
        int_rows += [encode_gripper_force_matrix(contact_matrix(3.0 + i))
                     for i in range(1, 6)]
        w = build_writer(os.path.join(tmp, "i16"), int_rows, 5)
        check("元素类型判定为 int16", w._matrix_dtype.get(MATRIX_KEY) == "int16",
              str(w._matrix_dtype))
        w._write_data_parquet()
        schema = pq.read_schema(
            os.path.join(tmp, "i16", "data", "chunk-000", "episode-000.parquet"))
        check("parquet 列类型 = list<int16>",
              schema.field(COLUMN).type == pa.list_(pa.int16()),
              str(schema.field(COLUMN).type))
        got = decode_int16(pq.read_table(
            os.path.join(tmp, "i16", "data", "chunk-000", "episode-000.parquet"),
            columns=[COLUMN]).column(COLUMN).to_pylist()[0])
        check("int16 列反解 = 截断后的矩阵",
              np.array_equal(got, ref.astype(np.int16)),
              f"中心 fz={int(got[120, 120, 2])}（期望 5，5.75 被截断）")
        w._write_info_json()
        feats = json.load(open(os.path.join(tmp, "i16", "meta", "info.json"),
                               encoding="utf-8"))["features"]
        check("features dtype=int16 / encoding=row_diff_quantized",
              feats[COLUMN]["dtype"] == "int16"
              and feats[COLUMN]["encoding"] == "row_diff_quantized",
              str(feats[COLUMN]))
        check("features shape=[250,250,3]",
              feats[COLUMN]["shape"] == [250, 250, 3],
              str(feats[COLUMN]["shape"]))

        check("int16 档 features.scale=1（未定标）",
              feats[COLUMN]["scale"] == 1, str(feats[COLUMN].get("scale")))

        print("[2] float32 规格（元素为 float）")
        f_rows = [encode_gripper_force_matrix(ref, "float32")]
        f_rows += [encode_gripper_force_matrix(contact_matrix(3.375 + i),
                                               "float32")
                   for i in range(1, 6)]
        w2 = build_writer(os.path.join(tmp, "f32"), f_rows, 5)
        check("元素类型判定为 float32",
              w2._matrix_dtype.get(MATRIX_KEY) == "float32",
              str(w2._matrix_dtype))
        w2._write_data_parquet()
        fpath = os.path.join(tmp, "f32", "data", "chunk-000", "episode-000.parquet")
        schema2 = pq.read_schema(fpath)
        check("parquet 列类型 = list<float32>",
              schema2.field(COLUMN).type == pa.list_(pa.float32()),
              str(schema2.field(COLUMN).type))
        back = np.asarray(
            pq.read_table(fpath, columns=[COLUMN]).column(COLUMN).to_pylist()[0],
            np.float32).reshape(250, 250, 3)
        check("float32 列往返逐位无损",
              np.array_equal(back, ref),
              f"中心 fz={float(back[120, 120, 2]):.4f}（期望 5.7500）")
        check("float32 亚毫牛小数位保留",
              abs(float(back[120, 120, 0]) - 1.4375) < 1e-6,
              f"中心 fx={float(back[120, 120, 0]):.4f}（期望 1.4375）")
        w2._write_info_json()
        feats2 = json.load(open(os.path.join(tmp, "f32", "meta", "info.json"),
                                encoding="utf-8"))["features"]
        check("features dtype=float32 / encoding=row_flat_raw",
              feats2[COLUMN]["dtype"] == "float32"
              and feats2[COLUMN]["encoding"] == "row_flat_raw",
              str(feats2[COLUMN]))
        check("float32 档 features.scale=1（无定标）",
              feats2[COLUMN]["scale"] == 1, str(feats2[COLUMN].get("scale")))

        print("[3] 录制中改规格 → 显式报错")
        w3 = build_writer(os.path.join(tmp, "mix"), [int_rows[0]], 1)
        raised = None
        try:
            w3.write_frame_row(1, 0.5, gripper={
                MATRIX_KEY: f_rows[0]})
        except ValueError as exc:
            raised = exc
        check("同一列混型被拒绝（ValueError）", raised is not None,
              str(raised)[:70] if raised else "未报错")
        check("报错信息点明列名与两种规格",
              raised is not None and MATRIX_KEY in str(raised)
              and "int16" in str(raised) and "float32" in str(raised))

        print("[4] 空样本不定型")
        w4 = EgoDataWriter()
        w4._task_dir = os.path.join(tmp, "empty")
        w4._episode_index = 0
        w4.write_frame_row(0, 0.0, gripper={MATRIX_KEY: []})
        check("空样本首帧不写规格", w4._matrix_dtype.get(MATRIX_KEY) is None,
              str(w4._matrix_dtype))
        w4.write_frame_row(1, 1 / 30.0,
                           gripper={MATRIX_KEY: f_rows[0]})
        check("后续样本到来时定型",
              w4._matrix_dtype.get(MATRIX_KEY) == "float32",
              str(w4._matrix_dtype))

        print("[5] 两种规格互不干扰（双夹爪各一侧不同规格）")
        w5 = EgoDataWriter()
        w5._task_dir = os.path.join(tmp, "dual")
        w5._episode_index = 0
        w5.write_frame_row(0, 0.0, gripper={
            "gripper_left_force_matrix": int_rows[0],
            "gripper_right_force_matrix":
                encode_gripper_force_matrix(contact_matrix(2.5), "float32"),
        })
        check("左右两侧各自定型",
              w5._matrix_dtype.get("gripper_left_force_matrix") == "int16"
              and w5._matrix_dtype.get("gripper_right_force_matrix") == "float32",
              str(w5._matrix_dtype))

        print("[6] 定标档 ×10/×100/×1000：列类型仍是 int16，倍率走 features.scale")
        for scale in (10, 100, 1000):
            spec = f"int16x{scale}"
            sdir = os.path.join(tmp, spec)
            w6 = EgoDataWriter()
            w6._task_dir = sdir
            w6._episode_index = 0
            ref6 = contact_matrix(5.75)
            rows6 = [encode_gripper_force_matrix(ref6, spec)]
            rows6 += [encode_gripper_force_matrix(contact_matrix(3.75 + i), spec)
                      for i in range(1, 4)]
            # 采集侧在录制开始时登记倍率（pipeline.set_force_matrix_spec →
            # writer.set_force_matrix_spec），writer 自己推不出来
            w6.set_force_matrix_spec(MATRIX_KEY, spec)
            for i, enc in enumerate(rows6):
                w6.write_frame_row(i, i / 30.0, gripper={MATRIX_KEY: enc})
            check(f"{spec} 落盘家族仍是 int16（倍率看不出）",
                  w6._matrix_dtype.get(MATRIX_KEY) == "int16",
                  str(w6._matrix_dtype))
            w6._write_data_parquet()
            spath = os.path.join(sdir, "data", "chunk-000", "episode-000.parquet")
            stype = pq.read_schema(spath).field(COLUMN).type
            check(f"{spec} parquet 列类型 = list<int16>",
                  stype == pa.list_(pa.int16()), str(stype))
            got6 = decode_int16(pq.read_table(
                spath, columns=[COLUMN]).column(COLUMN).to_pylist()[0]
            ).astype(np.float32) / np.float32(scale)
            # 期望值 = 编码端口径（rint 半偶入）÷scale，而不是「真值 ±半 LSB」：
            # 5.75 在 ×10 档正好落在半格上 → rint 到 58 → 5.8，误差恰为半个 LSB
            expect = float(np.rint(np.float32(5.75) * np.float32(scale))) / scale
            check(f"{spec} 解码÷{scale} 还原 5.75 mN（int16 档会丢成 5）",
                  abs(float(got6[120, 120, 2]) - expect) < 1e-4,
                  f"中心 fz={float(got6[120, 120, 2]):.4f}"
                  f"（期望 {expect:.4f}，真值 5.75）")
            w6._write_info_json()
            f6 = json.load(open(os.path.join(sdir, "meta", "info.json"),
                                encoding="utf-8"))["features"][COLUMN]
            check(f"{spec} features.scale={scale}",
                  f6["scale"] == scale, str(f6))
            check(f"{spec} features dtype 仍报 int16",
                  f6["dtype"] == "int16" and f6["encoding"] == "row_diff_quantized",
                  str(f6))

        print("[6b] 未登记倍率 → 回退 1（历史 episode 行为不变）")
        w7 = build_writer(os.path.join(tmp, "noscale"), int_rows[:2], 2)
        w7._write_data_parquet()
        w7._write_info_json()
        f7 = json.load(open(os.path.join(tmp, "noscale", "meta", "info.json"),
                            encoding="utf-8"))["features"][COLUMN]
        check("未登记 scale 默认 1", f7["scale"] == 1, str(f7))

        print("[7] meta/episodes 行带倍率：同任务换档，前一段不被后一段覆盖")
        # 复现现场：同一任务连录 ×10 与 float32 两段。info.json 是任务级、
        # 值以最新 episode 为准 → 第一段的倍率会被顶成 1；每段一行的
        # force_matrix_specs 必须各记各的。
        task = os.path.join(tmp, "pooled")
        # episode_index 是 1 起的全局序号（episode_chunk_file 里 -1 后取整），
        # 传 0 会和 1 撞到同一个文件上
        for index, spec in ((1, "int16x10"), (2, "float32")):
            w8 = EgoDataWriter()
            w8._task_dir = task
            w8._episode_index = index
            w8.set_force_matrix_spec(MATRIX_KEY, spec)
            w8.write_frame_row(0, 0.0, gripper={
                MATRIX_KEY: encode_gripper_force_matrix(contact_matrix(5.75),
                                                        spec)})
            w8._write_data_parquet()
            w8._append_episode_row()
            w8._write_info_json()          # 每次录制结束都重写 → 后者顶前者

        rows = {}
        for index in (1, 2):
            path = pooled_episodes_path(task, index)
            got = _read_episode_rows(path)
            check(f"第 {index} 段 meta 行数 = 1", len(got) == 1, str(len(got)))
            check(f"第 {index} 段 meta 行的 episode_index = {index}",
                  got and got[0]["episode_index"] == index,
                  str(got[0].get("episode_index")) if got else "无行")
            rows[index] = got[0]
        check("meta 行 schema 与 _EPISODE_SCHEMA 一致（列齐）",
              pq.read_schema(pooled_episodes_path(task, 1)) == _EPISODE_SCHEMA)

        specs0 = json.loads(rows[1]["force_matrix_specs"])
        specs1 = json.loads(rows[2]["force_matrix_specs"])
        check("第 1 段（×10）meta 行记 scale=10",
              specs0[COLUMN]["scale"] == 10, str(specs0.get(COLUMN)))
        check("第 2 段（float32）meta 行记 scale=1 且 dtype=float32",
              specs1[COLUMN]["scale"] == 1
              and specs1[COLUMN]["dtype"] == "float32"
              and specs1[COLUMN]["encoding"] == "row_flat_raw",
              str(specs1.get(COLUMN)))
        check("规格描述四项齐全（dtype/shape/encoding/scale）",
              set(specs0[COLUMN]) == {"dtype", "shape", "encoding", "scale"}
              and specs0[COLUMN]["shape"] == [250, 250, 3],
              str(specs0[COLUMN]))

        info_scale = json.load(open(os.path.join(task, "meta", "info.json"),
                                    encoding="utf-8"))["features"][COLUMN]["scale"]
        check("info.json 确实被后一段顶成 1（这就是必须另存每段的原因）",
              info_scale == 1,
              f"info.json scale={info_scale}（任务级、值以最新 episode 为准）")
        check("两段 meta 行没被互相顶掉（10 与 1 各自保留）",
              specs0[COLUMN]["scale"] == 10 and specs1[COLUMN]["scale"] == 1)

        # 追加第三段（不带力矩阵列）不得把前两段的规格弄丢
        w9 = EgoDataWriter()
        w9._task_dir = task
        w9._episode_index = 3
        w9.write_frame_row(0, 0.0, gripper={})
        w9._write_data_parquet()
        w9._append_episode_row()
        check("无夹爪列的段写空规格 {}（不是漏列）",
              _read_episode_rows(pooled_episodes_path(task, 3))[0][
                  "force_matrix_specs"] == "{}")
        again = _read_episode_rows(pooled_episodes_path(task, 1))
        check("追加新段后旧段 meta 行原样保留",
              len(again) == 1
              and json.loads(again[0]["force_matrix_specs"]) == specs0,
              str(again[0].get("force_matrix_specs")))
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
