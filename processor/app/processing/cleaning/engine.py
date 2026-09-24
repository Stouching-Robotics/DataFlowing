"""质检引擎 —— 把检查项串成一次运行，产出四层报告。

流程::

    collect.gather_evidence()      读批次目录 → Evidence
            │
            ▼
    applicable_checks()            按实际信道/模态选检查项
            │
            ▼
    逐项 run(ctx)                  产出 Finding
            │
            ▼
    rollup 成 StreamResult        每条流一个结论
            │
            ▼
    report.build_report()          四层结果

**只出结论，不产出数据**：不写文件、不改 episode 状态、不做任何清理动作。
报告由调用方决定怎么用（落盘 / 展示 / 过滤导出）。

设计取舍
--------
* **检查项失败不拖垮整轮** —— 单项抛异常记成 ``ERROR`` 结论继续跑，
  否则一个 bug 会让整集没有报告。
* **不设 `on_failure`** —— 按用户要求简化：清洗只记录，不碰 episode 状态流转。
  需要拦截时由调用方读报告的 ``passed`` / ``verdict`` 自行决定。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Iterable

from app.processing.cleaning import collect
from app.processing.cleaning.checks import (
    CheckContext, applicable_checks, all_checks,
)
from app.processing.cleaning.contract import (
    ERROR, PASS, SEVERITY_LABELS, StreamResult, worst,
)
from app.processing.cleaning.report import build_report, empty_report


def ruleset_revision() -> str:
    """规则集指纹 —— 任何检查项的 slug/version 变了它就变。

    用途：报告里带上这个值，规则更新后能识别出"这份报告是用旧规则跑的"。
    与 ``workflow_dispatch.workflow_revision()`` 同款思路 —— 用内容哈希而不是
    自增版本号，省掉版本分配和迁移。
    """
    parts = sorted(f"{check.slug}@{check.version}" for check in all_checks())
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def resolve_config(evidence: collect.Evidence,
                   config: dict | None) -> dict[str, dict]:
    """把节点配置解析成 ``{检查项 slug: {"enabled": bool, "params": {...}}}``。

    节点配置结构（见 data_quality 节点）::

        {
          "devices": {
            "gripper_device": {
              "checks": {
                "umi.slam_continuity": {"enabled": true,
                                        "params": {"max_linear_mps": 3.0}},
                "video.freeze":       {"enabled": false}
              }
            }
          }
        }

    本 episode 有多个设备卡片时，按 ``evidence.modalities`` 的顺序合并；
    **先出现的优先**（后面的不覆盖前面已设的值）。实际采集里一个 episode
    基本只对应一张卡片，多卡片是罕见情况，规则简单可预期即可。

    没配到的检查项走默认：启用、用检查项自己声明的 ``default_params``。
    """
    resolved: dict[str, dict] = {}
    devices = (config or {}).get("devices") or {}
    for modality in evidence.modalities:
        card = devices.get(str(modality)) or {}
        for slug, item in (card.get("checks") or {}).items():
            if slug in resolved:
                continue                    # 先出现的优先
            entry = item if isinstance(item, dict) else {}
            resolved[slug] = {
                "enabled": bool(entry.get("enabled", True)),
                "params": dict(entry.get("params") or {}),
            }
    return resolved


def _context_for(evidence: collect.Evidence, cfg: dict[str, dict]) -> CheckContext:
    """按证据构造检查上下文。阈值按"当前检查项"逐个注入。"""
    return CheckContext(
        batch_dir=evidence.batch_dir,
        channels=evidence.channels,
        modalities=evidence.modalities,
        data_rows=evidence.rows,
        data_columns=evidence.columns,
        duration_sec=evidence.duration_sec,
        fps=evidence.fps,
        streams=evidence.streams,
    )


def _stream_results(evidence: collect.Evidence,
                    findings: Iterable) -> list[StreamResult]:
    """把 findings 归拢成"每条流一个结论" —— 报告的第一层。"""
    by_stream: dict[str, list] = {}
    for item in findings:
        by_stream.setdefault(item.stream or "", []).append(item)

    results: list[StreamResult] = []
    seen: set[str] = set()
    for stream in evidence.streams:
        items = by_stream.get(stream.key) or []
        seen.add(stream.key)
        status = worst(item.status for item in items) if items else PASS
        results.append(StreamResult(
            stream=stream.key,
            modality=",".join(evidence.modalities),
            label=stream.key,
            channels=(stream.channel,) if stream.channel else (),
            status=status,
            reasons=tuple(item.rule for item in items if item.status != PASS),
            metrics={k: v for item in items
                     for k, v in (item.metrics or {}).items()
                     if isinstance(v, (int, float, str, bool))},
            ranges=tuple(r for item in items for r in (item.ranges or ())),
        ))

    # 没有对应证据流的 finding（如整集级结论）也要留在第一层里
    for key, items in by_stream.items():
        if key in seen:
            continue
        results.append(StreamResult(
            stream=key or "episode",
            status=worst(item.status for item in items),
            reasons=tuple(item.rule for item in items if item.status != PASS),
            ranges=tuple(r for item in items for r in (item.ranges or ())),
        ))
    return results


def run_checks(batch_dir: Path, *,
               episode_id: str = "",
               episode_index: int | None = None,
               project: str = "",
               config: dict | None = None,
               probe_video: bool = False,
               modality_hint: str | Iterable[str] | None = None) -> dict:
    """跑一个 episode 的全部适用检查，返回四层报告（纯 dict，不含副作用）。

    ``modality_hint`` 是设备模态（可多个，来自工作流连线的设备卡片）。给了它，
    ``resolve_config`` 才能查到用户在设置面板里按设备卡片配的阈值；给不出就
    走数据推断（推断不出 ``stereo_*``，那些 tab 的配置会被静默忽略）。

    ``episode_index`` 只在【一个文件装多集】的布局下需要（LeRobot v3.0 导出
    产物就是这种）；canonical 布局一集一文件，留空即可。**传错会导致把整份
    文件当成一集读**，所有指标因此失真 —— 而 ``localstore.get_episode()``
    返回的 ``path`` 是**项目目录**（多集共享），所以工作流路径**必须**带上它。

    ``probe_video=False``（默认）只读 parquet，毫秒级；视频检查要解码抽样，
    每集约几秒到几十秒，按需开。
    """
    started = time.perf_counter()
    evidence = collect.gather_evidence(
        batch_dir, episode_index=episode_index,
        probe_video=probe_video, modality_hint=modality_hint)

    if not evidence.streams:
        return empty_report(
            episode_id=episode_id, project=project,
            reason=str(evidence.notes.get("reason") or "no_data"))

    resolved = resolve_config(evidence, config)
    ctx = _context_for(evidence, resolved)

    # ★ 配置是【黑名单】，**不是白名单** —— 不传 ``enabled``。
    #
    # ``applicable_checks`` 的 ``enabled`` 是白名单语义（``slug not in allow``
    # 就跳过）。而前端设置面板只在用户**碰过**的检查项上写 entry（见
    # DeviceQualityModal 的 setEntry，未触碰的项不落进 config），所以
    # ``resolved`` 里通常只有一两条。把它当白名单 = 「用户在弹窗里改了一个
    # 阈值 → 其余检查项全部静默停跑」：实测只配 umi.gripper_range 时，
    # 9 项变 1 项，且不报任何错。
    #
    # 检查项没有"默认关闭"的概念（CleaningCheck 无 default_enabled），
    # 适用即跑；配置唯一的语义就是**关掉某项**。
    #
    # （曾经写成 `{...enabled} or None`：那个 ``or None`` 只修了空集那半边
    #   ——"空集=一个都不跑"——非空的白名单照样是错的。整条白名单都不该有。）
    deny = {slug for slug, item in resolved.items() if not item["enabled"]} or None

    checks = applicable_checks(
        channels=evidence.channels,
        modalities=evidence.modalities,
        cross_modal_active=len(set(evidence.modalities)) >= 2
                          or len({item.channel for item in evidence.streams}) >= 2,
        disabled=deny,
    )

    findings: list = []
    failed_checks: list[str] = []
    for check in checks:
        entry = resolved.get(check.slug) or {}
        ctx.params = dict(entry.get("params") or {})
        try:
            findings.extend(check.run(ctx) or [])
        except Exception as exc:
            failed_checks.append(f"{check.slug}: {exc}")
            findings.append(_error_finding(check, exc))

    # 视频流登记了却没检查 —— 必须显式记一笔，但**不能混进 findings**。
    #
    # 视频检查项"适用"（rgb 信道存在）不等于"查过"：``probe_video=False`` 时
    # 它们拿到的是空报告，内部直接 continue，一条结论都不产出。报告若就此收尾，
    # 会得出 ``passed=True 全部通过`` —— 而实际上视频根本没看。
    #
    # ★ 但它不该混进 findings 的严重度排序：``PENDING`` 排在 ``WARN`` 之前，
    #   一条"视频未检查"会把真实的力漂移警告盖掉（实测 52 集全被标成"待人工
    #   确认"）。"覆盖度"和"数据有多坏"是两个维度 —— 分开报。
    unchecked: list[dict] = []
    if not probe_video:
        rgb_streams = [item for item in evidence.streams if item.channel == "rgb"]
        if rgb_streams:
            unchecked.append({
                "channel": "rgb",
                "reason": "probe_video_disabled",
                "message": f"{len(rgb_streams)} 路视频未检查（本轮只读了 parquet）",
                "streams": [item.key for item in rgb_streams],
            })

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    report = build_report(
        _stream_results(evidence, findings),
        findings,
        episode_id=episode_id,
        project=project,
        modalities=_modality_summary(evidence, [c.slug for c in checks]),
        channels=evidence.channels,
        ruleset_revision=ruleset_revision(),
        template_source=modality_hint or "",
        elapsed_ms=elapsed_ms,
    )

    report["unchecked"] = unchecked

    if not checks:
        # 一个检查项都没跑 —— 报告在说"没问题"，而实际是"没查"。
        # 与 ``empty_report`` 同一条原则：没有结论不等于结论合格（fail closed）。
        report["no_checks_ran"] = True

    # ``episode`` 只反映【数据本身】的结论；``passed`` 额外要求没有未检查项。
    #
    # 分开的理由：前者是"数据有多坏"，后者是"我们看全了没有"。混在一起会让
    # "视频没检查"盖过"力漂移"，也会让"数据确实没问题但没看全"看起来像数据坏了。
    if unchecked or not checks:
        report["passed"] = False

    if failed_checks:
        # 检查项本身出错不改判定 —— 但要让人看见，否则会误以为"这集没问题"
        report["check_errors"] = failed_checks
    return report


def _modality_summary(evidence: collect.Evidence,
                      checks_run: Iterable[str]) -> dict:
    """PDF §1 的「数据清单」—— 这台设备是什么、有哪些信道、**实际跑了**哪些检查。

    ``checks_run`` 必须是【真正执行了】的检查项（``applicable_checks`` 的结果），
    不是配置里写的那份。两者可能差很远：设备卡片闸门会把不属于本设备的检查项
    滤掉，而报告如果只报配置，就分不清"检查了没问题"和"压根没检查"。
    """
    return {
        "device_cards": list(evidence.modalities),
        "channels": sorted(evidence.channels),
        "rows": evidence.rows,
        "fps": evidence.fps,
        "parquet": evidence.parquet,
        "checks_run": sorted(checks_run),
    }


def _error_finding(check, exc: Exception):
    """检查项抛异常时的占位结论。

    用 ``ERROR``（处理失败）而不是 ``FAIL`` —— 这是"没检查成"，
    不是"数据坏了"。混为一谈会让数据质量问题看起来比实际多。
    """
    from app.processing.cleaning.contract import Finding

    return Finding(
        rule=check.slug, version=check.version, status=ERROR,
        scope="episode",
        message=f"check failed to run: {type(exc).__name__}: {exc}",
    )
