"""action 有效性 —— 监督信号是否真的存在、是否可用。

为什么重要
----------
**action 是 ACT 的监督信号**（``L1(预测, action)``）。没有它就算不出 loss。
而它是**派生**出来的，不是采出来的，所以有四种失效方式：

  ① 工作流没跑 / 跑失败 → action 还是采集端的**全零占位**
     ★ 最阴险：有列、有 7 维、能加载，全是 0 —— 不报任何错
  ② 整集派生失败（位姿样本不足）→ 列根本不写 → 导出时缺失
     ★ 训练直接 KeyError（2026-09-11 踩过）
  ③ 部分帧派生失败 → 那些帧的 action 是 null
  ④ 派生成功但数值离群 → 归一化统计量被撑大（交给 umi.slam_continuity 解释）

严重度
------
① ② 判 **FAIL** —— 没有监督信号，这一集对训练毫无价值。
③ 判 **WARN** —— 部分帧缺失，剩余帧仍可训练。

关于"全零"
----------
采集端在 UMI 硬件上写的就是全零（硬件不录 action），所以**必须区分**
"还没派生"和"派生了但动作真的是零"。判据是**整列方差**：
派生的 action 即使动作很小，帧间也会有变化；全零占位则方差严格为 0。
"""

from __future__ import annotations

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, PASS, WARN, FAIL,
)


@register_check
class UmiActionPresentCheck(CleaningCheck):
    slug = "umi.action_present"
    label = "Action Validity"
    version = "1.0"
    description = "Missing supervision signal, all-zero placeholder, or frames never derived"

    requires_channels = ("action",)

    # 属于哪些设备卡片 —— 前端设置面板按它分 tab

    device_cards = ("gripper_device",)

    default_params = {
        "min_coverage": 0.95,     # 有 action 的帧占比下限
        "zero_epsilon": 1e-9,     # 判定"全零"的容差
    }
    default_severity = FAIL

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        min_coverage = float(self.param(ctx, "min_coverage", 0.95))
        epsilon = float(self.param(ctx, "zero_epsilon", 1e-9))

        for stream in ctx.streams_of("action"):
            rows = int(stream.rows or 0)
            if not rows:
                # 列存在但一行都没有 —— 与"列不存在"同义
                findings.append(self.finding(
                    ctx, FAIL, stream=stream.key,
                    message="the action column has no data at all (derivation failed for the whole episode)",
                    metrics={"rows": 0},
                ))
                continue

            values = stream.series.get("action") or ()
            usable = [item for item in values if item]
            coverage = len(usable) / rows if rows else 0.0

            # 整列方差 —— 区分"全零占位"和"动作真的很小"
            flat: list[float] = []
            for item in usable[:5000]:          # 抽样即可，全零占位必然处处为零
                try:
                    flat.extend(float(value) for value in item)
                except (TypeError, ValueError):
                    continue
            spread = (max(flat) - min(flat)) if flat else 0.0
            all_zero = spread <= epsilon

            metrics = {
                "rows": rows,
                "usable": len(usable),
                "coverage": round(coverage, 4),
                "value_spread": spread,
            }

            if all_zero and usable:
                findings.append(self.finding(
                    ctx, FAIL, stream=stream.key,
                    message="action is all zeros - likely still the recorder placeholder (workflow never ran, or derivation failed)",
                    metrics=metrics,
                ))
                continue

            if not usable:
                findings.append(self.finding(
                    ctx, FAIL, stream=stream.key,
                    message="the action column exists but every value is empty (derivation produced no frames)",
                    metrics=metrics,
                ))
                continue

            if coverage < min_coverage:
                findings.append(self.finding(
                    ctx, WARN, stream=stream.key,
                    message=f"only {len(usable)}/{rows} frames have action"
                            f"（{coverage:.1%}），其余帧无监督信号",
                    metrics=metrics,
                ))
                continue

            findings.append(self.finding(ctx, PASS, stream=stream.key,
                                         metrics=metrics))
        return findings
