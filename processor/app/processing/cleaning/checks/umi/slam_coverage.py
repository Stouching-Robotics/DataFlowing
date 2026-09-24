"""SLAM 位姿覆盖率 —— 多少帧的位姿是**插值补出来的**。

★ 别把"自己格子为空"当成问题
--------------------------------
采集端的位姿流是 20/20/60ms **突发**，而数据帧是 1/30s 均匀网格。某个
33ms 窗口里一个位姿都没到，这一格的缓冲就是空的 —— **这是设计使然**。

实测（52 集训练集）：**28.2% 的帧自己格子为空**（p90 32.8%）。
第一版检查项拿这个当指标，结果 52/52 集全部报警 —— 典型的阈值照搬。

真正该关心的是：既没有本地样本、**相邻格子也没有**，只能靠插值硬补的帧。
``poses_from_trajectory`` 汇总所有格子按时间取最近样本，实测能救回绝大多数
空帧，只有剩下的才插值。

实测（同上，4 集抽样）：
    集 0: 插值  2/326 = 0.6%
    集 1: 插值 17/313 = 5.4%   ← ★ 明显高于其他集
    集 2: 插值  4/384 = 1.0%
    集 7: 插值  0/308 = 0.0%

★ 集 1 正是 ``umi.slam_continuity`` 报出 6.8 m/s 跳变的那一集 ——
两个独立指标指向同一件事：那一集 SLAM 跟丢过。
所以这两项要**一起看**：插值率高 + 有跳变 = 基本可以确定是跟踪丢失。

判定口径
--------
默认 **WARN**：插值帧本身不代表数据不可用（插值的是平滑轨迹），
它是"这段 SLAM 不稳"的信号。
"""

from __future__ import annotations

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, PASS, WARN, FAIL,
)


@register_check
class UmiSlamCoverageCheck(CleaningCheck):
    slug = "umi.slam_coverage"
    label = "SLAM Pose Coverage"
    version = "2.0"
    description = "Poses only recoverable by interpolation (no real sample in nearby slots)"

    requires_channels = ("slam",)

    # 属于哪些设备卡片 —— 前端设置面板按它分 tab

    device_cards = ("gripper_device",)

    # 实测正常集 0~1%，异常集 5.4%。起点值取 2% / 10%：
    # 低于 2% 属正常抖动，超过 10% 说明这一段基本没有真实位姿。
    # ★ 这两个数是拿 52 集训练集跑出来的，不是拍脑袋 —— 但样本只有 52 集，
    #   上生产前应拿更多数据复核。
    default_params = {
        "warn_ratio": 0.02,
        "fail_ratio": 0.10,
    }
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        warn_ratio = float(self.param(ctx, "warn_ratio", 0.02))
        fail_ratio = float(self.param(ctx, "fail_ratio", 0.10))

        for stream in ctx.streams_of("slam"):
            # evidence 提供两种粒度：
            #   "interpolated" —— 逐帧布尔（本检查项用这个，能出区间）
            #   "coverage"     —— 直接给汇总数字（{"frames","direct","interpolated"}）
            flags = stream.series.get("interpolated")
            summary = stream.series.get("coverage") or {}
            if flags:
                total = len(flags)
                count = sum(1 for item in flags if item)
            elif summary:
                total = int(summary.get("frames") or 0)
                count = int(summary.get("interpolated") or 0)
            else:
                continue
            if not total:
                continue
            ratio = count / total

            metrics = {
                "frames": total,
                "interpolated_frames": count,
                "interpolated_ratio": round(ratio, 4),
            }

            if ratio < warn_ratio:
                findings.append(self.finding(ctx, PASS, stream=stream.key,
                                             metrics=metrics))
                continue
            findings.append(self.finding(
                ctx,
                FAIL if ratio >= fail_ratio else WARN,
                stream=stream.key,
                message=f"{count}/{total} poses ({ratio:.1%}) are interpolation-"
                        f"only - no real sample in the local or neighbouring slots,"
                        f" which suggests SLAM lost tracking (review together with"
                        f" SLAM Trajectory Continuity)",
                metrics=metrics,
            ))
        return findings
