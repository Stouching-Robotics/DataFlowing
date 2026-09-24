"""触觉阵列形状 —— 每帧必须是一整块 16×16。

为什么值得单列一项
------------------
看着像废话，但它是**静默截断的哨兵**：采集层曾经因为宽度表里没有 ``tactile``
而落到 3 的兜底值，256 个感应点被悄悄截成 3 个 —— 有列、有值、能加载，检查项
只看得到 1% 的阵列，而且不报任何错。同一类问题还会再来（换手套、换固件、改列
宽），所以这里明确把"整块阵列"写成契约。

判据
----
每帧长度必须等于 ``expected_size``（默认 256 = 16×16）。空元组（``None`` 帧）
也算不合格 —— 它意味着这一帧根本没读到。

超出 ``max_bad_ratio`` 判 FAIL，否则 WARN：单帧缺失多半是偶发，系统性截断会是
接近 100% 的比例。
"""

from __future__ import annotations

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, PASS, WARN, FAIL,
)


@register_check
class GloveArrayShapeCheck(CleaningCheck):
    slug = "glove.array_shape"
    label = "Tactile Array Integrity"
    version = "1.0"
    description = "Every frame must be a complete 16×16 array (catches silent truncation)"

    requires_channels = ("tactile",)
    device_cards = ("glove_sensor",)
    default_params = {
        "expected_size": 256,     # 16×16
        "max_bad_ratio": 0.01,    # 不合格帧超过 1% 判 FAIL
    }
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        expected = int(self.param(ctx, "expected_size", 256))
        max_ratio = float(self.param(ctx, "max_bad_ratio", 0.01))

        for stream in ctx.streams_of("tactile"):
            values = tuple(stream.series.get("tactile") or ())
            total = len(values)
            if not total:
                # 列在但一行都没有 —— 与"列不存在"同义，交给别的检查去报
                continue

            bad = [len(item) for item in values if len(item) != expected]
            ratio = len(bad) / total
            # 出现过的长度 —— 一眼看出是被截断(如 3)还是别的形状
            observed = sorted({len(item) for item in values})

            if not bad:
                findings.append(self.finding(
                    ctx, PASS, stream=stream.key,
                    message=f"{stream.key}: {expected}-wide for the whole episode",
                    metrics={"frames": total, "expected_size": expected},
                ))
                continue

            status = FAIL if ratio > max_ratio else WARN
            findings.append(self.finding(
                ctx, status, stream=stream.key,
                message=(f"{stream.key}: {len(bad)}/{total} frames are not "
                         f"{expected}-wide (observed lengths {observed[:5]})"),
                metrics={
                    "frames": total, "bad_frames": len(bad),
                    "bad_ratio": round(ratio, 4),
                    "expected_size": expected,
                    "observed_sizes": observed[:10],
                    "first_bad_frame": next(
                        (i for i, item in enumerate(values) if len(item) != expected), None),
                },
            ))

        return findings
