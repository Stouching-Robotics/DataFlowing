"""质检报告落盘 —— 写进 episode state 的 ``cleaning_report`` 字段。

**报告与状态流转是同一次写入**：判 FAIL/ERROR 的批次会被拦回
``to_review``（人工审核），并留下 ``cleaning_summary.blocking`` 这个粘性标记。

只写报告、不碰状态的方案看着更"干净"，但挡住不坏数据 —— 自动批准散在
``ai_annotation`` / ``video_quality`` 两处门禁里，它们各看各的报告，
谁都不会读质检结论。判 FAIL 的集只要视频干净，照样被自动批准放行。

另外**不动** ``deleted`` / ``rejected`` 这类状态：质检不能把用户删掉或否决过的
批次复活。

放在 ``state/episode_states/<episode_id>.json`` 而不是项目数据集里：
后者是给 LeRobot 消费的干净树（``project_dataset`` 明确不建 ``processed/``、
``meta/processing/``），而质检报告是 episode 级的可变状态 —— ``state/`` 正是
它的既有归属，``ai_quality_report`` / ``video_quality_report`` 也都在这。

读走 ``localstore.read_episode_state``（带内存缓存），写走
``write_episode_state``（原子替换 + 同步更新缓存）—— 两条路径都自带锁，
不要自己读写那个文件。
"""

from __future__ import annotations

from typing import Any

from app.processing.cleaning.contract import is_blocking

# episode state 里放质检报告的字段名。
REPORT_FIELD = "cleaning_report"
# 只放摘要，完整报告另存 —— 报告里可能有几十条区间，全塞进 state 会让
# 每次 ``read_episode_state`` 都要解析几百 KB。完整报告见 ``reports_dir()``。
SUMMARY_FIELD = "cleaning_summary"

# 完整报告单独存一个目录，避免污染 episode state
_REPORTS_SUBDIR = "quality_reports"

# 允许被质检拦回人工审核的状态。
#
# **不含 ``to_review``**（本来就在那儿，再写一次是空操作）、也不含 ``deleted``
# 与 ``rejected`` —— 质检不能把用户删掉/否决过的批次复活。
_BLOCKABLE_STATUSES = frozenset({
    "processing", "completed", "reviewed", "approved", "failed",
})


def _reports_dir():
    from app.localstore import STATE_ROOT
    return STATE_ROOT / _REPORTS_SUBDIR


def summary_of(report: dict) -> dict[str, Any]:
    """从完整报告里抽一个轻量摘要 —— 列表页/徽标只读这个。

    ``blocking`` 是**给自动批准路径看的**闸门（见 ``is_approval_blocked``），
    不是展示字段：判 FAIL/ERROR 才为真，WARN 只是警告、不拦。
    """
    episode = report.get("episode") or {}
    return {
        "schema_version": report.get("schema_version"),
        "passed": bool(report.get("passed")),
        "blocking": is_blocking(str(episode.get("status") or "")),
        "status": episode.get("status"),
        "status_label": episode.get("status_label"),
        "summary": episode.get("summary"),
        "counts": {
            "fail": len(episode.get("failed_streams") or []),
            "warn": len(episode.get("warn_streams") or []),
            "pending": len(episode.get("pending_streams") or []),
        },
        "ranges": len(report.get("ranges") or []),
        "ruleset_revision": report.get("ruleset_revision"),
        "computed_at": report.get("computed_at"),
        "reason": report.get("reason"),
    }


