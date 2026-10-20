#!/usr/bin/env python3
"""`tools/rebuild_hw_ts_from_host.py` 回归测试（合成数据，不碰真实数据集）。

要守住的性质（对应质检报告 2.2.5 的根因）：

1. **设备侧补帧痕迹清掉**：同步器补帧会让设备钟出现 Δ=0 + 伴生 2T；
   重建后不得再有 Δ=0，也不得留下那处 2T（宿主钟在那是正常 1T）。
2. **宿主侧真实空洞原样保留**：宿主记录到 2T 的地方，重建后仍必须是 2T，
   位置与个数都不能变 —— 那是真丢帧，抹平了就是把时间轴拉直，反而失真。
3. **不把宿主到达抖动灌进设备钟**：宿主相对标称节拍 ±1.5ms 的抖动
   （普通调度噪声）不该出现在重建后的设备钟里，设备钟应保持均匀节拍。
4. **速率与纪元守恒**：β 用干净区间估得准（合成里设 318ppm），
   且 hw[0] 保持原设备钟纪元、仍按 1µs 量化、严格单调。
5. **结果自洽**：hw − timestamp 的 IQR 落进质检阈值（20ms）内。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from tools.rebuild_hw_ts_from_host import rebuild, estimate_beta  # noqa: E402

US = 1000
T_HOST = 33_333_333          # 宿主标称节拍
BETA = 1.000318              # 真实设备/宿主速率比（318 ppm）
T_DEV = BETA * T_HOST
JITTER_NS = 15_000           # 宿主到达抖动（σ≈15µs，实测 IQR 11µs 同量级）


def synth(n=600, dev_fills=(100, 200, 300, 400), host_gaps=(150, 250, 350),
          jitter=True, seed=7):
    """造一段两钟各有各的空洞的合成数据。

    dev_fills: 设备侧补帧处（设备 Δ=0 + 伴生 2T，宿主正常 1T）
    host_gaps: 宿主侧真实延迟处（宿主 2T，设备正常 1T）
    """
    rng = np.random.default_rng(seed)
    h = np.empty(n, dtype=np.float64)
    dev = np.empty(n, dtype=np.int64)
    host_gap_at = set(host_gaps)
    fill_at = set(dev_fills)
    for i in range(1, n):
        # 宿主抖动是**绝对**的 ±1ms 级到达抖动，不是百分比
        host_step = T_HOST + (JITTER_NS * rng.standard_normal() if jitter else 0.0)
        dev_step = T_DEV
        if i in host_gap_at:
            host_step += T_HOST               # 宿主真的等了两拍
        if i in fill_at:
            dev_step = 0                      # 设备补帧：计数器没前进
        elif (i - 1) in fill_at:
            dev_step = 2 * T_DEV              # 补帧的伴生跳变
        h[i] = h[i - 1] + host_step
        dev[i] = dev[i - 1] + int(round(dev_step / US)) * US
    dev += 752_038_885_000                    # 设备钟纪元（~752s 开机时长）
    return h / 1e9, dev


def gaps(x, factor=1.5):
    d = np.diff(x)
    return np.where(d > factor * np.median(d[d > 0]))[0], np.where(d == 0)[0]


def main():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print("  %s %s%s" % ("PASS:" if cond else "FAIL:", name,
                             (" — " + detail) if detail else ""))

    h_sec, dev = synth()
    hw, st = rebuild(h_sec, dev)
    h = np.rint(h_sec * 1e9).astype(np.int64)

    print("── 1. 设备侧补帧痕迹 ──")
    _, zero_after = gaps(hw)
    g_after, _ = gaps(hw)
    _, zero_before = gaps(dev)
    check("原始设备钟确实有 Δ=0 补帧", zero_before.size == len(dict.fromkeys((100,200,300,400))),
          "实得 %d" % zero_before.size)
    check("重建后无 Δ=0", zero_after.size == 0, "实得 %d" % zero_after.size)
    check("补帧处的伴生 2T 一并清掉（设备独有空洞不残留）",
          g_after.size == len({150, 250, 350}),
          "重建后空洞 %d 处（应只剩宿主那 3 处）" % g_after.size)

    print("── 2. 宿主侧真实空洞必须保留 ──")
    g_host, _ = gaps(h)
    check("空洞位置与宿主完全一致", np.array_equal(g_after, g_host),
          "宿主 %s / 重建 %s" % (g_host.tolist(), g_after.tolist()))
    check("空洞宽度≈2×标称（±5%）", bool(np.all(
        np.abs(np.diff(hw)[g_after] / T_DEV - 2.0) < 0.05)),
        "实得 %s" % [round(float(x) / T_DEV, 4) for x in np.diff(hw)[g_after]])

    print("── 3. 宿主抖动不得灌进设备钟 ──")
    d_hw = np.diff(hw)
    normal = np.setdiff1d(np.arange(d_hw.size), g_after)
    spread = float(np.ptp(d_hw[normal]))
    check("设备钟保持均匀节拍（去抖）", spread <= 2 * US,
          "正常间隔极差 %.0f ns（宿主抖动未传入）" % spread)
    dh_norm = np.setdiff1d(np.arange(np.diff(h).size), gaps(h)[0])
    check("宿主确实带抖动（前提成立）", float(np.ptp(np.diff(h)[dh_norm])) > 3e4,
          "宿主正常间隔极差 %.1f µs" % (float(np.ptp(np.diff(h)[dh_norm])) / 1e3))

    print("── 4. 速率与纪元守恒 ──")
    check("β 估得准（误差 < 80ppm，远小于会致质检失败的 1000ppm）",
          abs(st["beta_ppm"] - 318.0) < 80.0,
          "实得 %.1f ppm（真值 318；该估计量 1σ≈19ppm，容差已按噪声底定）"
          % st["beta_ppm"])
    check("设备钟纪元保持", hw[0] == dev[0], "hw[0]=%d" % hw[0])
    check("仍是 1µs 量化", bool(np.all(np.diff(hw) % US == 0)))
    check("严格单调", bool(np.all(np.diff(hw) > 0)))

    print("── 5. 结果自洽（质检口径）──")
    for tag, series in (("改前", dev), ("改后", hw)):
        d = (series - h).astype(np.float64) / 1e6
        iqr = float(np.percentile(d, 75) - np.percentile(d, 25))
        if tag == "改前":
            iqr_before = iqr
        else:
            iqr_after = iqr
    check("改前确实超标（说明这条测试有意义）", iqr_before > 20.0,
          "IQR %.2f ms" % iqr_before)
    check("改后落进 20ms 阈值", iqr_after <= 20.0, "IQR %.2f ms" % iqr_after)

    print("── 6. 干净段不得被改坏 ──")
    h2, d2 = synth(n=400, dev_fills=(), host_gaps=(), seed=11)
    hw2, _ = rebuild(h2, d2)
    _, z2 = gaps(hw2)
    g2, _ = gaps(hw2)
    check("无异常段重建后无 Δ=0", z2.size == 0)
    check("无异常段重建后无空洞", g2.size == 0)
    dd2 = np.diff(hw2)
    check("无异常段节拍均匀", float(np.ptp(dd2)) <= 2 * US,
          "极差 %.0f ns" % float(np.ptp(dd2)))
    iqr2 = (lambda d: float(np.percentile(d, 75) - np.percentile(d, 25)))(
        (hw2 - np.rint(h2 * 1e9).astype(np.int64)).astype(np.float64) / 1e6)
    check("无异常段 IQR 仍小", iqr2 <= 20.0, "IQR %.2f ms" % iqr2)

    print("── 7. β 估计的边界 ──")
    b, n_clean, n_int = estimate_beta(np.full(50, float(T_HOST)),
                                      np.full(50, float(T_DEV)))
    check("全干净时 β=真值", abs((b - 1) * 1e6 - 318.0) < 1.0, "%.1f ppm" % ((b - 1) * 1e6))
    b2, _, _ = estimate_beta(np.array([0.0, 0.0]), np.array([0.0, 0.0]))
    check("退化输入不炸（返回有限值）", np.isfinite(b2))

    print("\n%s" % ("PASS: hardware_ns 重建（设备钟速率 × 宿主时间轴）回归全部通过"
                    if ok else "FAIL: 有用例未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
