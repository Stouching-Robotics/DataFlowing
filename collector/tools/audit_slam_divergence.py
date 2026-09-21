#!/usr/bin/env python
"""SLAM 位姿发散审计：每次「轨迹冻住 + rebase 刷屏」是哪一次停止引起的（只读）。

背景：2026-09-18 用户报「SLAM 解算崩溃了…轨迹停止…日志大量崩溃信息」。查实**进程
一次没死**（当天 18 个原生会话 returncode 全 0、无 FATAL_SIGNAL dump）——真相是位姿
解算**发散**：native 的 rebase 把参考位姿重锚成 ``Twc_out ≡ P_prev``，每帧都触发时
发布位姿被**钉死**（轨迹冻住，不是乱飞），步长按恒定加速度线性上涨（IMU 双重积分的
形状）。它发生在**录制收尾窗口**：收尾那几秒我们的进程抢占了 SLAM 分区的 SMT 兄弟
核（同一秒 ``image=32.3 imu=203.1 post_ms=101.16``），跟踪 LOST → 重定位进错误地图
→ 31m 跳 → rebase 自锁。本脚本把这件事**全量数清楚**，作为修复前的基线与回归对照。

用法:
    venv/bin/python tools/audit_slam_divergence.py [选项]
默认读 logs/slam_native/ 与 logs/main.log（相对**仓库根**，可从任意 cwd 运行）

选项:
    --date YYYY-MM-DD   只看这一天（停止事件 + 会话 + 洪水）
    --since YYYY-MM-DD  只看该日及以后
    --window-s 1.0      密度判据的滑窗宽度
    --min-lines 15      滑窗内至少这么多条 rebase（步长 ≥ --min-step-m）才算洪水
    --min-step-m 1.0    单条 rebase 计入门槛（米）
    --gap-s 2.0         相邻命中相隔 ≤ 该值合并成同一场洪水
    --all               连没有 rebase 的会话也列出
    --json              输出机器可读结果

判据（为什么不是「数行数」）
--------------------------
发散与「正常重定位」在**总行数**上分不开（良性会话 162342 也有 46 行 rebase、163131
有 13 行），把它们分开的是两条**形状**证据——都是发散独有、良性不会有的：

  ============  ================================  ===========================
  判据          阈值/读数                         2026-09-18 实测
  ============  ================================  ===========================
  密度（主）    任一 1 秒滑窗内 step≥1m 的条数    灾害 **34/45** 条 vs 良性最大
                                                  **3**（两侧余量 4.5×/11×）
  增长（判线性）8 等分桶内 step/dt 的中位数，     灾害 **R²=1.00**（137→986 m/s
                对时间拟合的 R²                   与 53→173 m/s，直线），良性
                                                  零散重定位不起涨也不成线
  ============  ================================  ===========================

「步长」本身没有单调性（每触发一次 rebase 参考位姿就被重锚，步长回落再涨：
31.4→17.8→18.1→16.6m 锯齿），所以看的是 **step/dt = 内部估计的漂移速度**——IMU
双重积分的发散里它线性上涨。桶内取**中位**是为了压掉起步那次重定位大跳
（165036 有一条 27079m、165700 有一条 31.4m）。

用秒窗而不是「连续 N 行」：原生 stdout 是**多线程无锁**写的，行会被撕裂（实测
2654 行 rebase 里 7 行，甚至有 ``tid=[POSE_REBASE] reason=translation_jump3720726
dt_s=0.020000 cpus= translation_step_m=9`` 这种两个消息**逐字交错**的），丢一两行
不该把计数打断。所以解析一律走**松散正则**：``translation_step_m=`` 出现在行内任意
位置即可，数值只取最长的合法数字前缀；取不到步长的 rebase 行单独计数、只算行数。

两个坑
------
1. ``logs/main.log`` 是**多天追加、无日期分隔**的（行首只有 ``[HH:MM:SS]``）⇒ 行号
   跨天不是时间序，不能直接按行读。这里先重建时间线：用日志内**带绝对日期的 SLAM
   转发行**（``[SLAM] ⚠ 2026-09-08 18:46:28.935 …``）当锚点，锚点之间用「时刻倒退
   >12 小时即跨天」递推，遇到锚点重新对齐；锚点之前的历史行日期未知，一律不配对。
2. ``logs/slam_native/*_stdout.log`` 是**每会话一份**的 JSON 行（``{"time":…,
   "line":…}``）⇒ 按文件读就自带绝对时间，**不要**把它们合并成文本再 grep。

2026-09-18 全天基线（改前，用来对照修复效果；这一天的数由本脚本产生）
------------------------------------------------------------------
    完成 55 / 中止 11 / 断链 59 / 重连 59 / 原生会话 20 / **发散 2 次**
    断链落在「完成」收尾窗口 [−3,+1]s 内 **54/55**（中位 −2.0s）；带断链的完成 52/55
    断链落在「中止」±3s 内 **0/11**  ← 中止不做 parquet，只 flush，一次都不触发
    （历史对照：09-10 是 94/94 完成带断链、中止 2/14；09-08 只有 4/15 —— 断链率
     本身在变，所以基线要按天看，别跨天比）
    发散 16:49:16.975（165036 会话，完成 −4.0s 起）：54.7s / 1474 行 / 3 段 /
         最大步长 2530.9m，速率 137→986 m/s（R²=1.00）
    发散 16:56:32.917（165700 会话，完成 −7.9s 起）：25.6s / 763 行 / 1 段 /
         最大步长 11.0m，速率 53→173 m/s（R²=1.00）
    两次都在「完成」之后数秒、且都在断链+重连的黑洞之后 ⇒ 与「收尾抢占 → 跟踪
    LOST → 重定位 → rebase 自锁」这条链一致（详见 docs 里那次的排查记录）。

口径说明（同名数字与历史记录对不上的原因）
----------------------------------------
「最长连跑」= 相邻 rebase 行里 **step≥1m** 的最长连跑（本脚本口径）；早期排查时
数的是**全部** rebase 行的连跑（165036 1105 / 165700 822），所以两者不同源；
判据用的是「1 秒窗峰值」，与两者都不同源。

退出码：发现发散 → 1；否则 0（与 tools/audit_frame_gaps.py 同）。
"""
import argparse
import datetime
import glob
import json
import os
import re
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_NATIVE_DIR = os.path.join(ROOT, "logs", "slam_native")
DEFAULT_MAIN_LOG = os.path.join(ROOT, "logs", "main.log")

