"""触觉阵列卡死 —— 设备停止更新，录制端却继续写。

为什么重要
----------
这是最难发现的一类：**录制链路完全正常**。帧数对得上、时间戳连续、阵列是完整
的 16×16、数值也在合理区间 —— 只是内容是**很久以前的那一帧**，一直没变。
事后看数据一切正常，只有对比相邻帧才发现它静止了。

判定口径（阈值有实测依据）
--------------------------
**静止本身不是问题**。实测设备更新率约 7.5Hz，而录制是 30fps，所以同一个值会
**连续保持 4 帧** —— 这是正常的，不是卡死。

  S80C 全部集：受压时的保持段长度**只有 4**（`{4}`，无其他取值）
  D435 全部集：没有受压保持段

（本文件统计的是"保持帧数"= 一个取值连续占了多少帧。同一现象另一种常见记法是
"相邻帧相等的对数"，那个数是 3 —— 4 帧有 3 个间隔。两个数不矛盾，别混。）

所以阈值有充足余量：``warn_frames=40``（约 1.3 秒）已经远超正常值。取 40 而不是
"比 4 大一点"，是给将来换更新率更低的设备留空间 —— 卡死检测宁可慢一点发现，
也不该把正常的低更新率设备全报一遍。

★ 只在**受压时**才判。全零段天然"完全相同"（都是零），把它算进去的话，一集里
  88% 都是全零帧的批次会报出几百帧的假静止（实测踩过：最长静止段 575 帧，全部
  来自全零段）。
"""

from __future__ import annotations

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, PASS, WARN, FAIL,
)
from app.processing.cleaning.contract import Range

# 报告的区间数上限 —— 真卡死时可能几百段，全塞进报告没意义
_MAX_RANGES = 20


def _static_runs(values: tuple, min_length: int) -> list[tuple[int, int]]:
    """受压时完全相同的连续帧段 ``[(起始帧, 长度), ...]``。

    只统计**非空且有压力**的帧：全零段天然完全相同，算进来会造出大片假区间。
    """
    runs: list[tuple[int, int]] = []
    total = len(values)
    index = 0
    while index < total:
        # 向后找同一取值的最长延伸
        end = index
        while end + 1 < total and values[end + 1] == values[index]:
            end += 1
        length = end - index + 1
        frame = values[index]
        pressed = bool(frame) and any(value > 0.0 for value in frame)
        if length >= min_length and pressed:
            runs.append((index, length))
        index = end + 1
    return runs


@register_check
class GloveFrozenArrayCheck(CleaningCheck):
    slug = "glove.frozen_array"
    label = "Tactile Array Frozen"
    version = "1.0"
    description = "Array unchanged for a long stretch while pressed (device stopped updating)"

    requires_channels = ("tactile",)
    device_cards = ("glove_sensor",)
    default_params = {
        "warn_frames": 40,        # 约 1.3 秒 @30fps；实测正常值 3 帧
        "fail_frames": 300,       # 约 10 秒
    }
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        fps = float(ctx.fps or 0.0) or 30.0
        warn_frames = int(self.param(ctx, "warn_frames", 40))
        fail_frames = int(self.param(ctx, "fail_frames", 300))

        for stream in ctx.streams_of("tactile"):
            values = tuple(stream.series.get("tactile") or ())
            if not values:
                continue

            runs = _static_runs(values, warn_frames)
            if not runs:
                findings.append(self.finding(
                    ctx, PASS, stream=stream.key,
                    message=f"{stream.key}: no abnormal frozen stretch",
                    metrics={"frames": len(values), "warn_frames": warn_frames},
                ))
                continue

            longest = max(length for _, length in runs)
            status = FAIL if longest >= fail_frames else WARN
            ranges = tuple(
                Range(
                    start_sec=start / fps,
                    end_sec=(start + length - 1) / fps,
                    code=self.slug,
                    severity=FAIL if length >= fail_frames else WARN,
                    stream=stream.key,
                    channel="tactile",
                    start_frame=start,
                    end_frame=start + length - 1,
                    rule=self.slug,
                    message=f"array unchanged for {length} frames",
                    evidence={"frozen_frames": length},
                )
                for start, length in runs[:_MAX_RANGES]
            )

            findings.append(self.finding(
                ctx, status, stream=stream.key,
                message=(f"{stream.key}: {len(runs)} frozen stretches while "
                         f"pressed (longest {longest} frames ~= {longest / fps:.1f}s)"),
                metrics={
                    "frames": len(values),
                    "static_runs": len(runs),
                    "longest_run_frames": longest,
                    "longest_run_sec": round(longest / fps, 3),
                    "warn_frames": warn_frames,
                    "fail_frames": fail_frames,
                },
                ranges=ranges,
            ))

        return findings
