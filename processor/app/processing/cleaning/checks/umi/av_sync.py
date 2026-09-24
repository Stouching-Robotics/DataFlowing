"""视频 ↔ 数据 帧同步 —— 各数据流的帧数与 RGB 视频是否一致。

为什么重要
----------
UMI 的 action 按帧号对齐：第 i 行动作对应第 i 帧画面。任何一路数据与视频
帧数不一致，都会让"看这一帧、预测下一步"的监督信号整体错位。

与 ``video.frame_drop`` 的分工
-----------------------------
``video.frame_drop`` 比的是 **视频帧数 vs parquet 总行数**（整体量）。
本项比的是 **每一路数据流各自的帧数**（结构性）——
UMI 特有：Slam / gripper_state / force 是三条独立写入的列，
采集端丢帧时可能只有其中一路短了，总数却对得上。

这也是用户提的"整个的数据有没有根据 RGB 视频进行帧同步"。
"""

from __future__ import annotations

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, PASS, WARN, FAIL,
)


@register_check
class UmiAvSyncCheck(CleaningCheck):
    slug = "umi.av_sync"
    label = "Video ↔ Data Frame Sync"
    version = "1.0"
    description = "Frame counts differ between data streams"

    # 需要视频和至少一路数据同时存在才有意义。
    # ★ 不加 cross_modal —— 它比的是【同一个 UMI 设备内部】的视频与各数据列，
    #   不是两张设备卡片之间。requires_channels 已经把"必须有 rgb"表达清楚了，
    #   再加 cross_modal 会把它错误地挤进"跨设备" tab。
    requires_channels = ("rgb", "time")
    # 属于哪些设备卡片 —— 前端设置面板按它分 tab
    device_cards = ("gripper_device",)

    default_params = {"tolerance": 2}
    default_severity = FAIL

    def run(self, ctx: CheckContext) -> list[Finding]:
        # 视频侧：取第一路 RGB 的帧数作为基准
        rgb_streams = ctx.streams_of("rgb")
        if not rgb_streams:
            return []
        reference = None
        reference_key = ""
        for stream in rgb_streams:
            frames = int((stream.video or {}).get("frame_count") or stream.rows or 0)
            if frames:
                reference, reference_key = frames, stream.key
                break
        if not reference:
            return []

        tolerance = int(self.param(ctx, "tolerance", 2))
        drifted: dict[str, int] = {}
        for stream in ctx.streams:
            if stream.channel in ("rgb", "depth") or not stream.rows:
                continue
            if stream.key == reference_key:
                continue
            diff = abs(int(stream.rows) - reference)
            if diff > tolerance:
                drifted[stream.key] = int(stream.rows)

        metrics = {"reference_stream": reference_key,
                   "reference_frames": reference,
                   "tolerance": tolerance,
                   "drifted": drifted}

        if not drifted:
            return [self.finding(ctx, PASS, metrics=metrics)]

        detail = "，".join(f"{key}={rows}" for key, rows in sorted(drifted.items()))
        return [self.finding(
            ctx,
            FAIL,
            message=f"these streams do not match the video frame count ({reference}): {detail}",
            metrics=metrics,
        )]
