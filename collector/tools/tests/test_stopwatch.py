"""Stopwatch 收尾分段计时单测（v1.3.11 L0-1）。

覆盖：lap 语义（距上一次 lap）、同名累加、未命中阶段显示 0ms 而不是省略、
order 钉顺序、total_ms、snapshot 是副本。

这个类只被停止通路调用（持 GIL 的窗口里），所以它的契约里有一条不成文的
要求：**别把 lap 写成会 sleep/分配大对象的东西**。测试里顺带钉住「format
不抛异常、不依赖阶段是否命中」，因为日志行是在 end_episode 之后打的，
一旦它抛异常，整个收尾线程会静默死掉（daemon 线程无人看）。

用法:
    venv/bin/python tools/tests/test_stopwatch.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.timing import Stopwatch

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def main():
    # ── 空表 ───────────────────────────────────────────────
    sw = Stopwatch()
    check(sw.snapshot() == {}, "空表：没有阶段")
    check(sw.total_ms() == 0, "空表：total 0")
    check(sw.format() == "", "空表：format 是空串")

    # ── lap 语义 ───────────────────────────────────────────
    sw = Stopwatch()
    time.sleep(0.02)
    ms = sw.lap("a")
    check(20 <= ms < 400, f"lap 记的是距上一次 lap 的耗时（实测 {ms}ms）")
    ms2 = sw.lap("b")
    check(ms2 < 20, f"紧邻的 lap 接近 0（实测 {ms2}ms）")
    check(sw.snapshot() == {"a": ms, "b": ms2}, "snapshot 保留插入序")

    # ── 同名累加（多路 ffmpeg / 同一段被调多次）──────────────
    sw = Stopwatch()
    sw.lap("flush")
    time.sleep(0.01)
    sw.lap("flush")
    got = sw.snapshot()["flush"]
    check(10 <= got < 400, f"同名 lap 累加而不是覆盖（实测 {got}ms，应≥10）")
    check(list(sw.snapshot()) == ["flush"], "累加后仍只有一个键")

    # ── add：并入别处测得的耗时（writer 的 flush/parquet/meta）──
    sw = Stopwatch()
    sw.lap("join")
    sw.add("flush", 1500)
    sw.add("parquet", 3200)
    sw.add("flush", 100)
    snap = sw.snapshot()
    check(snap["flush"] == 1600, "add 也累加")
    check(sw.total_ms() == snap["join"] + 1600 + 3200, "total = 各段之和")

    # ── format：未命中的阶段显示 0ms，不省略 ────────────────
    sw = Stopwatch()
    sw.add("flush", 12)
    sw.add("parquet", 34)
    check("flush=12ms parquet=34ms" in sw.format(), "format 输出键=值ms")
    order = ["join", "flush", "parquet", "meta"]
    txt = sw.format(order)
    check(txt == "join=0ms flush=12ms parquet=34ms meta=0ms",
          f"order 钉顺序且未命中显示 0ms（得到 {txt!r}）")
    check(sw.format([]) == "", "order 为空 → 空串")

    # ── snapshot 是副本（调用方改不动内部状态）──────────────
    sw = Stopwatch()
    sw.add("a", 5)
    snap = sw.snapshot()
    snap["a"] = 999
    check(sw.snapshot()["a"] == 5, "snapshot 返回副本")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: Stopwatch 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
