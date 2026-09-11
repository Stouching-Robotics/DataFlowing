#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按时间戳把力矩阵/触觉力对齐到 RGB 视频帧。

    venv/bin/python scripts/align_modalities.py <episode.parquet>            # 看统计
    venv/bin/python scripts/align_modalities.py <episode.parquet> --residual # 逐帧残差

也可以当库用：

    from align_modalities import align_episode
    mapping, residual_ns = align_episode("episode-076.parquet", side="left")

`mapping[k]` = 视频第 k 帧应对应的 **parquet 行号**（该行的力矩阵即 k 帧
时刻的力）；`residual_ns[k]` = 这次配对的残差（纳秒）。

为什么需要它
────────────
同一行里的 RGB 帧和力样本**不是同时刻采集的**，两者各走各的路：

  · RGB 走外部帧源队列，写线程每 tick 取**队头最旧帧**（不排空）；
  · 力/矩阵走 latest-wins 单槽，写线程取到的是**当下最新**样本。

于是「行号相同」≠「时刻相同」：力恒领先 RGB，领先量等于那张 RGB 帧在
队列里的滞留时间，而滞留时间随录制时长增长（RGB 源实测 30.84fps、写线程
30fps，队列约每分钟涨 1 帧/秒 → 10 秒的段落末尾能差到 ~10 帧）。段首还
可能因为启动窗口的陈旧帧积压再叠一次跳变。

v1.3.3 起每行同时落盘两条支路各自的采集时刻（宿主单调钟纳秒，同一时基）：

    hardware_ns                              该行 RGB 帧的采集时刻
    observation.gripper_{side}_force_ns      3 向量力样本的采集时刻
    observation.gripper_{side}_force_matrix_ns  力矩阵样本的采集时刻

有了它们，行号偏移就只是「可离线算出来的量」，按时间取最近邻即可还原
真实配对。

对齐精度的上界是**半个力样本间隔**，不是半个行周期。力样本并不保证每行
都有：`force_ns == 0` 表示「本行窗口内没有新样本」（不是时刻为 0），若某段
是每 k 行才落一个样本，上界就放宽到 k/2 个行周期。20 秒内没收到任何样本
时缺口更大，脚本会把中位样本间隔直接打出来，方便判断这段能不能用。

两头的帧（早于第一个力样本的段首陈旧帧、晚于最后一个样本的帧）在时间上
根本没有对应样本，只能夹到端点——这不是对齐结果，脚本单独报「越界帧」
数量，下游应当丢弃。

**2026-09-11 之前录的数据无法这样对齐**：那时力侧完全没有时间戳，RGB 侧
的 hardware_ns 还被 pyqtSignal(int) 截成了 32 位（每 2.147s 翻符号），
两条信息都不在，只能重录。本脚本对这类 episode 会明确报「无法对齐」，
不会拿行号或增长率去猜——那种近似在段首有大跳变的段上误差比不补还大。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 行周期（秒）：残差折算成「帧」时的分母，也是对齐精度的理论下界
DEFAULT_FPS = 30.0


def _columns_for(side: str, which: str):
    key = ("gripper_{}_force_matrix_ns" if which == "matrix"
           else "gripper_{}_force_ns").format(side)
    return f"observation.{key}"


def nearest_index(src_ns, dst_ns) -> np.ndarray:
    """对 dst_ns 的每个元素，返回它在 src_ns 中**时间最近**的下标。

    两侧都必须是已排序的一维整型序列（时间戳天然有序）。用 searchsorted
    做二分，O(n log m)；不用线性扫描是因为一段 episode 有几百到几千行，
    而调用方可能要对每一帧各查一次。

    平局（左右等距）取左边（更早的样本）——对因果性友好：宁可配稍早的
    力，也不要配到视频帧之后才发生的力。
    """
    src = np.asarray(src_ns, dtype=np.int64)
    dst = np.asarray(dst_ns, dtype=np.int64)
    if src.size == 0:
        raise ValueError("src_ns 为空，无法对齐")
    if src.size == 1:
        return np.zeros(dst.shape, dtype=np.int64)
    # 夹到 [1, n-1] 后 src[i-1]/src[i] 恒存在，边界情形自然落回端点
    i = np.clip(np.searchsorted(src, dst), 1, src.size - 1)
    left = src[i - 1]
    right = src[i]
    return np.where(dst - left <= right - dst, i - 1, i)


