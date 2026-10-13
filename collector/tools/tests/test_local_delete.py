"""本地删除统一口径（真删除）测试 —— 文件 + 录制行，全程校验。

2026-10-12：删除从「标一行痕迹 / 界面照报成功」改成**真删除 + 删除后校验**。
本测试钉死四件事：
  1. 池化删除删对组（数据 + 视频 + meta 行），且返回 True 只代表真删净；
  2. 删不掉（目录不可写）时返回 False 并给出残留文件——不许谎报成功；
  3. 旧格式会话目录整目录删 + 校验；目录不存在如实报错；
  4. RecordingRepo.delete_local_episode / delete_local_dir 真删库行（含尾斜杠
     等路径写法归一化）。

用法:
    venv/bin/python tools/tests/test_local_delete.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.database import Database
from core.egodata_writer import _atomic_write_parquet, _episode_rows_table
from core.helpers import (delete_local_session, delete_pooled_episode,
                          list_task_episodes, pooled_data_parquet_path,
                          pooled_episode_leftovers, pooled_episodes_path)
from core.recording_record import RecordingRecord

FAILS = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        FAILS.append(msg)


def make_episode(task_dir: str, file_no: int, key: str = "uvc_rgb"):
    """造一段池化数据：文件号 file_no（全局 N = file_no + 1）。"""
    n = file_no + 1
    d = os.path.join(task_dir, "data", "chunk-000")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"episode-{file_no:03d}.parquet"), "wb") as f:
        f.write(b"data")
    v = os.path.join(task_dir, "videos", "chunk-000", key)
    os.makedirs(v, exist_ok=True)
    with open(os.path.join(v, f"episode-{file_no:03d}.mp4"), "wb") as f:
        f.write(b"video")
    ep_path = pooled_episodes_path(task_dir, n)
    os.makedirs(os.path.dirname(ep_path), exist_ok=True)
    _atomic_write_parquet(
        _episode_rows_table([{"episode_index": n, "task_name": "t"}]),
        ep_path)


def test_pooled_true_delete(root: str):
    print("── 1. 池化真删除（删对组 + 校验）──")
    t = os.path.join(root, "pooled")
    for f in (0, 1, 2):
        make_episode(t, f)
    check(list_task_episodes(t) == [1, 2, 3], "造 3 段（全局 N=1..3）")
    check(delete_pooled_episode(t, 2), "delete N=2 返回 True")
    check(pooled_episode_leftovers(t, 2) == [], "N=2 残留为空（真删净）")
    check(list_task_episodes(t) == [1, 3], "删的是 N=2（文件号 001）")
    check(not os.path.isfile(pooled_data_parquet_path(t, 2))
          and not os.path.isfile(pooled_episodes_path(t, 2)),
          "N=2 的 parquet 与 meta 行文件都没了")
    check(os.path.isfile(pooled_data_parquet_path(t, 1))
          and os.path.isfile(pooled_data_parquet_path(t, 3)),
          "邻居段（N=1/N=3）原样在")
    check(delete_pooled_episode(t, 2), "重复删除幂等（返回 True）")


def test_honest_failure(root: str):
    print("── 2. 删不掉必须如实报错（不许报成功）──")
    t = os.path.join(root, "locked")
    make_episode(t, 0)
    vdir = os.path.join(t, "videos", "chunk-000", "uvc_rgb")
    os.chmod(vdir, 0o500)                     # 目录不可写 → unlink 失败
    try:
        check(delete_pooled_episode(t, 1) is False,
              "视频删不掉时 delete_pooled_episode 返回 False")
        left = pooled_episode_leftovers(t, 1)
        check(any(p.endswith(".mp4") for p in left),
              f"残留清单含那段视频（{len(left)} 个残留）")
        ok, err = delete_local_session(t, 1)
        check(ok is False and err, f"delete_local_session 报错: {err}")
    finally:
        os.chmod(vdir, 0o755)
    ok, err = delete_local_session(t, 1)
    check(ok is True and err == "", "恢复可写后重试成功")
    check(pooled_episode_leftovers(t, 1) == [], "重试后残留为空")


def test_legacy_dir(root: str):
    print("── 3. 旧格式会话目录 ──")
    d = os.path.join(root, "legacy_sess")
    os.makedirs(os.path.join(d, "meta"))
    with open(os.path.join(d, "meta", "info.json"), "w") as f:
        f.write("{}")
    check(delete_local_session(d, 0) == (True, ""), "整目录删除返回 (True, '')")
    check(not os.path.isdir(d), "目录真的没了")
    ok, err = delete_local_session(d, 0)
    check(ok is False and err, f"目录不存在如实报错: {err}")
    ok, err = delete_local_session("", 3)
    check(ok is False and err, f"空路径如实报错: {err}")


def test_repo_true_delete(root: str):
    print("── 4. 录制行真删除（不再留痕迹行）──")
    import core.recording_repository as rr
    db = Database(db_path=os.path.join(root, "test.db"))
    db.init_schema()
    rr.db = db                              # 注入临时库，别碰真实 pipeline.db
    try:
        rows = [("/x/UMI_9_29", 7), ("/x/UMI_9_29", 8), ("/x/UMI_9_29/", 9),
                ("/x/other", 1)]
        for i, (path, ep) in enumerate(rows):
            rr.RecordingRepo.save(RecordingRecord(
                id=f"r{i}", camera_index=0, camera_name="cam",
                file_path=path, episode_index=ep, file_size_mb=1.0,
                duration_sec=1.0, resolution_w=1, resolution_h=1,
                status="completed", started_at="t0", finished_at="t1"))
        check(len(rr.RecordingRepo.list_all()) == 4, "造 4 行（两组同目录）")
        check(rr.RecordingRepo.delete_local_episode("/x/UMI_9_29",
                                                    episode_index=7) == 1,
              "delete_local_episode 精确删 1 行")
        check(len(rr.RecordingRepo.list_all()) == 3, "只剩 3 行")
        check(rr.RecordingRepo.delete_local_dir("/x/UMI_9_29/") == 2,
              "delete_local_dir 归一化尾斜杠后删 2 行")
        left = [r.file_path for r in rr.RecordingRepo.list_all()]
        check(left == ["/x/other"], f"别的任务的行没被牵连: {left}")
        check(rr.RecordingRepo.delete_local_dir("") == 0, "空路径删 0 行")
    finally:
        db.close()
        rr.db = None


def main():
    root = tempfile.mkdtemp(prefix="local_delete_")
    try:
        test_pooled_true_delete(root)
        test_honest_failure(root)
        test_legacy_dir(root)
        test_repo_true_delete(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: 真删除（文件 + 录制行）测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
