"""可训练数据集的发现 —— ``state/datasets/<名字>``。

这些是**在视频审核页选中「已通过」的集、导出后留下来的成品**(落盘见
``app/routes/export.py: _persist_trainable_dataset``)。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.localstore import STATE_ROOT

# 与 `app/routes/export.py` 的 DATASETS_DIR 是同一个目录。从 STATE_ROOT 推出来
# 而不是 import 那个模块:写进去的和读出来的必须指向同一处,而"唯一真源"本来
# 就是 STATE_ROOT。两边若哪天分叉,导出落下的数据集训练页就会看不见 ——
# 改其中一处时记得对照另一处。
DATASETS_DIR = STATE_ROOT / "datasets"


def list_datasets() -> dict:
    """``{"datasets": [...], "root": str, "exists": bool}``。

    **不列 ``sessions/<项目>`` 原始项目。** 原始项目里混着未审核的集,而且
    留出集(UMIGripper_Action_AI 的 52–73)就在里面 —— 按项目选等于同时踩两个
    坑,UI 上只能报警拦不住。导出那一步才是唯一能明确控制"到底训了哪些集"的
    地方,所以以导出产物为准。

    **也不列 ``state/exports/*``。** 那边是按 run_id 命名的**处理产物**,实测
    168 条里每条 ``total_episodes`` 都是 1(单集),凑不成训练集。
    """
    root = DATASETS_DIR
    if not root.is_dir():
        return {"datasets": [], "root": str(root), "exists": False}

    datasets: list[dict] = []
    try:
        for entry in sorted(root.iterdir()):
            # 跳过 .staging / .old 这类交换中间态 —— 它们只在替换过程中短暂出现。
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            info_path = entry / "meta" / "info.json"
            if not info_path.is_file():
                continue
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(info, dict):
                continue
            episodes = int(info.get("total_episodes") or 0)
            if episodes <= 0:
                continue
            try:
                mtime = int(info_path.stat().st_mtime)
            except OSError:
                mtime = 0
            datasets.append({
                "name": entry.name,
                "path": str(entry),
                "total_episodes": episodes,
                "total_frames": int(info.get("total_frames") or 0),
                "fps": float(info.get("fps") or 0),
                "codebase_version": str(info.get("codebase_version") or ""),
                "mtime": mtime,
            })
    except OSError:
        return {"datasets": [], "root": str(root), "exists": True}

    datasets.sort(key=lambda d: (-d["total_episodes"], d["name"]))
    return {"datasets": datasets, "root": str(root), "exists": True}
