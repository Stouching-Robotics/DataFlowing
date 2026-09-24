"""手套连接状态 —— 录制途中掉线。

为什么重要
----------
手套掉线时阵列读数会**静默保持最后一个值**，看起来完全正常：有列、有 16×16、
数值也在合理范围。只有状态列说实话。所以这一项是唯一能看见"设备其实没了"
的检查。

判定口径（分两种，因为状态词表只见过一个值）
------------------------------------------
``connected`` 是实测见到的**唯一**取值（S80C / D435 两个项目、全部集、全部帧）。
其余词表未知，所以：

  录制中途**状态发生变化**  → **FAIL**
      无论变成什么，从"连着"变成别的就是掉线，这个判断不依赖词表。

  全程恒定但不是 ``connected`` → **WARN**
      可能是掉线，也可能是我们没见过的正常状态词（如 "ok"/"active"）。
      词表未证实之前不拦批次 —— 拦错了用户会开始无脑点通过，比不拦更糟。

  空字符串 → **WARN**（这一帧没读到状态）

★ 阈值不放进 ``default_params``：词表是字符串，而前端设置面板把所有参数都渲染
  成数字输入框。硬塞会把面板显示成 NaN。真需要可配时先改面板的类型支持。
"""

from __future__ import annotations

from app.processing.cleaning.checks import (
    CheckContext, CleaningCheck, register_check,
    Finding, PASS, WARN, FAIL,
)

# 实测唯一见过的取值。其余一律按"词表未知"处理，不直接判失败。
_HEALTHY = "connected"


@register_check
class GloveDeviceStatusCheck(CleaningCheck):
    slug = "glove.device_status"
    label = "Glove Connection"
    version = "1.0"
    description = "Device dropped out mid-recording (from status.*_glove)"

    requires_channels = ("device_status",)
    device_cards = ("glove_sensor",)
    default_params: dict = {}
    default_severity = FAIL

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []

        for stream in ctx.streams_of("device_status"):
            values = tuple(stream.series.get("device_status") or ())
            # 流名就是那一列的最后一段（left_glove / right_glove），用它报位置
            hand = stream.key
            if not values:
                findings.append(self.finding(
                    ctx, WARN, stream=hand,
                    message=f"{hand}: no status column data",
                    metrics={"frames": 0},
                ))
                continue

            blanks = sum(1 for item in values if not item)
            distinct: list[str] = []
            for item in values:
                if item and item not in distinct:
                    distinct.append(item)

            # ① 中途变过 —— 与词表无关，见到即掉线
            if len(distinct) > 1:
                changes = [i for i in range(1, len(values)) if values[i] != values[i - 1]]
                findings.append(self.finding(
                    ctx, FAIL, stream=hand,
                    message=(f"{hand}: status changed {len(changes)} times "
                             f"during recording: {' -> '.join(distinct)}"),
                    metrics={"frames": len(values), "distinct": distinct,
                             "first_change_frame": changes[0] if changes else None,
                             "blank_frames": blanks},
                ))
                continue

            status = distinct[0] if distinct else ""
            # ② 恒定但词表未知 / ③ 全空
            if not status:
                findings.append(self.finding(
                    ctx, WARN, stream=hand,
                    message=f"{hand}: status column is entirely blank",
                    metrics={"frames": len(values), "blank_frames": blanks},
                ))
            elif status != _HEALTHY:
                findings.append(self.finding(
                    ctx, WARN, stream=hand,
                    message=(f"{hand}: status is constantly {status!r} (only "
                             f"{_HEALTHY!r} has ever been observed; vocabulary "
                             f"unconfirmed - please verify manually)"),
                    metrics={"frames": len(values), "status": status},
                ))
            else:
                findings.append(self.finding(
                    ctx, PASS, stream=hand,
                    message=f"{hand}: {_HEALTHY} for the whole episode",
                    metrics={"frames": len(values), "status": status},
                ))

        return findings
