"""采集端自建任务（本机任务）测试 —— 离屏 Qt，全部落在临时目录。

覆盖：
  1. create_local_task 字段 / id 前缀 / source / created_by / 进度初值
  2. 名称校验：空、超长、非法字符（空格/点/斜杠/制表）、Windows 保留名、
     大小写不敏感重名（含 hidden 墓碑与种子任务）
  3. 目标段数边界（1~100000，bool/字符串拒绝）
  4. 仅创建者可见：filter_by_identity / load_tasks / 后端轮询不清除本机任务
  5. 进度：increment 后状态流转；pending_sync_tasks 不含本机任务
  6. 改名：无数据可改、重名/非法/已有数据/新目录已存在 拒绝、非本机任务拒绝
  7. 删除：remove_local_task 整条移除（无 hidden 墓碑）；服务端任务返回 False
  8. 任务页集成：5 列 / 来源列 / 新建后选中 / 改名 / 本机任务删除 / 语言切换
  9. 对话框实时校验：非法名称禁用、段数留空禁用、已有数据时改名禁用

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_task_create.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from config import settings
import core.task_record as task_record
from core.task_record import (
    TaskValidationError, create_local_task, filter_by_identity, load_tasks,
    merge_backend_tasks, pending_sync_tasks, remove_local_task,
    rename_local_task, validate_task_name,
)
import ui.task_page as task_page_mod
from ui.task_create_dialog import TaskCreateDialog, TaskRenameDialog

FAILS = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        FAILS.append(msg)


ROOT = ""
TASKS_FILE = ""
REC_DIR = ""


def setup():
    global ROOT, TASKS_FILE, REC_DIR
    ROOT = tempfile.mkdtemp(prefix="task_create_")
    data_dir = os.path.join(ROOT, "data")
    REC_DIR = os.path.join(ROOT, "recordings")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(REC_DIR, exist_ok=True)
    TASKS_FILE = os.path.join(data_dir, "tasks.json")
    task_record._TASKS_FILE = TASKS_FILE
    settings.RECORDING_DIR = REC_DIR
    task_record._write({"tasks": [], "updated_at": "2026-10-15T00:00:00"})


def raw_tasks():
    with open(TASKS_FILE, encoding="utf-8") as f:
        return json.load(f)["tasks"]


def reset_tasks(tasks=None):
    task_record._write({"tasks": list(tasks or []),
                        "updated_at": "2026-10-15T00:00:00"})


def expect_error(fn, code, label):
    try:
        fn()
    except TaskValidationError as e:
        check(e.code == code, f"{label} → {code}（实际 {e.code}）")
        return
    check(False, f"{label} 应抛 TaskValidationError({code})")


def make_local(name="Task_A", total=10, creator="guest", desc=""):
    return create_local_task(name, total, desc, created_by=creator)


class FakeMB:
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


def test_create():
    print("── 1. 创建本机任务：字段 / 可见性初值 ──")
    t = make_local("Grasp_Cup-01", 20, "alice", "右手抓取")
    check(t["id"].startswith(task_record.LOCAL_TASK_PREFIX), "id 带 local- 前缀")
    check(t["source"] == "local", "source=local")
    check(t["created_by"] == "alice", "created_by 记创建身份")
    check(t["assigned_user"] is None, "assigned_user=None（不算指派任务）")
    check(t["total_required"] == 20, "目标段数写入")
    check(t["local_count"] == t["synced_count"] == t["backend_count"] == 0,
          "进度三字段显式写 0（不触发目录回填）")
    check(t["completed_count"] == 0 and t["status"] == "pending",
          "初始 0 段 / 待采集")
    check(t["description"] == "右手抓取", "描述写入")
    try:
        datetime.fromisoformat(t["assigned_at"].replace("Z", "+00:00"))
        ok = True
    except ValueError:
        ok = False
    check(ok, "assigned_at 是合法 ISO 时间")
    persisted = [x for x in raw_tasks() if x["id"] == t["id"]]
    check(len(persisted) == 1 and persisted[0]["source"] == "local",
          "已落盘 tasks.json")


def test_name_validation():
    print("── 2. 名称校验：拒绝非法字符 / 重名 / 保留名 ──")
    check(validate_task_name("") == (False, "name_empty"), "空名拒绝")
    check(validate_task_name("   ") == (False, "name_empty"), "纯空白拒绝")
    check(validate_task_name("a" * (task_record.MAX_TASK_NAME_LEN + 1))[1]
          == "name_too_long", "超长拒绝")
    for bad in ("a b", "a.b", "a/b", "a\\b", "a\tb", "任务 名", "a:b"):
        check(validate_task_name(bad)[1] == "name_illegal",
              f"非法字符 {bad!r} 拒绝")
    check(validate_task_name("Grasp_Cup-02")[0], "合法名（汉字/下划线/连字符）通过")
    check(validate_task_name("抓取_圆柱-右侧")[0], "中文名通过")
    # 不变量：合法名经过 helpers.task_tag 清洗后必须原样不变
    # （任务名 = 录制目录名 = 上传会话/项目名）
    from core.helpers import task_tag
    for name in ("Grasp_Cup-02", "抓取_圆柱-右侧", "_x-1", "a" * 64):
        check(task_tag(name) == name, f"task_tag({name!r}) 不改变合法名")
    for reserved in ("CON", "con", "PRN", "NUL", "COM1", "LPT9"):
        check(validate_task_name(reserved)[1] == "name_reserved",
              f"Windows 保留名 {reserved} 拒绝")
    check(validate_task_name("grasp_cup-01")[1] == "name_duplicate",
          "大小写不敏感重名拒绝")
    check(validate_task_name(" Grasp_Cup-01 ")[1] == "name_duplicate",
          "首尾空白先 strip 再判重")
    reset_tasks([{"id": "hid-1", "name": "Hidden_Task", "total_required": 1,
                  "hidden": True}])
    check(validate_task_name("Hidden_Task")[1] == "name_duplicate",
          "hidden 墓碑重名也拒绝")
    reset_tasks([])
    expect_error(lambda: make_local("bad name"), "name_illegal",
                 "create_local_task 拒绝非法字符")
    expect_error(lambda: make_local("CON"), "name_reserved",
                 "create_local_task 拒绝保留名")
    check(len(raw_tasks()) == 0, "被拒绝的创建不落盘")


def test_total_validation():
    print("── 3. 目标段数边界 ──")
    for bad in (0, -1, task_record.MAX_TOTAL_REQUIRED + 1, True, "5", 1.5):
        expect_error(lambda b=bad: create_local_task("Total_Test", b),
                     "total_invalid", f"{bad!r} 拒绝")
    t1 = create_local_task("Total_Min", 1)
    t2 = create_local_task("Total_Max", task_record.MAX_TOTAL_REQUIRED)
    check(t1["total_required"] == 1 and t2["total_required"] == 100000,
          "1 与 100000 均可建")
    expect_error(
        lambda: create_local_task("Total_Desc", 1, "x" * 501),
        "description_too_long", "描述超长拒绝")
    reset_tasks([])


def test_visibility_and_merge():
    print("── 4. 仅创建者可见 + 后端轮询不清除本机任务 ──")
    reset_tasks([])
    g = make_local("Guest_Task", 5, "guest")
    a = make_local("Alice_Task", 5, "alice")
    b = make_local("Bob_Task", 5, "bob")

    names = lambda ts: {t["name"] for t in ts}
    check(names(filter_by_identity(load_tasks(), "guest")) == {"Guest_Task"},
          "游客只看到游客建的本机任务")
    check(names(filter_by_identity(load_tasks(), "alice")) == {"Alice_Task"},
          "alice 只看到自己建的本机任务")
    check(names(filter_by_identity(load_tasks(), "bob")) == {"Bob_Task"},
          "bob 只看到自己建的本机任务")
    check(names(filter_by_identity(load_tasks(), None)) ==
          {"Guest_Task", "Alice_Task", "Bob_Task"},
          "identity=None 返回全部（内部统计用）")

    # 后端推送空列表 + 账号身份轮询：不能把本机任务当「已撤销」删掉
    merged = merge_backend_tasks([], view_scope="alice")
    check({t["name"] for t in merged} ==
          {"Guest_Task", "Alice_Task", "Bob_Task"},
          "账号轮询空列表不清除本机任务")
    check(len(raw_tasks()) == 3, "tasks.json 里三条都还在")

    # 后端推送正常任务：本机任务保留，且进度字段不被改写
    task_record.increment_task_completed("Alice_Task", task_id=a["id"])
    server = {"id": "srv-1", "name": "Server_Task", "total_required": 3,
              "completed_count": 1, "status": "active"}
    merged = merge_backend_tasks([server], view_scope="alice")
    got = {t["name"]: t for t in merged}
    check("Server_Task" in got, "后端任务合并进来")
    check(got["Alice_Task"]["local_count"] == 1,
          "本机任务计数在合并后保留")
    check(got["Alice_Task"]["source"] == "local", "source 标记保留")

    # 无 created_by 的手工数据：对所有人可见（兼容旧数据）
    reset_tasks([{"id": "local-legacy", "name": "Legacy_Task",
                  "total_required": 1, "source": "local"}])
    check(names(filter_by_identity(load_tasks(), "guest")) == {"Legacy_Task"},
          "created_by 缺失视为无主，所有人可见")


def test_progress_and_sync():
    print("── 5. 本机任务进度 + 不上报 ──")
    reset_tasks([])
    t = make_local("Prog_Task", 3, "guest")
    for _ in range(2):
        task_record.increment_task_completed("Prog_Task", task_id=t["id"])
    got = task_record.task_by_id(t["id"])
    check(got["completed_count"] == 2 and got["status"] == "in_progress",
          "录 2 段后进度 2/3 采集中")
    check(got["local_count"] == 2 and got["synced_count"] == 0,
          "进度只记 local_count")
    task_record.increment_task_completed("Prog_Task", task_id=t["id"])
    got = task_record.task_by_id(t["id"])
    check(got["completed_count"] == 3 and got["status"] == "completed",
          "满 3 段后采集完成")
    pending = pending_sync_tasks()
    check(all(p["id"] != t["id"] for p in pending),
          "pending_sync_tasks 不含本机任务（不向服务器上报）")


def test_rename():
    print("── 6. 改名 ──")
    reset_tasks([])
    t = make_local("Old_Name", 4, "alice")
    task_record.increment_task_completed("Old_Name", task_id=t["id"])
    renamed = rename_local_task(t["id"], "New_Name")
    check(renamed["name"] == "New_Name" and renamed["id"] == t["id"],
          "改名成功且 id 不变")
    check(task_record.task_by_id(t["id"])["local_count"] == 1,
          "改名保留进度")
    check(validate_task_name("Old_Name")[0], "旧名释放，可再次使用")

    expect_error(lambda: rename_local_task("local-does-not-exist", "Whatever"),
                 "task_not_found", "不存在的任务")
    srv = {"id": "srv-r", "name": "Server_R", "total_required": 2}
    reset_tasks([srv, {**renamed}])
    expect_error(lambda: rename_local_task("srv-r", "Server_R2"),
                 "not_local", "服务端任务不可改名")
    expect_error(lambda: rename_local_task(t["id"], "bad name"),
                 "name_illegal", "新名非法字符拒绝")
    expect_error(lambda: rename_local_task(t["id"], "Server_R"),
                 "name_duplicate", "新名与已有任务重名拒绝")

    # 已有录制目录：默认拒绝（renamed 当前名 New_Name，目录已存在）
    os.makedirs(os.path.join(REC_DIR, "New_Name"), exist_ok=True)
    expect_error(lambda: rename_local_task(t["id"], "Another_Name"),
                 "has_data", "已有录制目录拒绝改名")
    # 新名字的目录已存在（孤儿目录）：拒绝
    reset_tasks([{**renamed, "name": "No_Data_Task",
                  "local_count": 0, "synced_count": 0,
                  "backend_count": 0, "completed_count": 0}])
    os.makedirs(os.path.join(REC_DIR, "Occupied"), exist_ok=True)
    expect_error(lambda: rename_local_task(t["id"], "Occupied"),
                 "name_dir_exists", "新名字目录已存在拒绝")


def test_remove():
    print("── 7. 删除本机任务 ──")
    reset_tasks([])
    t = make_local("Remove_Me", 2, "guest")
    check(remove_local_task(t["id"]) is True, "本机任务整条移除")
    check(all(x["id"] != t["id"] for x in raw_tasks()), "tasks.json 不再有该条")
    check(not any(x.get("hidden") for x in raw_tasks()),
          "不写 hidden 墓碑（本机任务不需要防复活）")
    srv = {"id": "srv-del", "name": "Server_Del", "total_required": 1}
    reset_tasks([srv])
    check(remove_local_task("srv-del") is False, "服务端任务不走本机删除")


class FakeCreateDialog:
    name_value = "Page_New_Task"
    total_value = 3

    def __init__(self, *a, **k):
        pass

    def exec_(self):
        return QDialog.Accepted

    def name(self):
        return FakeCreateDialog.name_value

    def total_required(self):
        return FakeCreateDialog.total_value

    def description(self):
        return "from page"


class FakeRenameDialog:
    new_name_value = "Page_Renamed"

    def __init__(self, parent, task):
        self._task = task

    def exec_(self):
        return QDialog.Accepted

    def new_name(self):
        return FakeRenameDialog.new_name_value


def test_page():
    print("── 8. 任务页集成：5 列 / 来源 / 新建 / 改名 / 删除 ──")
    reset_tasks([])
    from config.i18n import tr
    real_mb = task_page_mod.QMessageBox
    real_create = task_page_mod.TaskCreateDialog
    real_rename = task_page_mod.TaskRenameDialog
    task_page_mod.QMessageBox = FakeMB
    try:
        page = task_page_mod.TaskSelectionPage()
        check(page._table.columnCount() == 5, "任务表 5 列")
        check(page._table.horizontalHeaderItem(1).text() == tr("来源"),
              "第 2 列是来源")
        check(page._table.rowCount() == 0, "空任务列表")

        task_page_mod.TaskCreateDialog = FakeCreateDialog
        page._on_new_task_clicked()
        check(page._table.rowCount() == 1, "新建后列表出现一行")
        check(page._table.item(0, task_page_mod._COL_NAME).text()
              == "Page_New_Task", "名称正确")
        check(page._table.item(0, task_page_mod._COL_SOURCE).text() == tr("本机"),
              "来源列显示本机")
        check(page._table.currentRow() == 0, "自动选中新任务")
        check(page._rename_btn.isEnabled(), "本机任务可点改名")
        task = page._tasks[0]
        check(task["description"] == "from page", "描述写入")

        task_page_mod.TaskRenameDialog = FakeRenameDialog
        page._on_rename_clicked()
        check(page._tasks[0]["name"] == "Page_Renamed", "页面改名生效")
        check(page._table.item(0, task_page_mod._COL_NAME).text()
              == "Page_Renamed", "表格行名称同步")

        # 删除本机任务：不进 DB（无录制目录），记录整条移除
        import core.recording_repository as rr
        real_delete_dir = rr.RecordingRepo.delete_local_dir
        rr.RecordingRepo.delete_local_dir = staticmethod(lambda path: 0)
        try:
            page._table.setCurrentCell(0, 0)
            page._do_delete_task()
        finally:
            rr.RecordingRepo.delete_local_dir = real_delete_dir
        check(page._table.rowCount() == 0, "删除后列表为空")
        check(raw_tasks() == [], "tasks.json 里整条移除（无墓碑）")
        check(any(k == "question" for k, _ in FakeMB.calls), "删除前二次确认")

        # 服务端任务不显示改名可用
        reset_tasks([{"id": "srv-p", "name": "Server_Page",
                      "total_required": 2, "completed_count": 0}])
        page2 = task_page_mod.TaskSelectionPage()
        page2._table.setCurrentCell(0, 0)
        check(not page2._rename_btn.isEnabled(), "服务端任务改名按钮禁用")
        FakeMB.calls.clear()
        page2._on_rename_clicked()
        check(any(k == "information" for k, _ in FakeMB.calls),
              "服务端任务改名给提示")

        # 语言切换：5 列表头与来源文案同步（信号路径）
        from config.i18n import lang_manager
        lang_manager.set_language("zh")
        check(page2._table.horizontalHeaderItem(1).text() == "来源"
              and page2._table.item(0, task_page_mod._COL_SOURCE).text() == "服务端",
              "切中文后来源表头/文案同步")
        lang_manager.set_language("en")
        check(page2._table.horizontalHeaderItem(1).text() == "Source"
              and page2._table.item(0, task_page_mod._COL_SOURCE).text() == "Server",
              "切英文后来源表头/文案同步")
    finally:
        task_page_mod.QMessageBox = real_mb
        task_page_mod.TaskCreateDialog = real_create
        task_page_mod.TaskRenameDialog = real_rename


def test_dialogs():
    print("── 9. 对话框实时校验 ──")
    reset_tasks([])
    dlg = TaskCreateDialog(identity="alice")
    check(dlg.total_required() == 1, "目标段数默认 1")
    dlg._name_edit.setText("bad name")
    check(not dlg._ok_btn.isEnabled() and bool(dlg._error_label.text())
          and not dlg._error_label.isHidden(),
          "非法名称：创建按钮禁用并显示错误")
    dlg._name_edit.setText("good_name")
    check(dlg._ok_btn.isEnabled(), "合法名称：创建按钮可用")
    dlg._total_edit.setText("")
    check(not dlg._ok_btn.isEnabled(), "目标段数留空：创建按钮禁用")
    dlg._total_edit.setText("1")
    check(dlg._ok_btn.isEnabled(), "目标段数补回后按钮恢复")
    dlg._name_edit.setText("good name")
    dlg.accept()
    check(dlg.result() != QDialog.Accepted, "accept 时再次拦截非法名称")

    task = make_local("Rename_Src", 2, "alice")
    rdlg = TaskRenameDialog(task=task)
    rdlg._name_edit.setText("Rename_Dst")
    check(rdlg._ok_btn.isEnabled(), "改名对话框合法新名可用")
    os.makedirs(os.path.join(REC_DIR, "Rename_Src"), exist_ok=True)
    rdlg2 = TaskRenameDialog(task=task)
    check(not rdlg2._ok_btn.isEnabled(), "已有录制目录时改名按钮禁用")
    rdlg2.accept()
    check(rdlg2.result() != QDialog.Accepted, "已有数据时 accept 也不放行")


def main():
    app = QApplication(sys.argv)  # noqa: F841 — Qt 控件需要
    setup()
    try:
        test_create()
        test_name_validation()
        test_total_validation()
        test_visibility_and_merge()
        test_progress_and_sync()
        test_rename()
        test_remove()
        test_page()
        test_dialogs()
    finally:
        import shutil
        shutil.rmtree(ROOT, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: 本机任务测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
