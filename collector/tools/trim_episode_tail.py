#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁掉 episode 尾部的 N 秒（默认 1 秒），把尾部缺样本的模态对齐掉。

为什么需要它
------------
录制尾部有时某个模态**先停**：episode-003 的右手套在段末掉了 19 帧
（515ms）没有样本，而 RGB / 左手套 / IMU / SLAM 一直录到最后一帧。落盘后这
表现为「该流最后 19 行是空列表 + `status.right_glove='unmatched'`」。变长列
本来就能表达「这帧没有该模态」，但下游若要求**矩形张量**（每帧都必须有右手
256 维），就只能把尾巴统一裁掉。

裁多少、怎么裁（实测口径，2026-10-15）
--------------------------------------
* **视频**：`-c copy` 无损裁剪可以精确到任意帧，不限于关键帧。实测 ep003
  （h264 3200×1200 / GOP=30 / 1474 帧 / 关键帧在 0,30,…,1440,1470）用
  `-c copy -frames:v 1444` 得到**恰好 1444 帧**，且保留帧 0/720/1440/1441/
  1442/1443 与原件**逐像素 md5 相同**（B/P 帧回溯引用的关键帧 1440 仍在保留
  区间内，所以断在中途也解得出）。裁剪耗时 0.07s，无需重编码。
* **parquet**：行切片，天然无损。
* **不变量**：全库每段都满足「视频帧数 == parquet 行数」，所以两边必须一起裁，
  否则回放/按帧号取图的工具会错位（`tools/data_qc.py --check-video` 也会报）。

本工具做四件事（每段）
----------------------
1. `data/chunk-*/episode-*.parquet` 行切片到 `keep = 原行数 − 裁掉帧数`；
2. `videos/chunk-*/*/episode-*.<ext>` 用 `-c copy -frames:v keep` 同步裁；
3. `meta/episodes/chunk-*/episode-*.parquet` 同步：`length` / `end_frame_index` /
   `duration_sec` / `video_codec.frames`，以及 `drop_stats` 里**由保留行直接
   决定**的计数（`frames` / `last_user_data_seq` / `hand_samples_*` /
   `frames_without_*` / `imu_samples`）。会话级累计计数（`dropped_bytes` /
   `parse_errors` 等）保持原值 —— 它们描述的是录制过程，不随裁剪改写；
4. `meta/stats.json` **默认不动**（`--recalc-stats` 才重算，且见下警告）。

**默认预演，不改任何文件**；`--apply` 才落地，且自动备份到 `_pre_tail_trim/`。
落地后在 `meta/info.json` 写 `tail_trim` 记录，重复执行会被拒绝（除非 `--force`）。

⚠ 关于 stats.json：**本工具默认不碰它**，两个原因——
（a）`core.helpers.recalc_stats` 走 `np.stack`，对「每行样本数不等」的列（本数据集
的 `observation.left_glove` 每行 1/2/72 个样本）会抛 `ValueError` 被 `except` 静默
`continue`，结果是**把该列从 stats.json 里删掉**；
（b）stats.json 是池化转换器（`rv_to_pooled`）产出的任务级累加器，本数据集里它
**本来就是旧的**（记的是上一版 25 段 / 157,825 个手套样本，当前数据是 24 段 /
138,433）——裁 30 行对它 0.04% 的影响远小于它自身的陈旧度。
要准就在导出管线里重新生成，别在这里半吊子重算。

注意：**裁尾修不了段中间的缺失**。如果某流的空行不在末尾（例如早些时候那版
数据的 ep000 左手套在 row 1992 / 8227~8380 掉了 14 行），裁尾只会白丢数据。
落地前本工具会把「裁后仍存在的空行」逐流打出来核对。

用法
----
    venv/bin/python tools/trim_episode_tail.py <数据集目录> --episodes 003            # 预演
    venv/bin/python tools/trim_episode_tail.py <数据集目录> --episodes 003 --apply     # 落地
    venv/bin/python tools/trim_episode_tail.py <数据集目录>                           # 全部段

