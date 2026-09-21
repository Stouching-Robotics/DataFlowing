"""RawStallWatch 原始流接收停滞看门狗单测（v1.3.11 L0-2）。

覆盖：首包不产生间隔、正常包距静默、>stall 计数但不打行、≥alert 打一行、
repeat 内不重复、恢复后重新武装、单调钟倒退忽略、reset 全清。

它守的那条链（2026-09-18 查实）：SLAM 进程是原始流的**服务端**，16 包队列
≈0.53s 满就判 stereo loss 并主动 close；客户端接收线程一旦被 GIL 卡住
（当时怀疑是 ``_write_data_parquet`` 建力矩阵列 1.6s×2），断链就必然发生。
这个类的读数就是把那条推断变成数字——所以阈值与去抖行为必须钉死。

用法:
    venv/bin/python tools/tests/test_raw_stall_watch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.gripper.rgb_quality import RawStallWatch

MS = 1_000_000
STALL_NS = 100 * MS       # >100ms 计入
ALERT_NS = 300 * MS       # ≥300ms 打行
REPEAT_NS = 5_000 * MS

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def make():
    return RawStallWatch(stall_ns=STALL_NS, alert_ns=ALERT_NS,
                         repeat_ns=REPEAT_NS)


def main():
    # ── 首包与健康包距 ─────────────────────────────────────
    w = make()
    check(w.note_recv(1_000 * MS) == [], "首包无前驱 → 不产生间隔、不打行")
    s = w.snapshot()
    check(s["gap_max_ns"] == 0 and s["stall_count"] == 0 and s["packets"] == 1,
          "首包只计包数")
    check(w.note_recv(1_004 * MS) == [], "正常包距（4ms）静默")
    check(w.note_recv(1_008 * MS) == [], "连续正常包距静默")
    s = w.snapshot()
    check(s["gap_max_ns"] == 4 * MS, "峰值记到 4ms")
    check(s["stall_count"] == 0 and s["stall_ns"] == 0, "正常包距不计停滞")

    # ── 门槛边界 ───────────────────────────────────────────
    w = make()
    w.note_recv(0)
    check(w.note_recv(STALL_NS - 1) == [], "恰好低于 stall 门槛 → 不计")
    check(w.snapshot()["stall_count"] == 0, "低于门槛不计数")
    check(w.note_recv(2 * STALL_NS - 1) == [], "恰好等于门槛 → 计数但不打行")
    s = w.snapshot()
    check(s["stall_count"] == 1 and s["stall_ns"] == STALL_NS,
          f"命中门槛计入 1 次（{s['stall_count']} 次 / {s['stall_ns']}ns）")

    # ── ≥alert 打一行，repeat 内不重复 ─────────────────────
    w = make()
    w.note_recv(0)
    lines = w.note_recv(ALERT_NS)
    check(len(lines) == 1, f"≥alert 打一行（实际 {len(lines)} 行）")
    check("停滞 300ms" in lines[0] and "本段已停顿 1 次" in lines[0],
          f"行内含毫秒与累计次数: {lines[0]}")
    got = w.note_recv(ALERT_NS + ALERT_NS)         # 再停 300ms
    check(got == [], "repeat 窗口内不重复打行")
    got = w.note_recv(ALERT_NS * 2 + REPEAT_NS)    # 距上次告警已过 repeat
    check(len(got) == 1 and "仍未恢复" in got[0],
          f"超 repeat 打「仍未恢复」行: {got[:1]}")
    s = w.snapshot()
    check(s["stall_count"] == 3, "三次停顿全计数")
    check(s["gap_max_ns"] == REPEAT_NS and s["stall_ns"] == ALERT_NS * 2 + REPEAT_NS,
          f"峰值取最大（{s['gap_max_ns'] // MS}ms）、总停顿累加"
          f"（{s['stall_ns'] // MS}ms）")

    # ── 恢复 → 重新武装 ────────────────────────────────────
    w = make()
    w.note_recv(0)
    check(len(w.note_recv(ALERT_NS)) == 1, "第一次抽风打行")
    check(w.note_recv(ALERT_NS + 2 * MS) == [], "恢复正常包距不打行")
    lines = w.note_recv(ALERT_NS + 2 * MS + ALERT_NS)
    check(len(lines) == 1 and "停滞 300ms" in lines[0],
          "恢复后再次抽风重新打「进入」行（不是「仍未恢复」）")

    # ── 单调钟倒退（防御）──────────────────────────────────
    w = make()
    w.note_recv(10 * MS)
    check(w.note_recv(9 * MS) == [], "时间倒退不打行")
    s = w.snapshot()
    check(s["gap_max_ns"] == 0 and s["stall_count"] == 0, "倒退不计停滞")
    check(s["packets"] == 2, "倒退仍计包数（包确实到了）")

    # ── reset ──────────────────────────────────────────────
    w = make()
    w.note_recv(0)
    w.note_recv(ALERT_NS)
    w.reset()
    s = w.snapshot()
    check(all(v == 0 for v in s.values()), f"reset 全清: {s}")
    check(w.note_recv(0) == [], "reset 后重新等首包")

    # ── 真实场景：断链黑洞（跨重连的间隔也要计）────────────
    w = make()
    w.note_recv(0)
    lines = w.note_recv(1_200 * MS)                # 断链 → 1.2s 后重连首包
    s = w.snapshot()
    check(s["gap_max_ns"] == 1_200 * MS, "跨断链的 1.2s 黑洞计入峰值")
    check(len(lines) == 1 and "1200ms" in lines[0],
          f"黑洞同样打行（这是「停止即断链」最直接的读数）: {lines[:1]}")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: RawStallWatch 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
