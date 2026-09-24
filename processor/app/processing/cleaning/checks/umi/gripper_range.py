"""夹爪量程与力基线 —— 开合度越界、力读数漂移。

为什么重要
----------
**开合度越界**：``umi_slam_action._gripper_fraction()`` 会把开合度
``np.clip(0, 1)``。也就是说越界值在派生 action 时被**静默夹掉**了 ——
标定异常的集看起来"正常"，但动作里的夹爪维度是失真的。本项就是在
clip 之前把它抓出来（原始列 ``observation.gripper_state[0]`` 是 0–100）。

**力基线漂移**：夹爪没接触物体时力读数应在零附近。若整集的静息基线缓慢
上移，说明传感器漂移/温漂，用力做阈值判断的模型会失真。

判定口径
--------
开合度越界默认判 **WARN**（数据还能用，但要知道标定有问题）；
力漂移默认判 **WARN**（同上）。两者都不拦数据，只报告。
"""

from __future__ import annotations

import math

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, Range, PASS, WARN,
)

# 原始列里开合度的量程（采集端契约：[open_pct, gripped, fz_mn]）
OPEN_PCT_MIN = 0.0
OPEN_PCT_MAX = 100.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


@register_check
class UmiGripperRangeCheck(CleaningCheck):
    slug = "umi.gripper_range"
    label = "Gripper Range / Force Baseline"
    version = "1.0"
    description = "Gripper opening out of range, or force baseline drift"

    requires_channels = ("gripper_state",)

    # 属于哪些设备卡片 —— 前端设置面板按它分 tab

    device_cards = ("gripper_device",)

    default_params = {
        "max_out_of_range_ratio": 0.01,   # 越界帧占比上限
        "max_force_drift": 200.0,          # 前后半段基线差（mN）
    }
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        max_ratio = float(self.param(ctx, "max_out_of_range_ratio", 0.01))
        max_drift = float(self.param(ctx, "max_force_drift", 200.0))

        for stream in ctx.streams_of("gripper_state"):
            samples = stream.series.get("gripper_state") or ()
            if not samples:
                continue
            fps = float(stream.fps or ctx.fps or 30.0)

            out_of_range: list[Range] = []
            values: list[float] = []
            for index, sample in enumerate(samples):
                if not sample:
                    continue
                try:
                    pct = float(sample[0])
                except (TypeError, ValueError, IndexError):
                    continue
                values.append(pct)
                if OPEN_PCT_MIN <= pct <= OPEN_PCT_MAX:
                    continue
                out_of_range.append(Range(
                    start_sec=index / fps, end_sec=(index + 1) / fps,
                    code=self.slug, severity=WARN,
                    stream=stream.key, channel="gripper_state",
                    start_frame=index, end_frame=index,
                    rule=self.slug,
                    message=f"opening {pct:.1f} is outside the 0-100 range"
                            f"（派生 action 时会被静默夹掉）",
                    evidence={"open_pct": round(pct, 2)},
                ))

            if not values:
                continue

            ratio = len(out_of_range) / len(values)
            metrics: dict = {
                "samples": len(values),
                "out_of_range": len(out_of_range),
                "out_of_range_ratio": round(ratio, 4),
                "min": round(min(values), 2),
                "max": round(max(values), 2),
            }

            if ratio > max_ratio:
                findings.append(self.finding(
                    ctx, WARN, stream=stream.key,
                    message=f"{len(out_of_range)}/{len(values)} frames out of range"
                            f"（{ratio:.1%}），标定可能异常",
                    metrics=metrics,
                    ranges=out_of_range[:50],
                ))
            else:
                findings.append(self.finding(ctx, PASS, stream=stream.key,
                                             metrics=metrics))

        # 力基线漂移 —— 独立的信道，单独一轮
        for stream in ctx.streams_of("force"):
            forces = stream.series.get("force") or ()
            if len(forces) < 20:
                continue
            magnitudes = []
            for sample in forces:
                if not sample:
                    continue
                try:
                    magnitudes.append(_norm3(sample))
                except (TypeError, ValueError):
                    continue
            if len(magnitudes) < 20:
                continue

            half = len(magnitudes) // 2
            first, second = _median(magnitudes[:half]), _median(magnitudes[half:])
            drift = abs(second - first)
            metrics = {"baseline_first_half": round(first, 2),
                       "baseline_second_half": round(second, 2),
                       "drift_mN": round(drift, 2)}

            if drift <= max_drift:
                findings.append(self.finding(ctx, PASS, stream=stream.key,
                                             metrics=metrics))
                continue
            findings.append(self.finding(
                ctx, WARN, stream=stream.key,
                message=f"force baseline drift {drift:.0f} mN"
                        f"（前半 {first:.0f} → 后半 {second:.0f}），疑似传感器漂移",
                metrics=metrics,
            ))
        return findings


def _norm3(sample) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in sample[:3]))