选项
    --seconds 1.0    裁掉多少秒（按 info.json 的 fps 折算成帧数，四舍五入）
    --frames N       直接指定裁掉多少帧（覆盖 --seconds）
    --episodes LIST  逗号分隔的三位段号（003）或全局号（3）；默认全部
    --no-video       只裁 parquet，不动视频（会破坏 video==rows 不变量，慎用）
    --recalc-stats   **危险**：调 core.helpers.recalc_stats 全量重算 stats.json
                     （会把「每行样本数不等」的列静默删掉，见上文）
    --no-backup      不备份（默认备份到 _pre_tail_trim/）
    --force          已在 info.json 记录过也照做
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

BACKUP_DIR = "_pre_tail_trim"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ──────────────────────────── 枚举与路径 ────────────────────────────

def load_info(root: str) -> Dict[str, Any]:
    p = os.path.join(root, "meta", "info.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def list_episodes(root: str, info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """枚举 data/chunk-*/episode-*.parquet，附带 meta 行与视频路径。"""
    chunks_size = int(info.get("chunks_size") or 1000)
    exts = info.get("video_extensions") or {}
    out: List[Dict[str, Any]] = []
    for p in sorted(glob.glob(os.path.join(root, "data", "chunk-*", "episode-*.parquet"))):
        m = re.search(r"data[/\\]chunk-(\d+)[/\\]episode-(\d+)\.parquet$", p.replace("\\", "/"))
        if not m:
            continue
        chunk, fnum = int(m.group(1)), int(m.group(2))
        stem = "episode-%03d" % fnum
        meta_p = os.path.join(root, "meta", "episodes", "chunk-%03d" % chunk, stem + ".parquet")
        videos: List[Tuple[str, str]] = []
        vdir = os.path.join(root, "videos", "chunk-%03d" % chunk)
        if os.path.isdir(vdir):
            for key in sorted(os.listdir(vdir)):
                ext = exts.get(key, "mp4")
                cand = os.path.join(vdir, key, "%s.%s" % (stem, ext))
                if os.path.isfile(cand):
                    videos.append((key, cand))
        out.append({
            "fnum": fnum, "chunk": chunk, "stem": stem,
            "global_n": chunk * chunks_size + fnum + 1,
            "data_path": p, "meta_path": meta_p if os.path.isfile(meta_p) else None,
            "videos": videos,
        })
    return out


# ──────────────────────────── 度量与切片 ────────────────────────────

def empty_rows(t: pa.Table) -> Dict[str, Tuple[int, int]]:
    """每个列表级时间戳流 → (空行数, 样本总数)。"""
    out: Dict[str, Tuple[int, int]] = {}
    for name in t.column_names:
        if not (name.endswith("_ns") or name.endswith("_ts_ns")):
            continue
        col = t.column(name)
        if not pa.types.is_list(col.type):
            continue
        L = [len(x or []) for x in col.to_pylist()]
        out[name] = (sum(1 for x in L if x == 0), sum(L))
    return out


def slice_table(t: pa.Table, keep: int) -> pa.Table:
    return t.slice(0, keep)


def recount_drop_stats(t: pa.Table, keep: int,
                       orig: Dict[str, Any]) -> Dict[str, Any]:
    """只改「由保留行直接决定」的计数；会话级累计计数保持原值。"""
    ds = dict(orig)
    ds["frames"] = keep
    if "frame_index" in t.column_names:
        ds["last_user_data_seq"] = int(t.column("frame_index").to_pylist()[keep - 1])
    for side in ("left", "right"):
        c = "observation.%s_glove_ts_ns" % side
        if c in t.column_names:
            L = [len(x or []) for x in t.column(c).to_pylist()]
            ds["hand_samples_%s" % side] = int(sum(L))
            ds["frames_without_%s_hand" % side] = int(sum(1 for x in L if x == 0))
    if "imu_ts_ns" in t.column_names:
        L = [len(x or []) for x in t.column("imu_ts_ns").to_pylist()]
        ds["imu_samples"] = int(sum(L))
        ds["frames_without_imu"] = int(sum(1 for x in L if x == 0))
    return ds


# ──────────────────────────── 视频裁剪 ────────────────────────────

def trim_video(src: str, dst: str, keep: int, timeout: float = 600.0) -> Tuple[bool, str]:
    """`-c copy -frames:v keep` 无损裁到恰好 keep 帧。返回 (成功, 说明)。"""
    if not shutil.which("ffmpeg"):
        return False, "找不到 ffmpeg"
    tmp = dst + ".tmp.mp4"
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", src, "-c", "copy",
             "-frames:v", str(keep), tmp],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "ffmpeg 超时"
    if r.returncode != 0 or not os.path.isfile(tmp):
        return False, "ffmpeg 失败: %s" % (r.stderr or "").strip()[:200]
    # 复核：真解一遍数帧数（nb_frames 是容器头的声明，不可全信）
    cnt = None
    try:
        r2 = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", tmp],
            capture_output=True, text=True, timeout=timeout)
        v = (r2.stdout or "").strip()
        cnt = int(v) if v.isdigit() else None
    except Exception:
        pass
    if cnt != keep:
        os.unlink(tmp)
        return False, "裁后帧数 %s ≠ 期望 %d，已放弃" % (cnt, keep)
    os.replace(tmp, dst)          # 原子替换
    return True, "→ %d 帧" % cnt


# ──────────────────────────── stats.json ────────────────────────────

def stats_freshness(root: str, eps: List[Dict[str, Any]]) -> str:
    """stats.json 比最新的 data parquet 旧 ⇒ 它跟不上当前数据（只报，不改）。"""
    sp = os.path.join(root, "meta", "stats.json")
    if not os.path.isfile(sp):
        return "无 stats.json"
    newest = max((os.path.getmtime(e["data_path"]) for e in eps), default=0.0)
    if os.path.getmtime(sp) < newest:
        return ("⚠ stats.json 比最新数据旧 %s，可能本来就与当前数据不一致"
                "（本工具默认不动它）" % _ago(newest - os.path.getmtime(sp)))
    return "ok"


def _ago(sec: float) -> str:
    if sec < 90:
        return "%.0f 秒" % sec
    if sec < 5400:
        return "%.0f 分钟" % (sec / 60)
    return "%.1f 小时" % (sec / 3600)


def recalc_stats(root: str) -> Tuple[bool, str]:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from core.helpers import recalc_stats as _rc
    except Exception as e:
        return False, "导入 core.helpers 失败：%r" % (e,)
    try:
        _rc(root)
        return True, "已全量重算"
    except Exception as e:
        return False, "recalc_stats 失败：%r" % (e,)


# ──────────────────────────── 主流程 ────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="trim_episode_tail.py",
        description="裁掉 episode 尾部 N 秒（parquet + meta + 视频 + stats 一起），用于尾部模态缺失的对齐",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----")[-1].strip()[:1600])
    ap.add_argument("root", help="数据集目录（含 data/ videos/ meta/）")
    ap.add_argument("--seconds", type=float, default=1.0, help="裁掉多少秒（默认 1.0）")
    ap.add_argument("--frames", type=int, default=0, help="直接指定帧数（覆盖 --seconds）")
    ap.add_argument("--episodes", default="", help="逗号分隔：003 或全局号 3；默认全部")
    ap.add_argument("--apply", action="store_true", help="落地（默认只预演）")
    ap.add_argument("--no-video", action="store_true", help="不动视频")
    ap.add_argument("--recalc-stats", action="store_true",
                    help="危险：调 core.helpers.recalc_stats 全量重算 stats.json"
                         "（会静默删掉每行样本数不等的列）")
    ap.add_argument("--no-backup", action="store_true", help="不备份")
    ap.add_argument("--force", action="store_true", help="已记录过也照做")
    ap.add_argument("--jobs", type=int, default=1, help="预留（当前串行）")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root.rstrip("/") or "/")
    if not os.path.isdir(root):
        print("数据集目录不存在：%s" % root, file=sys.stderr)
        return 2
    info = load_info(root)
    fps = float(info.get("fps") or 0.0)
    if args.frames > 0:
        cut = int(args.frames)
    elif fps > 0:
        cut = int(round(args.seconds * fps))
    else:
        print("info.json 没有 fps，且未给 --frames，无法折算", file=sys.stderr)
        return 2
    if cut <= 0:
        print("裁掉帧数必须 > 0（算出 %d）" % cut, file=sys.stderr)
        return 2

    eps = list_episodes(root, info)
    if args.episodes:
        want = {x.strip() for x in re.split(r"[,\s]+", args.episodes) if x.strip()}
        eps = [e for e in eps if "%03d" % e["fnum"] in want or str(e["global_n"]) in want]
    if not eps:
        print("没匹配到任何 episode", file=sys.stderr)
        return 2

    done_rec = (info.get("tail_trim") or {})
    done_eps = set(done_rec.get("episodes") or [])
    print("数据集 : %s" % root)
    print("模式   : %s   fps=%s  裁掉 %d 帧 (%.3fs)  目标 %d 段"
          % ("落地（写文件）" if args.apply else "预演（不改任何文件）",
             fps or "?", cut, cut / fps if fps else 0.0, len(eps)))
    if args.apply and not args.no_video:
        print("视频   : ffmpeg -c copy -frames:v <keep>（无损，实测可精确到任意帧）")
    print("stats  : %s" % stats_freshness(root, eps))
    print()

    hdr = ("%-6s %8s %6s %8s %10s  %-26s %-20s %s"
           % ("ep", "原帧数", "裁掉", "保留", "视频帧数", "右手套 空行→ 样本", "左手套 空行→", "裁后仍空的流"))
    print(hdr)
    print("-" * len(hdr))

    bak = os.path.join(root, BACKUP_DIR)
    report: List[Dict[str, Any]] = []
    rc = 0
    for e in eps:
        ep = "%03d" % e["fnum"]
        t = pq.read_table(e["data_path"])
        n = t.num_rows
        keep = n - cut
        if keep <= 0:
            print("%-6s 原帧数 %d 不足以裁掉 %d 帧，跳过" % (ep, n, cut))
            rc = 1
            continue
        if ep in done_eps and not args.force:
            print("%-6s !! info.json 已记录裁过（tail_trim），拒绝重复（--force 可强制）" % ep)
            rc = 1
            continue

        before = empty_rows(t)
        t2 = slice_table(t, keep)
        after = empty_rows(t2)
        still = {k: v[0] for k, v in after.items() if v[0] > 0}

        # ── 落地模式：**先备份**，再动任何文件 ──
        # 顺序不能反：视频是就地替换的，备份若排在裁剪之后就只会备份到裁完的版本
        # （踩过一次：备份里的 mp4 已经是 1444 帧而 parquet 还是 1474 行）。
        # 备份还按**相对路径镜像**：data/ 与 meta/ 下同名 episode-NNN.parquet 若只按
        # basename 存会互相覆盖（也踩过一次）。
        if args.apply and not args.no_backup:
            for src in [e["data_path"]] + ([e["meta_path"]] if e["meta_path"] else []) \
                    + [vp for _, vp in e["videos"]]:
                dst = os.path.join(bak, os.path.relpath(src, root))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)

        vid_counts: List[str] = []
        vid_ok = True
        if not args.no_video:
            for key, vp in e["videos"]:
                if args.apply:
                    ok, msg = trim_video(vp, vp, keep)
                    vid_ok = vid_ok and ok
                    vid_counts.append("%s %s" % (key, msg if ok else "FAIL " + msg))
                else:
                    vid_counts.append("%s(预演)" % key)

        def fmt(d, k):
            if k not in d:
                return "-"
            return "%d→%d" % (before[k][0], after[k][0])

        right = "observation.right_glove_ts_ns"
        left = "observation.left_glove_ts_ns"
        print("%-6s %8d %6d %8d %10s  %-26s %-20s %s"
              % (ep, n, cut, keep,
                 ",".join(vid_counts) if vid_counts else "(跳过)",
                 "%s %d" % (fmt(before, right), after.get(right, (0, 0))[1]),
                 fmt(before, left),
                 ("、".join("%s×%d" % (k.replace("observation.", ""), v) for k, v in still.items())
                  if still else "无 ✓")))

        report.append({"episode": ep, "orig_frames": n, "cut": cut, "keep": keep,
                       "empty_before": {k: v[0] for k, v in before.items()},
                       "empty_after": {k: v[0] for k, v in after.items()},
                       "samples_after": {k: v[1] for k, v in after.items()},
                       "still_empty": still, "video_ok": vid_ok})

        if not args.apply:
            continue

        tmp = e["data_path"] + ".tmp"
        pq.write_table(t2, tmp, compression="zstd")
        os.replace(tmp, e["data_path"])

        if e["meta_path"]:
            mrow = pq.read_table(e["meta_path"]).to_pylist()[0]
            ts = t2.column("timestamp").to_pylist() if "timestamp" in t2.column_names else None
            if ts:
                mrow["duration_sec"] = float(ts[-1] - ts[0])
            mrow["length"] = keep
            mrow["end_frame_index"] = int(t2.column("frame_index").to_pylist()[-1]) \
                if "frame_index" in t2.column_names else keep - 1
            try:
                mrow["drop_stats"] = json.dumps(
                    recount_drop_stats(t2, keep, json.loads(mrow.get("drop_stats") or "{}")),
                    ensure_ascii=False)
            except Exception:
                pass
            try:
                vc = json.loads(mrow.get("video_codec") or "{}")
                if "frames" in vc:
                    vc["frames"] = keep
                    mrow["video_codec"] = json.dumps(vc, ensure_ascii=False)
            except Exception:
                pass
            mt = pa.Table.from_pylist([mrow], schema=pq.read_table(e["meta_path"]).schema)
            tmpm = e["meta_path"] + ".tmp"
            pq.write_table(mt, tmpm)
            os.replace(tmpm, e["meta_path"])

    print()
    if not args.apply:
        print("（预演结束，未改任何文件；确认后加 --apply）")
        return rc

    if not report:
        # 全部段都被拒/跳过 —— **不能**改写 info.json，否则会把上一次真正落地的
        # 记录覆盖成空（踩过一次：重跑被拒后 per_episode 变成了 {}）。
        print("没有任何段被处理，info.json 未改动")
        return rc

    # info.json 记录（与 tools/shift_glove_ts.py 的 alignment 记录同风格）：
    # 与已有记录**合并**，多次裁不同段不会互相抹掉。
    prev = info.get("tail_trim") or {}
    per = dict(prev.get("per_episode") or {})
    per.update({r["episode"]: {"orig_frames": r["orig_frames"], "keep": r["keep"],
                               "cut": r["cut"], "still_empty_after": r["still_empty"]}
                for r in report})
    info["tail_trim"] = {
        "seconds": args.seconds, "frames": cut, "fps": fps,
        "episodes": sorted(set(prev.get("episodes") or []) | {r["episode"] for r in report}),
        "reason": ("尾部模态先停（如右手套段末掉样本），把 RGB/左手套/IMU/SLAM 的尾帧"
                   "一起裁掉以得到每帧都齐的矩形数据"),
        "method": "parquet 行切片 + ffmpeg -c copy -frames:v keep（无损，逐像素核过）",
        "updated_meta": ["length", "end_frame_index", "duration_sec",
                         "drop_stats{frames,last_user_data_seq,hand_samples_*,frames_without_*}",
                         "video_codec.frames"],
        "backup_dir": None if args.no_backup else BACKUP_DIR,
        "per_episode": per,
    }
    if args.recalc_stats:
        ok, msg = recalc_stats(root)
        info["tail_trim"]["stats_recomputed"] = ok
        info["tail_trim"]["stats_note"] = msg
        print("stats.json：%s" % msg)
    with open(os.path.join(root, "meta", "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print("已在 meta/info.json 记录 tail_trim 字段（本次处理 %s）"
          % ",".join(r["episode"] for r in report))
    if not args.no_backup:
        print("原件已备份到 %s" % bak)
    print("\n裁后仍有空行的流（应为空；非空说明这是**段中间**的缺失，裁尾修不了）：")
    any_left = False
    for r in report:
        if r["still_empty"]:
            any_left = True
            print("  ep%s: %s" % (r["episode"], r["still_empty"]))
    if not any_left:
        print("  无 ✓ 全部流每帧都有样本")
    return rc


if __name__ == "__main__":
    sys.exit(main())
