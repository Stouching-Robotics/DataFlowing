#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/audit_frame_gaps.py 回归自检（**不依赖真机数据**，全部现场造）。

    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_audit_frame_gaps.py

为什么值得单测：这个脚本的结论会被当成「视频到底丢了多少」的权威数字写进
文档，而它的三层判据（画面 / 双钟 / 单钟）每一层都有一种**看起来很对但结论
相反**的坑：

  ① 画面复核是主判据。把「钟跳了但画面是连的」当成丢帧（本库 72 段 / 20.1s
     的 ~250ms 事件全是这一类），或把「钟跳了且画面真跳」当成没事，都会得到
     一个自洽但错误的结论——所以两种情形都要有正反用例钉住。
  ② 早年段的 hardware_ns 是**有符号 32 位**的截断计数器：不解卷 ⇒ 每次过零都
     是一次天文数字级的假空洞；解卷过头（对 64 位戳也解卷）⇒ 把真实的大空洞
     折叠成小数字（ep-099 的 4.68s 会变成 0.39s）。
  ③ 静止场景里「画面连续」和「画面跳变」长得一样（都≈噪声地板）⇒ 必须回落
     到「判不了」，不许判成「没丢」。

用例里的画面是**线性渐变条**：整体平移 δ 像素 ⇒ mad ≡ 255δ/宽，与位移严格
成正比。于是「一帧的运动量」和「30 帧的运动量」在数值上就分得开（6.4 vs 191），
阈值判据才有可判定的正反例。

覆盖:
  1 纯函数契约：时钟分类 / 解卷 / 段号归一 / 事件定性
  2 健康段：零事件零账
  3 画面复核三态：真丢（跳变）/ 钟跳（连续）/ 静止（判不了，不许说没丢）
  4 归账四桶互斥：丢了 / 钟跳没丢 / 打嗝 / 待复核，且同一形状只进一桶
  5 trunc32 解卷：跨符号边界仍恰好 1 起事件、大小正确（防回绕假空洞）
  6 63 位戳不解卷：>2.1s 的真空洞必须原样保留（防折叠）
  7 合并：15 行内的两次异常 → 1 起事件、取 max 不求和（防同一个洞数两遍）
  8 拒答：多槽 episode / 无时间戳 / mp4 帧数不符行数
  9 CLI：退出码、--json 结构、--no-video-check（未复核不许算「没丢」）、
     --clock-tol-ms 0 时必须变红、--camera-logs 时间窗
退出码 0 = 全部通过。
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from tools import audit_frame_gaps as audit                          # noqa: E402

FAILS = []
NOMINAL = int(1e9 / 30.0)
MS = 1_000_000


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


# ── 造数据 ──────────────────────────────────────────────────
def _gradient(offset, width=160, height=120):
    """线性渐变条（整体平移 offset 像素，环绕）。平移 δ ⇒ mad ≈ 255δ/width。"""
    xs = (np.arange(width) - int(offset)) % width
    row = (xs * 255 // width).astype("uint8")
    gray = np.tile(row, (height, 1))
    return np.dstack([gray] * 3)


def _write_video(path, count, step_px=4, jump_at=None, jump_px=120,
                 static=False):
    """count 帧；第 jump_at 帧前额外多平移 jump_px 像素（= 画面跳变）。"""
    import cv2
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 30.0,
                             (160, 120), isColor=True)
    if not writer.isOpened():
        raise RuntimeError("VideoWriter 打不开，测试无法取证")
    offset = 0
    for index in range(count):
        if jump_at is not None and index == jump_at:
            offset += jump_px
        writer.write(_gradient(offset))
        if not static:
            offset += step_px
    writer.release()
    return _video_count(path)


def _video_count(path):
    import cv2
    cap = cv2.VideoCapture(path)
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if cap.isOpened() else -1
    cap.release()
    return count


def _write_parquet(path, frame_index, hw, wall):
    import pyarrow as pa
    import pyarrow.parquet as pq
    pq.write_table(pa.table({
        "frame_index": pa.array(frame_index, pa.int64()),
        "hardware_ns": pa.array(hw, pa.int64()),
        "wall_time": pa.array(wall, pa.float64()),
    }), path)


