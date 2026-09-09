"""上传链路硬化单测 —— 不联网、不起真实上传线程。

覆盖：
  1. 传输层：_UPLOAD_RETRY 只放行建连重试、socket_options 注入到连接池、
     _PatientSendConnection.connect 用连接超时且退出后恢复原值
  2. 会话名匹配/快照差集/时间窗判定（session_name_base / session_matches /
     pick_new_session / pick_session_in_window）
  3. resume_pending：跳过条件、已入库判定（一对一分配、只认 POST 已开始的）
  4. _local_episode_exists / sweep_orphan_temp_files / inflight / UploadTask

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_upload_hardening.py
"""
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

import core.uploader as up
from core.uploader import (
    UploadManager, UploadTask, session_name_base, session_matches,
    pick_new_session, pick_session_in_window,
)
import core.api_client as api

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def _iso(dt):
    return dt.isoformat()


def _mk_task_dir(root, name, episodes=(1,)):
    """建一个池化任务目录（data/chunk-000/episode-NNN.parquet 即视为存在）。"""
    td = os.path.join(root, name)
    os.makedirs(os.path.join(td, "data", "chunk-000"), exist_ok=True)
    for n in episodes:
        open(os.path.join(td, "data", "chunk-000",
                          f"episode-{n - 1:03d}.parquet"), "w").close()
    return td


