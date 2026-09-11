"""采集端任务进度账本单测(app/device_progress.py)。

覆盖:幂等重放、多设备聚合、增量水位、参数校验、未知任务拒绝(400 而非
404 —— 采集端把 404 当"后端未实现"永久降级)、持久化与坏文件容错。

    pytest tests/test_device_task_progress.py
    python3 tests/test_device_task_progress.py     # 无 pytest 也能跑
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import device_progress as dp  # noqa: E402

TASK = "task-aaa"
OTHER = "task-bbb"
KNOWN = {TASK, OTHER}


def _report(tmp, increment, session_id, task=TASK, device="EGO_001"):
    return dp.apply_report(
        {"task_id": task, "device_name": device,
         "session_id": session_id, "increment": increment},
        KNOWN, path=tmp)


def test_first_report_sets_total(tmp_path):
    f = tmp_path / "p.json"
    out = _report(f, 1, "EGO_001:t:0")
    assert out["applied"] is True
    assert out["completed_count"] == 1
    assert dp.reported_total(TASK, path=f) == 1


def test_replayed_watermark_is_idempotent(tmp_path):
    """采集端崩溃重启后会重发同一水位 —— 不能重复计数。"""
    f = tmp_path / "p.json"
    first = _report(f, 12, "EGO_001:t:0")
    again = _report(f, 12, "EGO_001:t:0")
    assert first["completed_count"] == 12
    assert again["applied"] is False
    assert again["completed_count"] == 12
    # 重放仍要回当前聚合数(采集端拿它当后端权威数)
    assert isinstance(again["completed_count"], int)
    assert not isinstance(again["completed_count"], bool)


def test_incremental_watermarks_accumulate(tmp_path):
    f = tmp_path / "p.json"
    _report(f, 12, "EGO_001:t:0")
    out = _report(f, 1, "EGO_001:t:12")
    assert out["completed_count"] == 13


def test_multiple_devices_aggregate(tmp_path):
    f = tmp_path / "p.json"
    _report(f, 3, "EGO_001:t:0", device="EGO_001")
    _report(f, 2, "EGO_002:t:0", device="EGO_002")
    out = _report(f, 1, "EGO_003:t:0", device="EGO_003")
    assert out["completed_count"] == 6
    # 同一水位键跨设备不冲突
    assert dp.reported_total(TASK, path=f) == 6


def test_zero_increment_is_recorded_without_growth(tmp_path):
    f = tmp_path / "p.json"
    _report(f, 5, "EGO_001:t:0")
    out = _report(f, 0, "EGO_001:t:5")
    assert out["applied"] is True and out["completed_count"] == 5


def test_totals_only_contain_reported_tasks(tmp_path):
    """没上报过的项目不能出现在这里 —— 调用方据此回退 session 计数。"""
    f = tmp_path / "p.json"
    _report(f, 1, "EGO_001:t:0")
    totals = dp.reported_totals(path=f)
    assert totals == {TASK: 1}
    assert dp.reported_total(OTHER, path=f) is None


def test_validation_errors(tmp_path):
    f = tmp_path / "p.json"
    bad = [
        ({}, "task_id"),                                            # 空 body
        ({"task_id": TASK, "session_id": "s"}, "device_name"),
        ({"task_id": TASK, "device_name": "d"}, "session_id"),
        ({"task_id": TASK, "device_name": "d", "session_id": "s",
          "increment": -1}, "increment"),
        ({"task_id": TASK, "device_name": "d", "session_id": "s",
          "increment": "x"}, "increment"),
        ({"task_id": TASK, "device_name": "d", "session_id": "s",
          "increment": True}, "increment"),
        ({"task_id": TASK, "device_name": "d", "session_id": "s",
          "increment": dp.MAX_INCREMENT + 1}, "increment"),
    ]
    for body, needle in bad:
        try:
            dp.apply_report(body, KNOWN, path=f)
        except ValueError as exc:
            assert needle in str(exc), (body, str(exc))
        else:
            raise AssertionError(f"应当拒绝: {body}")
    assert dp.reported_totals(path=f) == {}  # 拒绝的请求不落账


def test_unknown_task_rejected_not_silently_accepted(tmp_path):
    """未知任务要报错(路由转 400)。绝不能 404:采集端会永久降级上报。"""
    f = tmp_path / "p.json"
    try:
        _report(f, 1, "EGO_001:t:0", task="no-such-task")
    except ValueError as exc:
        assert "unknown task_id" in str(exc)
    else:
        raise AssertionError("未知任务应当被拒绝")
    # known_task_ids=None(不校验)时照常落账 —— 路由必须传已知集合
    out = dp.apply_report({"task_id": "no-such-task", "device_name": "d",
                           "session_id": "s", "increment": 1}, None, path=f)
    assert out["completed_count"] == 1


def test_count_prefers_report_over_sessions(tmp_path):
    """核心口径:有上报就用录制数 —— 之后再上传多少条都不动它。"""
    f = tmp_path / "p.json"
    proj = {"id": TASK, "name": "UMIGripper_AI"}
    _report(f, 12, "EGO_001:t:0")
    reported = dp.reported_totals(path=f)
    assert dp.resolve_count(proj, reported, sessions=16) == (12, "reported")
    # 又上传了 3 条(项目下 session 变 19)→ 进度不变
    assert dp.resolve_count(proj, reported, sessions=19) == (12, "reported")
    # 录了一条 → 上报后 +1
    _report(f, 1, "EGO_001:t:12")
    assert dp.resolve_count(proj, dp.reported_totals(path=f),
                            sessions=19) == (13, "reported")


def test_count_falls_back_to_sessions_before_any_report(tmp_path):
    """升级前就有数据的历史项目不归零:没有上报时仍按 session 数。"""
    proj = {"id": TASK, "name": "UMIGripper_AI"}
    assert dp.resolve_count(proj, {}, sessions=16) == (16, "sessions")
    assert dp.needs_session_fallback([proj], {}) is True
    assert dp.needs_session_fallback([proj], {TASK: 12}) is False


def test_persisted_across_reload(tmp_path):
    f = tmp_path / "p.json"
    _report(f, 2, "EGO_001:t:0")
    raw = json.loads(f.read_text(encoding="utf-8"))
    assert raw["tasks"][TASK]["devices"]["EGO_001"]["count"] == 2
    # 重新读文件(模拟进程重启)后水位仍生效
    again = _report(f, 2, "EGO_001:t:0")
    assert again["applied"] is False and again["completed_count"] == 2


def test_corrupt_file_degrades_to_empty(tmp_path):
    f = tmp_path / "p.json"
    f.write_text("{ not json", encoding="utf-8")
    assert dp.reported_totals(path=f) == {}
    out = _report(f, 1, "EGO_001:t:0")
    assert out["completed_count"] == 1


if __name__ == "__main__":
    import tempfile
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
                print(f"  PASS: {name}")
            except Exception:
                failed += 1
                print(f"  FAIL: {name}")
                traceback.print_exc()
    print("FAIL" if failed else "PASS: device_task_progress 全部通过")
    sys.exit(1 if failed else 0)
