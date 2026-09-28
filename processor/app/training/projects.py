"""项目离「可以导出」还有多远 —— 训练页「到点提醒」的数据源。

判据由用户定:**已通过集数达到项目设置的采集上限**(``params.target_episodes``)
就算「到点」。到点之后训练页提醒一下,由人去点导出,而不是自动跑 8 小时。

★ 为什么不做全自动:导出一次约 **30 秒/集**(见 UI 的 batch_export_hint),
一个 1000 集的项目要跑 8 小时以上,而且构建好的数据集会被"固定"下来 ——
已经拿去训练的那份不能悄悄变。让机器跑 8 小时 vs 让人点一下,后者换到的是
"我知道现在在导什么、用哪份数据"。
"""

from __future__ import annotations

from app.localstore import list_projects, scan_sessions

# 与项目「已通过」口径一致的状态。与 export.py 的导出前置校验用的是同一组
# (那边是 ("reviewed", "approved")),不要各写一套。
_APPROVED = ("reviewed", "approved")


def _count_by_project() -> tuple[dict[str, int], dict[str, int]]:
    """``({项目名: 已上传集数}, {项目名: 已通过集数})``。

    **只扫一次。** ``scan_sessions()`` 是全量遍历,数据挂在 NAS/sshfs 上时很贵
    (见 README 里"打开 episode 慢"那一条);按项目各扫一遍就是 O(项目数) 次全量。
    """
    uploaded: dict[str, int] = {}
    approved: dict[str, int] = {}
    try:
        episodes = scan_sessions()
    except Exception:
        return uploaded, approved        # 存储暂时不可用时不该让整页炸掉
    for episode in episodes:
        name = episode.get("project")
        if not name:
            continue
        uploaded[name] = uploaded.get(name, 0) + 1
        if episode.get("status") in _APPROVED:
            approved[name] = approved.get(name, 0) + 1
    return uploaded, approved


def list_projects_progress() -> dict:
    """``{"projects": [...], "ready_count": int}``,按接近上限的程度排序。

    每个项目带 ``target`` / ``uploaded`` / ``approved`` / ``ready``:

        target    项目设置的采集上限(params.target_episodes);没设就是 0
        uploaded  已上传的集数(不分审核状态)
        approved  已通过(``reviewed``/``approved``)的集数
        ready     approved >= target,且 target > 0

    **target 为 0 的项目永远不算 ready** —— 没设上限就没有"到点"这回事,
    不能把"没配"当成"已达"。
    """
    uploaded, approved = _count_by_project()

    projects: list[dict] = []
    for project in list_projects():
        name = str(project.get("name") or "")
        if not name:
            continue
        target = int((project.get("params") or {}).get("target_episodes") or 0)
        done = approved.get(name, 0)
        projects.append({
            "name": name,
            "target": target,
            "uploaded": uploaded.get(name, 0),
            "approved": done,
            # target <= 0 ⇒ 没配上限 ⇒ 永不 ready(见 docstring)
            "ready": target > 0 and done >= target,
        })

    # 已达上限的排最前,其次按完成度高的排 —— 页面从上往下读就是"先看这个"。
    def sort_key(item: dict) -> tuple:
        ratio = (item["approved"] / item["target"]) if item["target"] else -1.0
        return (not item["ready"], -ratio, -item["approved"])

    projects.sort(key=sort_key)
    return {
        "projects": projects,
        "ready_count": sum(1 for p in projects if p["ready"]),
    }
