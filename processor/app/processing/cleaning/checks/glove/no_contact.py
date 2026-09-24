"""整集无接触 —— 手套没戴上、没开机，或阵列整体失效。

为什么单列一项
--------------
它和"某一根手指没碰"完全不同量级：后者是正常的（实测单集里 30–86% 的感应点
全程为 0，因为只压到阵列的一部分）。**整只手套一帧都没有值**才是设备层面的问题。

判据保守是故意的
----------------
实测最低的一集也有 **11.4%** 的帧存在压力，所以"全零"离正常值有极大余量，
不需要为它编阈值。``min_active_ratio`` 默认 0.0 = 只在**整集恒为 0** 时才报，
用户想更严可以往上调。

只报 WARN：手套没戴上也可能是**操作员故意的**（这一段就是不给触觉），
没到判失败的程度。
"""

from __future__ import annotations

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, PASS, WARN,
)


@register_check
class GloveNoContactCheck(CleaningCheck):
    slug = "glove.no_contact"
    label = "No Contact"
    version = "1.0"
    description = "No pressure readings at all in this episode (not worn / not working)"

    requires_channels = ("tactile",)
    device_cards = ("glove_sensor",)
    default_params = {
        # 0.0 = 只在整集恒为 0 时报。实测最低 11.4%，余量充足。
        "min_active_ratio": 0.0,
    }
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        min_ratio = float(self.param(ctx, "min_active_ratio", 0.0))

        for stream in ctx.streams_of("tactile"):
            values = tuple(stream.series.get("tactile") or ())
            if not values:
                continue

            active = sum(1 for frame in values
                         if frame and any(value > 0.0 for value in frame))
            ratio = active / len(values)

            if ratio > min_ratio:
                findings.append(self.finding(
                    ctx, PASS, stream=stream.key,
                    message=f"{stream.key}: has contact frames",
                    metrics={"frames": len(values), "active_frames": active,
                             "active_ratio": round(ratio, 4)},
                ))
            else:
                findings.append(self.finding(
                    ctx, WARN, stream=stream.key,
                    message=(f"{stream.key}: no pressure readings in any of the "
                             f"{len(values)} frames (not worn, or the array failed)"),
                    metrics={"frames": len(values), "active_frames": active,
                             "active_ratio": round(ratio, 4)},
                ))

        return findings
