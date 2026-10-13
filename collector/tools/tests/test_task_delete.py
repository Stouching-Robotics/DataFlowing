"""任务页「🗑 删除」= 真删除 测试（离屏 Qt，全部落在临时目录）。

2026-10-12：任务删除从「只往 tasks.json 写 hidden（目录原样留着）」改成
真删除——删掉该任务在本机的录制目录（含全部 episode 文件组）与库里的录制行，
tasks.json 只留 hidden 墓碑（防后端刷新复活）。删不干净就如实报错且**不隐藏**
任务（用户能重试）。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_task_delete.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QMessageBox

from config import settings
from core.database import Database
from core.egodata_writer import _atomic_write_parquet, _episode_rows_table
from core.helpers import pooled_data_parquet_path, pooled_episodes_path
from core.recording_record import RecordingRecord
import core.task_record as task_record
import ui.task_page as task_page_mod
from ui.task_page import TaskSelectionPage as TaskPage

FAILS = []
TASK_NAME = "Del_Test/1"
TASK_ID = "tid-del-1"


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        FAILS.append(msg)


class FakeMB:
    """QMessageBox 替身：记录弹窗文本，question 恒答 Yes。"""

    Yes, No = QMessageBox.Yes, QMessageBox.No
    calls = []

    @staticmethod
    def question(*a, **k):
        FakeMB.calls.append(("question", a[2] if len(a) > 2 else ""))
        return QMessageBox.Yes

    @staticmethod
    def warning(*a, **k):
        FakeMB.calls.append(("warning", a[2] if len(a) > 2 else ""))

    @staticmethod
    def information(*a, **k):
        FakeMB.calls.append(("information", a[2] if len(a) > 2 else ""))


def make_episode(task_dir: str, file_no: int):
    n = file_no + 1
    d = os.path.join(task_dir, "data", "chunk-000")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"episode-{file_no:03d}.parquet"), "wb") as f:
        f.write(b"data")
    v = os.path.join(task_dir, "videos", "chunk-000", "d435_rgb")
    os.makedirs(v, exist_ok=True)
    with open(os.path.join(v, f"episode-{file_no:03d}.mp4"), "wb") as f:
        f.write(b"video")
    ep_path = pooled_episodes_path(task_dir, n)
    os.makedirs(os.path.dirname(ep_path), exist_ok=True)
    _atomic_write_parquet(
        _episode_rows_table([{"episode_index": n, "task_name": TASK_NAME}]),
        ep_path)


def seed_db(db, task_dir):
    for i, ep in enumerate((1, 2)):
        db.conn.execute(
            """INSERT OR REPLACE INTO recording
               (id, camera_index, camera_name, file_path, episode_index,
                file_size_mb, duration_sec, resolution_w, resolution_h,
                status, started_at, finished_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"r{i}", 0, "cam", task_dir, ep, 1.0, 1.0, 1, 1,
             "completed", "t0", "t1"))
    db.conn.commit()


def write_tasks(tasks_file: str, hidden: bool = False, name: str = TASK_NAME):
    """写单任务 tasks.json（用例 2 要先把用例 1 的墓碑复原）。"""
    task = {"id": TASK_ID, "name": name, "status": "in_progress",
            "total_required": 10, "local_count": 2, "synced_count": 2,
            "backend_count": 0}
    if hidden:
        task["hidden"] = True
    with open(tasks_file, "w", encoding="utf-8") as f:
        json.dump({"tasks": [task], "updated_at": "2026-10-12T00:00:00"}, f)


def setup(root: str):
    """临时 tasks.json / RECORDING_DIR / DB 全部指到 root 下。"""
    data_dir = os.path.join(root, "data")
    rec_dir = os.path.join(root, "recordings")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(rec_dir, exist_ok=True)
    tasks_file = os.path.join(data_dir, "tasks.json")
    write_tasks(tasks_file)
    task_record._TASKS_FILE = tasks_file
    settings.RECORDING_DIR = rec_dir
    db = Database(db_path=os.path.join(root, "test.db"))
    db.init_schema()
    import core.recording_repository as rr
    rr.db = db
    return rec_dir, tasks_file, db


