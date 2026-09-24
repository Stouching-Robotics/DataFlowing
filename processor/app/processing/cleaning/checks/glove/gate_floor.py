"""触觉阵列取值下限 —— 检测**编码/量纲漂移**。

为什么需要它
------------
实测这份手套数据有个硬门限：取值**要么是 0，要么 ≥ 500.02**，0 到 500 之间
一个值都没有（从 0 直接跳到 508.09，没有过渡）。所以

    ``0`` 的含义是"未接触"，**不是"力为零"**

这个门限是固件行为，也是所有下游解读的前提。它一旦变了，**别的一切看起来都还
正常**：列还在、还是 16×16、数还在合理区间 —— 但含义已经不同了。典型触发：

  * 固件改版，直接输出原始 ADC（0–4095）而不是门限后的值
  * 标定系数 ``scale`` 被应用了两次（或该应用而没应用）
  * 换了另一款手套

判据
----
非零值里低于 ``min_contact_value``（默认 500）的**占比**。
超出 ``fail_ratio`` 判 FAIL，否则 WARN。实测两个项目全部集都是 0 个越界值，
所以正常数据有充足余量。
"""

from __future__ import annotations

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, PASS, WARN, FAIL,
)


@register_check
class GloveGateFloorCheck(CleaningCheck):
    slug = "glove.gate_floor"
    label = "Contact Threshold"
    version = "1.0"
    description = "Non-zero values must exceed the contact floor (catches silent encoding changes)"

    requires_channels = ("tactile",)
    device_cards = ("glove_sensor",)
    default_params = {
        # 实测最小非零值 500.02。留一点余量给固件微调，但远低于它就没有意义了
        # —— 那正是"编码变了"的样子。
        "min_contact_value": 500.0,
        "fail_ratio": 0.05,       # 非零值里超过 5% 低于门限 → FAIL
    }
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        floor = float(self.param(ctx, "min_contact_value", 500.0))
        fail_ratio = float(self.param(ctx, "fail_ratio", 0.05))

        for stream in ctx.streams_of("tactile"):
            values = tuple(stream.series.get("tactile") or ())
            flat = [float(v) for item in values if item for v in item]
            nonzero = [v for v in flat if v > 0.0]
            if not nonzero:
                # 整集没有非零值 —— 那是 glove.no_contact 的事，这里不重复报
                continue

            below = [v for v in nonzero if v < floor]
            ratio = len(below) / len(nonzero)

            if not below:
                findings.append(self.finding(
                    ctx, PASS, stream=stream.key,
                    message=f"{stream.key}: all non-zero values above the floor",
                    metrics={"nonzero": len(nonzero), "min_value": round(min(nonzero), 3)},
                ))
                continue

            status = FAIL if ratio > fail_ratio else WARN
            findings.append(self.finding(
                ctx, status, stream=stream.key,
                message=(f"{stream.key}: {len(below)}/{len(nonzero)} non-zero values"
                         f" are below the contact floor {floor:g} (min {min(below):.3f})"
                         f" - the array encoding may have changed"),
                metrics={
                    "nonzero": len(nonzero), "below_floor": len(below),
                    "below_ratio": round(ratio, 4),
                    "min_contact_value": floor,
                    "min_value": round(min(nonzero), 3),
                },
            ))

        return findings
