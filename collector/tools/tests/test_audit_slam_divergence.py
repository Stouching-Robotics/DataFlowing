"""tools/audit_slam_divergence.py 的单测（v1.3.11 L0-3）。

这个脚本是「发散 ← 哪次停止」的**基线工具**，它自己的两处推断最容易悄悄错：
① main.log 的**多天时间线重建**（日志无日期分隔，错了就把账记到别的天上）；
② 撕裂行里 rebase 步长的**松散解析**（错了要么漏数、要么把良性会话判成灾害）。
所以用合成 fixture 把这两条钉死，另加密度判据、发散合并、速率线性度的行为断言。

真实数据的端到端基线在脚本 docstring 里（2026-09-18：55 完成 / 54 收尾断链 /
11 中止且 0 断链 / 2 次发散，两次都在「完成」之后）。

用法:
    venv/bin/python tools/tests/test_audit_slam_divergence.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_slam_divergence as aud  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def write_native(lines, name="20260918_120000_3500000000000001_slam_stdout.log"):
    """把 (epoch, text) 写成原生会话日志（JSON 行）。"""
    fd, path = tempfile.mkstemp(suffix="_slam_stdout.log")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for t, text in lines:
            fh.write(json.dumps({"time": t, "arrival_monotonic": t,
                                 "line": text}) + "\n")
    os.rename(path, os.path.join(os.path.dirname(path), name))
    return os.path.join(os.path.dirname(path), name)


def rebase(t, step, dt=0.02, reason="translation_jump"):
    return ('[POSE_REBASE] reason={} dt_s={:.6f} translation_step_m={:.6f} '
            'translation_limit_m=0.35'.format(reason, dt, step))


def parse_events(lines):
    """写一份临时会话日志 → 走真实解析路径取回事件表（测完删）。"""
    path = write_native(lines)
    try:
        return aud.load_session(path)["events"]
    finally:
        os.unlink(path)


# ── ① 撕裂行：步长仍要解析出来 ───────────────────────────────
def test_torn_lines():
    t0 = 1_700_000_000.0
    path = write_native([
        (t0, "[FAYS-CALIB] WARN SetStereoFPS(50) failed"),
        # 两个消息逐字交错（真实样本）：步长被撕成 ``=9verified=yes``
        (t0 + 1, "[FAYS-AFFINITY] background stage: tid=[POSE_REBASE] reason="
                 "translation_jump3720726 dt_s=0.020000 cpus= "
                 "translation_step_m=9 verified=yes"),
        # 标签被上一个消息吃掉，行首直接是字段
        (t0 + 2, "translation_jump dt_s=0.060000 translation_step_m=58.003757 "
                 "translation_limit_m=0.230000"),
        # 只有标签、步长被吃掉
        (t0 + 3, "[POSE_REBASE] reason=tracking_recovery dt_s=1.22"),
    ])
    s = aud.load_session(path)
    steps = [e["step"] for e in s["events"]]
    check(len(s["events"]) == 3, f"三条 rebase 全记账（实际 {len(s['events'])}）")
    check(steps[0] == 9.0, f"交错行里的步长解析出来={steps[0]}")
    check(steps[1] == 58.003757, f"无标签行同样解析={steps[1]}")
    check(steps[2] is None, "步长被吃掉的那条记 None 不计数值")
    check(s["unparsed"] == 1, f"只算 1 条无法解析（实际 {s['unparsed']}）")
    check(abs(s["events"][1]["dt"] - 0.06) < 1e-9,
          f"dt_s 一并解析（{s['events'][1]['dt']}）")
    os.unlink(path)


# ── ② 密度判据：良性不报、灾害报 ─────────────────────────────
def test_density():
    t0 = 1_700_000_000.0
    # 良性：零散重定位，1 秒窗内最多 3 条（真实基线）
    benign = parse_events([(t0 + i * 2.0, rebase(t0 + i * 2.0, 2.0, dt=0.5))
                           for i in range(20)])
    check(aud.find_floods(benign, 1.0, 1.0, 15, 2.0) == [],
          "良性零散 rebase（2s 一条）不判为发散")
    # 灾害：每秒 30 条、步长都 ≥1m
    hot = parse_events([(t0 + i * 0.033, rebase(t0 + i * 0.033, 5.0))
                        for i in range(90)])
    floods = aud.find_floods(hot, 1.0, 1.0, 15, 2.0)
    check(len(floods) == 1, f"灾害串判出 1 段（实际 {len(floods)}）")
    check(floods and floods[0]["peak_window"] >= 15,
          f"1 秒窗峰值 ≥15（实际 {floods[0]['peak_window'] if floods else 0}）")
    # 步长 <1m 的高频 rebase 不算（重定位小幅抖动的形状）
    small = parse_events([(t0 + i * 0.02, rebase(t0 + i * 0.02, 0.05))
                          for i in range(200)])
    check(aud.find_floods(small, 1.0, 1.0, 15, 2.0) == [],
          "每秒 50 条但步长 0.05m 的不判为发散")


# ── ③ 同一次发散被密度低谷切成多段 → 合并 ───────────────────
def test_episode_merge():
    t0 = 1_700_000_000.0
    ev = []
    for i in range(60):                       # 第 1 段
        ev.append({"t": t0 + i * 0.033, "step": 5.0, "dt": 0.02})
    for i in range(8):                        # 中间掉到阈值以下（5s）
        ev.append({"t": t0 + 2.1 + i * 0.6, "step": 5.0, "dt": 0.02})
    for i in range(60):                       # 第 2 段
        ev.append({"t": t0 + 8.3 + i * 0.033, "step": 9.0, "dt": 0.02})
    floods = aud.find_floods(ev, 1.0, 1.0, 15, 2.0)
    check(len(floods) == 2, f"原始切成 2 段（实际 {len(floods)}）")
    for f in floods:
        f["session"] = "s1"
    ev = sorted(ev, key=lambda e: e["t"])
    eps = aud.merge_episodes(floods, 30.0, [{"name": "s1", "events": ev}])
    check(len(eps) == 1, f"相隔 30s 内并成 1 次发散（实际 {len(eps)}）")
    check(eps and eps[0]["segments"] == 2, "合并后段数=2")
    check(eps and eps[0]["lines"] == len(ev),
          f"行数按 [t0,t1] 全区间重算={len(ev)}（实际 {eps[0]['lines'] if eps else 0}）")
    check(eps and eps[0]["duration_s"] > 8.0, "跨越两段 → 时长按首尾算")
    # 会话不同绝不合并（换进程/重开夹爪必然是两次）
    floods[0]["session"], floods[1]["session"] = "a", "b"
    check(len(aud.merge_episodes(floods, 30.0)) == 2, "不同会话不合并")


# ── ④ 速率线性度：真发散 R²≈1，零散不起涨 ────────────────────
def test_growth():
    t0 = 1_700_000_000.0
    ramp = []
    for i in range(400):                      # 速率 10 → 110 m/s 匀速涨
        t = t0 + i * 0.033
        ramp.append({"t": t, "step": (10 + 0.25 * i) * 0.02, "dt": 0.02})
    g = aud._growth_stats(ramp)
    check(g is not None and g["r2"] is not None and g["r2"] > 0.99,
          f"线性上涨的速率 R²≈1（实际 {None if not g else round(g['r2'], 4)}）")
    check(g and abs(g["speed_slope"] - 7.5) < 1.0,
          f"斜率≈+7.5 m/s²（实际 {None if not g else round(g['speed_slope'], 2)}）")
    flat = [{"t": t0 + i * 0.033, "step": 0.5, "dt": 0.02} for i in range(400)]
    gf = aud._growth_stats(flat)
    check(gf is None or gf["r2"] is None or gf["r2"] < 0.5,
          "速率不涨的串 R² 很低（不成线）")


# ── ⑤ main.log 时间线：跨天递推 + 日期锚点 + 锚点前不配对 ────
def test_timeline():
    lines = [
        "[23:59:00] DAQ Video Pipeline started.",          # 锚点前：日期未知
        "[23:59:30] [SLAM] ⚠ 2026-09-17 23:59:30.100 [ ERROR] StereoOnlineCapture()",
        "[23:59:50] [gripper_stereo_left] ▶ Recording started — codec: HEVC",
        "[00:00:20] [gripper_stereo_left] ■ Recording completed: /tmp/x",
        "[00:00:21] [Gripper-Raw] 原始流连接中断：Fays raw stream closed unexpectedly",
        "[00:01:00] [SLAM] ⚠ 2026-09-18 00:01:00.200 [ ERROR] ImuOnlineCapture()",
        "[00:01:10] [gripper_stereo_left] ■ Recording completed: /tmp/y",
    ]
    fd, path = tempfile.mkstemp(suffix=".log")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    rows, known = aud.load_main_log(path)
    check(known == len(rows) - 1,
          f"只有锚点前那一行日期未知（解出 {known}/{len(rows)}）")
    check(rows[0][3] is None, "锚点之前的行 epoch=None（不参与配对）")
    import datetime
    days = [datetime.date.fromtimestamp(r[3]).isoformat() if r[3] else None
            for r in rows]
    check(days[1] == "2026-09-17", f"锚点行归 09-17（{days[1]}）")
    check(days[2] == "2026-09-17" and days[3] == "2026-09-18",
          f"23:59→00:00 的倒退 >12h ⇒ 判跨天（{days[2]} → {days[3]}）")
    check(days[5] == "2026-09-18", "第二个锚点把日期重新对齐")
    stops = aud.extract_stops(rows)
    kinds = [s["kind"] for s in stops]
    check(kinds == ["started", "completed", "disconnect", "completed"],
          f"事件摘取正确（{kinds}）")
    check(len(stops) == 4, "锚点前的 started 不参与（无 epoch）")
    os.unlink(path)


# ── ⑥ 配对：只认**之前**的停止，且窗口一视同仁 ───────────────
def test_attribute():
    t0 = 1_700_000_000.0
    stops = [
        {"kind": "completed", "epoch": t0 - 4.0, "idx": 1, "text": "c"},
        {"kind": "disconnect", "epoch": t0 - 6.0, "idx": 2, "text": "d"},
        {"kind": "completed", "epoch": t0 + 3.0, "idx": 3, "text": "c2"},
    ]
    ep = {"t0": t0, "t1": t0 + 10}
    label, first, near = aud.attribute(ep, stops, 30.0)
    check("完成" in label and "-4.0s" in label,
          f"归属＝之前 4 秒那次完成（{label}）")
    check(first is not None and first["epoch"] == t0 - 4.0,
          "返回的起因是那次完成，不是更近的断链")
    label2, first2, _ = aud.attribute({"t0": t0, "t1": t0 + 10}, stops, 2.0)
    check(first2 is None and label2 == "无邻近停止",
          f"回溯窗口收窄到 2s 就找不到起因（{label2}）")


# ── ⑦ 日中基线：完成/中止两路的断链率 ────────────────────────
def test_day_stats():
    import datetime
    day = "2026-09-18"
    base = datetime.datetime(2026, 9, 18, 12, 0).timestamp()
    stops = []
    for i in range(4):                       # 4 次完成，3 次带收尾断链
        t = base + i * 60
        stops.append({"kind": "completed", "epoch": t, "idx": i, "text": "c"})
        if i < 3:
            stops.append({"kind": "disconnect", "epoch": t - 2.0,
                          "idx": 100 + i, "text": "d"})
    for i in range(3):                       # 3 次中止，0 次带断链
        stops.append({"kind": "aborted", "epoch": base + 300 + i * 60,
                      "idx": 200 + i, "text": "a"})
    d = aud.day_stats(day, stops, [], [], 1.0, 15)
    check(d["completed"] == 4 and d["aborted"] == 3, "完成/中止计数")
    check(d["disc_in_completed_window"] == 3 and d["completed_with_disc"] == 3,
          f"收尾窗口命中 3/4（实际 {d['disc_in_completed_window']}，"
          f"带断链 {d['completed_with_disc']}）")
    check(d["disc_in_abort_window"] == 0 and d["aborted_with_disc"] == 0,
          "中止路径一条都不命中（判「断链主因是 parquet」的依据）")
    check(d["disc_in_completed_offsets"] == [-2.0] * 3,
          f"偏移记录为 −2.0s（{d['disc_in_completed_offsets']}）")


def main():
    for fn in (test_torn_lines, test_density, test_episode_merge, test_growth,
               test_timeline, test_attribute, test_day_stats):
        print(f"── {fn.__name__} " + "─" * max(0, 50 - len(fn.__name__)))
        fn()
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        for f in FAILS:
            print("   -", f)
        return 1
    print("PASS: audit_slam_divergence 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
