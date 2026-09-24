"""黑屏 —— 镜头被挡、补光灯没开、或采集程序写了空帧。

为什么重要
----------
黑屏段里的视觉信息为零。模型在这些帧上学到的是"画面全黑时该做什么"，
通常是噪声；若黑屏占比高，还会把图像归一化统计量带偏。

判定口径
----------
检测本身由 ``app/video_quality.py`` 完成（按灰度均值 + 暗像素占比双阈值）。
本检查项只负责把它的区间翻成统一的 Finding/Range 并套用节点配置的阈值 ——
**不重复实现检测算法**，避免两处口径漂移。
"""

from __future__ import annotations

from app.processing.cleaning.contract import VIDEO_CAPABLE_MODALITIES
from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, Range, PASS, WARN, FAIL,
)


@register_check
class VideoBlackScreenCheck(CleaningCheck):
    slug = "video.black_screen"
    label = "Black Screen"
    version = "1.0"
    description = "Frame is fully or nearly black"

    requires_channels = ("rgb",)

    # 属于哪些设备卡片 —— 前端设置面板按它分 tab

    device_cards = VIDEO_CAPABLE_MODALITIES

    default_params = {"min_sec": 0.5, "fail_ratio": 0.05}
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        for stream in ctx.streams_of("rgb"):
            report = stream.video or {}
            ranges = report.get("black_ranges") or []
            if not ranges:
                continue

            fps = float(report.get("fps") or stream.fps or ctx.fps or 30.0)
            min_sec = float(self.param(ctx, "min_sec", 0.5))
            converted: list[Range] = []
            total_sec = 0.0
            for item in ranges:
                try:
                    start, end = float(item[0]), float(item[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if end - start < min_sec:
                    continue
                total_sec += end - start
                converted.append(Range(
                    start_sec=start, end_sec=end,
                    code=self.slug, severity=WARN,
                    stream=stream.key, channel="rgb",
                    start_frame=int(start * fps), end_frame=int(end * fps),
                    rule=self.slug,
                    message=f"black screen for {end - start:.1f}s",
                ))

            if not converted:
                continue

            duration = float(ctx.duration_sec or 0.0)
            ratio = (total_sec / duration) if duration > 0 else 0.0
            fail_ratio = float(self.param(ctx, "fail_ratio", 0.05))
            status = FAIL if ratio >= fail_ratio else WARN

            findings.append(self.finding(
                ctx, status, stream=stream.key,
                message=f"{len(converted)} black-screen stretches, {total_sec:.1f}s total"
                        + (f"（占 {ratio:.1%}）" if duration else ""),
                metrics={"segments": len(converted),
                         "total_sec": round(total_sec, 3),
                         "ratio": round(ratio, 4)},
                ranges=converted,
            ))
        return findings
