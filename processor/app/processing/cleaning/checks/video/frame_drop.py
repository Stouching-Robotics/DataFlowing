"""丢帧 / 帧数对齐 —— 视频帧数与 parquet 行数不一致。

为什么重要
----------
两者不一致时，按帧号取动作会整体错位；漏帧则会让模型学到"同一帧对应两个
动作"。真实的采集侧表现是视频比数据少几帧（编码器丢帧）或多几帧（尾部多录）。

判定口径
--------
容差取 ``max(tolerance_min, data_rows × tolerance_ratio)``：
小批次按绝对帧数兜底（默认 2 帧），大批次按比例（默认 0.5%）。
超出容差 5 倍判 FAIL，否则 WARN —— 差几帧和差几百帧不是一回事。
"""

from __future__ import annotations

from app.processing.cleaning.contract import VIDEO_CAPABLE_MODALITIES
from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, PASS, WARN, FAIL,
)


@register_check
class VideoFrameDropCheck(CleaningCheck):
    slug = "video.frame_drop"
    label = "Frame Drop / Count Alignment"
    version = "1.0"
    description = "Video frame count differs from data row count"

    requires_channels = ("rgb",)

    # 属于哪些设备卡片 —— 前端设置面板按它分 tab

    device_cards = VIDEO_CAPABLE_MODALITIES

    # 阈值默认值 —— 节点配置可覆盖。
    # 方案要求「阈值不要照搬」：这是平台起点值，必须用人工确认过的好/坏数据验证。
    default_params = {"tolerance_ratio": 0.005, "tolerance_min": 2}
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        for stream in ctx.streams_of("rgb"):
            report = stream.video or {}
            if not report:
                continue
            # 打不开/读不了元数据的流不算"丢帧问题"，交给 video.decode_error
            if report.get("reason") in ("video_open_failed", "video_metadata_invalid"):
                continue

            video_frames = int(report.get("frame_count") or 0)
            data_rows = int(stream.rows or ctx.data_rows or 0)
            if not video_frames or not data_rows:
                continue

            diff = abs(video_frames - data_rows)
            tolerance = max(
                int(self.param(ctx, "tolerance_min", 2)),
                int(data_rows * float(self.param(ctx, "tolerance_ratio", 0.005))),
            )
            metrics = {
                "video_frames": video_frames,
                "data_rows": data_rows,
                "diff": diff,
                "tolerance": tolerance,
            }

            if diff <= tolerance:
                findings.append(self.finding(ctx, PASS, stream=stream.key,
                                             metrics=metrics))
                continue

            findings.append(self.finding(
                ctx,
                FAIL if diff > tolerance * 5 else WARN,
                stream=stream.key,
                message=f"video has {video_frames} frames / data has {data_rows} rows"
                        f"，差 {diff} 帧（容差 {tolerance}）",
                metrics=metrics,
            ))
        return findings
