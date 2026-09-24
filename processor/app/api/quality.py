"""数据质检 API —— 检查项目录、报告查询、手动触发。

给前端两件事：
* 设置面板的**数据源** —— ``/checks`` 返回检查项目录 + 按设备卡片的分组，
  面板按它渲染 tab 和每一项的阈值输入框。新增检查项前端零改动。
* 报告的**读取入口** —— 列表页和详情页。

写端点少而克制：只有"重跑某集"。质检本身不做任何数据修改，
所以没有删除/清理类的接口。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/v1/quality", tags=["quality"])


def _check_catalog() -> dict:
    """检查项目录 + 按设备卡片分组 —— 前端设置面板的渲染数据。

    ``checks`` 是平铺清单（每项带 ``device_cards`` / ``default_params``）；
    ``by_device`` 是按设备卡片分好组的视图，前端 tab 直接用。
    两者给同一份数据，前端按方便取用。
    """
    from app.processing.cleaning.checks import all_checks, all_check_specs
    from app.processing.cleaning.contract import (
        DEVICE_MODALITIES, MODALITY_CHANNELS,
    )
    from app.processing.cleaning.engine import ruleset_revision

    specs = all_check_specs()
    by_device: dict[str, list[str]] = {}
    for modality in DEVICE_MODALITIES:
        by_device[modality] = [
            spec["slug"] for spec in specs
            if modality in (spec.get("device_cards") or [])
        ]

    return {
        "ruleset_revision": ruleset_revision(),
        "checks": specs,
        "by_device": by_device,
        "device_labels": _device_labels(),
        "device_channels": {
            modality: list(channels)
            for modality, channels in MODALITY_CHANNELS.items()
        },
        "severity_labels": _severity_labels(),
        "total": len(all_checks()),
    }


def _device_labels() -> dict[str, str]:
    """设备卡片的显示名 —— 复用 device_naming，不另起一套。"""
    try:
        from app.device_naming import DEVICE_LABELS
        return dict(DEVICE_LABELS)
    except Exception:
        return {}


def _severity_labels() -> dict[str, str]:
    from app.processing.cleaning.contract import SEVERITY_LABELS
    return dict(SEVERITY_LABELS)


@router.get("/checks")
def list_checks() -> dict:
    """检查项目录（含按设备卡片分组）—— 设置面板用。"""
    return _check_catalog()


@router.get("/summary")
def list_summaries(project: str | None = Query(None),
                   status: str | None = Query(None),
                   limit: int = Query(200, ge=1, le=2000)) -> dict:
    """质检报告列表（摘要），按时间倒序。

    ``status`` 过滤按 episode 结论（pass / warn / fail / pending_review / error）。
    """
    from app.processing.cleaning.store import list_reports

    items = list_reports()
    if project:
        items = [item for item in items if str(item.get("project")) == project]
    if status:
        items = [item for item in items if item.get("status") == status]

    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1

    return {"reports": items[:limit], "total": len(items), "counts": counts}


@router.get("/episodes/{episode_id}")
def get_report(episode_id: str) -> dict:
    """某一集的完整质检报告（四层结果）。"""
    from app.processing.cleaning.store import load_report, load_summary

    report = load_report(episode_id)
    if report is not None:
        return report
    summary = load_summary(episode_id)
    if summary is not None:
        # 完整报告被清掉了但摘要还在（state 里）—— 至少把摘要给出去
        return {"episode_id": episode_id, "summary_only": True, **summary}
    raise HTTPException(status_code=404, detail="No quality report for this episode")


@router.post("/episodes/{episode_id}/run")
async def run_episode_checks(
    episode_id: str,
    probe_video: bool = Query(False, description="是否跑视频检查（慢）"),
    persist: bool = Query(True, description="是否把报告写进 episode state"),
) -> dict:
    """重跑某一集的质检。

    跑在**线程池**里 —— 读 parquet/视频是阻塞 IO，直接放事件循环上会把
    页面请求卡住（仓库里 ``routes/devices.py`` 有同样的处理）。
    """
    episode = await asyncio.to_thread(_find_episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    report = await asyncio.to_thread(_run_checks_for, episode, probe_video)
    saved = False
    if persist:
        from app.processing.cleaning.store import save_report
        saved = await asyncio.to_thread(save_report, episode_id, report)
    return {"episode_id": episode_id, "saved": saved, "report": report}


def _find_episode(episode_id: str) -> dict | None:
    """在会话索引里找这一集（含 ``path``，质检要用它读数据）。"""
    from app.localstore import scan_sessions

    for episode in scan_sessions():
        if str(episode.get("id")) == episode_id:
            return episode
    return None


def _run_checks_for(episode: dict, probe_video: bool) -> dict:
    from app.processing.cleaning.engine import run_checks

    path = Path(str(episode.get("path") or ""))
    if not path.is_dir():
        raise HTTPException(status_code=409, detail="Episode data directory missing")

    # 设备卡片取自 episode 的设备信息 —— 与工作流派发用的是同一套口径
    modality = None
    try:
        from app.workflow_dispatch import _episode_input_groups
        groups = _episode_input_groups(episode) or []
        if groups:
            modality = str(groups[0].get("input_type") or "") or None
    except Exception:
        modality = None

    return run_checks(
        path,
        episode_id=str(episode.get("id") or ""),
        project=str(episode.get("project") or ""),
        probe_video=probe_video,
        modality_hint=modality,
    )