def load_ns_columns(path: str, side: str = "left", which: str = "matrix"):
    """读出一段 episode 的对齐所需时间戳列。

    返回 (rgb_ns, force_ns, diag)：
      rgb_ns   —— 每行的 RGB 采集时刻（hardware_ns 列）
      force_ns —— 每行的力样本采集时刻（0 = 该行无样本/未知）
      diag     —— {"reason": ...} 说明为什么不可对齐时 force_ns 为 None
    """
    import pyarrow.parquet as pq

    schema = pq.read_schema(path)
    names = set(schema.names)
    diag = {"path": path, "side": side, "which": which}

    if "hardware_ns" not in names:
        diag["reason"] = "缺少 hardware_ns 列"
        return None, None, diag

    force_col = _columns_for(side, which)
    if force_col not in names:
        diag["reason"] = (
            f"缺少 {force_col} 列——该 episode 录于 v1.3.3 之前，力侧没有"
            f"采集时刻，无法按时间对齐（只能重录）")
        return None, None, diag

    t = pq.read_table(path, columns=["hardware_ns", force_col])
    rgb_ns = np.asarray(t.column("hardware_ns").to_pylist(), dtype=np.int64)
    force_ns = np.asarray(t.column(force_col).to_pylist(), dtype=np.int64)

    # 32 位截断的指纹：值域被压在 ±2^31 内。修好后 hardware_ns 是真实的
    # 宿主单调钟（开机纳秒数，量级 1e14~1e16），必然远超 2^31。
    if rgb_ns.size and np.abs(rgb_ns).max() < (1 << 31):
        diag["reason"] = (
            "hardware_ns 全在 ±2^31 内——这是 pyqtSignal(int) 的 32 位截断"
            "指纹（每 2.147s 翻符号），该 episode 录于修复之前，时间戳不可用")
        return None, None, diag

    if not np.all(np.diff(rgb_ns) >= 0):
        diag["reason"] = "hardware_ns 非单调，无法作为对齐基准"
        return None, None, diag

    return rgb_ns, force_ns, diag


def align_episode(path: str, side: str = "left", which: str = "matrix"):
    """把一段 episode 的 RGB 帧对齐到力样本。

    返回 (mapping, residual_ns)：
      mapping[k]     —— 视频第 k 帧对应的 parquet 行号
      residual_ns[k] —— 该配对的 |ΔT|（纳秒）

    只在**有真实力样本**的行里找最近邻（force_ns == 0 的行是「本行没有
    新样本」，不代表时刻为 0，必须排除，否则会把一堆帧吸到 0 上去）。
    """
    rgb_ns, force_ns, diag = load_ns_columns(path, side=side, which=which)
    if rgb_ns is None:
        raise ValueError(diag["reason"])

    valid = np.flatnonzero(force_ns > 0)
    if valid.size == 0:
        raise ValueError(f"{diag.get('path')} 没有任何有效的力采集时刻")

    picked = valid[nearest_index(force_ns[valid], rgb_ns)]
    return picked, np.abs(rgb_ns - force_ns[picked]).astype(np.int64)


