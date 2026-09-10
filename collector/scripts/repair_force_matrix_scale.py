#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补写历史 episode 的力矩阵倍率（meta/episodes 的 force_matrix_specs 列）。

    venv/bin/python scripts/repair_force_matrix_scale.py <任务目录>            # 只看
    venv/bin/python scripts/repair_force_matrix_scale.py <任务目录> --apply    # 写入

为什么要修
──────────
力矩阵落盘规格 int16×10/×100/×1000 的列类型都是 list<int16>、元素也都是
int，**倍率从数据上完全分辨不出**，只能靠随段记录的 scale 还原。倍率原先
只写在任务级 meta/info.json 的 features[列].scale 里，而它「值以最新
episode 为准」——同一个任务里换过档位，早先几段的 scale 就被后一段覆盖掉
（实测 ×10 / ×100 / ×1000 三段连录、再录一段 float32 后，三段全变成 1，
回放时数值分别放大 10 / 100 / 1000 倍）。本脚本把推断出的倍率补写进
**每段一份**的 meta/episodes/chunk-NNN/episode-NNN.parquet。

怎么推断
────────
同一帧的力矩阵 fz 平面逐点求和，等于 SDK 的 fz 总量（float32 档实测
比值 1.000）。定标档存的是 ×N 后的值，所以

    倍率 = median( Σ(解码后的 fz 平面，按 scale=1 解) / force 列 fz )

×10 档实测 10.003、×100 档 99.998、×1000 档 1000.0007 —— 精确到 0.1%。
倍率必须落在已知档位 {1,10,100,1000} 的 ±3% 内才认（两两相差 ≥10 倍，
这个判据很宽松）；否则报「无法判定」并跳过，绝不猜着写。

**×1 只在实测比值 ≤ UNSCALED_MAX 时才判**（1.15，见下），中间地带一律
无法判定：定标档的量化误差是每点 1/(2N) mN，接触点少、力又小的帧上这个
误差占总量不可忽略 —— 一段真 ×10 的录制理论上能测到 9 出头，此时若把
「不到 9.7 就算未定标」当规则，就会把它判成 ×1、数值放小 10 倍。判不出
比判错好：判不出只是留着回退 info.json，判错是静默 10 倍偏差。

判 ×1 的依据是**逐点向零截断只会让和变小**（同号为主）：未定标档实测老
数据只剩真值的 4%~55%，即比值 0.04~0.55，离 1.15 还有个数量级的富余；
真值低到截断后归零时和也接近 0，同样落在这一侧。这条也顺带把老 episode
写成自带倍率，免得将来同任务里录了 ×1000 之后，它们回退到任务级
info.json 被按 ×1000 还原（数值缩 1000 倍）。
仅对**同任务里出现过多档**的旧数据需要跑这个脚本；单档跑到底的任务里
info.json 本来就是对的，读取端回退到它即可。