# 松散解析：撕裂行里 ``translation_step_m=`` 可能被别的消息插在中间，
# 数值后面也可能紧跟别的 token（``=9verified=yes``）⇒ 只取最长数字前缀
RE_STEP = re.compile(r"translation_step_m=([0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?)")
RE_DT = re.compile(r"dt_s=([0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?)")
RE_REASON = re.compile(r"reason=(?:translation_jump|tracking_recovery)")
RE_REBASE_MARK = re.compile(r"\[POSE_REBASE\]")
# 会话日志名：%Y%m%d_%H%M%S_<serial>_slam_stdout.log（见 process_controller）
RE_NATIVE_NAME = re.compile(r"^(\d{8})_(\d{6})_(\d+)_slam_stdout\.log$")
# main.log 行首时刻；带绝对日期的转发行（SLAM stderr 转发）当时间线锚点
RE_MAIN_TS = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\]")
RE_EMBED_DATE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)")
_ANCHOR_TOL_S = 300.0           # 锚点行：行首时刻与内嵌时刻相差超过它就不当锚点
_DAY_JUMP_S = 12 * 3600         # 时刻倒退超过这么多 ⇒ 判为跨天

# 停止事件的配对窗口（秒；负 = 事件早于停止）
COMPLETED_WINDOW = (-3.0, 1.0)  # 完成：收尾窗口（实测断链中位 −2s）
ABORT_WINDOW = (-3.0, 3.0)      # 中止：中止不写 parquet，理论上不该有断链
NEAR_S = 60.0                   # 「这次洪水挨着谁」的搜索半径

_KIND_LABEL = {"completed": "完成", "aborted": "中止", "started": "开录",
               "disconnect": "断链", "reconnect": "重连"}