class FakeDb:
    """最小 DB 替身：真实 sqlite 内存库 + upload_task 建表。"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE upload_task (
                id TEXT PRIMARY KEY, session_path TEXT NOT NULL,
                session_name TEXT NOT NULL, episode_index INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending', progress REAL DEFAULT 0.0,
                retry_count INTEGER DEFAULT 0, server_url TEXT NOT NULL,
                server_session_id TEXT DEFAULT '', error_message TEXT DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def add(self, tid, path, ep, status, created_at, updated_at,
            server_url="http://srv:8000"):
        self.conn.execute(
            "INSERT INTO upload_task (id, session_path, session_name, "
            "episode_index, status, server_url, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, path, os.path.basename(path), ep, status, server_url,
             created_at, updated_at))
        self.conn.commit()


def main():
    app = QApplication(sys.argv)   # noqa: F841 — Qt 对象需要
    now = datetime.now()

    # ── 1. 传输层 ──────────────────────────────────────
    r = api._UPLOAD_RETRY
    check(r.allowed_methods == {"POST"}, "重试放行 POST")
    check((r.total, r.connect, r.read, r.status) == (2, 2, 0, 0),
          "重试参数 total=2/connect=2/read=0/status=0（发送/读阶段不自动重发）")
    check(r.respect_retry_after_header is False and r.raise_on_status is False,
          "不因 Retry-After 重试、状态码交回调用方")

    ad = api._PatientSendAdapter(max_retries=api._UPLOAD_RETRY)
    pool = ad.poolmanager.connection_from_url("http://127.0.0.1:9")
    opts = pool.conn_kw.get("socket_options") or []
    check((1, 9, 1) in opts or any(o[0] == 1 and o[2] == 1 for o in opts),
          "连接池注入 SO_KEEPALIVE")
    import socket as _s
    if getattr(_s, "TCP_KEEPIDLE", None) is not None:
        check(any(o[1] == _s.TCP_KEEPIDLE for o in opts),
              "Linux 注入 TCP_KEEPIDLE（死连接 2 分钟内报错）")
    check(all(len(o) == 3 for o in opts), "socket_options 均为三元组")

    # connect() 用连接超时、退出恢复：替换基类 connect 记录当时的 self.timeout
    import urllib3
    conn = api._PatientSendConnection("127.0.0.1", timeout=api.READ_TIMEOUT)
    seen = {}
    orig_connect = urllib3.connection.HTTPConnection.connect

    def _fake_connect(self):
        seen["timeout"] = self.timeout
        raise OSError("stop here")
    urllib3.connection.HTTPConnection.connect = _fake_connect
    try:
        try:
            conn.connect()
        except OSError:
            pass
    finally:
        urllib3.connection.HTTPConnection.connect = orig_connect
    check(seen.get("timeout") == api.CONNECT_TIMEOUT,
          "connect() 建连阶段用 CONNECT_TIMEOUT（不是 30 分钟读窗口）")
    check(conn.timeout == api.READ_TIMEOUT, "connect() 退出后恢复原 timeout")

    # ── 2. 会话名匹配 ──────────────────────────────────
    check(session_name_base("UMIGripper_AI") == "UMIGripper_AI", "基名：无后缀")
    check(session_name_base("UMIGripper_AI_000012") == "UMIGripper_AI",
          "基名：_000012")
    check(session_name_base("UMIGripper_AI_ep000012") == "UMIGripper_AI",
          "基名：_ep000012")
    check(session_name_base("UMIGripper_AI_episode-12") == "UMIGripper_AI",
          "基名：_episode-12")
    check(session_name_base("UMIGripper_AI.zip") == "UMIGripper_AI", "基名：.zip")
    check(session_name_base("") == "", "基名：空串")

    check(session_matches("UMIGripper_AI", {"name": "UMIGripper_AI_000002"}),
          "匹配：服务器加序号后缀")
    check(session_matches("UMIGripper_AI", {"name": "UMIGripper_AI"}),
          "匹配：同名")
    check(not session_matches("UMIGripper_AI", {"name": "OtherTask_000001"}),
          "不匹配：别的任务")
    check(not session_matches("UMIGripper_AI", {"name": ""}), "不匹配：空名")

    # ── 3. 快照差集 ────────────────────────────────────
    before = {"a", "b"}
    s_new = {"id": "c", "name": "UMIGripper_AI_000002", "created_at": _iso(now)}
    s_old = {"id": "a", "name": "UMIGripper_AI_000001", "created_at": _iso(now)}
    check(pick_new_session(before, [s_old, s_new], "UMIGripper_AI") is s_new,
          "差集：新出现的同名会话命中")
    check(pick_new_session(before, [s_old], "UMIGripper_AI") is None,
          "差集：没有新会话 → None")
    check(pick_new_session(before, [{"id": "c", "name": "Other",
                                     "created_at": _iso(now)}],
                           "UMIGripper_AI") is None, "差集：新会话名字不符 → None")
    s_newer = {"id": "d", "name": "UMIGripper_AI_000003",
               "created_at": _iso(now + timedelta(minutes=1))}
    check(pick_new_session(before, [s_new, s_newer], "UMIGripper_AI") is s_newer,
          "差集：多条取最新")

    # ── 4. 时间窗判定 ──────────────────────────────────
    anchor = now - timedelta(minutes=10)
    inside = {"id": "x", "name": "UMIGripper_AI_000009",
              "created_at": _iso(now - timedelta(minutes=9))}
    too_early = {"id": "y", "name": "UMIGripper_AI_000008",
                 "created_at": _iso(now - timedelta(minutes=30))}
    check(pick_session_in_window([inside], "UMIGripper_AI", _iso(anchor)) is inside,
          "窗口：锚点之后的会话命中")
    check(pick_session_in_window([too_early], "UMIGripper_AI", _iso(anchor))
          is None, "窗口：早于锚点太多 → None")
    check(pick_session_in_window([inside], "UMIGripper_AI", _iso(anchor),
                                 _iso(now - timedelta(minutes=9, seconds=30)))
          is None, "窗口：晚于上界 → None（防串到下一条的会话）")
    check(pick_session_in_window([inside], "UMIGripper_AI", "") is None,
          "窗口：锚点缺失 → None")
    check(pick_session_in_window(
        [{"id": "z", "name": "UMIGripper_AI_000007", "created_at": "bad"}],
        "UMIGripper_AI", _iso(anchor)) is None, "窗口：created_at 不可解析 → None")

    # ── 5. resume_pending ──────────────────────────────
    with tempfile.TemporaryDirectory() as root:
        pA = _mk_task_dir(root, "TaskA", (1, 2))
        pB = _mk_task_dir(root, "TaskB", (1,))
        pF = _mk_task_dir(root, "TaskF", (1,))
        pDone = _mk_task_dir(root, "TaskDone", (1,))
        fake = FakeDb()
        up.db = fake
        m = UploadManager("http://srv:8000")
        t_old = _iso(now - timedelta(hours=48))
        fake.add("r-old", pB, 1, "pending", t_old, t_old)          # 超窗口
        fake.add("r1", pA, 1, "uploading",
                 _iso(now - timedelta(hours=1)), _iso(now - timedelta(minutes=10)))
        fake.add("r2", pA, 2, "uploading",
                 _iso(now - timedelta(hours=1)), _iso(now - timedelta(minutes=5)))
        fake.add("r3", pB, 1, "pending",
                 _iso(now - timedelta(minutes=30)), _iso(now - timedelta(minutes=30)))
        fake.add("r4", os.path.join(root, "Gone"), 1, "pending",
                 _iso(now - timedelta(minutes=30)), _iso(now - timedelta(minutes=30)))
        fake.add("r5", _mk_task_dir(root, "TaskSrv"), 1, "pending",
                 _iso(now - timedelta(minutes=30)), _iso(now - timedelta(minutes=30)),
                 server_url="http://other:9000")
        fake.add("r6", pDone, 1, "pending",
                 _iso(now - timedelta(minutes=30)), _iso(now - timedelta(minutes=30)))
        fake.add("r6b", pDone, 1, "completed",
                 _iso(now - timedelta(minutes=20)), _iso(now - timedelta(minutes=20)))
        fake.add("r7", pF, 1, "pending",
                 _iso(now - timedelta(minutes=30)), _iso(now - timedelta(minutes=30)))
        # r7 已在队列中
        m.add_task(pF, 1)

        srv_hit = {"id": "TaskA_000001", "name": "TaskA_000001",
                   "created_at": _iso(now - timedelta(minutes=9))}
        m._resume_sessions_snapshot = lambda: [srv_hit]
        sigs_pre = []
        m.task_completed.connect(lambda tid: sigs_pre.append(tid))
        resumed, verified, skipped = m.resume_pending(max_age_hours=24)
        check(sigs_pre == [],
              "续传：verified 行不发完成信号（task_id 尚未登记，收尾交调用方）")

        vmap = {v[1:3]: v[3] for v in verified}
        rmap = {v[1:3] for v in resumed}
        smap = {(s["path"], s["episode_index"]): s["reason"] for s in skipped}

        check((pA, 1) in vmap and vmap[(pA, 1)] == "TaskA_000001",
              "续传：POST 已开始且服务器已入库 → 按成功收尾")
        check((pA, 2) in rmap, "续传：窗口外的同任务行照常重传")
        check((pB, 1) in rmap, "续传：pending 行照常重传")
        check(smap.get((pB, 1)) is None, "续传：超窗口的行不被选中")
        check(smap.get((os.path.join(root, "Gone"), 1)) == "missing_files",
              "跳过：本地文件不存在")
        check(smap.get((os.path.join(root, "TaskSrv"), 1)) == "server_changed",
              "跳过：服务器地址变更")
        check(smap.get((pDone, 1)) == "already_done", "跳过：已有完成记录")
        check(smap.get((pF, 1)) == "already_queued", "跳过：已在队列中")
        # 复用的 id 必须是 DB 行 id（续传后同一行被更新）
        check(any(t[0] == "r2" for t in resumed), "续传：复用 DB 行 id")
        rows = {r["id"]: r for r in fake.conn.execute(
            "SELECT * FROM upload_task").fetchall()}
        check(rows["r1"]["status"] == "completed", "已入库行落库为 completed")
        check(rows["r1"]["server_session_id"] == "TaskA_000001",
              "已入库行记录 server_session_id")

        # 快照查询失败 → 不做预判，全部照常重传
        fake2 = FakeDb()
        up.db = fake2
        m2 = UploadManager("http://srv:8000")
        fake2.add("q1", pA, 1, "uploading",
                  _iso(now - timedelta(hours=1)), _iso(now - timedelta(minutes=10)))
        m2._resume_sessions_snapshot = lambda: None
        resumed2, verified2, _ = m2.resume_pending()
        check(not verified2 and len(resumed2) == 1,
              "快照失败：不做已入库判定，照常重传")

        # ── 6. 本地文件存在性 ──────────────────────────
        check(UploadManager._local_episode_exists(pA, 1), "存在性：episode 1 在")
        check(not UploadManager._local_episode_exists(pA, 9),
              "存在性：episode 9 不在")
        check(not UploadManager._local_episode_exists(
            os.path.join(root, "Gone"), 1), "存在性：目录不存在 → False")

        # ── 7. 残留临时文件清扫 ────────────────────────
        def _touch(name, age_h):
            p = os.path.join(root, name)
            open(p, "w").close()
            ts = time.time() - age_h * 3600
            os.utime(p, (ts, ts))
            return p
        stale = [_touch("_TaskA_ep000012_upload_abc123.zip", 25),
                 _touch("_episodes_abc_1.parquet", 25),
                 _touch("_precomp_abc_0_cam.mp4", 25)]
        fresh = _touch("_TaskA_ep000013_upload_def456.zip", 0.1)
        keep = _touch("episode-000.parquet", 25)
        n = UploadManager.sweep_orphan_temp_files(root, max_age_hours=24)
        check(n == 3, f"清扫：删除 3 个过期临时文件（实得 {n}）")
        check(all(not os.path.exists(p) for p in stale), "清扫：过期临时文件已删")
        check(os.path.exists(fresh), "清扫：未过期的临时文件保留")
        check(os.path.exists(keep), "清扫：非临时文件保留")

        # ── 8. inflight ────────────────────────────────
        m3 = UploadManager("http://srv:8000")
        m3.add_task(pA, 1)
        m3.add_task(pA, 2)
        with up.QMutexLocker(m3._mutex):
            t1 = m3._queue.pop(0)
            m3._active[t1.id] = None
            m3._active_tasks[t1.id] = t1
            m3._started_at[t1.id] = time.monotonic() - 5
        st = m3.inflight()
        check(st[(pA, 1)]["state"] == "active", "inflight：执行中 → active")
        check(st[(pA, 2)]["state"] == "queued", "inflight：排队中 → queued")
        check(st[(pA, 1)]["elapsed"] >= 4.9, "inflight：elapsed 从开始时刻算")

        # ── 9. 失败后复查 / 按成功收尾 ──────────────────
        class FakeClient:
            def __init__(self, sessions):
                self._sessions = sessions

            def try_get_sessions(self, limit=200):
                return self._sessions

        t = UploadTask(pA, "http://srv:8000", 1)
        check(m._verify_after_failure(FakeClient(None), t, set(), True) is None,
              "复查：查询失败 → None（宁可重试）")
        check(m._verify_after_failure(FakeClient([]), t, set(), False) is None,
              "复查：无上传前基线 → None（区分不了新旧）")
        s_hit = {"id": "TaskA_000005", "name": "TaskA_000005",
                 "created_at": _iso(now)}
        check(m._verify_after_failure(FakeClient([s_hit]), t, set(), True)
              is s_hit, "复查：新出现的同名会话 → 判定已入库")
        check(m._verify_after_failure(FakeClient([s_hit]), t, {"TaskA_000005"},
                                      True) is None, "复查：旧会话不算数")

        up.db = fake          # 上面"快照失败"子用例换过 db，这里换回来
        fake.add("rv", pB, 2, "uploading", _iso(now), _iso(now))
        tv = UploadTask(pB, "http://srv:8000", 2, task_id="rv")
        m._mark_upload_verified(tv, s_hit, "单测")
        row = fake.conn.execute(
            "SELECT * FROM upload_task WHERE id='rv'").fetchone()
        check(row["status"] == "completed" and row["progress"] == 1.0,
              "_mark_upload_verified：落库 completed")
        check(row["server_session_id"] == "TaskA_000005",
              "_mark_upload_verified：记录会话 id")

        # emit=False（resume_pending 用）：只落库、不发完成信号，收尾交给调用方
        sigs = []
        m.task_completed.connect(lambda tid: sigs.append(tid))
        fake.add("rv2", pB, 3, "uploading", _iso(now), _iso(now))
        tv2 = UploadTask(pB, "http://srv:8000", 3, task_id="rv2")
        m._mark_upload_verified(tv2, s_hit, "单测", emit=False)
        check("rv2" not in sigs, "_mark_upload_verified(emit=False)：不发 task_completed")
        row2 = fake.conn.execute(
            "SELECT * FROM upload_task WHERE id='rv2'").fetchone()
        check(row2["status"] == "completed", "_mark_upload_verified(emit=False)：仍落库 completed")
        m._mark_upload_verified(tv2, s_hit, "单测", emit=True)
        check(sigs == ["rv2"], "_mark_upload_verified(emit=True)：发 task_completed")

        # ── 10. UploadTask 往返 ────────────────────────
        t = UploadTask.from_row({"id": "z1", "session_path": pA,
                                 "episode_index": 1})
        check(t.resumed is False, "from_row：缺 resumed → False")
        t2 = UploadTask.from_row({"id": "z2", "session_path": pA,
                                  "episode_index": 1, "resumed": 1})
        check(t2.resumed is True, "from_row：resumed 强转 bool")
        check("resumed" in t2.to_dict(), "to_dict 含 resumed")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: upload_hardening 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
