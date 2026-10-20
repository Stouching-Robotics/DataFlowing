#!/usr/bin/env python3
"""按时间戳把「手套支路」重新分箱到视频帧行 —— 让行配对与时间戳重新自洽。

为什么需要
----------
`Data_examples_2` 的转换器是**按时间戳**把每个手套样本嵌进对应视频帧行的
（实测原始配对偏移 8.3~8.7 ms，全库高度一致）。

`tools/shift_glove_ts.py` 把手套 host 时间戳后移（+85/+115/+72 ms）以对齐视频内容，
但**没有重新分箱** ⇒ 样本仍留在旧行里，于是：

* 行配对与时间戳脱节 **2.5~3.5 帧**（用行号读数据的下游会错 85~115 ms）；
* `tools/data_qc.py` 的 C5 检查（该流每行首样本 − 同行 wall_time）从 +8ms
  变成 平移量+8ms，出现 80/93/123 三个簇 —— **单个基线覆盖不了**
  （跨度 53.4ms > 2×25ms 容差），质检必然报错。

本工具做偏移修正的**最后一步**：按（已平移的）时间戳把样本重新分到各帧行。
时间戳一个字节都不改，只改样本归属。

* 落在视频窗外的样本（开录前积压、收尾越界）会被丢弃 —— 即"多出来的那些"；
* 行数、视频、其它列一律不动；
* 每行样本数回到 1~3 个（60.36Hz ÷ 30.001fps ≈ 2.01）。

用法
----
    venv/bin/python tools/renest_glove_by_ts.py <数据集目录>            # 预演
    venv/bin/python tools/renest_glove_by_ts.py <数据集目录> --apply    # 落地(自动备份)
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import sys

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

SIDES = ("left", "right")


def side_cols(side):
    return [f"observation.{side}_glove",
            f"observation.{side}_glove_imu_quat",
            f"observation.{side}_glove_imu_valid",
            f"observation.{side}_glove_ts_ns",
            f"observation.{side}_glove_device_ts_ns",
            f"observation.{side}_hand_pose"]


def sha_except(t, skip):
    h = hashlib.sha256()
    for c in t.column_names:
        if c in skip:
            continue
        h.update(c.encode())
        h.update(str(t.column(c).to_pylist()).encode())
    return h.hexdigest()


def record_offline_drops(root, ep, drops):
    """把本次重分箱丢弃的样本写进 meta episodes 的 drop_stats['offline_adjust']。

    `drop_stats` 是**录制器自报**的「收到了多少样本」，是历史事实，不能改；
    重分箱是**离线**动作，丢了多少必须留痕。`tools/data_qc.py` 的 C4 对账会把
    `offline_adjust` 加回实测样本数再比，于是「自报 = 现存 + 离线删」依旧成立，
    对账不会被削弱成空检查，也不会因为这次离线修正而误报。
    """
    mp = glob.glob(os.path.join(root, "meta/episodes/chunk-*", "episode-%s.parquet" % ep))
    if not mp:
        print("  ! 找不到 episode-%s 的 meta，未记账" % ep)
        return None
    mt = pq.read_table(mp[0])
    stats = json.loads(mt.column("drop_stats")[0].as_py())
    adj = {("hand_samples_%s" % s): int(d["removed"]) for s, d in drops.items()}
    stats["offline_adjust"] = adj
    stats["offline_adjust_detail"] = {
        "tool": "tools/renest_glove_by_ts.py",
        "reason": ("手套 host 时间戳平移后按新时间戳重新分箱：窗外样本（开录前积压 + "
                   "收尾越界）不再夹塞进首/末行，已删除"),
        "head_tail": {s: {"head": int(d["head"]), "tail": int(d["tail"])}
                      for s, d in drops.items()},
    }
    idx = mt.schema.get_field_index("drop_stats")
    mt = mt.set_column(idx, "drop_stats",
                       pa.array([json.dumps(stats, ensure_ascii=False)] * mt.num_rows,
                                type=mt.schema.field(idx).type))
    pq.write_table(mt, mp[0], compression="zstd")
    return adj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--episodes", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sync-drop-stats", action="store_true",
                    help="不改数据，只按 _pre_renest 备份补记 drop_stats.offline_adjust")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    root = args.root.rstrip("/")
    want = set(x.strip() for x in args.episodes.split(",") if x.strip())
    files = sorted(glob.glob(os.path.join(root, "data/chunk-*", "*.parquet")))
    files = [p for p in files if not want or os.path.basename(p)[8:11] in want]
    if not files:
        print("没找到 data/chunk-*/*.parquet")
        return 1

    bak = os.path.join(root, "_pre_renest")
    all_skip = set(c for s in SIDES for c in side_cols(s))

    if args.sync_drop_stats:
        print("模式=补记账（只改 meta，不动数据）  目标 %d 段" % len(files))
        for p in files:
            ep = os.path.basename(p)[8:11]
            bp = os.path.join(bak, os.path.basename(p))
            if not os.path.exists(bp):
                print("  ! %s 无备份，跳过" % ep)
                continue
            before = pq.read_table(bp)
            after = pq.read_table(p)
            drops = {}
            for side in SIDES:
                c = f"observation.{side}_glove_ts_ns"
                nb = sum(len(x.as_py() or []) for x in before.column(c))
                na = sum(len(x.as_py() or []) for x in after.column(c))
                drops[side] = {"removed": nb - na, "head": -1, "tail": -1}
            adj = record_offline_drops(root, ep, drops)
            print("  %s  移除 %d/%d  → %s" % (
                ep, drops["left"]["removed"], drops["right"]["removed"], adj))
        return 0

    print("模式=%s  目标 %d 段" % ("落地" if args.apply else "预演（不改文件）", len(files)))
    print("%-6s %7s %9s %9s %9s %11s %11s %9s" %
          ("ep", "行数", "样本前", "样本后", "丢弃", "偏移前(ms)", "偏移后(ms)", "其它列不变"))
    report = []
    for p in files:
        ep = os.path.basename(p)[8:11]
        t = pq.read_table(p)
        pre_sha = sha_except(t, all_skip)
        n = t.num_rows
        wall_ns = np.rint(np.array(t.column("wall_time"), dtype=np.float64) * 1e9).astype(np.int64)
        period = int(np.median(np.diff(wall_ns)))
        lo, hi = int(wall_ns[0]), int(wall_ns[-1]) + period      # 视频窗（含末帧区间）

        cols = {c: t.column(c) for c in t.column_names}
        stat = {}
        pre_med = []
        drop_info = {}
        for side in SIDES:
            names = [c for c in side_cols(side) if c in t.column_names]
            raw = {c: [x.as_py() or [] for x in t.column(c)] for c in names}
            n_before = sum(len(v) for v in raw[names[0]])
            pre_med.append(float(np.median(
                [v[0] - wall_ns[i] for i, v in enumerate(raw[f"observation.{side}_glove_ts_ns"]) if v])) / 1e6)
            # 展平（行主序 → 与录制顺序一致）
            flat = {c: [y for row in raw[c] for y in row] for c in names}
            ts = np.array(flat[f"observation.{side}_glove_ts_ns"], dtype=np.int64)
            keep = (ts >= lo) & (ts < hi)
            head = int((ts < lo).sum())
            tail = int((ts >= hi).sum())
            tgt = np.clip(np.searchsorted(wall_ns, ts, side="right") - 1, 0, n - 1)
            new = {c: [[] for _ in range(n)] for c in names}
            for k in np.where(keep)[0]:
                r = int(tgt[k])
                for c in names:
                    new[c][r].append(flat[c][k])
            for c in names:
                idx = t.schema.get_field_index(c)
                fld = t.schema.field(c)
                cols[c] = pa.array(new[c], type=fld.type)
                t = t.set_column(idx, c, cols[c])
            # 该流的配对准入统计
            first = np.array([(v[0] - wall_ns[i]) if v else np.nan
                              for i, v in enumerate(new[f"observation.{side}_glove_ts_ns"])],
                             dtype=np.float64)
            stat[side] = (n_before, int(keep.sum()), float(np.nanmedian(first) / 1e6))
            drop_info[side] = {"removed": n_before - int(keep.sum()),
                               "head": head, "tail": tail}
        other_ok = sha_except(t, all_skip) == pre_sha
        b0 = sum(stat[s][0] for s in SIDES)
        b1 = sum(stat[s][1] for s in SIDES)
        med_pre = float(np.median(pre_med))
        med_after = float(np.median([stat[s][2] for s in SIDES]))
        report.append({"episode": ep, "rows": n, "samples_before": b0,
                       "samples_after": b1, "dropped": b0 - b1,
                       "offset_before_ms": round(med_pre, 3),
                       "offset_after_ms": round(med_after, 3),
                       "other_cols_ok": other_ok})
        print("%-6s %7d %9d %9d %9d %11.3f %11.3f %9s" %
              (ep, n, b0, b1, b0 - b1, med_pre, med_after, other_ok))
        if args.apply:
            if not args.no_backup:
                os.makedirs(bak, exist_ok=True)
                dst = os.path.join(bak, os.path.basename(p))
                if not os.path.exists(dst):
                    shutil.copy2(p, dst)
            pq.write_table(t, p, compression="zstd")
            adj = record_offline_drops(root, ep, drop_info)
            if adj:
                print("       └ drop_stats.offline_adjust = %s" % adj)

    print("\n合计：行数不变 %s；其它列逐字节不变 %s" %
          (all(r["rows"] > 0 for r in report), all(r["other_cols_ok"] for r in report)))
    if report:
        offs = [r["offset_after_ms"] for r in report]
        print("重分箱后的「首样本 − wall_time」偏移：中位 %.2f ms  范围 %.2f~%.2f ms"
              % (float(np.median(offs)), min(offs), max(offs)))
        print("丢弃样本合计 %d（开录前积压 + 收尾越界）"
              % sum(r["dropped"] for r in report))
    if args.apply:
        info_p = os.path.join(root, "meta/info.json")
        if os.path.isfile(info_p):
            info = json.load(open(info_p, encoding="utf-8"))
            al = info.setdefault("alignment", {})
            base = float(np.median([r["offset_after_ms"] for r in report]))
            al["glove_shift_ms"] = round(base, 3)
            al["glove_shift_ms_semantics"] = (
                "期望的「实测偏移」= 该流每行首样本 − 同行 wall_time（data_qc.py C5 口径）")
            al["renested_by_ts"] = True
            al["renest_note"] = ("时间戳偏移（shift_ms_by_episode）后按新时间戳把样本重新分到"
                                 "各帧行；行配对与时间戳重新自洽，窗外样本已丢弃")
            json.dump(info, open(info_p, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print("meta/info.json: glove_shift_ms 已设为 %.3f（实测基线口径）" % base)
        if not args.no_backup:
            print("原文件已备份到 %s" % bak)
    else:
        print("\n（预演结束，未改任何文件；确认后加 --apply）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
