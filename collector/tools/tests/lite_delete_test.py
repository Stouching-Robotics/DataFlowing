"""极简版「上传后删除」编号回归测试（无硬件，离屏 Qt）。

回归点（2026-10-12 改真删除时发现，对抗性审查复现）：
  lite 的 `_delete_after_upload` 收的必须是**全局 episode 号 N（1 基）**——
  上传器按 N 取文件（`episode_chunk_file(N)` → 文件号 N-1）、`UploadManager`
  按 (path, N) 记账，自动上传路径传进来的 `pipeline.last_episode_index` 也是 N。
  这里多 +1（或 `_episode_max` 少 +1）都会指向另一段：轻则「上传的那段没删、
  删了一段没上传过的」，重则目标段本就不存在 → `delete_pooled_episode` 的残留
  复检空集 → 返回 True → 界面报「已删除」而文件一个没动。

本测试造 3 段池化数据（文件号 0/1/2 = N 1/2/3）：
  - `_episode_max` 返回 N=3（不是文件号 2）；
  - `_on_upload_done` 原样把 N 透传给 `_delete_after_upload`（不 +1）；
  - 按 N=3 删掉的是文件号 002 那一组（数据 + 视频 + meta 行），N=1/2 原样在；
  - 单段目录删的是那一段本身，不整目录 rmtree；
  - 无 parquet 的旧格式目录按整目录删；
  - 跨 chunk（chunk-000 满 1000 段后开 chunk-001 并从 episode-000 重新计数）：
    `episode_max` 必须按 `chunk*1000 + f + 1` 还原成真值 N=1001，**不能**取
    「最大文件名 f + 1」—— f 会回绕，那个值指向 chunk-000 里**真实存在**的前
    一段，于是上传/删除静默操作到错的段。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/lite_delete_test.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

import main_lite  # noqa: F401 — 生产入口，先 import 再构造窗口
from config import settings
from core.egodata_writer import _atomic_write_parquet, _episode_rows_table
from core.helpers import (POOLED_CHUNK_SIZE, list_task_episodes,
                          pooled_data_parquet_path, pooled_episodes_path)
from core.pipeline import CameraPipeline
from ui.lite_window import LiteWindow

ROOT = "/tmp/lite_delete_test"
FAILS = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        FAILS.append(msg)


def make_episode(task_dir: str, file_no: int, chunk: int = 0):
    """造一段池化数据：chunk 内文件号 file_no。

    全局号 N = chunk * 1000 + file_no + 1 —— 每 chunk 1000 段，chunk-000 满
    1000 段后开 chunk-001 并从 episode-000 重新计数。
    """
    n = chunk * POOLED_CHUNK_SIZE + file_no + 1
    cdir = f"chunk-{chunk:03d}"
    d = os.path.join(task_dir, "data", cdir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"episode-{file_no:03d}.parquet"), "wb") as f:
        f.write(b"data")
    v = os.path.join(task_dir, "videos", cdir, "uvc_rgb")
    os.makedirs(v, exist_ok=True)
    with open(os.path.join(v, f"episode-{file_no:03d}.mp4"), "wb") as f:
        f.write(b"video")
    ep_path = pooled_episodes_path(task_dir, n)
    os.makedirs(os.path.dirname(ep_path), exist_ok=True)
    _atomic_write_parquet(
        _episode_rows_table([{"episode_index": n, "task_name": "t"}]),
        ep_path)


def group_exists_n(task_dir: str, n: int) -> bool:
    """全局号 N 的三件套是否齐全（数据 / 视频 / meta 行）。"""
    from core.helpers import episode_chunk_file
    c, f = episode_chunk_file(n)
    return (os.path.isfile(pooled_data_parquet_path(task_dir, n))
            and os.path.isfile(os.path.join(
                task_dir, "videos", f"chunk-{c:03d}", "uvc_rgb",
                f"episode-{f:03d}.mp4"))
            and os.path.isfile(pooled_episodes_path(task_dir, n)))


def group_exists(task_dir: str, file_no: int) -> bool:
    """chunk-000 里该文件号的三件套（旧调用点的简写）。"""
    return group_exists_n(task_dir, file_no + 1)


def main():
    shutil.rmtree(ROOT, ignore_errors=True)
    app = QApplication(sys.argv)  # noqa: F841 — 持有引用才有 QApplication
    settings.UPLOAD_AUTO_SYNC = False
    settings.ENCODER_PROBE_ENABLED = False
    win = LiteWindow(pipeline=CameraPipeline(output_dir=ROOT))
    try:
        t1 = os.path.join(ROOT, "multi")
        for f in (0, 1, 2):
            make_episode(t1, f)
        check(len(list_task_episodes(t1)) == 3, "造 3 段池化数据")

        # ── 1. 列表口径：ep 必须是全局号 N，不是文件号 ──
        check(win._episode_max(t1) == 3,
              "上传列表的 ep = 全局 N 3（不是文件号 2；列表值会被上传器当 N 用）")

        # ── 2. 上传完成后删除：原样透传 N，不许 +1 ──
        real_delete = win._delete_after_upload
        seen = []
        win._delete_after_upload = lambda p, e: seen.append((p, e))
        settings.UPLOAD_DELETE_AFTER = True
        win._upload_task_map["tid"] = (t1, 3)     # 自动上传路径存的就是 N
        win._on_upload_done("tid")
        check(seen == [(t1, 3)],
              f"_on_upload_done 原样透传 N=3（实测 {seen}；+1 会删到不存在的 N=4）")
        win._delete_after_upload = real_delete

        # ── 3. 真删：N=3 删的是文件号 002 那一组 ──
        win._delete_after_upload(t1, 3)
        check(not group_exists(t1, 2), "删掉的是文件号 002 那一段（上传的那段）")
        check(group_exists(t1, 0) and group_exists(t1, 1),
              "文件号 000/001 两组原样保留（没误删邻居）")
        check(list_task_episodes(t1) == [1, 2], "剩余 episode 号 = [1, 2]")
        check(os.path.isdir(t1), "任务目录本身保留（只删该段）")

        # ── 4. 单段目录：删的是那一段，不是整目录 ──
        t2 = os.path.join(ROOT, "single")
        make_episode(t2, 0)
        check(win._episode_max(t2) == 1, "单段目录的 ep = N 1（不是 0 = 整目录）")
        win._delete_after_upload(t2, 1)
        check(list_task_episodes(t2) == [], "单段目录删掉该段")
        check(os.path.isdir(t2), "单段目录仍存在（不整目录 rmtree）")

        # ── 5. 池化 parquet 全无（旧格式/空目录）→ 按整目录删 ──
        t3 = os.path.join(ROOT, "legacy")
        os.makedirs(os.path.join(t3, "meta"), exist_ok=True)
        with open(os.path.join(t3, "meta", "info.json"), "w") as f:
            f.write("{}")
        check(win._episode_max(t3) == 0, "旧格式目录 ep = 0（整目录）")
        win._delete_after_upload(t3, 0)
        check(not os.path.isdir(t3), "无 parquet 的目录整目录删除")

        # ── 6. 跨 chunk：f 回绕后必须按 chunk 号还原全局 N ──
        t4 = os.path.join(ROOT, "chunked")
        for f in (0, 1, POOLED_CHUNK_SIZE - 1):
            make_episode(t4, f)                 # chunk-000 → N 1 / 2 / 1000
        make_episode(t4, 0, chunk=1)            # chunk-001 → N 1001（f 从 0 重开）
        check(len(list_task_episodes(t4)) == 4, "造跨 chunk 数据：N = [1, 2, 1000, 1001]")
        check(win._episode_max(t4) == POOLED_CHUNK_SIZE + 1,
              f"跨 chunk 的任务 ep = 真值 N {POOLED_CHUNK_SIZE + 1}"
              f"（不是「最大文件名 f + 1」= 1 —— 那指向 chunk-000 里真实存在的 N=1）")
        win._delete_after_upload(t4, POOLED_CHUNK_SIZE + 1)
        check(not group_exists_n(t4, POOLED_CHUNK_SIZE + 1),
              "删掉的是 chunk-001/episode-000（N 1001）")
        check(group_exists_n(t4, 1) and group_exists_n(t4, 2)
              and group_exists_n(t4, POOLED_CHUNK_SIZE),
              "chunk-000 的 N=1/2/1000 原样保留（没回绕删到真值 N=1）")
    finally:
        win.close()
        shutil.rmtree(ROOT, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: lite 上传后删除编号回归全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
