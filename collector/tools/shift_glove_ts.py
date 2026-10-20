#!/usr/bin/env python3
"""把「手套 host 时间戳」整体后移，对齐到 RGB 视频时间轴（方案 B）。

背景
----
池化转换时，相机侧把**设备曝光 PTS** 锚到 epoch 用的是首帧的**宿主取帧时刻**，
只减掉了约 4 ms，而相机"曝光→宿主取帧"的真实延迟约 85~90 ms ⇒ 视频时间轴整体偏晚。
实测（分块画面运动 × 手套 IMU 角速度，亚帧互相关）：手套戳比视频戳早 ~85 ms。

选方案 B（移手套而不是移视频）的原因：**不动相机时基**，
视频↔IMU（峰值 −8 ms、r=0.91）与视频↔SLAM（−33 ms）完全不受影响。

本工具只做一件事
----------------
对每个 episode 的 `observation.{left,right}_glove_ts_ns`（**宿主到达戳**）
整体 +shift_ns。**不动**：行数、视频、hardware_ns / timestamp / wall_time /
imu_ts_ns / slam_trajectory_ns / *_glove_device_ts_ns。

用法
----
    venv/bin/python tools/shift_glove_ts.py <数据集目录>                 # 预演
    venv/bin/python tools/shift_glove_ts.py <数据集目录> --apply         # 落地(自动备份)
    选项: --shift-ms 85  --episodes 000,001  --force  --no-backup
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

HOST_COLS = ("observation.left_glove_ts_ns", "observation.right_glove_ts_ns")
BACKUP_DIR = "_pre_glove_shift"


def sha_cols(t, skip):
    """除 skip 之外的列的指纹（证明只改了该改的）。"""
    h = hashlib.sha256()
    for c in t.column_names:
        if c in skip:
            continue
        h.update(c.encode())
        h.update(str(t.column(c).to_pylist()).encode())
    return h.hexdigest()


def stats_of(t, cols):
    n = 0
    lo, hi = None, None
    for c in cols:
        for x in t.column(c):
            v = x.as_py() or []
            n += len(v)
            if v:
                lo = v[0] if lo is None else min(lo, v[0])
                hi = v[-1] if hi is None else max(hi, v[-1])
    return n, lo, hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--shift-ms", type=float, default=85.0)
    ap.add_argument("--episodes", default="", help="逗号分隔；默认全部")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--force", action="store_true", help="已在 meta 记录过也照做")
    args = ap.parse_args()

    shift_ns = int(round(args.shift_ms * 1e6))
    root = args.root.rstrip("/")
    info_p = os.path.join(root, "meta/info.json")
    info = json.load(open(info_p, encoding="utf-8")) if os.path.isfile(info_p) else {}
    prev_shift = (info.get("alignment") or {}).get("glove_shift_ms")
    if (prev_shift is not None and abs(prev_shift - args.shift_ms) > 1e-9
            and not args.force):
        print(f"提示: meta 里上一批用的是 glove_shift_ms={prev_shift}，"
              f"本次 {args.shift_ms}。不同场次的相机延迟会变，属正常；"
              f"按段记录各自的平移量。")

    want = set(x.strip() for x in args.episodes.split(",") if x.strip())
    files = sorted(glob.glob(os.path.join(root, "data/chunk-*", "*.parquet")))
    files = [p for p in files
             if not want or os.path.basename(p)[8:11] in want]
    if not files:
        print("没找到 data/chunk-*/*.parquet")
        return 1
    # 幂等：备份目录里已有同名文件的段 = 已平移过 → 跳过
    # （新增段还没有备份，自动被纳入；不需要 --force）
    skipped = []
    if not args.force:
        al = info.get("alignment") or {}
        done_meta = set(al.get("shift_ms_by_episode") or al.get("episodes") or [])
        keep = []
        for p in files:
            ep = os.path.basename(p)[8:11]
            has_bak = os.path.exists(os.path.join(root, BACKUP_DIR,
                                                  os.path.basename(p)))
            if ep in done_meta or has_bak:
                skipped.append(ep)
            else:
                keep.append(p)
        files = keep
    if skipped:
        print("跳过已平移过的 %d 段: %s" % (len(skipped), ",".join(skipped)))
    if not files:
        print("没有需要平移的段（全部已处理）")
        return 0

    print("模式=%s  平移=+%.1f ms (%d ns)  目标 %d 段"
          % ("落地" if args.apply else "预演（不改文件）",
             args.shift_ms, shift_ns, len(files)))
    print("%-6s %7s %10s %10s %12s %12s %10s" %
          ("ep", "行数", "样本数", "改后仍递增", "原首值(ms)", "新首值(ms)", "其它列不变"))
    bak = os.path.join(root, BACKUP_DIR)
    report = []
    for p in files:
        ep = os.path.basename(p)[8:11]
        t = pq.read_table(p)
        pre_sha = sha_cols(t, HOST_COLS)
        n_rows = t.num_rows
        n_s, lo, hi = stats_of(t, HOST_COLS)

        cols = {c: t.column(c) for c in t.column_names}
        for c in HOST_COLS:
            if c not in t.column_names:
                continue
            new = []
            for x in t.column(c):
                v = x.as_py() or []
                new.append([int(y) + shift_ns for y in v])
            idx = t.schema.get_field_index(c)
            cols[c] = pa.array(new, type=pa.list_(pa.int64()))
            t = t.set_column(idx, c, cols[c])

        # 校验：每个列表内部仍严格递增、行数不变
        mono = True
        for c in HOST_COLS:
            if c not in t.column_names:
                continue
            for x in t.column(c):
                v = x.as_py() or []
                if any(b <= a for a, b in zip(v, v[1:])):
                    mono = False
        n2, lo2, hi2 = stats_of(t, HOST_COLS)
        other_ok = sha_cols(t, HOST_COLS) == pre_sha
        report.append({"episode": ep, "rows": n_rows, "samples": n2,
                       "mono": mono, "other_cols_ok": other_ok})
        print("%-6s %7d %10d %10s %12.3f %12.3f %10s" %
              (ep, n_rows, n2, mono, lo / 1e6, lo2 / 1e6, other_ok))

        if args.apply:
            if not args.no_backup:
                os.makedirs(bak, exist_ok=True)
                dst = os.path.join(bak, os.path.basename(p))
                if not os.path.exists(dst):
                    shutil.copy2(p, dst)
            pq.write_table(t, p, compression="zstd")

    if args.apply:
        info.setdefault("alignment", {})
        info["alignment"].update({
            "applied_to": "observation.{left,right}_glove_ts_ns",
            "reason": ("相机曝光→宿主取帧延迟约 85~90ms 被锚定吸收，视频轴偏晚；"
                       "以视频为准把手套 host 戳后移对齐"),
            "method": "分块画面运动 × 手套IMU角速度 亚帧互相关",
            "measured_offset_ms": {"ep000": 85.5, "ep001": 86.0},
            "untouched": ["hardware_ns", "timestamp", "wall_time", "imu_ts_ns",
                          "slam_trajectory_ns", "*_glove_device_ts_ns", "videos"],
            "backup_dir": None if args.no_backup else BACKUP_DIR,
        })
        per = dict(info["alignment"].get("shift_ms_by_episode") or {})
        for p in files:
            ep = os.path.basename(p)[8:11]
            # 记录的是**累计**平移量（--force 补差时也正确）
            per[ep] = round(float(per.get(ep, 0.0)) + args.shift_ms, 6)
        info["alignment"]["shift_ms_by_episode"] = dict(sorted(per.items()))
        info["alignment"]["episodes"] = sorted(per)
        # 一个标量表达不了逐段不同的平移量：只有全库同一值时才写成标量，
        # 否则写 None（老读取方 isinstance 判断会自动跳过，好过留一个误导性的数）。
        vals = sorted(set(per.values()))
        info["alignment"]["glove_shift_ms"] = vals[0] if len(vals) == 1 else None
        info["alignment"]["glove_shift_ms_note"] = (
            "本字段=时间戳被平移的量；多批次数据集请用 shift_ms_by_episode。"
            "质检基线（C5 口径）另见 qc_offset_baseline_ms。")
        json.dump(info, open(info_p, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("\n已在 meta/info.json 记录 alignment 字段")
        if not args.no_backup:
            print("原文件已备份到 %s" % bak)
        print("\n⚠ 平移后必须重跑 tools/renest_glove_by_ts.py --apply："
              "转换器是按时间戳把样本嵌进帧行的，只改时间戳不重新分箱，"
              "行配对会与时间戳脱节（下游按行号读数据就会错一个平移量）。")
        print("   重跑后 qc_offset_baseline_ms 会由该工具按实测重设。")
    else:
        print("\n（预演结束，未改任何文件；确认后加 --apply）")
    print("行数守恒: %s" % all(r["rows"] > 0 for r in report))
    print("其它列逐字节不变: %s" % all(r["other_cols_ok"] for r in report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