def _series(count, hw_step=NOMINAL, wall_step_ns=NOMINAL, hw0=3_600_000_000_000,
            wall0=1_789_000_000.0, hw_jump_at=None, hw_jump_ns=0,
            wall_jump_at=None, wall_jump_ns=0, wrap32=False):
    """造两条时间轴；jump_at 处的**间隔**额外加 jump_ns（洞就在那两行之间）。"""
    hw, wall = [hw0], [wall0]
    for index in range(1, count):
        hw.append(hw[-1] + hw_step + (hw_jump_ns if index == hw_jump_at else 0))
        wall.append(wall[-1] + (wall_step_ns
                                + (wall_jump_ns if index == wall_jump_at else 0))
                    / 1e9)
    if wrap32:                       # 折进有符号 32 位：值域 [−2^31, 2^31)
        hw = [((value + 2 ** 31) % 2 ** 32) - 2 ** 31 for value in hw]
    return hw, wall


class _Sandbox:
    """一个临时任务目录；add() 造一段（parquet + mp4）。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ksq-audit-test-")
        os.makedirs(os.path.join(self.root, "data", "chunk-000"))
        os.makedirs(os.path.join(self.root, "videos", "chunk-000",
                                 "gripper_rgb"))

    def paths(self, ep):
        return (os.path.join(self.root, "data", "chunk-000",
                             f"episode-{ep}.parquet"),
                os.path.join(self.root, "videos", "chunk-000", "gripper_rgb",
                             f"episode-{ep}.mp4"))

    def add(self, ep, count=60, video_count=None, **kw):
        """kw 分两路：video_* 给 _write_video，其余给 _series。"""
        video_kw = {key[6:]: kw.pop(key) for key in list(kw)
                    if key.startswith("video_")}
        hw, wall = _series(count, **kw)
        parquet, video = self.paths(ep)
        _write_parquet(parquet, list(range(count)), hw, wall)
        frames = _write_video(video, count if video_count is None
                              else video_count, **video_kw)
        return parquet, video, frames

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def audit_one(parquet, video, **kw):
    kw.setdefault("video_check", True)
    return audit.audit_episode(parquet, video, NOMINAL, 100.0, 100.0, 15, **kw)


def events_of(result):
    return result["events"]


# ── 1 纯函数 ────────────────────────────────────────────────
def check_pure_functions():
    print("\n[1] 纯函数契约")
    check(audit.clock_class([0, 0, 0]) == "none", "全 0 ⇒ none（无时间戳）")
    check(audit.clock_class([100, 200, 300]) == "hw64", "小正数 ⇒ hw64")
    check(audit.clock_class([10 ** 14, 10 ** 14 + 33 * MS]) == "hw64",
          "宿主单调钟量级 ⇒ hw64")
    check(audit.clock_class([1_004_913_502, -1_774_230_385]) == "trunc32",
          "出现负值 ⇒ trunc32（有符号 32 位）")
    check(audit.clock_class([2 ** 31 - 10, 2 ** 31 - 5]) == "hw64",
          "值域上界 <2^31 且全正、跨度小 ⇒ hw64（不能一见 <2^31 就喊 trunc32）")
    check(audit._unwrap(-2_779_143_887) == 1_515_823_409,
          "解卷：−2779.1ms 的回绕读作 +1515.8ms")
    check(audit._unwrap(33 * MS) == 33 * MS, "解卷：正常间隔原样返回")
    check(audit.episode_stem("/x/episode-099.parquet") == "099"
          and audit.episode_stem("episode-051.mp4") == "051",
          "parquet 与 mp4 归一到同一个段号（否则一段被数成两条）")
    merged = audit._classify([(10, 30 * MS, 220 * MS), (12, 210 * MS, 33 * MS)],
                             100 * MS, 100 * MS, 15)
    check(len(merged) == 1 and merged[0]["kind"] == "both",
          "15 行内两侧都停合并成一件事（同一件事不许数两遍）")
    check(merged[0]["d_hw"] == 210 * MS and merged[0]["d_wall"] == 220 * MS,
          "合并取 max 而不是求和（求和会把 250ms 的洞报成 500ms）")
    check(audit._classify([(10, 30 * MS, 220 * MS)], 100 * MS, 100 * MS,
                          15)[0]["kind"] == "wall",
          "只有墙钟跳 ⇒ wall（写侧打嗝）")
    check(audit._classify([(10, 220 * MS, 33 * MS)], 100 * MS, 100 * MS,
                          15)[0]["kind"] == "hw",
          "只有戳跳（读数取帧处的大间隔）⇒ hw（读侧停顿）")
    check(audit._classify([(10, 220 * MS, 400 * MS)], 100 * MS, 100 * MS,
                          15)[0]["kind"] == "mismatch",
          "双钟都跳但差 180ms > 容差 ⇒ mismatch（两侧不齐，不当成互证）")


# ── 2/3/4 画面复核与归账 ────────────────────────────────────
def check_healthy(box):
    print("\n[2] 健康段：零事件、零账")
    parquet, video, frames = box.add("001")
    check(frames == 60, f"造出的 mp4 帧数与行数一致（{frames}）")
    result = audit_one(parquet, video)
    check(result["clock"] == "hw64", "时钟判定 hw64")
    check(events_of(result) == [], "零事件")
    check(result["gap_ms"] == 0 and result["pending_ms"] == 0
          and result["stamp_ms"] == 0 and result["wall_ms"] == 0,
          "四桶全 0")
    check(result["video_ok"] is True, "mp4 帧数校验通过（画面复核真的跑了）")


def check_real_loss(box):
    print("\n[3a] 真丢帧：双钟跳 + 画面跳 ⇒ 计入「丢了」")
    parquet, video, _ = box.add("002", count=80, hw_jump_at=10,
                                hw_jump_ns=1000 * MS, wall_jump_at=10,
                                wall_jump_ns=1000 * MS,
                                video_jump_at=10, video_jump_px=120)
    result = audit_one(parquet, video)
    event = events_of(result)[0]
    check(len(events_of(result)) == 1, "恰好 1 起事件")
    check(event["evidence"] == "jump",
          f"画面复核判跳变（boundary={event['boundary']} vs "
          f"near={event['near']}）")
    check(event["verdict"] == "丢了", "结论「丢了」")
    check(abs(result["gap_ms"] - 1000.0) < 2,
          f"空洞 {result['gap_ms']}ms ≈ 1000ms（间隔 1033ms − 标称 33ms）")
    check(result["stamp_ms"] == 0 and result["pending_ms"] == 0,
          "不进「戳跳」也不进「待复核」桶（四桶互斥）")


def check_stamp_only(box):
    print("\n[3b] 钟跳但画面连续 ⇒ **不算丢**（本库 72 段 20.1s 的形态）")
    parquet, video, _ = box.add("003", count=80, hw_jump_at=10,
                                hw_jump_ns=1000 * MS, wall_jump_at=10,
                                wall_jump_ns=1000 * MS)
    result = audit_one(parquet, video)
    event = events_of(result)[0]
    check(event["evidence"] == "continuous",
          f"画面复核判连续（boundary={event['boundary']} ≈ "
          f"near={event['near']}）")
    check(event["verdict"] == "没丢(画面连续)", "结论「没丢(画面连续)」")
    check(result["gap_ms"] == 0, "**不计入损失**（这正是本次审计的关键结论）")
    check(result["stamp_ms"] > 900,
          f"进「钟跳没丢」桶（{result['stamp_ms']}ms）")


def check_static_scene(box):
    print("\n[3c] 静止场景里的钟跳 ⇒ 判不了（不许判「没丢」）")
    parquet, video, _ = box.add("004", count=80, hw_jump_at=10,
                                hw_jump_ns=1000 * MS, wall_jump_at=10,
                                wall_jump_ns=1000 * MS, video_static=True)
    result = audit_one(parquet, video)
    event = events_of(result)[0]
    check(event["evidence"] == "unknown",
          "画面整窗静止 ⇒ 不定（少了静止画面也看不出来，不能反着判）")
    check(result["gap_ms"] == 0 and result["pending_ms"] > 900,
          f"只进「待复核」桶（{result['pending_ms']}ms），不计入损失也不说没事")
    check(event["verdict"] == "两侧同停",
          "结论按双钟口径标出「哪一侧停了」（不是「没丢」）")


def check_writer_hiccup(box):
    print("\n[4] 写侧打嗝：只有墙钟跳 ⇒ 帧没少、只是写晚")
    parquet, video, _ = box.add("005", count=80, wall_jump_at=10,
                                wall_jump_ns=300 * MS)
    result = audit_one(parquet, video)
    check(result["wall_ms"] > 250 and result["gap_ms"] == 0,
          f"进「打嗝」桶（{result['wall_ms']}ms）且不计入损失")
    check(events_of(result)[0]["kind"] == "wall", "事件定性 wall")


# ── 5/6 时钟与解卷 ──────────────────────────────────────────
def check_trunc32(box):
    print("\n[5] 早年段（有符号 32 位戳）：跨符号边界仍恰好 1 起事件")
    parquet, video, _ = box.add("006", count=200, hw_jump_at=100,
                                hw_jump_ns=800 * MS, wall_jump_at=100,
                                wall_jump_ns=800 * MS, wrap32=True,
                                video_jump_at=100, video_jump_px=120)
    result = audit_one(parquet, video)
    check(result["clock"] == "trunc32", "时钟判定 trunc32")
    check(len(events_of(result)) == 1,
          f"200 行里 6 次过零，只报 1 起事件（回绕不该造出假空洞）："
          f"{len(events_of(result))} 起")
    check(abs(result["gap_ms"] - 800.0) < 2,
          f"空洞 {result['gap_ms']}ms ≈ 800ms（解卷正确）")
    check(result["resyncs"] == 0, "解卷后没有「时间倒退」")


def check_hw64_no_unwrap(box):
    print("\n[6] 64 位戳不解卷：>2.1s 的真空洞必须原样保留")
    parquet, video, _ = box.add("007", count=80, hw_jump_at=10,
                                hw_jump_ns=4680 * MS, wall_jump_at=10,
                                wall_jump_ns=4680 * MS,
                                video_jump_at=10, video_jump_px=120)
    result = audit_one(parquet, video)
    check(result["clock"] == "hw64", "时钟判定 hw64（不按 trunc32 处理）")
    check(abs(result["gap_ms"] - 4680.0) < 2,
          f"空洞 {result['gap_ms']}ms ≈ 4680ms —— 若误按回绕解卷会变成 418ms")


# ── 8 拒答 ─────────────────────────────────────────────────
def check_refusals(box):
    print("\n[8] 宁可拒答也不猜")
    # 多槽 episode：帧序号按槽各自从 0 编号
    parquet, video = box.paths("008")
    _write_parquet(parquet, [0, 1, 0, 1, 0, 1],
                   [3_600_000_000_000 + i * NOMINAL for i in range(6)],
                   [1_789_000_000.0 + i / 30 for i in range(6)])
    _write_video(video, 6)
    result = audit_one(parquet, video)
    check(result["status"] == "skip" and "多槽" in result["note"],
          f"多槽 episode 跳过（{result['note']}）")

    # hardware_ns 全 0（lite 那种无时间戳槽位）
    parquet, video, _ = box.add("009", count=40)
    _write_parquet(parquet, list(range(40)), [0] * 40, [0.0] * 40)
    result = audit_one(parquet, video)
    check(result["status"] == "skip" and "时间戳" in result["note"],
          f"无时间戳跳过（{result['note']}）")

    # mp4 帧数 ≠ 行数：画面复核的序号对不上，结论不可用
    parquet, video, _ = box.add("010", count=80, hw_jump_at=10,
                                hw_jump_ns=1000 * MS, wall_jump_at=10,
                                wall_jump_ns=1000 * MS, video_count=70)
    result = audit_one(parquet, video)
    check(result["status"] == "warn" and result["video_frames"] == 70,
          f"帧数不符 ⇒ warn（{result['note']}）")
    check(events_of(result)[0]["evidence"] == "unknown"
          and result["gap_ms"] == 0 and result["pending_ms"] > 900,
          "画面对不上号 ⇒ 不判跳变也不判连续，落「待复核」")


# ── 9 CLI ──────────────────────────────────────────────────
def _run(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = audit.main(argv)
    return code, buffer.getvalue()


def check_cli(box):
    print("\n[9] CLI：退出码 / JSON / 未复核口径 / 双钟阈值 / 留档关联")
    task_dir = os.path.join(box.root, "cli")
    shutil.copytree(os.path.join(box.root, "data"),
                    os.path.join(task_dir, "data"), dirs_exist_ok=True)
    shutil.copytree(os.path.join(box.root, "videos"),
                    os.path.join(task_dir, "videos"), dirs_exist_ok=True)
    # 只留 003（戳跳、画面连续）与 005（写侧打嗝）：两者都不该判「丢了」
    for name in os.listdir(os.path.join(task_dir, "data", "chunk-000")):
        if not name.startswith(("episode-003", "episode-005")):
            os.unlink(os.path.join(task_dir, "data", "chunk-000", name))
    for name in os.listdir(os.path.join(task_dir, "videos", "chunk-000",
                                        "gripper_rgb")):
        if not name.startswith(("episode-003", "episode-005")):
            os.unlink(os.path.join(task_dir, "videos", "chunk-000",
                                   "gripper_rgb", name))

    code, out = _run([task_dir])
    check(code == 0, f"画面证实零丢帧 ⇒ 退出码 0（rc={code}）")
    check("画面证实丢了 0 段" in out, "合计行说 0 段（不许把钟跳算成丢）")
    check("钟跳但画面连续" in out and "写侧打嗝" in out, "两个非损失桶都列出来")

    # 加一个真丢帧的段 ⇒ 必须变红
    parquet, video, _ = box.add("011", count=80, hw_jump_at=10,
                                hw_jump_ns=1000 * MS, wall_jump_at=10,
                                wall_jump_ns=1000 * MS, video_jump_at=10)
    for kind, src in (("data", parquet), ("videos", video)):
        dst = os.path.join(task_dir, kind, "chunk-000",
                           "gripper_rgb" if kind == "videos" else "",
                           os.path.basename(src))
        shutil.copyfile(src, dst)
    code, out = _run([task_dir])
    check(code == 1, f"有真丢帧 ⇒ 退出码 1（rc={code}）")
    check("画面证实丢了 1 段" in out, "合计行数到 1 段")
    check("开录后 0." in out and "启动窗口" in out,
          "报出丢帧落在开录后哪一段（启动窗口是排查方向）")

    code, out = _run([task_dir, "--json"])
    payload = json.loads(out)
    totals = payload["totals"]
    check(code == 1 and totals["lost_episodes"] == 1, "JSON 里 lost_episodes=1")
    check(totals["lost_max_ms"] > 900 and totals["lost_max_episode"] == "011",
          f"最大空洞归属正确（{totals['lost_max_ms']}ms @ {totals['lost_max_episode']}）")
    check(totals["stamp_only_ms"] > 900 and totals["writer_hiccup_ms"] > 250,
          "JSON 也分桶报（钟跳没丢 / 打嗝）")
    check(payload["episodes"][0]["events"][0]["evidence"] in
          ("jump", "continuous", "unknown"), "JSON 带逐事件画面证据")

    # --no-video-check：未复核 ⇒ 一律进「待复核」，且仍变红（未知不等于没事）
    code, out = _run([task_dir, "--no-video-check"])
    check(code == 1, f"未复核也返回 1（rc={code}）——不把未知当没事")
    check("画面证实丢了 0 段" in out and "全部事件都在这一桶" in out,
          "明确标注未复核，不进「证实丢了」")

    # 双钟阈值设 0：差 14ms 的「互证」必须散伙（本库 ep-099 是 4680.8 vs 4666.4）
    parquet, video, _ = box.add("012", count=80, hw_jump_at=10,
                                hw_jump_ns=1000 * MS, wall_jump_at=10,
                                wall_jump_ns=986 * MS, video_static=True)
    result = audit.audit_episode(parquet, video, NOMINAL, 100.0, 0.0001, 15)
    check(events_of(result)[0]["kind"] == "mismatch",
          "容差压到 0 时，差 14ms 的两条不算互证（阈值真的在起作用）")
    result = audit_one(parquet, video)
    check(events_of(result)[0]["kind"] == "both",
          "默认 100ms 容差下同样两条算互证（对照组）")

    # --camera-logs：留档发生在会话结束时，只往后关联；窗外的不挂
    logs = os.path.join(box.root, "camera_service")
    os.makedirs(logs, exist_ok=True)
    wall_abs = events_of(audit_one(parquet, video))[0]["wall_abs"]
    names = {}
    for offset, key in ((120, "in"), (99_999, "out")):
        when = datetime.datetime.fromtimestamp(wall_abs + offset)
        name = (f"{when.strftime('%Y%m%d_%H%M%S')}"
                f"_bus1-port2_camera-service.log")
        with open(os.path.join(logs, name), "w", encoding="utf-8") as handle:
            handle.write(key)
        names[key] = name
    code, out = _run([task_dir, "--camera-logs", logs])
    check(code == 1 and names["in"] in out,
          f"留档按时刻关联到事件行（{names['in']}）")
    check(names["out"] not in out, "10 万秒后的留档不挂（时间窗真的在起作用）")


def check_missing_dir(box):
    print("\n[10] 参数与失败路径")
    code, out = _run([os.path.join(box.root, "nope")])
    check(code == 2 and "任务目录不存在" in out, "目录不存在 ⇒ 2 且说清原因")
    empty = tempfile.mkdtemp(prefix="ksq-audit-empty-", dir=box.root)
    code, out = _run([empty])
    check(code == 2 and "不是任务目录" in out, "缺 data/ ⇒ 2")
    code, out = _run([box.root, "--camera-logs",
                      os.path.join(box.root, "no-logs")])
    check(code == 2 and "留档目录不存在" in out, "留档目录不存在 ⇒ 2")
    shutil.rmtree(empty, ignore_errors=True)


def main():
    box = _Sandbox()
    try:
        check_pure_functions()
        check_healthy(box)
        check_real_loss(box)
        check_stamp_only(box)
        check_static_scene(box)
        check_writer_hiccup(box)
        check_trunc32(box)
        check_hw64_no_unwrap(box)
        check_refusals(box)
        check_cli(box)
        check_missing_dir(box)
    finally:
        box.close()

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        for msg in FAILS:
            print(f"  - {msg}")
        return 1
    print("PASS: 帧空洞审计回归通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
