"""SLAM 轨迹连续性 —— 相邻位姿之间出现不可能的速度/角速度跳变。

为什么重要
----------
UMI 的 action 是从 SLAM 位姿**差分**出来的（见 ``app/umi_slam_action.py``）。
位姿一旦跳变，差分出来的动作就是一个巨大的假位移；模型会学到"突然瞬移"。
官方也承认 ORB_SLAM3 是整条流水线"最脆弱的部分"。

常见来源：视觉跟踪丢失后重定位、弱纹理/暗光、快速转手腕导致运动模糊。

判定分两档（这正是本项的设计，不是"只报告不判定"）
--------------------------------------------------
方案明确警告：「SLAM 单帧跳了 113.9 毫米可以作为问题线索，但不能马上定成
全平台红线」。113.9mm 来自本平台实测（`umi_slam_action.py` 的 docstring 记录了
插值方案会横跨重定位跳变），它是**观测线索**不是阈值来源。

所以**单次跳变只判 WARN** —— 一次重定位不该废掉一整集（实测
UMIGripper_Action_AI_000001 就有一次真实的 6.8 m/s 跳变，属"该警告、不该拦"）。

但跳变**达到 ``fail_jumps``（默认 10 处）判 FAIL**：那已经不是偶发重定位，而是
SLAM 全程没跟住，差分出来的动作整段都是假的 —— 这种数据确实不能训，按"数据不对
就拦回人工审核"处理。

★ 本文件的旧注释曾写成"默认判 WARN 而非 FAIL……只报告"，与实现不符（实现一直
  是分档的）。以本段为准。阈值本身仍须用人工确认过的好/坏数据校准后才固化。
"""

from __future__ import annotations

import math

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, Range, PASS, WARN, FAIL,
)
from app.processing.cleaning.contract import pose_is_valid


def _norm3(vector) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in vector[:3]))


@register_check
class UmiSlamContinuityCheck(CleaningCheck):
    slug = "umi.slam_continuity"
    label = "SLAM Trajectory Continuity"
    version = "1.0"
    description = "Impossible linear or angular velocity between adjacent poses"

    requires_channels = ("slam",)

    # 属于哪些设备卡片 —— 前端设置面板按它分 tab

    device_cards = ("gripper_device",)

    # 单位：米/秒、弧度/秒。人手快速挥动约 2 m/s、6 rad/s 量级，
    # 这里给的是"明显超出人类可达"的起点值。
    #
    # 判级用两个计数阈值，而不是"速度超过多少倍"—— 后者在默认值下会退化成
    # "任何一次跳变都判失败"（一次跳变就废掉一整集太重了）。实测 episode 1
    # 帧 293 有一次 6.8 m/s 的真实重定位跳变，属"该警告、但不该直接拦"。
    default_params = {
        "max_linear_mps": 5.0,
        "max_angular_rps": 20.0,
        "warn_jumps": 1,      # 达到此数即警告
        "fail_jumps": 10,     # 达到此数判失败（说明 SLAM 全程不稳）
    }
    default_severity = WARN

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        max_linear = float(self.param(ctx, "max_linear_mps", 5.0))
        max_angular = float(self.param(ctx, "max_angular_rps", 20.0))
        fail_jumps = int(self.param(ctx, "fail_jumps", 10))

        for stream in ctx.streams_of("slam"):
            poses = stream.series.get("pose") or ()
            stamps = stream.series.get("t") or ()
            if len(poses) < 2:
                continue
            fps = float(stream.fps or ctx.fps or 30.0)
            dt_default = 1.0 / fps if fps > 0 else 1.0 / 30.0

            jumps: list[Range] = []
            peak_linear = 0.0
            peak_angular = 0.0

            for index in range(1, len(poses)):
                prev, cur = poses[index - 1], poses[index]
                # ★ 必须用 pose_is_valid，不能只判长度。
                #
                # 全零占位位姿长度也是 7，能过 len 检查，但四元数模为 0 ——
                # 两个零四元数点积为 0，acos(0)=π/2 ⇒ 夹角 180° ⇒ 每帧
                # 94.25 rad/s。实测 Test94 的 slam_pose 有 437/599 行是全零，
                # 于是 599 帧报出 465 处"跳变"，整集被假判 FAIL 并拦住批次。
                if not pose_is_valid(prev) or not pose_is_valid(cur):
                    continue
                try:
                    delta = [float(cur[i]) - float(prev[i]) for i in range(3)]
                except (TypeError, ValueError):
                    continue

                # 时间间隔取真实时间戳；缺失时回落到 fps
                dt = dt_default
                if len(stamps) > index and stamps[index] and stamps[index - 1]:
                    try:
                        span = float(stamps[index]) - float(stamps[index - 1])
                        if 0 < span < 1.0:      # 超过 1 秒说明中间丢了整段
                            dt = span
                    except (TypeError, ValueError):
                        pass

                linear = _norm3(delta) / dt
                # 姿态差：四元数点积 → 夹角。|dot| 接近 1 表示几乎没转。
                dot = sum(float(prev[3 + i]) * float(cur[3 + i]) for i in range(4))
                angle = 2.0 * math.acos(max(-1.0, min(1.0, abs(dot))))
                angular = angle / dt

                peak_linear = max(peak_linear, linear)
                peak_angular = max(peak_angular, angular)

                if linear <= max_linear and angular <= max_angular:
                    continue
                jumps.append(Range(
                    start_sec=index / fps, end_sec=(index + 1) / fps,
                    code=self.slug, severity=WARN,
                    stream=stream.key, channel="slam",
                    start_frame=index - 1, end_frame=index,
                    rule=self.slug,
                    message=f"pose jump at frame {index}: "
                            f"{_norm3(delta) * 1000:.1f}mm / {math.degrees(angle):.1f}°",
                    evidence={"linear_mps": round(linear, 3),
                              "angular_rps": round(angular, 3)},
                ))

            metrics = {
                "frames": len(poses),
                "jumps": len(jumps),
                "peak_linear_mps": round(peak_linear, 3),
                "peak_angular_rps": round(peak_angular, 3),
            }
            if not jumps:
                findings.append(self.finding(ctx, PASS, stream=stream.key,
                                             metrics=metrics))
                continue

            findings.append(self.finding(
                ctx,
                FAIL if len(jumps) >= fail_jumps else WARN,
                stream=stream.key,
                message=f"{len(jumps)} pose jumps, peak "
                        f"{peak_linear:.1f} m/s / "
                        f"{math.degrees(peak_angular):.0f} deg/s "
                        f"(verify against the video)",
                metrics=metrics,
                ranges=jumps[:50],       # 报告里最多带 50 个，避免 JSON 过大
            ))
        return findings
