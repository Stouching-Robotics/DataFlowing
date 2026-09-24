"""「数据不对 → 卡在人工审核」的行为锁。

盯住三件事，每一件坏掉都是**静默**的（不报错、流程照常走完）：

  1. 判 FAIL/ERROR 的批次被推回 ``to_review``，且清掉 ``approved_at``
  2. WARN 只记录、不拦 —— 否则警告阈值一响，整批数据全卡住
  3. **自动批准路径主动让路** —— AI 标注 / 视频门禁各看各的报告，不看质检
     结论；不查那个粘性标记的话，它们会在质检推回 to_review 之后又把状态
     置成 reviewed，坏数据照样被放行

全部在临时存储根上跑（monkeypatch ``localstore`` 的路径常量），不碰真实数据。

    pytest tests/test_cleaning_gate.py
    python3 tests/test_cleaning_gate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # noqa: E402

import pytest  # noqa: E402

from app import localstore  # noqa: E402
from app.processing.cleaning import store  # noqa: E402
from app.processing.cleaning import contract as C  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把 episode state 落到临时目录 —— 别碰 ~/.cache/egodata-data。"""
    states = tmp_path / "state" / "episode_states"
    states.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(localstore, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(localstore, "EPISODE_STATES_DIR", states)
    localstore._episode_state_cache.clear()
    yield states
    localstore._episode_state_cache.clear()


def _report(status, *, ranges=0):
    """造一份最小报告 —— 只关心 ``episode.status``，那是拦截判据。"""
    return {
        "schema_version": 2,
        "episode_id": "E1",
        "computed_at": "2026-09-24T00:00:00+00:00",
        "passed": status == C.PASS,
        "streams": [],
        "ranges": [{}] * ranges,
        "episode": {
            "status": status,
            "status_label": C.SEVERITY_LABELS.get(status, status),
            "failed_streams": [], "warn_streams": [], "pending_streams": [],
            "summary": "test",
        },
        "findings": [],
    }


def _seed(states, status="to_review", approved_at=None):
    import json
    eid = "E1"
    (states / f"{eid}.json").write_text(json.dumps({
        "id": eid, "name": eid, "project": "P", "status": status,
        "approved_at": approved_at,
    }, ensure_ascii=False), encoding="utf-8")
    localstore._episode_state_cache.pop(eid, None)
    return eid


# ── 拦截 ────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [C.FAIL, C.ERROR])
def test_failure_pushes_back_to_review(sandbox, status):
    eid = _seed(sandbox, status="reviewed", approved_at="2026-01-01T00:00:00Z")
    assert store.save_report(eid, _report(status)) is True

    state = localstore.read_episode_state(eid)
    assert state["status"] == "to_review"          # 自动批准被撤销
    assert state["approved_at"] is None            # 批准时间一起清掉
    assert store.is_approval_blocked(state) is True


@pytest.mark.parametrize("status", [C.WARN, C.PASS, C.PENDING])
def test_non_failure_does_not_block(sandbox, status):
    """WARN/PENDING 只是警告 —— 拦的话整批数据都会卡住。"""
    eid = _seed(sandbox, status="reviewed")
    store.save_report(eid, _report(status))

    state = localstore.read_episode_state(eid)
    assert state["status"] == "reviewed"           # 状态没被动过
    assert store.is_approval_blocked(state) is False


def test_does_not_resurrect_deleted_episode(sandbox):
    """质检不能把用户删掉/否决过的批次复活。"""
    for status in ("deleted", "rejected"):
        eid = _seed(sandbox, status=status)
        store.save_report(eid, _report(C.FAIL))
        assert localstore.read_episode_state(eid)["status"] == status


def test_report_still_written_when_not_blocking(sandbox):
    """不拦也得把报告写进去 —— 拦截不是写报告的前提。"""
    eid = _seed(sandbox)
    store.save_report(eid, _report(C.WARN, ranges=3))
    summary = store.load_summary(eid)
    assert summary["status"] == C.WARN and summary["ranges"] == 3


# ── 自动批准让路 ────────────────────────────────────────────
def _quality_gate(state):
    """转调 ai_annotation 的真实写入路径，避免测试复制一份逻辑。"""
    from app.ai_annotation import _set_ai_quality_state
    return _set_ai_quality_state


def test_ai_gate_refuses_to_auto_approve_when_blocked(sandbox):
    """★ 核心用例：质检判 FAIL 后，AI 标注门禁即使自己通过也不得自动批准。"""
    eid = _seed(sandbox, status="to_review")
    store.save_report(eid, _report(C.FAIL))

    _quality_gate(None)(eid, {"passed": True}, auto_approve=True)

    state = localstore.read_episode_state(eid)
    assert state["status"] == "to_review", "坏数据被 AI 门禁放行了"
    assert state["ai_quality_status"] == "passed"   # 它自己的结论照常记录


def test_ai_gate_still_auto_approves_when_not_blocked(sandbox):
    """别把拦截修成"自动批准彻底失效" —— 干净数据该自动过。"""
    eid = _seed(sandbox, status="to_review")
    store.save_report(eid, _report(C.PASS))

    _quality_gate(None)(eid, {"passed": True}, auto_approve=True)

    assert localstore.read_episode_state(eid)["status"] == "reviewed"


def test_video_gate_refuses_to_auto_approve_when_blocked(sandbox):
    """视频干净 ≠ 数据干净 —— 视频门禁同样要让路。"""
    from app.video_quality import _apply_video_quality_result

    eid = _seed(sandbox, status="to_review")
    store.save_report(eid, _report(C.FAIL))

    _apply_video_quality_result(eid, {"passed": True})

    state = localstore.read_episode_state(eid)
    assert state["status"] == "to_review", "坏数据被视频门禁放行了"
    assert state["video_quality_status"] == "passed"


# ── 标记的粘性 ──────────────────────────────────────────────
def test_block_clears_once_data_is_fixed(sandbox):
    """数据修好后重跑要能自动批准 —— 否则卡住就再也出不来了。

    注意 ``save_report`` **只拦不批**：它解封后不会自己把状态改回 reviewed，
    批准是 AI/视频门禁或人工的事。所以这里走一遍真实的"修好 → 重跑"顺序：
    worker 先置 to_review，质检复核（清除标记），随后门禁自动批准。
    """
    eid = _seed(sandbox, status="to_review")

    store.save_report(eid, _report(C.FAIL))
    assert store.is_approval_blocked(localstore.read_episode_state(eid)) is True

    # —— 重跑 ——
    localstore.set_episode_status(eid, "to_review")   # worker 完成回调做的
    store.save_report(eid, _report(C.PASS))           # 质检复核：数据已修好
    state = localstore.read_episode_state(eid)
    assert store.is_approval_blocked(state) is False

    _quality_gate(None)(eid, {"passed": True}, auto_approve=True)
    assert localstore.read_episode_state(eid)["status"] == "reviewed"


def test_save_report_never_approves(sandbox):
    """质检**只拦不批** —— 它不该越过门禁自己把批次标成通过。"""
    eid = _seed(sandbox, status="to_review")
    store.save_report(eid, _report(C.PASS))
    assert localstore.read_episode_state(eid)["status"] == "to_review"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
