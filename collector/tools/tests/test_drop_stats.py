"""DropStats 计数器单测（v1.0.9；v1.3.10 补 note_max 与帧数口径 helper）。

覆盖：inc 累计、note_max 峰值语义、snapshot 副本语义、clear、多线程自增一致性；
      以及 is_frame_drop_key / frame_drop_total 的真值表（帧数口径的唯一判据）。

用法:
    venv/bin/python tools/tests/test_drop_stats.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.pipeline import DropStats, frame_drop_total, is_frame_drop_key

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def main():
    ds = DropStats()
    check(ds.snapshot() == {}, "初始为空")

    ds.inc("a")
    ds.inc("a", 2)
    ds.inc("b", 5)
    check(ds.snapshot() == {"a": 3, "b": 5}, "inc 累计")

    snap = ds.snapshot()
    snap["a"] = 999
    check(ds.snapshot()["a"] == 3, "snapshot 返回副本（改副本不影响计数）")

    # note_max：峰值语义（不能累加，否则第二次小滞后会把第一次大滞后加进去）
    ds.note_max("lag_max_ms", 120)
    ds.note_max("lag_max_ms", 40)
    check(ds.snapshot()["lag_max_ms"] == 120, "note_max 保留峰值（小的不覆盖）")
    ds.note_max("lag_max_ms", 250)
    check(ds.snapshot()["lag_max_ms"] == 250, "note_max 更大的才覆盖")
    ds.note_max("lag_max_ms", 250)
    check(ds.snapshot()["lag_max_ms"] == 250, "note_max 等值不重复计")
    ds.inc("k")
    ds.note_max("lag_max_ms", 0)
    check(ds.snapshot()["lag_max_ms"] == 250 and ds.snapshot()["k"] == 1,
          "note_max 与 inc 互不干扰")

    ds.clear()
    check(ds.snapshot() == {}, "clear 清空")

    # ── 帧数口径真值表 ─────────────────────────────────────
    # 判据是「这个键能不能加进丢帧总数」，不是「键名看起来像什么」。
    # 表里的 False 项覆盖全部会落进 drop_stats 的非帧键：`rgb_quality_snapshot()`
    # 的 7 个（UI 前缀槽位名后落盘）+ pipeline 注入的 3 个空洞键 + 遗留的
    # imu_overflow。漏掉任何一个，它就会被当成帧数加进「丢帧统计」，把用户
    # 指去调编码器（旧名 readfail_episodes 就这么漏了两天）。
    for key, want in [
        ("ext:gripper_rgb", True),        # 外部队列满 —— 帧数
        ("sensor_queue", True),           # 采集线程队列满 —— 帧数
        ("uvc:head_left_rgb", True),      # UVC 槽位 —— 帧数
        ("gripper_rgb_gap_ms", False),    # 空洞时长
        ("gripper_rgb_gap_max_ms", False),
        ("gripper_rgb_gap_count", False),
        ("gripper_rgb_emit_lag_max_ms", False),
        ("gripper_rgb_dispatch_lag_max_ms", False),
        ("gripper_rgb_readfail_ms", False),
        ("gripper_rgb_readfail_max_ms", False),
        ("gripper_rgb_readfail_stall_count", False),   # 旧名 _episodes 曾漏网
        ("gripper_rgb_overwrite_count", False),
        ("gripper_rgb_reconnect_count", False),
        ("gripper_rgb_gap_max_at_ns", False),          # 没进 drop_stats，留着当反例
        ("imu_overflow", False),          # 遗留键（防丢缓冲超限次数）
    ]:
        got = is_frame_drop_key(key)
        check(got is want, f"is_frame_drop_key({key!r}) == {want}（实际 {got}）")

    mixed = {"ext:gripper_rgb": 3, "gripper_rgb_gap_ms": 4680,
             "imu_overflow": 2, "gripper_rgb_overwrite_count": 37}
    check(frame_drop_total(mixed) == 3,
          f"混合字典只加帧数项: {frame_drop_total(mixed)} == 3")
    check(frame_drop_total({}) == 0, "空字典 → 0")
    check(frame_drop_total({"a_gap_ms": 5000}) == 0, "全是时长项 → 0")

    # 多线程自增：8 线程 × 5000 次，两个键各半
    N_THREADS, N_INC = 8, 5000
    def worker(key):
        for _ in range(N_INC):
            ds.inc(key)
    ts = [threading.Thread(target=worker, args=(f"k{i % 2}",))
          for i in range(N_THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    snap = ds.snapshot()
    check(snap == {"k0": 20000, "k1": 20000}, f"多线程自增无丢失: {snap}")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: drop_stats 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
