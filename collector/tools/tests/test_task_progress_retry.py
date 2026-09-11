"""进度上报的降级/恢复/口径/范围单测 —— 不联网、不开线程。

背景：
  * 老后端没有 /api/v1/device/tasks/progress，采集端收到 404 就永久降级，
    服务端补上端点后若不重启采集端就永远恢复不了 → 加冷却期重探。
  * 上报范围：客户端里本地自建的任务（平台没这个项目）上报必被拒 400。
  * 上报口径：后端从没收到过上报的项目（progress_source="sessions"）首次
    上报必须送本机全量；只送增量会把后端计数从 session 数砸成增量本身
    （现场：USB-DECXIN---*/S80C---glove_sensor_AI 这 3/3/3 的三个项目）。

覆盖：
  1. 404 → 降级 + 排冷却期；冷却期内不再发任何请求
  2. 冷却期过后试探成功 → 自动恢复上报，并回写后端权威数
  3. 冷却期过后仍 404 → 继续降级、冷却期推后
  4. 正常成功路径不受影响（不会把 supported 弄反）
  5. set_server_url 立即解除降级
  6. 后端列表里没有的任务直接跳过，不发 POST 也不报错
  7. 白名单只放行列表内的任务
  8. 口径 sessions → 首报送本机全量；转 reported 后回到增量
  9. 首报成功后同一轮询窗口内的第二段不会被幂等键吞掉

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


BASE = {"id": "task-aaa", "local_count": 12, "synced_count": 0}
PENDING = [dict(BASE)]
SYNCED = []


def _install_stubs():
    """task_record 走内存替身：pending 可读、mark_synced 会推进水位。"""
    task_record.pending_sync_tasks = lambda: [dict(p) for p in PENDING]

    def _mark_synced(tid, sc, backend):
        SYNCED.append((tid, sc, backend))
        for p in PENDING:            # 模拟真实 watermark 前进
            if p["id"] == tid:
                p["synced_count"] = sc
    task_record.mark_synced = _mark_synced


def _service(results, known=("task-aaa",), source="reported", pending=None):
    """建一个 TaskService，_post_progress 换成按队列出结果的桩。

    results: [(new_backend | None, err)] —— 每次 POST 消费一条。
    known:   最近一次成功轮询拿到的后端任务 id（上报白名单）。
    source:  后端这些项目的进度口径（"reported" / "sessions"）。
    pending: 本机待同步任务快照（默认 watermark 0、本地 12 条）。
    """
    PENDING[:] = [dict(p) for p in (pending or [BASE])]
    svc = ts.TaskService("http://127.0.0.1:9")
    svc._trigger_login_and_poll = lambda: None  # 别真去联网/起线程
    svc._trigger_poll = lambda: None
    svc._cached_tasks = [{"id": i} for i in known]
    svc._progress_source = {i: source for i in known}
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
    check(svc.calls[0][1].endswith(":0"), "幂等键是 设备:任务:水位")

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

    # ── 6. 后端不认识的任务不上报 ────────────────────
    # 现场：本地自建的 Test01/Test9/4/Project_Test10 等被当成待同步任务，
    # 上报换回 400 unknown task_id，每个 tick 刷一条"上报失败"。
    print("[6] 后端列表里没有的任务直接跳过")
    svc4 = _service([(1, "")], known=())      # 后端一个任务都没有
    errs = []
    svc4.error_occurred.connect(errs.append)
    svc4._flush_progress()
    check(svc4.calls == [], "本地自建任务不发 POST")
    check(errs == [], "也不报错（不是失败，是没这个任务）")

    before = len(svc4.calls)
    svc4._cached_tasks = [{"id": "task-aaa"}]
    svc4._progress_source = {"task-aaa": "reported"}
    svc4._flush_progress()
    check(len(svc4.calls) == before + 1, "后端列表出现后照常上报")
    check(svc4.calls[-1][0] == "task-aaa", "上报的正是该任务")

    # ── 7. 白名单只放行列表内的任务 ──────────────────
    print("[7] 白名单只放行列表内的任务")
    svc5 = _service([(2, "")], known=("task-bbb",), source="reported")
    svc5._flush_progress()
    check(svc5.calls == [], "白名单不含 task-aaa → 不发")

    # ── 8. 后端还没台账时先送本机全量基线 ────────────
    print("[8] 后端口径 sessions → 首报送本机全量")
    SYNCED.clear()
    svc6 = _service([(4, "")], source="sessions")
    svc6._flush_progress()
    check(svc6.calls[0][2] == 12, "首报增量 = 本机全量 12（不是增量 1）")
    check(svc6.calls[0][1].endswith(":0"), "幂等键水位记 0")
    check(SYNCED == [("task-aaa", 12, 4)], "水位推到 12，后端数 4 采纳")
    check(svc6._progress_source["task-aaa"] == "reported",
          "本地口径随即转 reported（不必等下一轮询）")

    # 已是台账口径、又没有新录制 → 增量 0
    SYNCED.clear()
    svc6.results.append((5, ""))
    svc6._flush_progress()
    check(svc6.calls[-1][2] == 0, "无新录制时增量为 0")
    check(SYNCED == [("task-aaa", 12, 5)], "水位不再前进")

    # ── 9. 首报成功后同一轮询窗口内的第二段 ──────────
    print("[9] 首报成功后同窗口内又录一段")
    PENDING[0]["local_count"] = 13          # 又录了一条
    SYNCED.clear()
    svc6.results.append((13, ""))
    svc6._flush_progress()
    check(svc6.calls[-1][1].endswith(":12"), "用新水位 12 作幂等键（不是又发 :0）")
    check(svc6.calls[-1][2] == 1, "只送增量 1（没被幂等键吞掉）")
    check(SYNCED == [("task-aaa", 13, 13)], "水位推到 13")

    print("FAIL" if FAILS else "PASS: 进度上报降级/恢复 全部通过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    app = QApplication(sys.argv)
    sys.exit(main())