改什么
──────
只改 meta/episodes 那一行（单行 parquet，重写整个文件但内容逐列保留），
**不碰 data/ 里的矩阵数据一个字节**。
"""

import argparse
import json
import os
import sys

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.egodata_writer import (            # noqa: E402
    _EPISODE_SCHEMA, _atomic_write_parquet, _episode_rows_table,
    _read_episode_rows)
from core.helpers import (                   # noqa: E402
    pooled_data_parquet_path, pooled_episodes_path)

TIERS = (1, 10, 100, 1000)
TIER_TOLERANCE = 0.03            # 实测倍率与标称档位的相对误差上限
MIN_FRAMES = 5                   # 参与统计的最少有效帧
UNSCALED_MAX = 1.15              # 判 ×1 的实测比值上限（截断只会让和变小，
                                 # 超过它就不是截断能解释的了；1~10 之间
                                 # 判不出，见文件头「怎么推断」）
MATRIX_ROW_LEN = 750             # 250 列 × 3 通道
_MATRIX_SUFFIX = "_force_matrix"


def _decode_int16_ref(enc: np.ndarray) -> np.ndarray:
    """按 scale=1 解出 (250,250,3) —— 与 demo 的整数分支同一口径。"""
    rows = np.cumsum(
        enc.reshape(-1, MATRIX_ROW_LEN).astype(np.int32), axis=1)
    return rows.astype(np.int16).reshape(250, 250, 3)


def _list_column_to_numpy(column):
    """list<数值> 列 → (offsets, values)；走 pyarrow 缓冲区，避开 pylist。"""
    dtype = (np.float32 if pa.types.is_floating(column.type.value_type)
             else np.int16)
    offsets, values, base = [], [], 0
    for chunk in column.chunks:
        offs = np.asarray(chunk.offsets, np.int64)
        vals = np.asarray(chunk.values, dtype=dtype)
        offsets.append(offs[:-1] + base)
        values.append(vals)
        base += int(vals.size)
    if not offsets:
        return np.zeros(1, np.int64), np.zeros(0, dtype)
    return (np.concatenate(offsets + [np.array([base], np.int64)]),
            np.concatenate(values))


def infer_scale(data_path: str, column: str, force_column: str):
    """实测该列的倍率 → (倍率, 证据串)；判不出返回 (None, 原因)。

    倍率来自「矩阵平面和 ÷ 同帧 force 列 fz」的中位比，必须与某个已知
    档位相差 ≤3% 才认。force 列缺失/全零/有效帧不足都判不出。
    """
    schema = pq.read_schema(data_path)
    if force_column not in schema.names:
        return None, "无 force 列可比对"
    table = pq.read_table(data_path, columns=[column, force_column])
    offsets, values = _list_column_to_numpy(table.column(column))
    force = np.asarray(table.column(force_column).to_pylist(), np.float32)
    n = table.num_rows
    is_float = values.dtype == np.float32

    # 稀疏列：表行号就是帧号，第 row 行有样本时其样本即该帧的矩阵。
    # 只遍历有样本的行（空行的 offsets 首尾相等，不能拿行号当样本序号）。
    ratios = []
    for row in np.flatnonzero(np.diff(offsets) > 0):
        row = int(row)
        if row >= n:
            break
        enc = values[int(offsets[row]):int(offsets[row + 1])]
        if enc.size != 250 * MATRIX_ROW_LEN:
            continue                      # 损坏行
        plane = (enc.reshape(250, 250, 3) if is_float
                 else _decode_int16_ref(enc))
        total = float(plane[:, :, 2].astype(np.float64).sum())
        fz = float(force[row][2]) if force.ndim == 2 else float(force[row])
        if abs(fz) < 50.0:                # 近零帧比不出倍率（分母噪声）
            continue
        ratios.append(total / fz)
        if len(ratios) >= 200:
            break
    if len(ratios) < MIN_FRAMES:
        return None, f"有效帧不足（{len(ratios)}）"
    median = float(np.median(ratios))
    counts = f"{len(ratios)} 帧"
    tier = min(TIERS, key=lambda t: abs(t - median))
    rel = abs(median - tier) / tier
    if rel <= TIER_TOLERANCE:
        return tier, f"实测 {median:.4f} → ×{tier}（{counts}，偏差 {rel:.2%}）"
    if median <= UNSCALED_MAX:
        return 1, (f"实测 {median:.4f} ≤ {UNSCALED_MAX} → ×1（{counts}；"
                   f"向零截断只会让和变小，偏小只能来自未定标档）")
    return None, (f"实测 {median:.4f} 无处可归（{counts}）：既不在档位 "
                  f"±{TIER_TOLERANCE:.0%} 内，又比 ×1 的上限 "
                  f"{UNSCALED_MAX} 高 —— 小接触/小力的定标档量化噪声就能"
                  f"压到这个区间，猜不得")


def plan_task(task_dir: str):
    """扫任务下所有 episode，列出「该补倍率」的那些 → [(stem, 索引, 提议)]。"""
    episodes_dir = os.path.join(task_dir, "meta", "episodes")
    found = []
    for root, _dirs, files in os.walk(episodes_dir):
        for name in sorted(files):
            if not (name.startswith("episode-") and name.endswith(".parquet")):
                continue
            meta_path = os.path.join(root, name)
            rows = _read_episode_rows(meta_path)
            rows = [r for r in rows if r.get("episode_index") is not None]
            if len(rows) != 1:
                print(f"  [{name}] 元数据行数 {len(rows)} ≠ 1，跳过")
                continue
            row = rows[0]
            try:
                existing = json.loads(row.get("force_matrix_specs") or "{}")
            except (TypeError, ValueError):
                existing = {}
            index = int(row["episode_index"])
            data_path = pooled_data_parquet_path(task_dir, index)
            if not os.path.isfile(data_path):
                print(f"  [{name}] 找不到 {data_path}，跳过")
                continue
            schema = pq.read_schema(data_path)
            cols = [c for c in schema.names
                    if c.endswith(_MATRIX_SUFFIX)]
            if not cols:
                continue                       # 本段没有力矩阵列，无需倍率
            missing = [c for c in cols
                       if not isinstance(existing.get(c), dict)
                       or not existing[c].get("scale")]
            if not missing:
                continue                       # 已有权威记录，不动
            found.append((name, index, data_path, cols, existing))
    return found


def build_specs(data_path: str, cols, existing: dict):
    """逐列推断 → (规格 dict, 判不出的列说明)。

    每列的证据各自独立，一列判不出只跳过那一列（读取端对它是回退 info.json），
    不牵连同段其余列。已有权威值的列不重推——录制端写下的倍率是事实，推断
    只是补救手段。
    """
    specs = dict(existing)
    failed = []
    schema = pq.read_schema(data_path)
    for column in cols:
        key = column.split("observation.", 1)[-1]
        if isinstance(specs.get(column), dict) and specs[column].get("scale"):
            print(f"      {key}: 已有权威值 {specs[column]}，保持不动")
            continue
        force_column = f"observation.{key[:-len('_force_matrix')]}_force"
        scale, evidence = infer_scale(data_path, column, force_column)
        if scale is None:
            print(f"      {key}: {evidence} → 跳过该列")
            failed.append(f"{key}: {evidence}")
            continue
        float32 = pa.types.is_floating(
            schema.field(column).type.value_type)
        specs[column] = {
            "dtype": "float32" if float32 else "int16",
            "shape": [250, 250, 3],
            "encoding": "row_flat_raw" if float32 else "row_diff_quantized",
            "scale": int(scale),
        }
        print(f"      {key}: {evidence}")
    return specs, failed


def repair(task_dir: str, apply: bool) -> int:
    print(f"任务目录: {task_dir}")
    print(f"模式: {'写入' if apply else '只读预览（加 --apply 才写）'}")
    candidates = plan_task(task_dir)
    if not candidates:
        print("\n没有需要补写倍率的 episode。")
        return 0
    print(f"\n发现 {len(candidates)} 段缺倍率记录：")
    pending = []
    skipped = []
    for name, index, data_path, cols, existing in candidates:
        print(f"\n  [{name}] episode_index={index}，{len(cols)} 个力矩阵列")
        specs, failed = build_specs(data_path, cols, existing)
        if specs == existing:      # 补不进去（列都判不出，或都已记录）
            skipped.append(f"{name}（{'、'.join(failed) if failed else '已记录'}）")
            continue
        if failed:
            skipped.append(f"{name}（{len(failed)} 列判不出：{failed[0]}）")
        pending.append((name, index, specs))

    if skipped:
        print("\n以下段落这次没（全部）写进去（读取端回退 info.json）：")
        for line in skipped:
            print(f"  - {line}")
    if not pending:
        print("\n没有新判出的倍率，未做任何改动。")
        return 0
    if not apply:
        print(f"\n预览完毕：{len(pending)} 段可补写。确认无误后加 --apply。")
        return 0

    print()
    for name, index, specs in pending:
        path = pooled_episodes_path(task_dir, index)
        rows = _read_episode_rows(path)
        rows = [r for r in rows if r.get("episode_index") == index]
        if len(rows) != 1:
            print(f"  [{name}] 写入前复查：行数 {len(rows)} ≠ 1，跳过")
            continue
        row = dict(rows[0])
        # 只补缺失的列，已有的权威值不覆盖
        merged = json.loads(row.get("force_matrix_specs") or "{}")
        merged.update(specs)
        row["force_matrix_specs"] = json.dumps(merged, ensure_ascii=False)
        table = _episode_rows_table([row])
        if table.schema != _EPISODE_SCHEMA:     # 防御：schema 必须一致
            print(f"  [{name}] schema 不符，跳过")
            continue
        _atomic_write_parquet(table, path)
        print(f"  [{name}] 已补写 {len(specs)} 列 → {os.path.relpath(path, REPO_ROOT)}")
    print("\n完成。重新打开 demo 即可按真实倍率还原。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="补写历史 episode 的力矩阵倍率（meta/episodes 行）")
    parser.add_argument("task_dir", help="任务目录（含 data/ 与 meta/ 的那层）")
    parser.add_argument("--apply", action="store_true",
                        help="真正写入；不给则只预览")
    args = parser.parse_args()
    if not os.path.isdir(os.path.join(args.task_dir, "meta")):
        print(f"不是任务目录（缺 meta/）: {args.task_dir}")
        return 2
    return repair(os.path.abspath(args.task_dir), args.apply)


if __name__ == "__main__":
    sys.exit(main())
