"""质检触发 —— 把纯引擎接到运行时（工作流跑完之后）。

三层分工，别混：

    engine.py    纯函数：读证据 → 跑检查 → 出报告。不写盘、不改状态。
    store.py     落盘：写进 episode state 的 ``cleaning_report``，**不碰 status**。
    trigger.py   本文件：从 run 快照里判断"该不该跑、按什么配置跑"，
                 丢到线程池执行，再把报告交给 store。

调用方是 ``api/worker.py`` 的 run 完成回调（与 ``video_quality`` 的门禁任务
同一个挂载点）。

★ 为什么这里要 `to_thread`
-------------------------
``engine.run_checks`` 是**同步**的，而且会读 parquet / 必要时解码视频。直接在
协程里调用会把整个事件循环卡住几秒到几十秒（存储根在 NAS 上时更甚，见
``routes/pages.py`` 里同样的告诫）。``asyncio.to_thread`` 把它挪到线程池。

★ 与 video_quality 门禁的关系
----------------------------
两者挂在同一张 Data Quality 卡片上，**可能同时触发**（相机类工作流就是）：

    video_quality_gate   → 通过→reviewed / 失败→to_review
    cleaning（本模块）    → 判 FAIL/ERROR 才拦回 to_review；其余只记录

三条路都写同一份 episode state，所以读改写都必须走
``localstore.mutate_episode_state``（锁内 RMW），否则后写的一方会把另一方
刚写的字段用旧快照冲掉 —— 尤其是 ``status``，被冲掉意味着失败批次看起来
像已通过。

**光推状态还不够**：几条门禁是并发跑的，谁先写没有保证。质检除了推状态，
还在 ``cleaning_summary.blocking`` 上留一个粘性标记，让
``ai_annotation`` / ``video_quality`` 的**自动批准**路径主动让路 ——
否则质检刚推回 to_review，视频门禁随后又置成 reviewed。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.processing.cleaning import engine, store
from app.processing.cleaning.contract import normalize_device_modality
from app.workflow_types import canonical_node_type

# 质检节点在工作流里的 slug。合并前的两个旧 slug（ai_quality_review /
# data_cleaning）由 ``workflow_types.canonical_node_type`` 统一映射到这里。
_QUALITY_NODE = "data_quality"


def _nodes_by_id(graph: dict) -> dict[str, dict]:
    nodes = (graph or {}).get("nodes") or []
    return {str(node.get("id")): node for node in nodes if node.get("id") is not None}


def _incoming_edges(graph: dict) -> dict[str, list[str]]:
    """``{目标节点 id: [源节点 id, ...]}``。"""
    incoming: dict[str, list[str]] = {}
    for edge in (graph or {}).get("edges") or []:
        source, target = edge.get("source"), edge.get("target")
        if source is None or target is None:
            continue
        incoming.setdefault(str(target), []).append(str(source))
    return incoming


def _node_type(node: dict | None) -> str:
    return canonical_node_type((node or {}).get("data", {}).get("nodeType"))


def _upstream_device_cards(node_id: str, by_id: dict[str, dict],
                           incoming: dict[str, list[str]]) -> tuple[str, ...]:
    """从质检节点往回找，收集上游的**设备卡片**模态。

    往回找而不是只看直接上游，是因为中间可能夹着处理节点
    （相机 → RGB-D_3D_BareHand → 质检），直接上游是个处理节点、认不出模态。

    设备卡片就是链路源头，遇到就不再往回走 —— 否则会顺着"卡片A → 卡片B"
    这种不合法的连线继续爬到别处。
    """
    seen: set[str] = set()
    queue = list(incoming.get(str(node_id), ()))
    cards: list[str] = []
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        modality = normalize_device_modality(_node_type(by_id.get(current)))
        if modality:
            if modality not in cards:
                cards.append(modality)
            continue
        queue.extend(incoming.get(current, ()))
    return tuple(cards)


def data_quality_target(graph: dict,
                        node_configs: dict) -> tuple[dict, tuple[str, ...]] | None:
    """找到**接在数据链路上**的 Data Quality 卡片。

    返回 ``(卡片配置, 上游设备卡片模态)``；没有卡片、或卡片没接线 → ``None``
    （画布上孤零零摆一张卡片只是配置，不该触发）。

    ★ 判据是"**有入边**"，不是列举"哪些源节点算数"。
      ``ai_annotation.video_quality_gate_config`` 用的是后者，它的白名单里
      **没有 ``gripper_device``** —— 于是 UMI 夹爪工作流接上质检卡片后静默
      地什么都不跑。这种白名单还会继续漏：每加一种设备就漏一次，而且漏了
      不报错。质检节点的语义本来就与上游是哪类设备无关 —— 它检查的是
      "这个批次目录里实际有什么数据"，跑哪些检查由引擎按真实信道决定。
    """
    by_id = _nodes_by_id(graph)
    incoming = _incoming_edges(graph)

    # 排序保证多张卡片时结果确定，不受画布 JSON 顺序影响
    for node_id in sorted(by_id):
        node = by_id[node_id]
        if _node_type(node) != _QUALITY_NODE:
            continue
        if not incoming.get(node_id):
            continue                        # 没接线 —— 只是块配置，不触发

        data = node.get("data") or {}
        config = dict(data.get("config") or {})
        config.update((node_configs or {}).get(node.get("id")) or {})
        return config, _upstream_device_cards(node_id, by_id, incoming)
    return None


async def run_cleaning_checks(episode_id: str, batch_dir: str | Path, *,
                              config: dict | None = None,
                              device_cards: tuple[str, ...] = (),
                              episode_index: int | None = None,
                              project: str = "",
                              probe_video: bool = True) -> dict:
    """跑一次质检并落盘，返回四层报告。

    ``episode_index`` 必传 —— 工作流给的 ``batch_dir`` 是**项目目录**，多集
    共享（见 ``collect.gather_evidence`` 的告诫）。漏传的后果不是报错，而是
    拿第 0 集的数据去比全部集的视频，得出一堆假的丢帧结论。

    ``probe_video=True`` 是刻意的：只读 parquet 时视频检查拿不到证据，
    报告会被标 ``unchecked`` 并强制 ``passed=False`` —— 每一集都"未通过"的
    门禁等于没有门禁。代价实测约 4.8s/集（单流 UMI，本机），跑在线程池里。
    """
    report = await asyncio.to_thread(
        engine.run_checks,
        Path(batch_dir),
        episode_id=str(episode_id),
        episode_index=episode_index,
        project=str(project or ""),
        config=config,
        probe_video=probe_video,
        modality_hint=device_cards or None,
    )
    await asyncio.to_thread(store.save_report, str(episode_id), report)
    return report


# 后台任务的强引用。
#
# ``asyncio.create_task`` 的返回值**必须有人持有**：只把它丢给事件循环的话，
# 任务可能在执行到一半时被 GC 回收（asyncio 文档原文："Save a reference to
# the result of this function, to avoid a task disappearing mid-execution"）。
# 这里持有到它跑完为止。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def spawn_cleaning_for_run(run: dict, episode: dict) -> bool:
    """worker 完成回调调用的**同步**入口。返回是否已调度。

    先做图查找再建任务：卡片没接线时连任务都不建（同步、纯字典遍历，很快），
    这样"没有质检节点"这条常见路径不会往事件循环里塞空任务。
    """
    if data_quality_target((run or {}).get("graph") or {},
                           (run or {}).get("node_configs") or {}) is None:
        return False
    task = asyncio.create_task(trigger_cleaning_for_run(run, episode))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return True


async def trigger_cleaning_for_run(run: dict, episode: dict) -> dict | None:
    """跑一次质检的后台协程。卡片没接线时返回 None。

    **永不抛异常**：这是后台任务，抛出去会变成 unhandled asyncio exception，
    而调用方（worker 的完成响应）已经返回了，没人能处理。失败只记日志 ——
    与 ``video_quality.run_video_quality_review`` 同样的取舍。
    """
    episode_id = str((run or {}).get("episode_id") or (episode or {}).get("id") or "")
    batch_dir = (episode or {}).get("path")
    if not episode_id or not batch_dir:
        return None

    try:
        target = data_quality_target(
            (run or {}).get("graph") or {},
            (run or {}).get("node_configs") or {},
        )
        if target is None:
            return None
        config, device_cards = target

        report = await run_cleaning_checks(
            episode_id, batch_dir,
            config=config, device_cards=device_cards,
            # ``get_episode`` 的记录里 episode_index 可能是 0，不能用 or 兜底
            episode_index=(episode.get("episode_index")
                           if episode.get("episode_index") is not None else None),
            project=str(episode.get("project") or ""),
        )
    except Exception as exc:                # noqa: BLE001 —— 后台任务不抛
        print(f"[Quality] Data quality checks failed for {episode_id}: "
              f"{type(exc).__name__}: {exc}")
        return None

    summary = store.summary_of(report)
    verdict = str(summary.get("status"))
    if summary.get("blocking"):
        verdict += " -> blocked, back to manual review"
    print(f"[Quality] {episode_id}: {verdict} "
          f"({summary.get('ranges', 0)} ranges, "
          f"{len(device_cards)} device card(s))")
    return report
