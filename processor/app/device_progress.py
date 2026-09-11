"""采集端任务进度上报账本 —— 本地 JSON 存储(无数据库)。

为什么要这个账本:``GET /device/tasks`` 原先把「项目目录下的 session 数」当
采集进度,而 session 是**上传**的产物。采集端本身在「录制完成」时就已经把
增量上报上来(见采集端 ``core/task_record.py`` 的分账模型),于是同一条数据
被算两次——录制一次、上传一次。2026-09-10 现场:UMIGripper_AI 本机录了 12 条、
服务器上 16 个 session,界面显示 28/20,两边的数都不可信。

现在的口径:

    项目进度 = 各设备上报的录制完成数之和

只有**从未收到过上报**的项目才回退到 session 计数,这样升级前就存在的历史
项目不会突然归零;某个项目一旦有设备上报,它就固定按上报口径走(上传不再
改变它,采集端录一条 +1)。

幂等:采集端用 ``session_id = "{device}:{task_id}:{水位}"`` 标识一次增量,
同一 (task_id, device_name, session_id) 只计一次 —— 网络抖动重试、程序被杀
后重放同一个水位,都不会重复计数。

文件: ``data/state/device_task_progress.json``

    {"tasks": {"<task_id>": {
        "devices": {"<device_name>": {
            "count": 12,
            "sessions": ["EGO_001:<task_id>:0", ...],   # 已计入的幂等键
            "updated_at": "2026-09-10T02:11:39+00:00"}},
        "updated_at": "..."}},
     "updated_at": "..."}
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# 读-改-写必须串行:uvicorn 单进程多线程,两个设备同时上报时不能丢增量
_LOCK = threading.RLock()

PROGRESS_FILE_NAME = "device_task_progress.json"

# 单次上报上限:采集端 flush 的是「本机未同步增量」,正常是 1~几十;
# 超过这个数说明上报方数据脏了,直接拒绝而不是把进度顶飞。
MAX_INCREMENT = 100000

_MAX_FIELD = 200  # task_id / device_name / session_id 的长度上限


def _progress_file(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    from app.localstore import STATE_ROOT
    return STATE_ROOT / PROGRESS_FILE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path | None = None) -> dict:
    try:
        data = json.loads(_progress_file(path).read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": {}}
    if not isinstance(data, dict):
        return {"tasks": {}}
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        data["tasks"] = {}
    return data


def _write(data: dict, path: Path | None = None) -> None:
    target = _progress_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(target)


def _clean(value, limit: int = _MAX_FIELD) -> str:
    return str(value or "").strip()[:limit]


def reported_totals(path: Path | None = None) -> dict[str, int]:
    """各任务的上报聚合数。**只含收到过上报的任务** —— 没上报过的项目不在
    这里,调用方据此回退到 session 计数。"""
    out: dict[str, int] = {}
    with _LOCK:
        data = _read(path)
    for task_id, entry in (data.get("tasks") or {}).items():
        if not isinstance(entry, dict):
            continue
        total = 0
        for dev in (entry.get("devices") or {}).values():
            if isinstance(dev, dict):
                total += int(dev.get("count") or 0)
        out[str(task_id)] = total
    return out


def reported_total(task_id: str, path: Path | None = None) -> int | None:
    """单个任务的上报聚合数;从未上报过返回 None。"""
    return reported_totals(path).get(str(task_id))


def needs_session_fallback(projects: list[dict],
                           reported: dict[str, int]) -> bool:
    """是否还有项目需要 session 计数兜底。

    全都有上报时调用方可跳过目录扫描(远程存储上的一次全量遍历)。
    """
    return any(str(p.get("id") or "") not in reported for p in projects)


def resolve_count(project: dict, reported: dict[str, int],
                  sessions: int) -> tuple[int, str]:
    """单个项目的 (current_count, 口径)。

    有上报 → 上报聚合数(录制口径,上传不再影响它);
    没有 → session 数(升级前的历史项目不归零)。
    """
    pid = str(project.get("id") or "")
    if pid in reported:
        return int(reported[pid]), "reported"
    return int(sessions or 0), "sessions"


def apply_increment(task_id: str, device_name: str, session_id: str,
                    increment: int, path: Path | None = None) -> dict:
    """把一次上报落账,返回该任务的最新聚合数。

    幂等:同一 (task_id, device_name, session_id) 第二次进来只读取不累加
    (``applied`` 为 False),采集端重试因此安全。
    """
    task_id = _clean(task_id)
    device_name = _clean(device_name)
    session_id = _clean(session_id)
    with _LOCK:
        data = _read(path)
        tasks = data.setdefault("tasks", {})
        entry = tasks.setdefault(task_id, {"devices": {}})
        if not isinstance(entry.get("devices"), dict):
            entry["devices"] = {}
        dev = entry["devices"].setdefault(
            device_name, {"count": 0, "sessions": []})
        sessions = dev.get("sessions")
        if not isinstance(sessions, list):
            sessions = dev["sessions"] = []
        applied = False
        if session_id not in sessions:
            dev["count"] = int(dev.get("count") or 0) + int(increment)
            sessions.append(session_id)
            dev["updated_at"] = _now()
            entry["updated_at"] = dev["updated_at"]
            data["updated_at"] = dev["updated_at"]
            applied = True
        total = sum(int(d.get("count") or 0)
                    for d in entry["devices"].values() if isinstance(d, dict))
        if applied:
            _write(data, path)
    return {"total": total, "applied": applied,
            "device_count": int(dev.get("count") or 0),
            "device": device_name, "task_id": task_id}


def apply_report(body: dict, known_task_ids: Iterable[str] | None = None,
                 path: Path | None = None) -> dict:
    """校验并落账一次上报,返回响应体(路由直接回给采集端)。

    Raises:
        ValueError: 参数缺失/非法/任务不存在 —— 调用方转 HTTP 400。

    刻意**不返回 404**:采集端把 404 当成「后端还没实现该端点」并永久降级
    (``_progress_supported = False``),此后整机所有任务都不再上报。参数错
    与未知任务都属于「这次请求有问题」,用 400 表达即可。
    """
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    task_id = _clean(body.get("task_id"))
    device_name = _clean(body.get("device_name"))
    session_id = _clean(body.get("session_id"))
    if not task_id:
        raise ValueError("task_id required")
    if not device_name:
        raise ValueError("device_name required")
    if not session_id:
        raise ValueError("session_id required (idempotency key)")

    raw = body.get("increment", 0)
    if isinstance(raw, bool):
        raise ValueError("increment must be an integer")
    try:
        increment = int(raw)
    except (TypeError, ValueError):
        raise ValueError("increment must be an integer")
    if increment < 0:
        raise ValueError("increment must be >= 0")
    if increment > MAX_INCREMENT:
        raise ValueError(f"increment too large (max {MAX_INCREMENT})")

    if known_task_ids is not None and task_id not in set(known_task_ids):
        raise ValueError(f"unknown task_id: {task_id}")

    result = apply_increment(task_id, device_name, session_id, increment, path)
    return {
        "ok": True,
        "task_id": task_id,
        "device_name": device_name,
        "applied": result["applied"],
        "increment": increment,
        # 采集端读这个字段当后端权威数 —— 必须是 int(布尔会被 isinstance 放行)
        "completed_count": int(result["total"]),
        "updated_at": _now(),
    }
