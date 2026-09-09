"""端到端验证：走真实 resume_pending → 打包 → POST → 服务器复查。

⚠ 会真的上传数据到 server_config.json 里配置的服务器（不是单测）。
只上传 DB 里 pending/uploading 的遗留任务，没有遗留任务时什么都不做。
不动 GUI，只驱动 UploadManager（与主程序启动续传同一条代码路径）。

用法:
    venv/bin/python tools/tests/upload_e2e_resume.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.uploader import UploadManager
from core.task_service import TaskService
from config import settings


def main():
    url = settings.load_server_url()
    user, pwd = settings.load_credentials()
    ok, msg, cookies = TaskService.verify_credentials(url, user, pwd)
    print("login:", ok, msg)
    if not ok:
        return 1
    svc = TaskService(url)
    svc.adopt_login(url, user, cookies)

    m = UploadManager(url, session=svc._session)
    resumed, verified, skipped = m.resume_pending()
    print("resumed:", resumed)
    print("verified:", verified)
    print("skipped:", skipped)
    if not resumed and not verified:
        print("没有需要续传的任务")
        return 0

    done = {"n": 0}
    m.task_status.connect(lambda tid, msg: print(f"  [status {tid}] {msg}"))
    m.task_progress.connect(
        lambda tid, r: print(f"  [progress {tid}] {r*100:.1f}%"))
    m.task_completed.connect(lambda tid: (done.__setitem__("n", done["n"] + 1),
                                          print(f"  [completed {tid}]")))
    m.task_failed.connect(lambda tid, e: print(f"  [failed {tid}] {e}"))
    m.start()
    t0 = time.time()
    while not m.all_done() and time.time() - t0 < 900:
        time.sleep(1)
    print(f"all_done={m.all_done()} 用时 {time.time()-t0:.0f}s")

    from core.database import db
    for r in db.conn.execute(
            "SELECT id, episode_index, status, server_session_id, retry_count, "
            "error_message, updated_at FROM upload_task "
            "WHERE session_name LIKE 'UMIGripper%' ORDER BY created_at DESC "
            "LIMIT 3"):
        print(dict(r))
    return 0 if m.all_done() else 1


if __name__ == "__main__":
    sys.exit(main())
