"""清洗报告组装 —— PDF §5 的「四层结果」。

    第一层 数据流     哪条流好、哪条流坏
    第二层 时间区间   问题发生在哪几秒、哪些帧
    第三层 episode    这一集整体什么结论
    第四层 训练用途   能用于什么训练、不能用于什么

全部是纯函数 —— 输入是检查项产出的 ``StreamResult`` / ``Finding``，
输出是可 JSON 序列化的 dict。不碰磁盘、不碰 cv2，因此完全可单测。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from app.processing.cleaning.contract import (
    Finding, Range, StreamResult,
    PASS, WARN, FAIL, PENDING, ERROR,
    SEVERITY_ORDER, SEVERITY_LABELS, worst, is_blocking,
)

SCHEMA_VERSION = 2

# 「训练用途」判定表 —— 声明式，不是 if-else 堆出来的。
# requires 里的信道必须全部具备，该用途才成立。
# PDF §7 要求"说明 episode 适合纯视觉、触觉或联合训练"，这张表就是那个说明。
TRAINING_USE_RULES: tuple[dict[str, Any], ...] = (
    {"use": "pure_vision",           "label": "Pure vision",
     "requires": ("rgb", "time")},
    {"use": "vision_gripper_joint",  "label": "Vision + gripper",
     "requires": ("rgb", "gripper_state", "time")},
    {"use": "slam_trajectory",       "label": "SLAM trajectory",
     "requires": ("slam", "time")},
    {"use": "vision_tactile_joint",  "label": "Vision + tactile",
     "requires": ("rgb", "tactile", "time")},
    {"use": "bimanual_tactile",      "label": "Bimanual tactile",
     "requires": ("tactile", "time")},
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_timeline(streams: Iterable[StreamResult | dict]) -> list[dict]:
    """把各条流里的区间拍平成一条时间轴，按时间排序去重。

    详情页要一次拉完整条时间轴，而 streams 是嵌套的 —— 所以顶层冗余一份。
    单一来源、单一方向（从 streams 派生），有测试保证一致。
    """
    items: list[dict] = []
    for stream in streams:
        if isinstance(stream, StreamResult):
            for item in stream.ranges:
                items.append(_range_row(item.to_dict(), stream.stream))
        elif isinstance(stream, dict):
            for item in stream.get("ranges") or []:
                if isinstance(item, dict):
                    items.append(_range_row(item, str(stream.get("stream") or "")))
    items.sort(key=lambda row: (row["start_sec"], row["end_sec"], row["code"]))
    deduped: list[dict] = []
    for row in items:
        if deduped and all(
            row[key] == deduped[-1][key]
            for key in ("start_sec", "end_sec", "code", "stream")
        ):
            continue
        deduped.append(row)
    return deduped


def _range_row(item: dict, stream: str) -> dict:
    return {
        "stream": item.get("stream") or stream,
        "channel": item.get("channel") or "",
        "start_sec": float(item.get("start_sec") or 0.0),
        "end_sec": float(item.get("end_sec") or 0.0),
        "start_frame": item.get("start_frame"),
        "end_frame": item.get("end_frame"),
        "code": str(item.get("code") or ""),
        "severity": str(item.get("severity") or WARN),
        "rule": str(item.get("rule") or ""),
        "message": str(item.get("message") or ""),
    }


def rollup_episode(streams: Iterable[StreamResult | dict],
                   findings: Iterable[Finding | dict] = ()) -> dict:
    """第三层：整集结论。纯函数。

    取所有流 + 所有 finding 里最严重的那个状态。
    """
    statuses: list[str] = []
    failed: list[str] = []
    warned: list[str] = []
    pending: list[str] = []

    for stream in streams:
        if isinstance(stream, StreamResult):
            name, status = stream.stream, stream.status
        else:
            name, status = str(stream.get("stream") or ""), str(stream.get("status") or PASS)
        statuses.append(status)
        if is_blocking(status):
            failed.append(name)
        elif status == PENDING:
            pending.append(name)
        elif status == WARN:
            warned.append(name)

    for finding in findings:
        status = finding.status if isinstance(finding, Finding) else str(finding.get("status") or PASS)
        statuses.append(status)

    overall = worst(statuses) if statuses else PASS
    pieces: list[str] = []
    if failed:
        pieces.append(f"{len(failed)} stream(s) failed")
    if warned:
        pieces.append(f"{len(warned)} stream(s) with warnings")
    if pending:
        pieces.append(f"{len(pending)} stream(s) pending review")
    return {
        "status": overall,
        "status_label": SEVERITY_LABELS.get(overall, overall),
        "failed_streams": failed,
        "warn_streams": warned,
        "pending_streams": pending,
        "summary": "; ".join(pieces) if pieces else "All checks passed",
    }


def rollup_training_use(channels: Iterable[str],
                        episode_status: str = PASS) -> list[dict]:
    """第四层：这个 episode 能用于哪些训练。纯函数。

    信道不具备 → 该用途判 fail（"未采集"），这是**用途维度**的失败，
    不代表数据本身坏 —— 所以 reason 写清楚是"未采集"而不是"质量差"。
    """
    have = {str(item) for item in channels}
    rows: list[dict] = []
    for rule in TRAINING_USE_RULES:
        required = tuple(rule["requires"])
        missing = [channel for channel in required if channel not in have]
        if missing:
            rows.append({
                "use": rule["use"],
                "label": rule["label"],
                "status": FAIL,
                "reason": f"not collected: {', '.join(missing)}",
                "requires": list(required),
                "missing": missing,
            })
        else:
            rows.append({
                "use": rule["use"],
                "label": rule["label"],
                "status": episode_status,
                "reason": "" if episode_status == PASS else "data quality problem",
                "requires": list(required),
                "missing": [],
            })
    return rows


def passed_for(status: str) -> bool:
    """该状态是否算"通过"（只有 PASS 算通过，WARN 不算）。"""
    return status == PASS


def build_report(streams: Iterable[StreamResult],
                 findings: Iterable[Finding] = (),
                 *,
                 episode_id: str = "",
                 project: str = "",
                 modalities: dict[str, Any] | None = None,
                 channels: Iterable[str] = (),
                 ruleset_revision: str = "",
                 template_source: str = "",
                 elapsed_ms: int = 0) -> dict:
    """组装完整报告。纯函数 —— 唯一的副作用是 ``_utcnow()``。"""
    stream_list = list(streams)
    finding_list = list(findings)
    episode = rollup_episode(stream_list, finding_list)
    use_channels = {str(item) for item in channels}
    for stream in stream_list:
        use_channels.update(stream.channels)

    return {
        "schema_version": SCHEMA_VERSION,
        "episode_id": episode_id,
        "project": project,
        "computed_at": _utcnow(),
        "elapsed_ms": int(elapsed_ms),
        "ruleset_revision": ruleset_revision,
        "template_source": template_source,

        # 模态清单 —— PDF §1 的「数据清单」
        "modalities": dict(modalities or {}),

        # 判定总入口：只有 PASS 算通过
        "passed": passed_for(episode["status"]),

        # 四层结果
        "streams": [
            item.to_dict() if isinstance(item, StreamResult) else dict(item)
            for item in stream_list
        ],
        "ranges": build_timeline(stream_list),
        "episode": episode,
        "training_use": rollup_training_use(use_channels, episode["status"]),

        "findings": [
            item.to_dict() if isinstance(item, Finding) else dict(item)
            for item in finding_list
        ],
    }


def empty_report(*, episode_id: str = "", project: str = "",
                 reason: str = "no_data") -> dict:
    """没有任何数据可检查时的占位报告。

    ``passed`` 恒为 False —— 没有数据不等于数据合格（fail closed）。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "episode_id": episode_id,
        "project": project,
        "computed_at": _utcnow(),
        "elapsed_ms": 0,
        "ruleset_revision": "",
        "template_source": "",
        "modalities": {},
        "passed": False,
        "streams": [],
        "ranges": [],
        "episode": {
            "status": ERROR,
            "status_label": SEVERITY_LABELS[ERROR],
            "failed_streams": [],
            "warn_streams": [],
            "pending_streams": [],
            "summary": reason,
        },
        "training_use": [],
        "findings": [],
        "reason": reason,
    }