def hidden_in_file(tasks_file: str) -> bool:
    with open(tasks_file, encoding="utf-8") as f:
        tasks = json.load(f)["tasks"]
    return any(t.get("id") == TASK_ID and t.get("hidden") for t in tasks)


def main():
    root = tempfile.mkdtemp(prefix="task_delete_")
    app = QApplication(sys.argv)
    real_mb = task_page_mod.QMessageBox
    task_page_mod.QMessageBox = FakeMB
    db = None
    try:
        rec_dir, tasks_file, db = setup(root)
        task_dir = os.path.join(rec_dir, "Del_Test_1")
        make_episode(task_dir, 0)
        make_episode(task_dir, 1)
        seed_db(db, task_dir)

        print("── 1. 真删除任务：目录 + 录制行 + 墓碑 ──")
        page = TaskPage()
        page._table.setCurrentCell(0, 0)
        page._do_delete_task()
        check(not os.path.isdir(task_dir), "录制目录真的被删除")
        check(not os.path.exists(pooled_data_parquet_path(task_dir, 1)),
              "episode 文件组一并删除")
        check(len(db.conn.execute("SELECT 1 FROM recording").fetchall()) == 0,
              "该任务的录制行真删（不留痕迹行）")
        check(hidden_in_file(tasks_file), "tasks.json 保留 hidden 墓碑")
        check(all(t.get("id") != TASK_ID for t in page._tasks),
              "任务从列表消失")
        check(FakeMB.calls and FakeMB.calls[0][0] == "question",
              "删除前有二次确认")
        qtext = FakeMB.calls[0][1]
        check("Del_Test_1" in qtext and "2 个片段" in qtext,
              f"确认框写明要删的目录与片段数: {qtext!r}")

        print("── 2. 删不干净：如实报错且不隐藏任务 ──")
        FakeMB.calls.clear()
        write_tasks(tasks_file)           # 复原墓碑，让任务重新出现在列表
        task_dir2 = os.path.join(rec_dir, "Del_Test_1")
        make_episode(task_dir2, 0)
        os.chmod(rec_dir, 0o500)          # 根目录不可写 → rmtree 失败
        try:
            page2 = TaskPage()
            check(len(page2._tasks) == 1, "任务已复原到列表（用例前提）")
            page2._table.setCurrentCell(0, 0)
            page2._do_delete_task()
        finally:
            os.chmod(rec_dir, 0o755)
        check(os.path.isdir(task_dir2), "删除失败时目录还在（没装作删掉了）")
        check(any(k == "warning" for k, _ in FakeMB.calls),
              "失败有弹窗提示")
        check(not hidden_in_file(tasks_file),
              "失败时任务不隐藏（仍可重试）")

        print("── 3. 纯空白任务名：拒绝删除，绝不 rmtree 到 RECORDING_DIR ──")
        # _sanitize_tag("   ") 返回空串 → task_dir_of 退化成 RECORDING_DIR 本身，
        # 不拦就会把**所有任务**的录制一起删掉。
        FakeMB.calls.clear()
        write_tasks(tasks_file, name="   ")
        decoy = os.path.join(rec_dir, "Other_Task")
        make_episode(decoy, 0)
        page3 = TaskPage()
        check(len(page3._tasks) == 1, "空白名任务在列表里（用例前提）")
        page3._table.setCurrentCell(0, 0)
        page3._do_delete_task()
        check(os.path.isdir(decoy) and os.path.isdir(rec_dir),
              "录制根目录与别的任务原样都在（没被整根 rmtree）")
        check(any(k == "warning" and "拒绝删除" in str(t)
                  for k, t in FakeMB.calls), "拒绝删除有弹窗提示")
        check(not hidden_in_file(tasks_file), "拒绝时任务不隐藏")
    finally:
        task_page_mod.QMessageBox = real_mb
        if db is not None:
            db.close()
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: 任务删除真删除测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