def _fmt_stats(mapping, residual_ns, rgb_ns, force_ns, fps):
    res_ms = residual_ns / 1e6
    res_frames = residual_ns / 1e9 * fps
    # 行号偏移：不加时间戳时下游会默认的配对，用来对比"修了多少"
    naive = np.arange(mapping.size, dtype=np.int64)
    row_shift = mapping - naive

    # 越界帧：RGB 时刻落在力样本时间范围之外，只能夹到最近端点，残差是
    # "离端点多远"而非对齐误差。段首的陈旧帧（录到开机前的画面）就落在
    # 这里——它们本来就没有对应的力样本，下游应当丢弃而不是当对齐结果用。
    valid = force_ns[force_ns > 0]
    outside = ((rgb_ns < valid.min()) | (rgb_ns > valid.max())) \
        if valid.size else np.ones(rgb_ns.shape, dtype=bool)
    inside = ~outside

    # 对齐精度的上界由**力样本间隔**决定，不是行周期：样本每 k 行才有一个
    # 时上界就放宽到 k/2 帧。
    if valid.size > 1:
        span_ns = float(np.median(np.diff(np.sort(valid))))
    else:
        span_ns = 1e9 / fps
    bound_ns = span_ns / 2

    out = [
        f"  可对齐帧数      {mapping.size}",
        f"  有效力样本行数  {int((force_ns > 0).sum())}",
        f"  力样本间隔      中位 {span_ns / 1e6:.2f} ms"
        f"  → 对齐精度上界 {bound_ns / 1e9 * fps:.2f} 帧",
    ]
    if inside.any():
        out.append(
            f"  残差 |ΔT|       中位 {np.median(res_ms[inside]):.2f} ms"
            f" / 最大 {res_ms[inside].max():.2f} ms"
            f"  ← 只在范围内的 {int(inside.sum())} 帧上统计")
    if outside.any():
        idx = np.flatnonzero(outside)
        out.append(
            f"  越界帧          {int(outside.sum())} 帧"
            f"（{idx.min()}..{idx.max()}）—— 早于第一个或晚于最后一个"
            f"力样本，只能夹到端点，**不是对齐结果**，建议丢弃")
    out.append(
        f"  行号偏移        中位 {np.median(row_shift):+.0f}"
        f" / 范围 {row_shift.min():+d}..{row_shift.max():+d}"
        f"  ← 不按时间戳、直接同行配对的误差")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="按时间戳把力矩阵/触觉力对齐到 RGB 视频帧")
    ap.add_argument("episode", help="episode-NNN.parquet 路径")
    ap.add_argument("--side", default="left", choices=("left", "right"))
    ap.add_argument("--which", default="matrix", choices=("matrix", "force"),
                    help="matrix=力矩阵（默认），force=3 向量力")
    ap.add_argument("--residual", action="store_true",
                    help="打印逐帧残差（最多 40 行）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.episode):
        print(f"找不到文件：{args.episode}", file=sys.stderr)
        return 2

    rgb_ns, force_ns, diag = load_ns_columns(
        args.episode, side=args.side, which=args.which)
    if rgb_ns is None:
        print(f"无法对齐：{diag['reason']}", file=sys.stderr)
        return 1

    fps = DEFAULT_FPS
    try:
        import pyarrow.parquet as pq
        info = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(args.episode)))), "meta", "info.json")
        if os.path.isfile(info):
            with open(info, "r", encoding="utf-8") as f:
                fps = float(json.load(f).get("fps") or DEFAULT_FPS)
    except (OSError, ValueError, TypeError):
        pass

    try:
        mapping, residual_ns = align_episode(
            args.episode, side=args.side, which=args.which)
    except ValueError as exc:
        print(f"无法对齐：{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "mapping": mapping.tolist(),
            "residual_ns": residual_ns.tolist(),
        }))
        return 0

    print(f"{args.episode}\n  侧别={args.side}  模态={args.which}")
    print(_fmt_stats(mapping, residual_ns, rgb_ns, force_ns, fps))
    if args.residual:
        print("\n  帧号  对应行  残差(ms)")
        for k in range(min(40, mapping.size)):
            print(f"  {k:>4}  {mapping[k]:>6}  {residual_ns[k] / 1e6:>8.2f}")
        if mapping.size > 40:
            print(f"  ...（其余 {mapping.size - 40} 帧略）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