# ── native 侧：会话与 rebase 事件 ────────────────────────────
def load_session(path):
    """读一份原生会话日志 → rebase 事件表（容错撕裂行与截断）。"""
    name = os.path.basename(path)
    m = RE_NATIVE_NAME.match(name)
    started = None
    if m:
        started = time.mktime(time.strptime(m.group(1) + m.group(2),
                                            "%Y%m%d%H%M%S"))
    events = []
    first_t = last_t = None
    unparsed = 0
    t = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue                    # 包装侧带锁写，理论不会坏；坏了就跳过
            if "time" in obj:
                try:
                    t = float(obj["time"])
                except (TypeError, ValueError):
                    pass
            if t is None:
                continue
            if first_t is None:
                first_t = t
            last_t = t
            line = obj.get("line")
            if not isinstance(line, str) or not line:
                continue
            steps = RE_STEP.findall(line)
            if steps:
                dts = RE_DT.findall(line)
                for i, s in enumerate(steps):
                    try:
                        step = float(s)
                    except ValueError:
                        step = None
                        unparsed += 1
                    dt = None
                    if i < len(dts):
                        try:
                            dt = float(dts[i])
                        except ValueError:
                            dt = None
                    events.append({"t": t, "step": step, "dt": dt})
            elif RE_REASON.search(line) or RE_REBASE_MARK.search(line):
                # 步长被撕裂吃掉了：仍是一次 rebase，只丢数值
                events.append({"t": t, "step": None, "dt": None})
                unparsed += 1
    events.sort(key=lambda e: e["t"])
    return {
        "name": name,
        "path": path,
        "serial": m.group(3) if m else "",
        "started": started,
        "first_t": first_t,
        "last_t": last_t,
        "events": events,
        "unparsed": unparsed,
        "rebase": len(events),
        "max_step": max((e["step"] for e in events if e["step"] is not None),
                        default=0.0),
        "max_run": _max_run(events),
        "max_window": _max_window(events, 1.0, 1.0),
    }


def _max_run(events):
    """相邻 rebase 行里步长 ≥1m 的最长连跑（不设窗；老口径，便于与历史对照）。"""
    run = best = 0
    for e in events:
        if e["step"] is not None and e["step"] >= 1.0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _max_window(events, window_s, min_step):
    """任一滑窗内 step≥min_step 的条数最大值（密度判据的读数）。"""
    q = [e["t"] for e in events
         if e["step"] is not None and e["step"] >= min_step]
    best = 0
    j = 0
    for i in range(len(q)):
        if j < i:
            j = i
        while j + 1 < len(q) and q[j + 1] - q[i] <= window_s:
            j += 1
        best = max(best, j - i + 1)
    return best


