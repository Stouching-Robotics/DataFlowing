#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SLAM 诊断证据留存自检。

    venv/bin/python tools/tests/test_slam_evidence_retention.py

背景：SLAM 崩溃排查一度卡在「没有证据」——
  1. 原生日志 slam_stdout.log 只活在 /tmp/ksq-gripper-fays-*/ 里，而
     SingleFaysLease.release() 正常收尾时会 rmtree 整个目录；而
     ORB_STAGE / FAYS-AFFINITY 这类逐秒诊断**只**写这个文件。
  2. 桥接自己的 [FPS_DATA] 逐秒遥测被正确解析成 fays_rates 事件后交给
     on_fays_rates，而 core/ 下没有任何调用方接线（默认空函数）——实测
     main.log 只有 9 条（全是崩溃尾部转储带出来的），原生日志有 580 条。

覆盖:
  1. _archive_native_log 逐字节留档、源文件不动、文件名带序列号与时刻
  2. 没有 slam_stdout.log 时静默返回 None，不抛异常
  3. 留档目录不可用时只记一行日志，绝不打断 release()
  4. _prune_native_log_archive 只留最近 20 份（按文件名时间序）
  5. release() 端到端：留档先于 rmtree，且 runtime_dir 确实被删掉
  6. format_fays_rates 三个基本字段 + 四个阶段耗时；缺 stage_times 不崩
  7. **文案不得命中 protocol._ERROR_RE**（否则会被判成 error 弹到 UI）
  8. [FPS_DATA] 健康态静默、帧率过低才出声（正反两面）
  9. FaysRateAlarm 去抖：进入报一次、持续受限频、恢复报一次
 10. [TIME_DROP]/[TIME_REBASE] 结构化解析，且**绕开未匹配行的 50 条上限**
退出码 0 = 全部通过。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from core.gripper import fays_single                             # noqa: E402
from core.gripper.slam.process_controller import (               # noqa: E402
    FaysRateAlarm, SlamProcessController, fays_rate_problems,
    format_fays_rates, format_frame_time_regression,
)
from core.gripper.slam.protocol import (                         # noqa: E402
    _ERROR_RE, FaysRateSample, FrameTimeRegression, parse_slam_line,
)

FAILS = []
SERIAL = "3500000262300094"
PAYLOAD = b'{"time": 1.0, "line": "[FPS_DATA] image=49.999 imu=1007.8"}\n'
# 这一条取自 2026-09-11 现场：process=22.697 低于 25 的阈值 → 属于异常样本
SAMPLE_LINE = ("[FPS_DATA] image=49.999 imu=1007.800 process=22.697 "
               "wait_ms=34.29 preprocess_ms=44.28 middle_ms=9.74 post_ms=0.75")
# 这一条取自同一会话的健康稳态（实测 image 48.47~50.41 / process 28.31~31.97）
HEALTHY_LINE = ("[FPS_DATA] image=50.004 imu=1000.043 process=30.728 "
                "wait_ms=21.50 preprocess_ms=22.31 middle_ms=11.12 post_ms=0.60")
DROP_LINE = ("[TIME_DROP] ts=15250.4 previous=15250.5 delta=-0.1 "
             "seq=7901 prev_seq=7903 consecutive=1 total=1")
REBASE_LINE = ("[TIME_REBASE] ts=15255.41 previous=15255.43 delta=-0.02 "
               "seq=8149 prev_seq=8103 consecutive=30 total=30")
# 老桥接（2026-09-11 14:2x 之前的二进制）没有 prev_seq 字段，仍须解析得出来
OLD_DROP_LINE = ("[TIME_DROP] ts=15250.4 previous=15250.5 delta=-0.1 "
                 "seq=7901 consecutive=1 total=1")
DUP_LINE = ("[TIME_DROP] ts=15250.4 previous=15250.5 delta=-0.1 "
            "seq=7901 prev_seq=7901 consecutive=1 total=1")
STALE_TS_LINE = ("[TIME_DROP] ts=15250.4 previous=15250.5 delta=-0.1 "
                 "seq=7901 prev_seq=7898 consecutive=1 total=1")


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


