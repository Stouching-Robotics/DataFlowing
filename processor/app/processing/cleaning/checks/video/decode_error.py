"""解码失败 / 文件打不开 —— 最硬的一类问题。

为什么重要
----------
解码失败意味着这段视频在任何下游都无法使用：训练读取会抛异常、浏览器预览
会黑屏、导出会失败。而且它常常是文件损坏的前兆 —— 与其等到训练时才发现，
不如在入库后就标出来。

严重度
------
本项默认 **FAIL**（其余视频检查默认 WARN）：能解码是"数据可用"的最低门槛，
不存在"阈值调松就能接受"的余地。

opencv 不可用是**环境问题不是数据问题** —— 判 ERROR（处理失败）而不是 FAIL，
避免把环境缺依赖记成数据质量事故。
"""

from __future__ import annotations

from app.processing.cleaning.contract import VIDEO_CAPABLE_MODALITIES
from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, Range, PASS, FAIL, ERROR,
)

# 这些 reason 说明"没检查成"，不是"数据坏了"
_ENV_REASONS = {"opencv_unavailable"}


@register_check
class VideoDecodeErrorCheck(CleaningCheck):
    slug = "video.decode_error"
    label = "Decode Errors"
    version = "1.0"
    description = "Video won't open, or contains undecodable frames"

    requires_channels = ("rgb",)

    # 属于哪些设备卡片 —— 前端设置面板按它分 tab

    device_cards = VIDEO_CAPABLE_MODALITIES

    default_params = {"max_errors": 0}
    default_severity = FAIL

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        for stream in ctx.streams_of("rgb"):
            report = stream.video or {}
            if not report:
                continue

            reason = str(report.get("reason") or "")
            if reason in _ENV_REASONS:
                findings.append(self.finding(
                    ctx, ERROR, stream=stream.key,
                    message="OpenCV unavailable, could not check (environment issue, not a data issue)",
                    metrics={"reason": reason},
                ))
                continue

            # 整条流打不开 —— 比丢几帧严重得多
            if reason in ("video_open_failed", "video_metadata_invalid"):
                findings.append(self.finding(
                    ctx, FAIL, stream=stream.key,
                    message=f"video cannot be opened or has invalid metadata ({reason})",
                    metrics={"reason": reason,
                             "error": str(report.get("error") or "")[:200]},
                ))
                continue

            count = int(report.get("decode_error_count") or 0)
            max_errors = int(self.param(ctx, "max_errors", 0))
            if count <= max_errors:
                findings.append(self.finding(ctx, PASS, stream=stream.key,
                                             metrics={"decode_error_count": count}))
                continue

            fps = float(report.get("fps") or stream.fps or ctx.fps or 30.0)
            frames = [int(item) for item in (report.get("decode_errors") or [])
                      if isinstance(item, (int, float))]
            # 每个失败帧给一个零长度区间 —— 时间轴上就是一个可点击的红点
            marks = tuple(
                Range(start_sec=frame / fps, end_sec=frame / fps,
                      code=self.slug, severity=FAIL,
                      stream=stream.key, channel="rgb",
                      start_frame=frame, end_frame=frame,
                      rule=self.slug, message=f"frame {frame} failed to decode")
                for frame in frames[:20]
            )
            findings.append(self.finding(
                ctx, FAIL, stream=stream.key,
                message=f"{count} frames failed to decode"
                        + (f"（示例：{frames[:5]}）" if frames else ""),
                metrics={"decode_error_count": count,
                         "sample_frames": frames[:20]},
                ranges=marks,
            ))
        return findings