def is_approval_blocked(state: dict) -> bool:
    """该集的质检结论是否禁止**自动批准**。

    自动批准（``to_review`` → ``reviewed``）散在好几处：AI 标注的质量门禁
    （``ai_annotation._set_ai_quality_state``）、视频门禁
    （``video_quality._apply_video_quality_result``）。它们只看**自己的**
    报告，不看数据质检的结论 —— 于是质检判 FAIL、视频门禁判 PASS 时，坏数据
    会被自动放行。

    ★ 为什么不能只靠"把 status 推回 to_review"：
      几条门禁是**并发**跑的（都在 run 完成回调里 create_task），写的先后没有
      保证。若质检先把状态推回 to_review、视频门禁随后又置成 reviewed，
      拦截就白做了。所以质检除了推状态，还留下这个**粘性标记**，让所有自动
      批准路径都先看一眼。

    标记不需要手工清除：下一次质检会整份覆盖 ``cleaning_summary``，
    数据修好后 ``blocking`` 自然变回 False。
    """
    summary = (state or {}).get(SUMMARY_FIELD) or {}
    return bool(summary.get("blocking"))


def save_report(episode_id: str, report: dict) -> bool:
    """把报告写进 episode state。返回是否写入成功。

    **不改 ``status``** —— 只设 ``cleaning_report`` / ``cleaning_summary`` /
    ``cleaning_at`` 三个字段。

    episode 不存在时静默跳过（外键不存在就别凭空造一条 state）。
    """
    from app.localstore import mutate_episode_state, _write_json

    key = str(episode_id)
    if not key:
        return False

    # 完整报告先单独落盘 —— 它可能有几十条区间，塞进 episode state 会让每次
    # read_episode_state 都要解析几百 KB。放在下面临界区**外面**：这是个大文件
    # 写入（在 NAS 上可能是几十毫秒），不该占着状态锁。
    try:
        _write_json(_reports_dir() / f"{key}.json", report)
    except Exception:
        pass                                # 摘要照样进 state，完整报告失败不影响

    summary = summary_of(report)

    def _apply(state: dict) -> bool:
        state[REPORT_FIELD] = report
        state[SUMMARY_FIELD] = summary
        state["cleaning_at"] = report.get("computed_at")

        # 数据不对 → 拦回人工审核。与写报告**同一次锁内 RMW**：分成两次写的话
        # 中间那个窗口里，另一条并发的门禁可能把状态改成 reviewed。
        if summary.get("blocking"):
            current = str(state.get("status") or "")
            if current in _BLOCKABLE_STATUSES:
                state["status"] = "to_review"
                state["approved_at"] = None
        return True

    # 走锁内 RMW：跑完工作流后 video_quality 的门禁任务与本任务是并发写的，
    # 手写 read→write 会让其中一方的字段被另一方的旧快照覆盖（详见
    # localstore.mutate_episode_state 的注释）。episode 不存在时返回 None，
    # 保持"不凭空造 state"的既有行为。
    return bool(mutate_episode_state(key, _apply))


def load_report(episode_id: str) -> dict | None:
    """读完整报告（不存在返回 None）。"""
    from app.localstore import _read_json

    return _read_json(_reports_dir() / f"{episode_id}.json")


def load_summary(episode_id: str) -> dict | None:
    """读摘要（从 episode state 取，快）。"""
    from app.localstore import read_episode_state

    return (read_episode_state(str(episode_id)) or {}).get(SUMMARY_FIELD)


def list_reports(limit: int = 0) -> list[dict]:
    """列出全部摘要，按时间倒序 —— 给质量列表页用。

    只扫 ``quality_reports/`` 目录，不 join ``scan_sessions()`` —— 后者在
    SSHFS 上冷读极慢，而报告是自包含的（项目名就在里面）。
    """
    from app.localstore import _read_json

    directory = _reports_dir()
    if not directory.is_dir():
        return []
    items: list[dict] = []
    for path in directory.glob("*.json"):
        report = _read_json(path)
        if not isinstance(report, dict):
            continue
        item = summary_of(report)
        item["episode_id"] = report.get("episode_id") or path.stem
        item["project"] = report.get("project")
        items.append(item)
    items.sort(key=lambda item: str(item.get("computed_at") or ""), reverse=True)
    return items[:limit] if limit else items


def forget_report(episode_id: str) -> None:
    """删除报告（episode 被删除时调用）。"""
    for path in (_reports_dir() / f"{episode_id}.json",):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
