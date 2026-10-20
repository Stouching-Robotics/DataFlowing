#!/usr/bin/env python3
"""把 hardware_ns 重建成「均匀 + 严格递增」——保留全部帧，只改时间戳数值。

背景（9_21_Test 真机复核）
--------------------------
`hardware_ns` 取自帧组的深度成员，而 rs2 syncer 会给「本拍没出新帧」的那一路
补上一帧旧帧 → 相邻两行出现**同一个 hardware_ns**（Δ=0），紧接着一个 2T 的
双倍间隔；另有少量孤立的 2T。实测 7149 行里 Δ=0 共 18 次、Δ=2T 共 25 次。
而宿主侧到达节奏全程正常（Δwall ≈ 33ms）、行数 = 视频帧数 = 容器帧数 ——
**一帧都没丢，坏的只是时间戳数值**。

本工具就只做这件事：行数与视频一帧不动，把 hardware_ns 重建成均匀严格递增。

两种口径
--------
uniform（默认）  每一步都 = 帧周期 T（= 该段干净间隔的众数，实测恒 33344 µs）
                 → 严格等差，与视频 PTS(i/30) 只差设备钟 0.032% 的速率
interp           只把异常间隔（Δ≤0 或 Δ>1.5T）按 T 重建，其余保留设备原值
                 → 严格递增、无 0/2T，但保留设备钟 33343/33344 的 1µs 抖动

用法
----
    venv/bin/python tools/fix_hardware_ns_uniform.py <数据集目录>            # 预演
    venv/bin/python tools/fix_hardware_ns_uniform.py <数据集目录> --apply    # 落地(带备份)
    选项: --mode interp | --no-backup | --json
"""
import argparse
import glob
import json
import os
import shutil
import sys

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

DEF_ANOM_LO = 0.5      # Δ < 0.5T 视为异常
DEF_ANOM_HI = 1.5      # Δ > 1.5T 视为异常


def pick_T(d):
    """帧周期：**干净间隔的均值**（整数 ns，保证严格等差）。

    用均值而不是众数/中位：均匀网格要求 n×T ≈ 真实跨度，只有均值满足。
    宿主钟源（UMI 夹爪）的到达间隔是偏斜双峰（30~33ms 与 36~38ms，
    均值 33.337ms、中位 32.1ms）—— 中位会把速率算低 3.7%。
    设备钟源（D435）间隔锁死在 33343/33344 µs，均值与众数只差 2ppm。
    """
    med = float(np.median(d))
    good = d[(d > med * 0.9) & (d < med * 1.1)]
    if good.size == 0:
        return int(round(med))
    return int(round(float(good.mean())))


def rebuild(hw, mode):
    """返回 (新序列, T, 改动行数)。"""
    if hw.size < 2:
        return hw.copy(), 0, 0
    d = np.diff(hw)
    T = pick_T(d)
    if mode == "uniform":
        fixed = hw[0] + np.arange(hw.size, dtype=np.int64) * T
    else:                                   # interp
        bad = (d <= 0) | (d > T * DEF_ANOM_HI) | (d < T * DEF_ANOM_LO)
        d2 = np.where(bad, T, d)
        fixed = np.concatenate(([hw[0]], hw[0] + np.cumsum(d2)))
    changed = int((fixed != hw).sum())
    return fixed.astype(np.int64), T, changed


def stats(hw):
    d = np.diff(hw)
    return {
        "n": int(hw.size),
        "zero": int((d == 0).sum()),
        "back": int((d < 0).sum()),
        "big": int((d > np.median(d) * 1.5).sum()),
        "span": float((hw[-1] - hw[0]) / 1e9),
        "steps": int(len(np.unique(d))),
        "dmin": int(d.min()) if d.size else 0,
        "dmax": int(d.max()) if d.size else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--mode", choices=("uniform", "interp"), default="uniform")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.root, "data/chunk-*", "*.parquet")))
    if not files:
        print("没找到 data/chunk-*/ *.parquet")
        return 1

    bak_dir = os.path.join(args.root, "_hardware_ns_backup")
    report = []
    print("模式=%s  %s\n" % (args.mode, "落地" if args.apply else "预演（不改文件）"))
    print("%-6s %5s %6s %6s %6s %10s %10s %9s %9s %7s %11s" %
          ("ep", "行数", "Δ=0前", "Δ=0后", "2T前", "span前(s)", "span后(s)",
           "step后min", "step后max", "改动行", "max|位移|"))
    for path in files:
        ep = os.path.basename(path)[8:11]
        t = pq.read_table(path)
        hw = np.array(t.column("hardware_ns"), dtype=np.int64)
        before = stats(hw)
        fixed, T, changed = rebuild(hw, args.mode)
        after = stats(fixed)
        shift = int(np.abs(fixed - hw).max()) if hw.size else 0
        report.append({"ep": ep, "T_ns": T, "before": before, "after": after,
                       "changed": changed, "max_shift_ns": shift})
        print("%-6s %5d %6d %6d %6d %10.4f %10.4f %9d %9d %7d %10.3fms" %
              (ep, before["n"], before["zero"], after["zero"], before["big"],
               before["span"], after["span"], after["dmin"], after["dmax"],
               changed, shift / 1e6))

        if args.apply:
            if not args.no_backup:
                os.makedirs(bak_dir, exist_ok=True)
                dst = os.path.join(bak_dir, os.path.basename(path))
                if not os.path.exists(dst):
                    shutil.copy2(path, dst)
            idx = t.schema.get_field_index("hardware_ns")
            new_t = t.set_column(idx, "hardware_ns",
                                 pa.array(fixed, type=pa.int64()))
            pq.write_table(new_t, path, compression="zstd")

    tot_b = sum(r["before"]["n"] for r in report)
    tot_z = sum(r["before"]["zero"] for r in report)
    tot_z2 = sum(r["after"]["zero"] for r in report)
    tot_big = sum(r["before"]["big"] for r in report)
    tot_big2 = sum(r["after"]["big"] for r in report)
    print("\n合计：%d 段 / %d 行" % (len(report), tot_b))
    print("  Δ=0  : %d → %d" % (tot_z, tot_z2))
    print("  Δ>1.5T: %d → %d" % (tot_big, tot_big2))
    print("  严格递增: %s" % all(r["after"]["back"] == 0 and r["after"]["zero"] == 0
                                 for r in report))
    print("  step 取值数: %s" % sorted(set(r["after"]["steps"] for r in report)))
    if not args.apply:
        print("\n（预演结束，未改任何文件；确认后加 --apply）")
    else:
        if not args.no_backup:
            print("\n原文件已备份到 %s" % bak_dir)
        print("已改写 %d 个 parquet；视频/元数据未触碰。" % len(report))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
