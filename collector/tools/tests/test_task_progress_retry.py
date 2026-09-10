"""进度上报「404 降级 → 冷却期 → 自动恢复」单测 —— 不联网、不开线程。

背景：老后端没有 /api/v1/device/tasks/progress，采集端收到 404 就永久降级
（整机不再上报进度）。服务端补上该端点后，若不重启采集端就永远恢复不了，
所以在 404 分支加了冷却期重探（PROGRESS_RETRY_INTERVAL_S）。

覆盖：
  1. 404 → 降级 + 排冷却期；冷却期内不再发任何请求
  2. 冷却期过后试探成功 → 自动恢复上报，并回写后端权威数
  3. 冷却期过后仍 404 → 继续降级、冷却期推后
  4. 正常成功路径不受影响（不会把 supported 弄反）

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_task_progress_retry.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from core import task_record
from core import task_service as ts

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


PENDING = [{"id": "task-aaa", "local_count": 12, "synced_count": 0}]
SYNCED = []


def _install_stubs():
    """task_record 走内存替身：只关心「谁在什么时候被回写了什么」。"""
    task_record.pending_sync_tasks = lambda: [dict(p) for p in PENDING]
    task_record.mark_synced = (
        lambda tid, sc, backend: SYNCED.append((tid, sc, backend)))


def _service(results):
    """建一个 TaskService，_post_progress 换成按队列出结果的桩。

    results: [(new_backend | None, err)] —— 每次 POST 消费一条。
    """
    svc = ts.TaskService("http://127.0.0.1:9")
    svc._trigger_login_and_poll = lambda: None  # 别真去联网/起线程
    svc._trigger_poll = lambda: None
    calls = []
    svc._post_progress = lambda tid, sid, inc, dev: (
        calls.append((tid, sid, inc, dev)),
        results.pop(0) if results else (None, "http"),
    )[1]
    svc.calls = calls
    svc.results = results
    return svc


def main():
    _install_stubs()

    # ── 1. 404 → 降级 + 冷却期 ────────────────────────
    print("[1] 404 降级并排冷却期")
    svc = _service([(None, "404")])
    svc._flush_progress()
    check(svc._progress_supported is False, "404 后进入降级")
    check(svc._progress_retry_at > time.monotonic(), "已排下一次试探时刻")
    check(len(svc.calls) == 1, "本轮只发了 1 次请求（遇 404 立刻返回）")
    check(svc.calls[0][1] == "{}:task-aaa:0".format(
        getattr(ts.settings, "DEVICE_NAME", "EGO_001")),
        "幂等键仍是 设备:任务:水位")

    n = len(svc.calls)
    svc._flush_progress()
    svc._flush_progress()
    check(len(svc.calls) == n, "冷却期内再 flush 两次都不发请求")

    # ── 2. 冷却期结束 + 后端已实现 → 自动恢复 ─────────
    print("[2] 冷却期后试探成功 → 恢复上报")
    svc.results.append((12, ""))                   # 后端这次答 200
    svc._progress_retry_at = time.monotonic() - 1  # 模拟冷却期已过
    SYNCED.clear()
    svc._flush_progress()
    check(len(svc.calls) == n + 1, "冷却期过后试探了一次")
    check(svc._progress_supported is True, "试探成功后恢复上报")
    check(SYNCED == [("task-aaa", 12, 12)], "试探成功即回写 (水位, 后端权威数)")

    svc.results.append((13, ""))
    svc._progress_retry_at = 0.0
    svc._flush_progress()
    check(len(svc.calls) == n + 2, "恢复后按正常节奏继续上报")
    check(SYNCED[-1] == ("task-aaa", 12, 13), "后续增量照常回写")

    # ── 3. 冷却期结束但仍 404 → 继续降级、冷却期推后 ──
    print("[3] 仍 404 → 冷却期推后")
    svc2 = _service([(None, "404"), (None, "404")])
    svc2._flush_progress()
    at1 = svc2._progress_retry_at
    svc2._progress_retry_at = time.monotonic() - 1
    svc2._flush_progress()
    check(svc2._progress_supported is False, "仍然保持降级")
    check(svc2._progress_retry_at > time.monotonic(),
          "冷却期被重新推后（不会每个 tick 都探）")
    check(svc2._progress_retry_at != at1, "试探时刻确实变了")
    check(len(svc2.calls) == 2, "两次试探各发 1 次请求")

    # ── 4. 正常成功路径 ──────────────────────────────
    print("[4] 正常路径不受影响")
    svc3 = _service([(5, "")])
    SYNCED.clear()
    svc3._flush_progress()
    check(svc3._progress_supported is True, "成功不上锁")
    check(svc3._progress_retry_at == 0.0, "成功不排冷却期")
    check(SYNCED == [("task-aaa", 12, 5)], "后端权威数 5 被采纳")

    # ── 5. set_server_url 立刻解除降级 ───────────────
    print("[5] 换服务器地址立刻解除降级")
    svc._progress_supported = False
    svc._progress_retry_at = time.monotonic() + 999
    svc.set_server_url("http://127.0.0.1:8")
    check(svc._progress_supported is True and svc._progress_retry_at == 0.0,
          "换地址后立即允许上报（不等冷却期）")

    print("FAIL" if FAILS else "PASS: 进度上报降级/恢复 全部通过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    app = QApplication(sys.argv)
    sys.exit(main())
