#!/usr/bin/env python3
"""重建 `hardware_ns`：让它成为「无重复、单调、且与宿主时间轴同构」的设备时钟。

问题（`视塔手套数据集质检报告.pdf` 2.2.5，Data_examples_1 的 ep050/054/064）
--------------------------------------------------------------------------
质检报「两个帧级时钟的差在漂」：`timestamp|hardware_ns` 的差 IQR 达 29~61ms。
实测根因**不是**速率不一致（速率差稳定在 318ppm，20s 只积 6ms），而是：

* **设备钟与宿主钟各有各的「缺帧」，且位置互不重叠。**
  - ep050：设备钟在行 117/247/458/603 有 2T 空洞，宿主钟在这些行是正常 1T；
    宿主钟在行 208/438/584 有 2T 空洞，设备钟在这些行是正常 1T。
  - 每有一处「只有一方空洞」，两钟的差就永久跳一个帧周期 ⇒ IQR ≈ 33ms/处。
* 设备钟独有的 2T 空洞是**同步器补帧**的痕迹（那一帧是上一帧的重复，
  设备计数器没前进），宿主钟是按时收到的 ⇒ 对「按宿主轴做跨模态对齐」而言，
  设备钟那一跳是假的。
* 宿主钟独有的 2T 空洞是**真实延迟** ⇒ 必须保留。
* 另外设备钟还带 Δ=0 重复（旧 `fix_hardware_ns_uniform.py` 之所以存在），
  均匀化虽然消掉了重复，却把上面这些**真实空洞也一起抹平了** —— 于是从
  「重复+跳变」变成了「与宿主钟持续漂移」。本工具同时解决这两件事。

做法
----
保留设备钟的**速率与纪元**，只把「哪些间隔是真实的」交给宿主时间轴决定：

    β      = Σ设备Δ / Σ宿主Δ   （只用两钟都正常的干净区间估，逐段约 318ppm，极稳）
    T_dev  = β · median(宿主Δ)
    第 k 个间隔 = β·宿主Δ[k]     若宿主Δ[k] > 1.5×标称（真实空洞，原样保留）
                 T_dev           否则（去宿主到达抖动，用设备标称节拍）
    hw_new[i] = hw[0] + Σ_{k<i} 间隔[k]

于是：无 Δ=0、单调、真实空洞全在、设备速率与纪元不变，
且 `hw_new − timestamp` 只剩宿主到达抖动（≈2~3ms），远在质检 20ms 阈值内。

用法
----
    venv/bin/python tools/rebuild_hw_ts_from_host.py <数据集>                    # 预演
    venv/bin/python tools/rebuild_hw_ts_from_host.py <数据集> --apply            # 落地
    # 若当前 hardware_ns 已被均匀化重建过，从原始导出取设备钟：
    venv/bin/python tools/rebuild_hw_ts_from_host.py <数据集> \
        --device-src /home/stouch/out/offline/Data_examples/data/chunk-000 --apply
"""
import argparse
import glob
import json
import os
import shutil
import sys

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

US = 1000                      # 设备钟原生 1µs 量化
GAP_FACTOR = 1.5               # 判定「真实空洞」的倍数
BACKUP = "_pre_hw_rebuild"


def to_ns(seconds: np.ndarray) -> np.ndarray:
    return np.rint(np.asarray(seconds, dtype=np.float64) * 1e9).astype(np.int64)


def estimate_beta(dh: np.ndarray, dd: np.ndarray):
    """设备/宿主速率比 β，只用两钟都正常的干净区间估。"""
    th, td = float(np.median(dh)), float(np.median(dd))
    if th <= 0 or td <= 0:
        return 1.0, 0, 0
    clean = ((dh > 0.9 * th) & (dh < 1.1 * th) & (dd > 0.9 * td) & (dd < 1.1 * td))
    if int(clean.sum()) < 10:
        return td / th, int(clean.sum()), int(dh.size)
    return float(dd[clean].sum()) / float(dh[clean].sum()), int(clean.sum()), int(dh.size)