class _Sandbox:
    """把留档目录重定向到临时目录（settings.LOGS_DIR 是模块级读取的）。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ksq-evidence-test-")
        self._saved = fays_single.settings
        fays_single.settings = SimpleNamespace(
            LOGS_DIR=os.path.join(self.root, "logs"))

    def archive_dir(self):
        return fays_single._native_log_archive_dir()

    def make_runtime_dir(self, payload=PAYLOAD, name="slam_stdout.log"):
        runtime_dir = tempfile.mkdtemp(prefix="ksq-runtime-", dir=self.root)
        if payload is not None:
            with open(os.path.join(runtime_dir, name), "wb") as handle:
                handle.write(payload)
        return runtime_dir

    def close(self):
        fays_single.settings = self._saved
        shutil.rmtree(self.root, ignore_errors=True)


def check_archive_basic(box):
    print("\n[1] 留档基本行为")
    runtime_dir = box.make_runtime_dir()
    source = os.path.join(runtime_dir, "slam_stdout.log")

    target = fays_single._archive_native_log(runtime_dir, SERIAL)
    check(target is not None and os.path.isfile(target),
          f"留档文件已生成（{os.path.basename(target or 'None')}）")
    check(os.path.isfile(source), "源文件仍留在 runtime_dir（先拷后删）")
    with open(target, "rb") as handle:
        check(handle.read() == PAYLOAD, "留档内容与源逐字节一致")
    check(SERIAL in os.path.basename(target), "留档文件名带序列号")
    check(os.path.dirname(target) == box.archive_dir(),
          "留档落在 logs/slam_native/（与 main.log 同级、独立子目录）")
    check(len(os.path.basename(target)) > len("_slam_stdout.log") + 8,
          "留档文件名带时刻前缀（同名不同次会话不会互相覆盖）")
    shutil.rmtree(runtime_dir, ignore_errors=True)


def check_missing_source(box):
    print("\n[2] 没有原生日志时不报错")
    runtime_dir = box.make_runtime_dir(payload=None)
    result = fays_single._archive_native_log(runtime_dir, SERIAL)
    check(result is None, "返回 None（静默跳过，不抛异常）")
    shutil.rmtree(runtime_dir, ignore_errors=True)

    only_other = box.make_runtime_dir(payload=b"x", name="orb_trajectory.txt")
    result = fays_single._archive_native_log(only_other, SERIAL)
    check(result is None, "目录里有别的文件但没有 slam_stdout.log → 仍返回 None")
    shutil.rmtree(only_other, ignore_errors=True)


def check_archive_unwritable(box):
    print("\n[3] 留档目录不可用时不打断清理")
    blocker = os.path.join(box.root, "logs", "slam_native")
    os.makedirs(os.path.dirname(blocker), exist_ok=True)
    shutil.rmtree(blocker, ignore_errors=True)            # 前面几项可能已建出目录
    with open(blocker, "w", encoding="utf-8") as handle:  # 目录位置被一个文件占住
        handle.write("not a directory")

    runtime_dir = box.make_runtime_dir()
    logged = []
    result = fays_single._archive_native_log(runtime_dir, SERIAL, logged.append)
    check(result is None, "无法建目录时返回 None（不抛异常）")
    check(logged and "留档失败" in logged[0],
          f"把失败记进日志（{logged[0] if logged else '没记'}）")

    os.unlink(blocker)
    shutil.rmtree(runtime_dir, ignore_errors=True)


def check_prune(box):
    print("\n[4] 只保留最近 20 份")
    directory = box.archive_dir()
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory, exist_ok=True)
    # 造 25 份，文件名前缀是 %Y%m%d_%H%M%S（字典序即时间序）
    names = [f"20260901_0000{index:02d}_{SERIAL}_slam_stdout.log"
             for index in range(25)]
    for name in names:
        with open(os.path.join(directory, name), "w", encoding="utf-8") as h:
            h.write(name)

    fays_single._prune_native_log_archive()
    remaining = sorted(os.listdir(directory))
    check(len(remaining) == fays_single._NATIVE_LOG_ARCHIVE_KEEP,
          f"25 份被裁到 {fays_single._NATIVE_LOG_ARCHIVE_KEEP} 份"
          f"（实际 {len(remaining)}）")
    check(remaining == sorted(names)[-fays_single._NATIVE_LOG_ARCHIVE_KEEP:],
          "被删的是最旧的 5 份，保留的是最新的 20 份")
    shutil.rmtree(directory, ignore_errors=True)


def _archived(box):
    """留档目录里现有的 slam_stdout.log 文件名（目录不存在时返回空）。"""
    directory = box.archive_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(name for name in os.listdir(directory)
                  if name.endswith("slam_stdout.log"))


def check_release_end_to_end(box):
    print("\n[5] release() 端到端：先留档、再 rmtree")
    lease = fays_single.SingleFaysLease(logger=lambda _line: None)
    runtime_dir = box.make_runtime_dir()
    lease._runtime_dir = runtime_dir
    lease._selected = {"product_serial": SERIAL}

    lease.release()

    check(not os.path.exists(runtime_dir), "runtime_dir 已被 rmtree")
    archived = _archived(box)
    check(len(archived) == 1, f"原生日志被留档（{archived}）")
    if archived:
        path = os.path.join(box.archive_dir(), archived[0])
        with open(path, "rb") as handle:
            check(handle.read() == PAYLOAD, "留档内容完整（删目录前拷贝）")

    # 幂等：再 release 一次不该炸、也不该多留一份
    lease.release()
    check(len(_archived(box)) == len(archived), "release() 幂等，不重复留档")


def check_format(box):
    print("\n[6] format_fays_rates 字段与安全文案")
    sample = parse_slam_line(SAMPLE_LINE).payload
    check(isinstance(sample, FaysRateSample),
          "[FPS_DATA] 被解析成 fays_rates 事件（不会被当成 unmatched 行）")

    problems = fays_rate_problems(sample)
    check(problems == ["处理帧率低 process=22.7<25"],
          f"process=22.7 被判为过低（实际 {problems}）")
    check(fays_rate_problems(parse_slam_line(HEALTHY_LINE).payload) == [],
          "健康样本没有任何问题项")

    line = format_fays_rates(sample, problems)
    for token in ("image=50.0", "imu=1007.8", "process=22.7",
                  "wait=34.3", "preprocess=44.3", "middle=9.7", "post=0.8"):
        check(token in line, f"含 {token}")
    print(f"        → {line}")

    bare = FaysRateSample(49.9, 1007.0, 30.0)
    line_bare = format_fays_rates(bare, ["图像帧率低 image=49.9<45"])
    check("image=49.9" in line_bare and "wait=" not in line_bare,
          "缺 stage_times 时只出三个基本字段，不崩")
    check(line_bare.startswith("[SLAM] "), "带 [SLAM] 前缀（与 GUI 日志其余行一致）")

    # 恢复行的文案也要一起把关
    _alarm = FaysRateAlarm()
    _alarm.update(sample, 0.0)
    _recovery = _alarm.update(parse_slam_line(HEALTHY_LINE).payload, 1.0)

    print("\n[7] 文案安全性（被误判成 error 会锁死就绪门）")
    for label, text in (
        ("FPS 异常行（带阶段耗时）", line),
        ("FPS 异常行（不带阶段耗时）", line_bare),
        ("FPS 恢复行", _recovery[0] if _recovery else ""),
        ("TIME_DROP 行", format_frame_time_regression(
            parse_slam_line(DROP_LINE).payload)),
        ("TIME_DROP 行（重复投递）", format_frame_time_regression(
            parse_slam_line(DUP_LINE).payload)),
        ("TIME_DROP 行（帧配旧戳）", format_frame_time_regression(
            parse_slam_line(STALE_TS_LINE).payload)),
        ("TIME_REBASE 行", format_frame_time_regression(
            parse_slam_line(REBASE_LINE).payload)),
    ):
        check(_ERROR_RE.search(text) is None,
              f"{label}的文案不命中 _ERROR_RE")


def check_wiring(box):
    print("\n[8] fays_rates 只在异常时落到 GUI 日志")
    # 只测 _handle_event 的这条分支，绕开需要设备的构造函数
    controller = SlamProcessController.__new__(SlamProcessController)
    seen_rates, seen_logs = [], []
    controller._on_fays_rates = seen_rates.append
    controller._log = seen_logs.append
    controller._fays_rates_alarm = FaysRateAlarm()
    controller._clock = lambda: 0.0

    # 健康态：回调照旧、GUI 日志静默 —— 这就是「别塞满日志」的修复本体
    controller._handle_event(parse_slam_line(HEALTHY_LINE), {}, None, None)
    check(len(seen_rates) == 1, "回调仍然被调用（旧契约不变）")
    check(seen_logs == [], f"健康态不写日志（实际 {seen_logs}）")

    # 异常态：出声，且点明低在哪
    controller._handle_event(parse_slam_line(SAMPLE_LINE), {}, None, None)
    check(len(seen_logs) == 1, "帧率过低时写一行")
    check(seen_logs and "处理帧率低" in seen_logs[0],
          f"行里点明低在哪（{seen_logs[0] if seen_logs else '空'}）")

    # 反向验证：注释里声明「不打 GUI 日志」的两类仍必须被吞掉
    for kind in ("orb_stage", "affinity"):
        before = len(seen_logs)
        controller._handle_event(
            parse_slam_line(f"{'[ORB_STAGE]' if kind == 'orb_stage' else '[FAYS-AFFINITY]'} x=y"),
            {}, None, None,
        )
        check(len(seen_logs) == before, f"{kind} 仍然只进原生日志（不刷屏）")


def check_alarm(box):
    print("\n[9] FaysRateAlarm 去抖：进入 / 限频 / 恢复")
    healthy = parse_slam_line(HEALTHY_LINE).payload
    bad = parse_slam_line(SAMPLE_LINE).payload
    alarm = FaysRateAlarm(repeat_s=30.0)

    check(alarm.update(healthy, 0.0) == [], "健康态静默")
    check(len(alarm.update(bad, 1.0)) == 1, "进入异常报一次")
    check(alarm.update(bad, 2.0) == [], "紧接着的异常样本被限频吞掉")
    check(alarm.update(bad, 30.9) == [], "未到重复间隔仍然静默")
    check(len(alarm.update(bad, 31.5)) == 1,
          "超过重复间隔再报一次（长时间故障不刷屏、也不至于静默）")
    back = alarm.update(healthy, 32.0)
    check(len(back) == 1 and "已恢复" in back[0],
          f"恢复正常报一行收尾（{back[0] if back else '空'}）")
    check(alarm.update(healthy, 33.0) == [], "恢复之后继续静默")

    alarm.reset()
    check(alarm.update(healthy, 34.0) == []
          and len(alarm.update(bad, 35.0)) == 1,
          "reset() 后重新按「进入」处理（新会话不复用上一段的告警状态）")


def check_time_regression(box):
    print("\n[10] [TIME_DROP]/[TIME_REBASE] 结构化解析")
    drop = parse_slam_line(DROP_LINE)
    check(drop.kind == "time_drop"
          and isinstance(drop.payload, FrameTimeRegression),
          "[TIME_DROP] 被解析成结构化事件（不再是 unmatched 行）")
    check((drop.payload.verdict, drop.payload.seq, drop.payload.consecutive,
           drop.payload.total) == ("drop", 7901, 1, 1),
          "字段逐个对上（verdict/seq/consecutive/total）")
    check(drop.payload.prev_seq == 7903,
          "prev_seq 解析出来（上一帧已入库的 SDK 序号）")
    check(abs(drop.payload.delta + 0.1) < 1e-9, "delta 保留符号（回退必为负）")
    check(parse_slam_line(REBASE_LINE).kind == "time_rebase",
          "[TIME_REBASE] 是另一个 kind（时钟真跳变要与脏读分开）")

    old = parse_slam_line(OLD_DROP_LINE)
    check(old is not None and old.payload.prev_seq is None,
          "老桥接格式（无 prev_seq）仍然解析得出来，prev_seq 记 None")

    line = format_frame_time_regression(drop.payload)
    for token in ("[TIME_DROP]", "ts=15250.400", "delta=-0.100s",
                  "seq=7901", "prev_seq=7903", "total=1"):
        check(token in line, f"含 {token}")
    print(f"        → {line}")
    check("落后 2 帧" in line, "seq < prev_seq 判成投递乱序并给出落后帧数")
    check("prev_seq" not in format_frame_time_regression(old.payload),
          "没有 prev_seq 时不硬编一个出来（老日志照旧显示）")

    dup = format_frame_time_regression(parse_slam_line(DUP_LINE).payload)
    check("重复投递" in dup, f"seq == prev_seq 判成重复投递（{dup}")
    stale = format_frame_time_regression(
        parse_slam_line(STALE_TS_LINE).payload)
    check("旧时间戳" in stale, f"seq > prev_seq 判成时间戳字段错（{stale}")

    rebase_line = format_frame_time_regression(
        parse_slam_line(REBASE_LINE).payload)
    check("[TIME_REBASE]" in rebase_line and "换基准" in rebase_line,
          "TIME_REBASE 文案一眼可与 DROP 区分")

    print("\n[11] 绕开未匹配行的 50 条上限（它此前看不见的直接原因）")
    controller = SlamProcessController.__new__(SlamProcessController)
    seen_logs = []
    controller._log = seen_logs.append
    controller._unmatched_lines = 0
    for _ in range(60):                       # 先把预算耗光
        controller._handle_event(
            parse_slam_line("[STAT] frames=100 fps=30.0"), {}, None, None)
    check(len(seen_logs) == 51,
          f"预算已耗尽（{len(seen_logs)} 行 = 50 条 + 1 条抑制提示）")

    seen_logs.clear()
    controller._handle_event(parse_slam_line(DROP_LINE), {}, None, None)
    check(len(seen_logs) == 1 and "TIME_DROP" in seen_logs[0],
          "预算耗尽后 [TIME_DROP] 仍然打得出来（修复前这里必然为空）")


def main():
    box = _Sandbox()
    try:
        check_archive_basic(box)
        check_missing_source(box)
        check_archive_unwritable(box)
        check_prune(box)
        check_release_end_to_end(box)
        check_format(box)
        check_wiring(box)
        check_alarm(box)
        check_time_regression(box)
    finally:
        box.close()

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        for msg in FAILS:
            print(f"  - {msg}")
        return 1
    print("PASS: SLAM 诊断证据留存回归通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