def _slope(pts):
    """最小二乘斜率（y 对 x）。点少于 3 个或 x 无展布返回 None。"""
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    if den <= 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def _growth_stats(events, buckets=8):
    """发散的**增长**读数：把事件按时间等分成若干桶，取每桶 step/dt 的中位数。

    为什么看**速率**（step/dt）而不是步长：每触发一次 rebase 参考位姿就被重锚，
    步长随之回落再涨（实测 31.4→17.8→18.1→16.6m 锯齿）⇒ 单条步长没有单调性；
    而 step/dt 是**内部估计的漂移速度**，发散时它随时间**线性上涨**（IMU 双重
    积分：速度线性、位置二次）。取桶内中位是为了压掉那次重定位大跳（165036 有
    一条 27079m、165700 有 31.4m 的起步跳）。

    **线性度（R²）才是判据**：良性会话的 rebase 是零散重定位，速率既不起涨也
    不成线；发散是「每秒都在 rebase 且越来越快」。2026-09-18 实测两条灾害会话的
    桶速率 R² = 0.98/0.99（165036: 122→964 m/s，约 +16 m/s²；
    165700: 34→170 m/s，约 +5 m/s²），且都是从**静止/慢速**起涨。
    """
    pts = sorted((e["t"], e["step"], e["step"] / e["dt"]) for e in events
                 if e["step"] is not None and e["dt"] and e["dt"] > 0)
    if len(pts) < 3 * buckets:
        return None
    size = max(1, len(pts) // buckets)
    series = []
    for i in range(0, len(pts), size):
        chunk = pts[i:i + size]
        if len(chunk) < 3:
            continue
        series.append({
            "t": sum(p[0] for p in chunk) / len(chunk),
            "n": len(chunk),
            "step": _median([p[1] for p in chunk]),
            "speed": _median([p[2] for p in chunk]),
        })
    if len(series) < 3:
        return None
    t0 = series[0]["t"]
    xs = [s["t"] - t0 for s in series]
    ys = [s["speed"] for s in series]
    slope = _slope(list(zip(xs, ys)))
    r2 = None
    if slope is not None:
        my = sum(ys) / len(ys)
        ss_tot = sum((y - my) ** 2 for y in ys)
        mx = sum(xs) / len(xs)
        a = my - slope * mx
        ss_res = sum((y - (a + slope * x)) ** 2 for x, y in zip(xs, ys))
        r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    return {
        "speed_head": series[0]["speed"],
        "speed_tail": series[-1]["speed"],
        "speed_slope": slope,
        "r2": r2,
        "step_head": series[0]["step"],
        "step_tail": series[-1]["step"],
        "series": series,
    }


def find_floods(events, window_s, min_step, min_lines, gap_s):
    """滑窗密度 ≥ min_lines 的 rebase 串 → 洪水段（相邻段相隔 ≤gap_s 合并）。"""
    q = [e for e in events if e["step"] is not None and e["step"] >= min_step]
    hot = [False] * len(q)
    j = 0
    for i in range(len(q)):
        if j < i:
            j = i
        while j + 1 < len(q) and q[j + 1]["t"] - q[i]["t"] <= window_s:
            j += 1
        if j - i + 1 >= min_lines:
            for k in range(i, j + 1):
                hot[k] = True
    segs = []
    for k, e in enumerate(q):
        if not hot[k]:
            continue
        if segs and e["t"] - segs[-1][-1]["t"] <= gap_s:
            segs[-1].append(e)
        else:
            segs.append([e])
    floods = []
    for seg in segs:
        t0 = seg[0]["t"]
        t1 = seg[-1]["t"]
        inside = [e for e in events if t0 <= e["t"] <= t1]
        floods.append({
            "t0": t0,
            "t1": t1,
            "duration_s": t1 - t0,
            "lines": len(inside),
            "peak_window": _max_window(inside, window_s, min_step),
            "max_run": _max_run(inside),
            "max_step": max((e["step"] for e in inside
                             if e["step"] is not None), default=0.0),
            "events": inside,
        })
    return floods


def merge_episodes(floods, gap_s, sessions=None):
    """把同一会话里相隔 ≤gap_s 的洪水段并成**一次发散**。

    发散一旦开始就自锁（每帧 rebase），直到重开夹爪才结束——中途 rebase 密度会
    随跟踪状态起伏掉到阈值以下（165036 就被切成 3 段：37.3s / 10.7s / 0.1s），
    那是**同一次发散**，不该按三次计。合并后 onset 取第一段的起点。

    ``sessions`` 传进来时，行数按 [t0,t1] 区间里的**全部** rebase 行重算（含阈值
    以下那几段垫底），这样「1474 行」读起来就是「这次发散期间刷了多少行」。
    """
    episodes = []
    for f in sorted(floods, key=lambda x: x["t0"]):
        if (episodes and episodes[-1]["session"] == f["session"] and
                f["t0"] - episodes[-1]["t1"] <= gap_s):
            ep = episodes[-1]
            ep["segments"] += 1
            ep["t1"] = max(ep["t1"], f["t1"])
            ep["lines"] += f["lines"]
            ep["peak_window"] = max(ep["peak_window"], f["peak_window"])
            ep["max_run"] = max(ep["max_run"], f["max_run"])
            ep["max_step"] = max(ep["max_step"], f["max_step"])
            ep["events"] = ep["events"] + f["events"]
            ep["duration_s"] = ep["t1"] - ep["t0"]
        else:
            ep = dict(f)
            ep["events"] = list(f["events"])
            ep["segments"] = 1
            episodes.append(ep)
    by_name = {s["name"]: s["events"] for s in (sessions or [])}
    for ep in episodes:
        ep.update(_growth_stats(ep["events"]) or {
            "speed_head": None, "speed_tail": None, "speed_slope": None,
            "r2": None, "step_head": None, "step_tail": None, "series": []})
        if ep["session"] in by_name:
            ep["lines"] = len([e for e in by_name[ep["session"]]
                               if ep["t0"] <= e["t"] <= ep["t1"]])
    return episodes


# ── main.log 侧：时间线重建与停止事件 ───────────────────────
def _midnight(date_str):
    """'YYYY-MM-DD' → 该日本地零点 epoch（走 mktime，时区/夏令时交给 libc）。"""
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    return time.mktime(d.timetuple())


def load_main_log(path):
    """读 main.log → 行表（含重建出的 epoch；日期未知的行 epoch=None）。

    行表元素：[tod, date_or_None, text, epoch_or_None]
    """
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            ts = RE_MAIN_TS.match(raw)
            tod = None
            date = None
            if ts:
                tod = int(ts.group(1)) * 3600 + int(ts.group(2)) * 60 + \
                    int(ts.group(3))
            emb = RE_EMBED_DATE.search(raw)
            if emb and tod is not None:
                etod = (int(emb.group(4)) * 3600 + int(emb.group(5)) * 60 +
                        int(emb.group(6)))
                if abs(etod - tod) <= _ANCHOR_TOL_S:
                    date = "-".join(emb.group(1, 2, 3))
            rows.append([tod, date, raw, None])

    day = None
    last_tod = None
    last_epoch = None
    known = 0
    for row in rows:
        tod, date = row[0], row[1]
        if tod is None:
            row[3] = last_epoch          # 续行（多行堆栈等）与上一行同一时刻
            continue
        if day is not None and last_tod is not None:
            delta = tod - last_tod
            if delta < -_DAY_JUMP_S:
                day += 86400.0           # 跨天
            elif delta > _DAY_JUMP_S:
                day -= 86400.0           # 时钟回拨/日志重排（防御）
        if date is not None:
            day = _midnight(date)        # 锚点：重新对齐
        row[3] = (day + tod) if day is not None else None
        if row[3] is not None:
            known += 1
        last_tod = tod
        last_epoch = row[3]
    return rows, known


def extract_stops(rows):
    """从行表里摘出录制/链路事件（epoch 未知的行跳过）。"""
    stops = []
    for idx, row in enumerate(rows):
        epoch, text = row[3], row[2]
        if epoch is None:
            continue
        if "■ Recording completed" in text:
            kind = "completed"
        elif "⛔ Recording aborted" in text:
            kind = "aborted"
        elif "▶ Recording started" in text:
            kind = "started"
        elif "原始流连接中断" in text:
            kind = "disconnect"
        elif "原始流已重连" in text:
            kind = "reconnect"
        else:
            continue
        stops.append({"kind": kind, "epoch": epoch, "idx": idx,
                      "text": text[:100]})
    return stops


# ── 配对与统计 ──────────────────────────────────────────────
def attribute(flood, stops, lookback_s):
    """这次发散 ← 哪一次停止：**往前**找最近的完成/中止（默认 30s 内）。

    不能取「时间上最近的事件」：发散自锁后会一直持续，中途的断链/重连/开录都
    比起因更近，会把账记到错的事件上。所以只认**之前的停止**，取最近的；找不
    到就报「无邻近停止」并把附近事件列出来供人工判断。
    """
    cand = [s for s in stops
            if s["kind"] in ("completed", "aborted") and
            0.0 <= flood["t0"] - s["epoch"] <= lookback_s]
    first = max(cand, key=lambda x: x["epoch"]) if cand else None
    label = ("{}（{:+.1f}s）".format(_KIND_LABEL[first["kind"]],
                                    first["epoch"] - flood["t0"])
             if first else "无邻近停止")
    near = [s for s in stops if abs(s["epoch"] - flood["t0"]) <= NEAR_S]
    near.sort(key=lambda s: abs(s["epoch"] - flood["t0"]))
    return label, first, near


def _match(targets, others, window):
    """把 others 按窗口配到 targets 上 → (窗口内条数, 命中过的 target 数, 偏移表)。"""
    hits = 0
    matched = 0
    offsets = []
    for tgt in targets:
        got = [o for o in others
               if window[0] <= o["epoch"] - tgt["epoch"] <= window[1]]
        if got:
            matched += 1
            nearest = min(got, key=lambda o: abs(o["epoch"] - tgt["epoch"]))
            offsets.append(nearest["epoch"] - tgt["epoch"])
            hits += len(got)
    return hits, matched, offsets


def day_stats(day, stops, sessions, floods, window_s, min_lines):
    of_day = [s for s in stops
              if datetime.date.fromtimestamp(s["epoch"]).isoformat() == day]
    kinds = {}
    for s in of_day:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    completed = [s for s in of_day if s["kind"] == "completed"]
    aborted = [s for s in of_day if s["kind"] == "aborted"]
    disconnect = [s for s in of_day if s["kind"] == "disconnect"]
    hits, matched, offsets = _match(completed, disconnect, COMPLETED_WINDOW)
    a_hits, a_matched, a_offsets = _match(aborted, disconnect, ABORT_WINDOW)
    day_floods = [f for f in floods
                  if datetime.date.fromtimestamp(f["t0"]).isoformat() == day]
    return {
        "date": day,
        "started": kinds.get("started", 0),
        "completed": len(completed),
        "aborted": len(aborted),
        "disconnect": len(disconnect),
        "reconnect": kinds.get("reconnect", 0),
        "sessions": len([s for s in sessions if s["first_t"] and
                         datetime.date.fromtimestamp(s["first_t"]).isoformat()
                         == day]),
        "floods": day_floods,
        "disc_in_completed_window": hits,
        "completed_with_disc": matched,
        "disc_in_completed_offsets": offsets,
        "disc_in_abort_window": a_hits,
        "aborted_with_disc": a_matched,
        "disc_in_abort_offsets": a_offsets,
    }


# ── 打印 ────────────────────────────────────────────────────
def _hms(epoch, ms=False):
    if epoch is None:
        return "—"
    out = datetime.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
    if ms:
        out += ".{:03d}".format(int(round((epoch % 1) * 1000)) % 1000)
    return out


def _dur(sec):
    if sec is None:
        return "—"
    if sec < 60:
        return "{:.1f}s".format(sec)
    return "{:.0f}m{:02.0f}s".format(sec // 60, sec % 60)


def _session_of(epoch):
    return next((s for s in _SESSION_CACHE
                 if s["first_t"] is not None and
                 s["first_t"] <= epoch <= (s["last_t"] or s["first_t"])), None)


def print_sessions(sessions, episodes, show_all):
    print("── 逐会话：位姿 rebase ─────────────────────────────────────"
          "──────────")
    print("  {:<10}{:>10}{:>8}{:>8}{:>9}{:>10}{:>10}   {}".format(
        "会话起点", "日志 id", "时长", "rebase", "最长连跑", "1秒窗峰值",
        "最大步长", "发散"))
    by_id = {}
    for ep in episodes:
        by_id.setdefault(ep["session"], []).append(ep)
    for s in sessions:
        mine = by_id.get(s["name"], [])
        if not show_all and not s["rebase"] and not mine:
            continue
        start = s["first_t"]
        dur = (s["last_t"] - start) if (start and s["last_t"]) else None
        tag = "  ".join("★{} {}行".format(_dur(ep["duration_s"]), ep["lines"])
                        for ep in mine)
        print("  {:<10}{:>10}{:>8}{:>8}{:>9}{:>10}{:>9.2f}m   {}".format(
            _hms(start), s["name"].split("_")[1] if "_" in s["name"] else "",
            _dur(dur), s["rebase"], s["max_run"], s["max_window"],
            s["max_step"], tag or "—"))
    if any(s["unparsed"] for s in sessions):
        print("  （撕裂/截断导致取不到步长的 rebase 行：{} 条，只计行数）".format(
            sum(s["unparsed"] for s in sessions)))
    print("  注：文件名里的时刻是包装侧建文件的时间，比会话真正的首行晚 1~3 分钟，"
          "所以「会话起点」取首行时刻。")


def print_episodes(episodes, stops, lookback_s):
    print()
    print("── 每次发散 ← 哪一次停止 ───────────────────────────────────"
          "──────────")
    if not episodes:
        print("  无（阈值内没有会话发生 rebase 洪水）")
        return
    print("  {:<9}{:>13}{:>8}{:>4}{:>7}{:>10}{:>17}{:>8}   {}".format(
        "会话起点", "发散起点", "时长", "段", "行数", "最大步长",
        "漂移速率 首→末", "线性度", "归属"))
    for ep in sorted(episodes, key=lambda x: x["t0"]):
        sid = _session_of(ep["t0"])
        speed = ("{:.0f}→{:.0f} m/s".format(ep["speed_head"], ep["speed_tail"])
                 if ep["speed_head"] is not None else "—")
        r2 = "{:.2f}".format(ep["r2"]) if ep["r2"] is not None else "—"
        label, first, near = attribute(ep, stops, lookback_s)
        print("  {:<9}{:>13}{:>8}{:>4}{:>7}{:>9.1f}m{:>17}{:>8}   {}".format(
            _hms(sid["first_t"]) if sid else "—", _hms(ep["t0"], ms=True),
            _dur(ep["duration_s"]), ep["segments"], ep["lines"],
            ep["max_step"], speed, r2, label))
        for s in near[:4]:
            print("      {:<6}{:>10}  {:+.1f}s  {}".format(
                _KIND_LABEL.get(s["kind"], s["kind"]), _hms(s["epoch"]),
                s["epoch"] - ep["t0"], s["text"][:76]))
        if first is not None:
            print("      ↳ 起因而这次停止：{}  {}（发散起点在它之后 {:+.1f}s）"
                  .format(_KIND_LABEL[first["kind"]], _hms(first["epoch"]),
                          ep["t0"] - first["epoch"]))
    print("  判据：漂移速率 = rebase 步长/dt（内部估计速度），按时间等分 8 桶取中位；")
    print("        线性度 = 桶速率对时间拟合的 R²（发散＝线性上涨；良性零散重定位"
          "既不起涨也不成线）。")


_SESSION_CACHE = []


def print_baseline(days):
    print()
    print("── 逐日基线 ───────────────────────────────────────────────"
          "──────────")
    print("  {:<12}{:>6}{:>6}{:>9}{:>6}{:>9}{:>7}{:>7}{:>9}".format(
        "日期", "会话", "完成", "收尾断链", "中止", "中止断链", "断链", "重连",
        "发散"))
    for d in days:
        offs = d["disc_in_completed_offsets"]
        med = ("{:.1f}s".format(statistics.median(offs)) if offs else "—")
        print("  {:<12}{:>6}{:>6}{:>9}{:>6}{:>9}{:>7}{:>7}{:>9}".format(
            d["date"], d["sessions"], d["completed"],
            "{}/{}".format(d["disc_in_completed_window"], d["completed"]),
            d["aborted"],
            "{}/{}".format(d["disc_in_abort_window"], d["aborted"]),
            d["disconnect"], d["reconnect"], len(d["floods"])))
        if d["completed"]:
            print("      「完成」收尾窗口 {}s 内命中断链 {} 条；"
                  "带断链的完成 {}/{}；最近偏移中位 {}（正=断链更晚）".format(
                      "{:g}~{:g}".format(*COMPLETED_WINDOW),
                      d["disc_in_completed_window"], d["completed_with_disc"],
                      d["completed"], med))
        if d["aborted"]:
            print("      「中止」窗口 {}s 内命中断链 {} 条；带断链的中止 {}/{}"
                  "（中止不做 parquet 确认，只 flush ⇒ 与「完成」的对比是"
                  "「断链主因是不是 parquet」的判据）".format(
                      "{:g}~{:g}".format(*ABORT_WINDOW),
                      d["disc_in_abort_window"], d["aborted_with_disc"],
                      d["aborted"]))
        for ep in d["floods"]:
            print("      发散 {} → {}（{} 行 / {} 段，最大步长 {:.1f}m）".format(
                _hms(ep["t0"], ms=True), _hms(ep["t1"]),
                ep["lines"], ep["segments"], ep["max_step"]))


# ── main ────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="SLAM 位姿发散审计（只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--native-dir", default=DEFAULT_NATIVE_DIR,
                        help="原生会话日志目录（默认 logs/slam_native）")
    parser.add_argument("--main-log", default=DEFAULT_MAIN_LOG,
                        help="主程序日志（默认 logs/main.log）")
    parser.add_argument("--date", default=None, help="只看这一天 YYYY-MM-DD")
    parser.add_argument("--since", default=None,
                        help="只看该日及以后 YYYY-MM-DD")
    parser.add_argument("--window-s", type=float, default=1.0,
                        help="密度判据的滑窗宽度（默认 1.0）")
    parser.add_argument("--min-lines", type=int, default=15,
                        help="滑窗内至少这么多条 rebase 才算洪水（默认 15）")
    parser.add_argument("--min-step-m", type=float, default=1.0,
                        help="单条 rebase 计入门槛，米（默认 1.0）")
    parser.add_argument("--gap-s", type=float, default=2.0,
                        help="相邻命中相隔 ≤ 该值合并成同一段洪水（默认 2.0）")
    parser.add_argument("--episode-gap-s", type=float, default=30.0,
                        help="同一会话里两段洪水相隔 ≤ 该值视为同一次发散"
                             "（默认 30；发散自锁后会持续，中途密度会掉下去）")
    parser.add_argument("--lookback-s", type=float, default=30.0,
                        help="往前找起因果的停止事件的最大回溯（默认 30）")
    parser.add_argument("--all", action="store_true",
                        help="连没有 rebase 的会话也列出")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.native_dir):
        print(f"原生日志目录不存在: {args.native_dir}")
        return 2
    if not os.path.isfile(args.main_log):
        print(f"主程序日志不存在: {args.main_log}")
        return 2

    sessions = [load_session(p) for p in
                sorted(glob.glob(os.path.join(args.native_dir,
                                              "*_stdout.log")))]
    sessions.sort(key=lambda s: s["first_t"] or 0)
    global _SESSION_CACHE
    _SESSION_CACHE = sessions

    floods = []
    for s in sessions:
        for f in find_floods(s["events"], args.window_s, args.min_step_m,
                             args.min_lines, args.gap_s):
            f["session"] = s["name"]
            floods.append(f)
    # 会话之间不合并：不同会话的洪水必然是两次（换过进程/重开过夹爪）
    episodes = merge_episodes(floods, args.episode_gap_s, sessions)

    rows, known = load_main_log(args.main_log)
    stops = extract_stops(rows)

    if args.date or args.since:
        lo = args.date or args.since
        hi = args.date
        def keep(ep):
            d = datetime.date.fromtimestamp(ep).isoformat()
            if hi:
                return d == hi
            return d >= lo
        stops = [s for s in stops if keep(s["epoch"])]
        sessions = [s for s in sessions
                    if s["first_t"] and keep(s["first_t"])]
        episodes = [ep for ep in episodes if keep(ep["t0"])]
        floods = [f for f in floods if keep(f["t0"])]

    dates = sorted({datetime.date.fromtimestamp(s["epoch"]).isoformat()
                    for s in stops} |
                   {datetime.date.fromtimestamp(s["first_t"]).isoformat()
                    for s in sessions if s["first_t"]})
    days = [day_stats(d, stops, sessions, episodes, args.window_s,
                      args.min_lines) for d in dates]

    if args.json:
        print(json.dumps({
            "main_log_lines": len(rows),
            "dated_lines": known,
            "sessions": [{k: v for k, v in s.items()
                          if k not in ("events", "path")} for s in sessions],
            "episodes": [{k: v for k, v in ep.items() if k != "events"}
                         for ep in episodes],
            "days": [{k: v for k, v in d.items() if k != "floods"} | {
                "divergences": len(d["floods"])} for d in days],
            "criteria": {"window_s": args.window_s,
                         "min_lines": args.min_lines,
                         "min_step_m": args.min_step_m,
                         "gap_s": args.gap_s,
                         "episode_gap_s": args.episode_gap_s,
                         "lookback_s": args.lookback_s},
        }, ensure_ascii=False, indent=2, default=str))
    else:
        print("原生会话日志: {} 份（{}）".format(
            len(sessions), os.path.relpath(args.native_dir, ROOT)))
        print("主程序日志: {} 行，其中 {} 行解出日期（锚点之前的行不参与配对）"
              .format(len(rows), known))
        print()
        print_sessions(sessions, episodes, args.all)
        print_episodes(episodes, stops, args.lookback_s)
        print_baseline(days)

    return 1 if episodes else 0


if __name__ == "__main__":
    sys.exit(main())
