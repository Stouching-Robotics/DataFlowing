"""RgbReadWatch / MaxLagWatch 单测（v1.3.10）。

覆盖：三态告警（进入/限频/恢复）、**门槛边界不许提前开火**、短促抖动静默但记账、
      恢复行的停摆时长、进行中的停摆计入快照、覆盖计数、滞后峰值、reset。

去抖契约照抄 core/gripper/slam/process_controller.py 的 FaysRateAlarm：健康态静默，
一次抽风只留两行（进入 + 恢复），长期故障按 repeat 限频。这里的断言就是那个契约。

用法:
    venv/bin/python tools/tests/test_rgb_quality.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.gripper.rgb_quality import MaxLagWatch, RgbReadWatch

ALERT_NS = 1_000_000_000         # 1s
REPEAT_NS = 30_000_000_000       # 30s
T0 = 1_000_000_000_000

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def new_watch():
    return RgbReadWatch(alert_ns=ALERT_NS, repeat_ns=REPEAT_NS, tag="夹爪-RGB")


def main():
    # ── 健康态：完全静默 ────────────────────────────────────
    w = new_watch()
    check(w.note_ok(T0) == [], "健康帧不产生日志行")
    check(w.note_ok(T0 + 33_000_000) == [], "持续健康仍然静默")
    s = w.snapshot()
    check(s == {"readfail_ns": 0, "readfail_max_ns": 0,
                "readfail_stall_count": 0, "overwrite_count": 0},
          f"健康态快照全零: {s}")

    # ── 门槛边界：alert-1ns 不许开火 ────────────────────────
    w = new_watch()
    w.note_fail(T0)
    check(w.note_fail(T0 + ALERT_NS - 1) == [],
          "停摆 = 门槛-1ns → 不开火（防误报的关键断言）")
    lines = w.note_fail(T0 + ALERT_NS)
    check(len(lines) == 1, "停摆 = 门槛 → 开火一次")
    print(f"       {lines[0]}")
    check("RGB 采集停摆 1.0s" in lines[0] and lines[0].startswith("[夹爪-RGB告警]"),
          "进入行含停摆时长与标签")
    check(lines[0].count("；最近错误") == 0, "无错误信息时不留空尾巴")

    # ── 限频：repeat 之前闭嘴，到点再喊一次 ─────────────────
    check(w.note_fail(T0 + ALERT_NS + REPEAT_NS - 1) == [],
          "持续停摆未到 repeat → 静默（不刷屏）")
    lines = w.note_fail(T0 + ALERT_NS + REPEAT_NS)
    check(len(lines) == 1 and "仍未恢复" in lines[0],
          "到 repeat → 打一行「仍未恢复」")
    print(f"       {lines[0]}")
    check(w.note_fail(T0 + ALERT_NS + REPEAT_NS + 1) == [],
          "重复行之后立刻又静默（限频生效）")

    # ── 恢复：收尾一行 + 记账 ──────────────────────────────
    stall_end = T0 + 4_680_800_000
    lines = w.note_ok(stall_end, reconnects=2, error="timed out")
    check(len(lines) == 1 and "已恢复" in lines[0], "报过警的停摆恢复时打一行收尾")
    print(f"       {lines[0]}")
    check("停摆 4.7s" in lines[0] and "重连 2 次" in lines[0]
          and "最近错误: timed out" in lines[0], "恢复行含停摆时长/重连/最近错误")
    check("本段视频该处会有一段静止" in lines[0], "恢复行点明视频后果（用户看得到的现象）")
    s = w.snapshot()
    check(s["readfail_ns"] == 4_680_800_000, "恢复后停摆时长入账")
    check(s["readfail_max_ns"] == 4_680_800_000, "峰值入账")
    check(s["readfail_stall_count"] == 1, "停摆次数 1")
    check(w.note_ok(stall_end + 33_000_000) == [], "恢复之后回到静默")

    # ── 短促抖动：静默但记账（不许因为没报警就不算数）────────
    w = new_watch()
    w.note_fail(T0)
    w.note_fail(T0 + 200_000_000)
    check(w.note_ok(T0 + 300_000_000) == [], "未达门槛的抖动不产生任何日志行")
    s = w.snapshot()
    check(s["readfail_ns"] == 300_000_000 and s["readfail_stall_count"] == 1,
          "未报警的停摆照样入账（日志静默 ≠ 没发生）")

    # ── 多次停摆：累计 + 取最大 ────────────────────────────
    w = new_watch()
    w.note_fail(T0)
    w.note_ok(T0 + 2_000_000_000)
    w.note_fail(T0 + 10_000_000_000)
    w.note_ok(T0 + 11_000_000_000)
    s = w.snapshot()
    check(s["readfail_ns"] == 3_000_000_000, "两次停摆累计 3s")
    check(s["readfail_max_ns"] == 2_000_000_000, "峰值取大的那次（2s）")
    check(s["readfail_stall_count"] == 2, "停摆次数 2")

    # ── 进行中的停摆：给 now_ns 才计入 ──────────────────────
    w = new_watch()
    w.note_fail(T0)
    check(w.snapshot()["readfail_ns"] == 0,
          "不给 now_ns → 进行中的停摆不计（快照是「已闭合」口径）")
    s = w.snapshot(now_ns=T0 + 5_000_000_000)
    check(s["readfail_ns"] == 5_000_000_000 and s["readfail_max_ns"] == 5_000_000_000
          and s["readfail_stall_count"] == 1, "给 now_ns → 进行中的停摆也计入")
    check(w.snapshot(now_ns=T0 + 7_000_000_000)["readfail_ns"] == 7_000_000_000,
          "进行中停摆随 now_ns 增长（录制中途停摆也能报出时长）")

    # ── 错误信息：只在报过警的恢复行里出现，且截断 ──────────
    w = new_watch()
    w.note_fail(T0)
    check(w.note_ok(T0 + ALERT_NS, reconnects=1, error="timed out") == [],
          "未达门槛的停摆即使有传输错误也静默（重连次数另有 _reconnect_count 兜底）")

    w = new_watch()
    w.note_fail(T0)
    w.note_fail(T0 + ALERT_NS)                     # 先真的报一次警
    lines = w.note_ok(T0 + 2 * ALERT_NS, error="x" * 500)
    check(len(lines) == 1 and lines[0].count("x") == 60,
          "超长错误信息截断到 60 字符（不刷屏）")

    # ── 覆盖计数 ───────────────────────────────────────────
    w = new_watch()
    w.note_overwrite()
    w.note_overwrite(36)
    check(w.snapshot()["overwrite_count"] == 37, "覆盖计数累加")
    check(w.note_ok(T0) == [] and w.snapshot()["readfail_ns"] == 0,
          "覆盖计数与采集停顿互不干扰")

    # ── reset ──────────────────────────────────────────────
    w = new_watch()
    w.note_fail(T0)
    w.note_ok(T0 + 2_000_000_000)
    w.note_overwrite(5)
    w.reset()
    s = w.snapshot()
    check(s == {"readfail_ns": 0, "readfail_max_ns": 0,
                "readfail_stall_count": 0, "overwrite_count": 0}, "reset 全清")
    check(w.note_fail(T0 + 10_000_000_000) == [],
          "reset 后重新计门槛（不继承上一段的停摆起点）")

    # ── MaxLagWatch ────────────────────────────────────────
    m = MaxLagWatch("emit")
    check(m.snapshot() == {"lag_max_ns": 0, "lag_samples": 0}, "初始零")
    m.note(5_000_000)
    m.note(210_000_000)
    m.note(12_000_000)
    s = m.snapshot()
    check(s["lag_max_ns"] == 210_000_000, "取峰值（平均值会被 30Hz 小抖动稀释）")
    check(s["lag_samples"] == 3, "样本数 3")
    m.note(-1)
    check(m.snapshot()["lag_samples"] == 3, "负滞后丢弃（时钟毛刺不算样本）")
    m.note(0)
    check(m.snapshot()["lag_samples"] == 4 and m.snapshot()["lag_max_ns"] == 210_000_000,
          "0 是合法滞后（正好赶上）")
    m.reset()
    check(m.snapshot() == {"lag_max_ns": 0, "lag_samples": 0}, "reset 全清")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: rgb_quality 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
