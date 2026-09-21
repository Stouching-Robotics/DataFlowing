"""GapWatch 帧空洞看门狗单测（v1.3.10）。

覆盖：正常帧距不误报、空洞累计/峰值/发生位置、门槛边界、0 哨兵、
      混源不产生伪空洞、时间倒退重新锚定、reset。

**这个类的口径是录制侧与离线审计脚本共用的那一份**，所以边界条件必须钉死：
2026-09-18 排查 episode-099 的 4.68s 空洞时，就是因为两边口径不一致 + 混源，
差点把「设备钟与宿主钟的基准差」当成一次天文数字级的空洞。

用法:
    venv/bin/python tools/tests/test_frame_gap.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.frame_gap import GapWatch

FRAME_NS = 33_333_333            # 30fps 标称
MIN_GAP_NS = 100_000_000         # 100ms ≈ 3 帧

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def main():
    # ── 空 / 单帧 ──────────────────────────────────────────
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    s = w.snapshot()
    check(s["frames"] == 0 and s["gap_ns"] == 0 and s["source"] == "",
          "空看门狗：零帧、零空洞、基座未锁")
    check(w.note(1_000_000_000) == 0, "首帧不产生空洞")
    check(w.snapshot()["frames"] == 1 and w.snapshot()["source"] == "hw",
          "首帧锁定基座 hw 并计帧")

    # ── 正常帧距 ───────────────────────────────────────────
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    t = 1_000_000_000
    for _ in range(30):
        w.note(t)
        t += FRAME_NS
    s = w.snapshot()
    check(s["gap_ns"] == 0 and s["gap_count"] == 0, "30 帧 @30fps 零空洞")
    check(s["frames"] == 30, "帧数 = 30")
    check(s["span_ns"] == 29 * FRAME_NS, f"连续跨度 = 29 帧间隔（实际 {s['span_ns']}）")

    # 抖动（实测 29.9~37.5ms）不该报
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    t = 1_000_000_000
    w.note(t)
    for d in (29_900_000, 33_300_000, 37_500_000, 30_100_000):
        t += d
        w.note(t)
    check(w.snapshot()["gap_count"] == 0, "29.9~37.5ms 抖动不算空洞")

    # ── 门槛边界 ───────────────────────────────────────────
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    w.note(1_000_000_000)
    w.note(1_000_000_000 + MIN_GAP_NS - 1)
    check(w.snapshot()["gap_count"] == 0, "间隔 = 门槛-1ns → 不计（不许提前开火）")
    w.note(1_000_000_000 + MIN_GAP_NS - 1 + MIN_GAP_NS)
    check(w.snapshot()["gap_count"] == 1, "间隔 = 门槛 → 计入")
    check(w.snapshot()["gap_ns"] == MIN_GAP_NS - FRAME_NS,
          "空洞 = 间隔 - 标称间隔")

    # ── 真洞：ep-099 的 4.68s ───────────────────────────────
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    base = 1_700_000_000_000_000_000
    for i in range(45):                            # 45 帧正常
        w.note(base + i * FRAME_NS)
    hole_start = base + 44 * FRAME_NS              # 洞前最后一帧（= row44）
    w.note(hole_start + 4_680_800_000)             # row45：4.68s 之后
    for i in range(10):
        w.note(hole_start + 4_680_800_000 + (i + 1) * FRAME_NS)
    s = w.snapshot()
    check(s["gap_count"] == 1, "4.68s 洞计 1 次")
    check(abs(s["gap_ns"] - (4_680_800_000 - FRAME_NS)) < 1000,
          f"空洞 ≈ 4647.5ms（实测 {s['gap_ns'] / 1e6:.1f}ms）")
    check(s["gap_max_ns"] == s["gap_ns"], "峰值 = 累计（只有一个洞）")
    check(s["gap_max_at_ns"] == hole_start,
          "峰值位置 = 洞前最后一帧的时刻（能回指到 row44）")
    check(s["first_ns"] == base, "first_ns = 本段首帧（相对时间的原点，与钟无关）")
    check((s["gap_max_at_ns"] - s["first_ns"]) / 1e9 == 44 * FRAME_NS / 1e9,
          "空洞起点可换算成「开录后 1.47s」（日志与审计脚本共用这个换算）")
    check(s["frames"] == 56, "洞中丢的帧不计入（帧数只数到的帧）")

    # ── 0 哨兵 与 负值时刻（早期段的设备钟是**有符号** 32 位，值可为负）──
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    check(w.note(0) == 0, "0 哨兵返回 0")
    check(w.snapshot()["frames"] == 0 and w.snapshot()["source"] == "",
          "0 哨兵不锁基座、不计帧")
    check(w.note(1_000_000_000) == 0 and w.snapshot()["source"] == "hw",
          "哨兵之后的首个有效帧才是基座")

    # 负值**不是**哨兵：整段都可能落在负区（trunc32 段过零前），按 <= 0 跳过会
    # 把整段负区当成「无戳」，下一个正值帧就成了新基座 —— 一次真空洞凭空消失
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    check(w.note(-2_000_000_000) == 0, "负时刻锁基座（不是哨兵，返回 0 只因是首帧）")
    check(w.snapshot()["source"] == "hw" and w.snapshot()["frames"] == 1,
          "负时刻锁了基座、计了帧（旧实现按 <=0 跳过，这里会挂）")
    gap = w.note(-2_000_000_000 + 4_680_800_000)   # 负区里的一次 4.68s 空洞
    check(abs(gap - (4_680_800_000 - FRAME_NS)) < 1000,
          f"负区里的空洞照常报（{gap / 1e6:.1f}ms）")
    check(w.note(3_000_000_000) > 0, "跨过零点（负→正）仍按真实间隔算，不重锚")

    # ── 混源（本类最容易踩的坑）────────────────────────────
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    w.note(1_000_000_000, source="hw")
    n = w.note(5_000_000_000, source="mono")       # 换钟且跳了 4s
    check(n == 0, "混源帧不产生空洞")
    check(w.snapshot()["gap_ns"] == 0, "混源不污染累计")
    check(w.snapshot()["frames"] == 2, "混源帧仍计帧")
    check(w.snapshot()["source"] == "hw", "基座不被混源改写")
    w.note(1_000_000_000 + FRAME_NS, source="hw")  # 回到基座：锚点仍是 1s 那帧
    check(w.snapshot()["gap_count"] == 0, "混源后回到基座不报伪空洞")

    # ── 时间倒退 ───────────────────────────────────────────
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    w.note(10_000_000_000)
    w.note(9_000_000_000)                          # 时钟回退 1s
    s = w.snapshot()
    check(s["gap_count"] == 0 and s["resyncs"] == 1,
          "时间倒退 → 重新锚定（resyncs +1，不报空洞）")
    w.note(9_000_000_000 + FRAME_NS)
    check(w.snapshot()["gap_count"] == 0, "倒退后从新锚点正常计量")

    # ── reset ──────────────────────────────────────────────
    w = GapWatch(FRAME_NS, MIN_GAP_NS)
    w.note(1_000_000_000)
    w.note(1_000_000_000 + 5_000_000_000)
    w.reset()
    s = w.snapshot()
    check(s["frames"] == 0 and s["gap_ns"] == 0 and s["gap_max_at_ns"] == 0
          and s["resyncs"] == 0 and s["source"] == "", "reset 全清（含基座）")
    w.note(7_000_000_000, source="mono")
    check(w.snapshot()["source"] == "mono", "reset 后可换基座")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: frame_gap 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