def rebuild(h_sec: np.ndarray, dev: np.ndarray):
    """返回 (hw_new, 统计)。dev = 原始设备钟（未动过的 hardware_ns）。"""
    h = to_ns(h_sec)
    dh = np.diff(h).astype(np.float64)
    dd = np.diff(dev).astype(np.float64)
    beta, n_clean, n_int = estimate_beta(dh, dd)
    th = float(np.median(dh))
    t_dev = beta * th
    gaps = dh > GAP_FACTOR * th                       # 宿主记录里的真实空洞
    steps = np.where(gaps, beta * dh, t_dev)
    hw = np.empty(h.size, dtype=np.int64)
    hw[0] = int(dev[0])
    hw[1:] = hw[0] + np.rint(np.cumsum(steps)).astype(np.int64)
    hw = (hw // US) * US                              # 保持设备钟 1µs 量化
    np.maximum.accumulate(hw + np.arange(hw.size), out=hw)  # 严格递增（防量化撞车）
    hw -= np.arange(hw.size)
    hw = (hw // US) * US
    return hw, {"beta": beta, "beta_ppm": (beta - 1.0) * 1e6, "clean": n_clean,
                "n_int": n_int, "host_gaps": int(gaps.sum()), "T_dev": t_dev}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--episodes", default="")
    ap.add_argument("--device-src", default="",
                    help="取原始设备钟的目录（含 episode-XXX.parquet）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    root = args.root.rstrip("/")
    want = set(x.strip() for x in args.episodes.split(",") if x.strip())
    files = sorted(glob.glob(os.path.join(root, "data/chunk-*", "*.parquet")))
    files = [p for p in files if not want or os.path.basename(p)[8:11] in want]
    if not files:
        print("没找到 data/chunk-*/*.parquet")
        return 1

    src_map = {}
    if args.device_src:
        for p in glob.glob(os.path.join(args.device_src.rstrip("/"), "*.parquet")):
            src_map[os.path.basename(p)] = p
    bak = os.path.join(root, BACKUP)
    print("模式=%s  目标 %d 段%s" % ("落地" if args.apply else "预演（不改文件）",
                                   len(files),
                                   "  设备钟取自 %s" % args.device_src if args.device_src else ""))
    print("%-5s %6s %8s %9s %9s | %-22s | %s" %
          ("ep", "行数", "β(ppm)", "设备异常", "宿主空洞", "hw−timestamp IQR(ms)", "结论"))
    print("%-5s %6s %8s %9s %9s | %9s %9s |" %
          ("", "", "", "0/2T", "", "改前", "改后"))

    report = []
    for p in files:
        name = os.path.basename(p)
        ep = name[8:11]
        t = pq.read_table(p)
        if "timestamp" not in t.column_names or "hardware_ns" not in t.column_names:
            print("%-5s 缺 timestamp/hardware_ns，跳过" % ep)
            continue
        h_sec = np.array(t.column("timestamp"), dtype=np.float64)

        devp = src_map.get(name, p)
        dev = np.array(pq.read_table(devp).column("hardware_ns"), dtype=np.int64)
        if dev.size != h_sec.size:
            print("%-5s 设备钟行数 %d ≠ 本段 %d，跳过" % (ep, dev.size, h_sec.size))
            continue

        d0 = np.diff(dev)
        n0, g0 = int((d0 == 0).sum()), int((d0 > GAP_FACTOR * np.median(d0[d0 > 0])
                                            if (d0 > 0).any() else 0).sum())

        hw_new, st = rebuild(h_sec, dev)
        h = to_ns(h_sec)
        old = np.array(t.column("hardware_ns"), dtype=np.int64)
        def iqr(x):
            d = (x - h).astype(np.float64) / 1e6
            return float(np.percentile(d, 75) - np.percentile(d, 25))
        iqr_before, iqr_after = iqr(old), iqr(hw_new)
        mono = bool(np.all(np.diff(hw_new) > 0))
        ok = mono and iqr_after <= 20.0
        print("%-5s %6d %8.1f %9s %9d | %9.3f %9.3f | %s" %
              (ep, t.num_rows, st["beta_ppm"], "%d/%d" % (n0, g0), st["host_gaps"],
               iqr_before, iqr_after,
               ("✓ 单调且 IQR≤20ms" if ok else "✗ 见下") + ("" if mono else " [非单调!]")))
        report.append({"episode": ep, "rows": t.num_rows, "beta_ppm": st["beta_ppm"],
                       "T_dev_ns": st["T_dev"], "host_gaps": st["host_gaps"],
                       "dev_zero_gaps": n0, "dev_2T_gaps": g0,
                       "iqr_before_ms": iqr_before, "iqr_after_ms": iqr_after,
                       "monotonic": mono, "ok": ok})

        if args.apply:
            if not args.no_backup:
                os.makedirs(bak, exist_ok=True)
                d = os.path.join(bak, name)
                if not os.path.exists(d):
                    shutil.copy2(p, d)
            idx = t.schema.get_field_index("hardware_ns")
            t = t.set_column(idx, "hardware_ns",
                             pa.array(hw_new, type=t.schema.field(idx).type))
            pq.write_table(t, p, compression="zstd")

    if not report:
        return 1
    print("\n合计 %d 段" % len(report))
    print("  β 中位 %.1f ppm（范围 %.1f~%.1f）" %
          (np.median([r["beta_ppm"] for r in report]),
           min(r["beta_ppm"] for r in report), max(r["beta_ppm"] for r in report)))
    print("  全部单调递增: %s" % all(r["monotonic"] for r in report))
    print("  hw−timestamp IQR：改前中位 %.2fms/最大 %.2fms → 改后中位 %.2fms/最大 %.2fms"
          % (np.median([r["iqr_before_ms"] for r in report]),
             max(r["iqr_before_ms"] for r in report),
             np.median([r["iqr_after_ms"] for r in report]),
             max(r["iqr_after_ms"] for r in report)))
    print("  超 20ms 阈值: 改前 %d 段 → 改后 %d 段" %
          (sum(r["iqr_before_ms"] > 20 for r in report),
           sum(r["iqr_after_ms"] > 20 for r in report)))

    if args.apply:
        info_p = os.path.join(root, "meta/info.json")
        if os.path.isfile(info_p):
            info = json.load(open(info_p, encoding="utf-8"))
            al = info.setdefault("alignment", {})
            al["hardware_ns_rebuilt"] = True
            al["hardware_ns_note"] = (
                "hardware_ns 已重建为「设备钟速率+纪元 × 宿主帧时间轴」："
                "设备侧同步器补帧造成的 Δ=0 与伴生 2T 跳变已消除，"
                "宿主侧真实 2T 空洞原样保留；设备钟速率 β 逐段实测（≈318ppm）。"
                "生成工具 tools/rebuild_hw_ts_from_host.py")
            json.dump(info, open(info_p, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print("meta/info.json: 已记 alignment.hardware_ns_rebuilt")
        if not args.no_backup:
            print("原文件已备份到 %s" % bak)
    else:
        print("\n（预演结束，未改任何文件；确认后加 --apply）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
