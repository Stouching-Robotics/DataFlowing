"""设备时间戳归一（补帧修复）单测 —— 纯类，无 Qt / 无真机 / 无 IO。

覆盖 core.timestamp_normal.UniformTsClock：
  1. 干净序列 → 输出严格等差，T 锁定为实测帧周期
  2. **真实伪影形态**（syncer 补帧给的 0 / 2T）→ 一帧不丢、输出仍均匀严格递增
  3. 0 表示「本帧没有时间戳」→ 返回 0，且不占用序号（不能把后续帧平白推进一拍）
  4. 首帧用真实值做锚点；reset 后重新锚定
  5. 伪影不被当成帧周期样本（否则 T 会被带偏）

背景：D435 真机实测 7134 个相邻间隔只有 5 种取值，Δ=0 共 18 次、Δ=2T 共 25 次，
而宿主侧到达节奏全程正常、行数 == 视频帧数 —— 帧没丢，坏的是时间戳数值。

用法:
    venv/bin/python tools/tests/test_uniform_ts_clock.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from core.timestamp_norm import UniformTsClock

NOMINAL = 33_333_333          # 30fps 标称
DEV_T = 33_344_000            # D435 设备钟实测帧周期（33344 µs）

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def uniform_strict(ns):
    """返回 (是否严格递增, 步长取值集合)。"""
    d = [b - a for a, b in zip(ns, ns[1:])]
    return (all(x > 0 for x in d), set(d))


def main():
    print("=== 1. 干净序列：T 锁定为设备实测周期 ===")
    c = UniformTsClock(NOMINAL, lock_frames=10)
    raw = [1_000_000 + i * DEV_T for i in range(50)]
    out = [c.to_ns(x) for x in raw]
    inc, steps = uniform_strict(out)
    print(f"  T={c.period_ns} locked={c.locked} 步长取值={steps}")
    check(out[0] == raw[0], "首帧用真实值做锚点")
    check(inc, "严格递增")
    check(c.locked and c.period_ns == DEV_T, f"T 锁定为实测 {DEV_T}")

    print("\n=== 2. 补帧伪影（0 / 2T 成对）—— 一帧不丢且仍均匀 ===")
    # T 用 lock_frames=1 立即锁定，单独考察「异常被改写」这一条性质
    c = UniformTsClock(NOMINAL, lock_frames=1)
    base = 2_000_000
    raw = [base + i * DEV_T for i in range(20)]
    raw[10] = raw[9]                      # Δ=0：syncer 补帧
    raw[11] = raw[9] + 2 * DEV_T          # Δ=2T：跳一拍
    raw[15] = raw[14] + 2 * DEV_T         # 孤立 2T
    out = [c.to_ns(x) for x in raw]
    inc, steps = uniform_strict(out)
    print(f"  输入 {len(raw)} 帧 → 输出 {len(out)} 帧；步长取值={steps}")
    check(len(out) == len(raw), "一帧都没少")
    check(inc, "严格递增（无 Δ=0、无回退）")
    check(steps == {DEV_T}, f"每步都是 T（均匀）：{steps}")
    check(out[0] == raw[0], "锚点仍是首帧真实值")

    print("\n=== 2b. 生产参数（lock_frames=60）：锁定后全程严格等步 ===")
    c = UniformTsClock(NOMINAL, lock_frames=60)
    raw = [9_000_000 + i * DEV_T for i in range(200)]
    for k in (80, 81, 120, 160):          # 锁定之后再注入伪影
        if k % 2 == 0:
            raw[k] = raw[k - 1]
            raw[k + 1] = raw[k - 1] + 2 * DEV_T
    out = [c.to_ns(x) for x in raw]
    steps = [b - a for a, b in zip(out, out[1:])]
    kink_i = max(range(len(steps)), key=lambda i: abs(steps[i] - DEV_T))
    kink = abs(steps[kink_i] - DEV_T)
    after = set(steps[60:])
    print(f"  锁定后（第 60 步起）步长取值={after}")
    print(f"  全程唯一一个偏离 T 的步在第 {kink_i} 步，偏差 {kink / 1e3:.1f}µs"
          f"（锁定前用标称 33333333 造成）")
    check(c.locked and c.period_ns == DEV_T, "T 锁定为实测周期")
    check(after == {DEV_T}, "锁定之后每一步都严格等于 T")
    pre_dev = max(abs(x - DEV_T) for x in steps[:60])
    check(pre_dev < 1e6,
          f"锁定前偏差有界 < 1ms（实测最大 {pre_dev / 1e3:.1f}µs，锁定瞬间追赶）")
    check(all(x > 0 for x in steps), "全程严格递增")

    print("\n=== 3. 0 = 没有时间戳：返回 0 且不占序号 ===")
    c = UniformTsClock(NOMINAL, lock_frames=1)
    a = c.to_ns(5_000_000)                 # 锚点
    c.to_ns(5_000_000 + DEV_T)             # 一拍 → T 锁定为 DEV_T
    c.to_ns(0)                             # 无戳帧（不占序号）
    c.to_ns(0)                             # 连续两帧无戳
    nxt = c.to_ns(5_000_000 + 2 * DEV_T)
    print(f"  锚点={a} 两帧无戳 T={c.period_ns} 下一帧={nxt}")
    check(c.to_ns(0) == 0, "无戳帧输出 0（沿用仓库 0=未知 约定）")
    check(nxt == a + 2 * c.period_ns,
          "无戳帧不占用序号（下一帧仍只前进两拍，不被平白推进）")

    print("\n=== 4. 伪影不被当成周期样本（T 不被带偏）===")
    c = UniformTsClock(NOMINAL, lock_frames=1000)   # 故意不锁定，看样本收集
    raw = [7_000_000]
    for i in range(30):
        raw.append(raw[-1] + DEV_T)
    raw.append(raw[-1])                    # Δ=0 伪影
    raw.append(raw[-1] + 2 * DEV_T)        # Δ=2T 伪影
    n_intervals = len(raw) - 1
    for x in raw:
        c.to_ns(x)
    print(f"  输入 {len(raw)} 帧 = {n_intervals} 个间隔（干净 30 + Δ=0 ×1 + Δ=2T ×1）")
    print(f"  收集到的干净间隔样本数={len(c._clean)}")
    check(len(c._clean) == 30, "两个伪影都被排除在周期估计之外")

    print("\n=== 5. reset 后重新锚定（重连语义）===")
    c = UniformTsClock(NOMINAL, lock_frames=5)
    for i in range(10):
        c.to_ns(1_000 + i * DEV_T)
    c.reset()
    first = c.to_ns(9_999_999)
    print(f"  reset 后首帧={first} locked={c.locked}")
    check(first == 9_999_999 and not c.locked, "reset 清空锚点与锁定状态")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        return 1
    print("PASS: 设备时间戳归一单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
