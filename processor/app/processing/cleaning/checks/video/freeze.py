"""冻结 —— 画面连续多帧几乎不变。

为什么重要
----------
冻结段通常是采集程序卡住、编码器 stall、或 USB 带宽不足导致丢帧后补重复帧。
它和黑屏不同：画面是"正常的样子"，但时间已经不动了。模型会以为任务停滞。

注意与"合理的静止"区分
----------------------
人拿着夹爪停在物体上方思考时，画面也接近静止。所以本项默认判 **WARN 而非
FAIL**，并在 metrics 里带上每段时长供人工/AI 复核 —— 方案明确要求
「阈值不要照搬」，静止多久算异常要按任务节奏定。
"""

from __future__ import annotations

from app.processing.cleaning.contract import VIDEO_CAPABLE_MODALITIES
from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, Range, WARN, FAIL,
)


@register_check
class VideoFreezeCheck(CleaningCheck):
    slug = "video.freeze"
    label = "Frozen Frame"
    version = "1.0"
    description = "Frame nearly unchanged across consecutive frames"

    requires_channels = ("rgb",)

    # 属于哪些设备卡片 —— 前端设置面板按它分 tab

    device_cards = VIDEO_CAPABLE_MODALITIES

    default_params = {"min_sec": 2.0, "fail_sec": 10.0}
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        for stream in ctx.streams_of("rgb"):
            report = stream.video or {}
            ranges = report.get("freeze_ranges") or []
            if not ranges:
                continue

            fps = float(report.get("fps") or stream.fps or ctx.fps or 30.0)
            min_sec = float(self.param(ctx, "min_sec", 2.0))
            fail_sec = float(self.param(ctx, "fail_sec", 10.0))

            converted: list[Range] = []
            longest = 0.0
            worst_status = WARN
            for item in ranges:
                try:
                    start, end = float(item[0]), float(item[1])
                except (TypeError, ValueError, IndexError):
                    continue
                span = end - start
                if span < min_sec:
                    continue
                longest = max(longest, span)
                status = FAIL if span >= fail_sec else WARN
                if status == FAIL:
                    worst_status = FAIL
                converted.append(Range(
                    start_sec=start, end_sec=end,
                    code=self.slug, severity=status,
                    stream=stream.key, channel="rgb",
                    start_frame=int(start * fps), end_frame=int(end * fps),
                    rule=self.slug,
                    message=f"frozen for {span:.1f}s",
                ))

            if not converted:
                continue

            findings.append(self.finding(
                ctx, worst_status, stream=stream.key,
                message=f"{len(converted)} frozen stretches, longest {longest:.1f}s",
                metrics={"segments": len(converted),
                         "longest_sec": round(longest, 3)},
                ranges=converted,
            ))
        return findings
