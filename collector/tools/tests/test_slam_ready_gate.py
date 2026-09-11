#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SLAM 就绪门自检：信息性输出不得锁死 wait_sdk_ready（2026-09-10 定案）。

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_slam_ready_gate.py

背景：2026-09-10 18:32 连续两次「SLAM SDK 初始化超时（未等到 [FAYS-CALIB]
标定标记；确认夹爪 USB3 头插在 USB3 口）」，但报错距启动只有 1 秒、45s 超时
根本没走完，失败消息还夹在 native 的 `-Loaded image info` 与 `-Loaded IMU
calibration` 之间 —— SLAM 进程活得好好的。真因：LD_PRELOAD 指向一个不存在
的 .so，ld.so 打了三条 "... cannot be preloaded ...: ignored."，其中
"cannot" 命中 _ERROR_RE → `state.error` 被置上（该字段粘滞，只在 start() 清）
→ wait_sdk_ready 末尾的 `and not snapshot.error` 返回 False。

覆盖:
  1. ld.so 的 preload 警告被 parse_slam_line 忽略（返回 None），不再当 error
  2. 但 _ERROR_RE 确实匹配该行 —— 证明确实修的是白名单，没放松正则
  3. 真错误行仍判成 error、SetStereoFPS 仍被忽略（白名单没过度放行）
  4. 标定标记已到 + error 粘滞 → wait_sdk_ready 仍返回 True（本次回归）
  5. 标定标记没到（worker 退出）→ 仍返回 False
  6. 进程已死（running=False）→ 仍返回 False
  7. 2026-09-11 futex/UAF 轮新增的桥接仪器文案同样不命中 _ERROR_RE
     （含启动期 "rejected" 措辞），且都落到 log 而非 error
  8. [ORB_MAP_DIAG] 字段集是精确匹配 —— 多一个字段整行被丢弃，这正是堆
     分解必须另起 [ORB_HEAP] 行的原因
退出码 0 = 全部通过。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from core.gripper.slam.process_controller import (          # noqa: E402
    SlamProcessController,
    SlamState,
)
from core.gripper.slam.protocol import (                    # noqa: E402
    _ERROR_RE,
    parse_slam_line,
)

# 现场日志逐字原文（logs/main.log 2026-09-10 18:32:30，三条中的一条）。
LD_SO_LINE = (
    "ERROR: ld.so: object "
    "'/home/stouch/collector/tools/debug/slam_crashcatcher.so' "
    "from LD_PRELOAD cannot be preloaded (cannot open shared object file): "
    "ignored."
)
# 同一句话的另一种括号内容（glibc 按失败原因填括号）。
LD_SO_LINE_ELFCLASS = (
    "ERROR: ld.so: object '/tmp/x.so' from LD_PRELOAD cannot be preloaded "
    "(wrong ELF class: ELFCLASS32): ignored."
)
SET_STEREO_FPS = "[FAYS-CALIB] WARN SetStereoFPS(25) failed"
REAL_ERROR = "2026-09-10 10:35:40.295 [ ERROR] ImuInit(): setup imu failed"

# 2026-09-11 futex/UAF 修复轮新增的桥接文案，逐字取自 fayssense_orb_slam.cc
# 的发射语句（含各自的字段），不是凭记忆写的。改那些格式必须同步改这里。
NEW_BRIDGE_LINES = (
    ("fatal signal 头",
     "[FATAL_SIGNAL] signo=-6 code=-6 fault_addr=0x0"),
    ("native 回溯头",
     "[NATIVE_BACKTRACE] scope=fatal frames=64"),
    ("sigaltstack 被拒",
     "[FATAL_SIGNAL] sigaltstack rejected errno=22"),
    ("sigaction 被拒",
     "[FATAL_SIGNAL] sigaction rejected signo=11 errno=22"),
    ("处理器安装完成",
     "[FATAL_SIGNAL] handlers installed signo=ABRT,SEGV,BUS,ILL,FPE"),
    ("堆分解",
     "[ORB_HEAP] in_use_kb=812340 arena_kb=910000 mmap_kb=2048 "
     "free_kb=97660 kf_created=4333 mp_created=38211"),
    ("MP 清理（含新增 kf_* 字段）",
     "[MP_CLEANUP] passes=1773 candidates=672907 compacted=672907 "
     "queued=7779 duration_ms=12.480 rss_before_kb=812340 "
     "rss_after_kb=812340 trigger_elapsed_s=30.000 reason=high_water "
     "threshold=1048576 mp_queued_before=7779 mp_queued_after=7779 "
     "kf_candidates=40 kf_compacted=40 kf_queued_before=40 kf_queued_after=0"),
)
# 逐字取自桥接 [ORB_MAP_DIAG] 的发射语句：恰好 ORB_MAP_DIAGNOSTIC_SPECS 的
# 10 个字段，顺序也一致。
ORB_MAP_DIAG_OK = (
    "[ORB_MAP_DIAG] state=2 inliers=180 maps=1 active_kf=60 active_mp=980 "
    "local_kf=12 local_mp=300 lm_queue=0 kf_created=4333 mp_created=38211")

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def make_controller() -> SlamProcessController:
    return SlamProcessController(
        SlamState(),
        executable="/bin/true",
        vocabulary="/dev/null",
        settings="/dev/null",
        device_config="/dev/null",
        trajectory="/dev/null",
        work_dir="/tmp",
        runtime_env_factory=lambda: {},
        cpu_roles={
            "fays_left_orb": (0,),
            "fays_right_orb": (1,),
            "fays_background": (2,),
        },
    )


def gate(*, marker: bool, worker_exited: bool, running: bool,
         error=None) -> bool:
    """按生产路径摆好状态后问一次 wait_sdk_ready。"""
    controller = make_controller()
    if marker:
        controller._sdk_ready_event.set()
    if marker or worker_exited:
        controller._sdk_initialization_done.set()
    controller._set_state(running=running, error=error)
    return controller.wait_sdk_ready(timeout=0.05)


def main() -> int:
    print("[1] ld.so preload 警告不再当 error")
    for label, line in (("shared object file", LD_SO_LINE),
                        ("wrong ELF class", LD_SO_LINE_ELFCLASS)):
        event = parse_slam_line(line)
        check(f"parse_slam_line({label}) is None", event is None,
              repr(event))

    print("[2] 但 _ERROR_RE 确实匹配该行（修的是白名单，不是放松正则）")
    check("_ERROR_RE.search(ld.so 行) 非空", _ERROR_RE.search(LD_SO_LINE)
          is not None)

    print("[3] 白名单没过度放行")
    event = parse_slam_line(REAL_ERROR)
    check("真错误行仍判成 error",
          event is not None and event.kind == "error",
          repr(event))
    check("SetStereoFPS 仍被忽略",
          parse_slam_line(SET_STEREO_FPS) is None)

    print("[4] 标定标记已到 + error 粘滞 → 仍返回 True（本次回归）")
    check("wait_sdk_ready 不被 error 锁死",
          gate(marker=True, worker_exited=False, running=True,
               error=LD_SO_LINE) is True)

    print("[5] 标定标记没到 → 仍返回 False")
    check("worker 退出（未打标记）时返回 False",
          gate(marker=False, worker_exited=True, running=True) is False)

    print("[6] 进程已死 → 仍返回 False")
    check("running=False 时返回 False",
          gate(marker=True, worker_exited=False, running=False) is False)

    # 2026-09-11 新增的桥接文案（futex/UAF 修复那一轮的仪器）。同一条坑：
    # 任何一句命中 _ERROR_RE 就会置粘滞 state.error，把就绪门锁死。启动期
    # 的 sigaltstack/sigaction 失败尤其危险 —— 丢掉的只是崩溃回溯能力，
    # 不该连带让 SLAM 起不来，所以文案用 "rejected" 而非 "failed"。
    print("[7] 新增的桥接仪器文案不会命中 _ERROR_RE")
    for label, line in NEW_BRIDGE_LINES:
        check(f"_ERROR_RE 不匹配 {label}", _ERROR_RE.search(line) is None,
              repr(line))
    print("[7b] 且都被解析成 log（而非 error）")
    for label, line in NEW_BRIDGE_LINES:
        event = parse_slam_line(line)
        check(f"{label} 落到 log",
              event is None or event.kind != "error", repr(event))

    # [ORB_MAP_DIAG] 的字段集是精确匹配（多一个字段整行就被丢弃），所以堆分解
    # 只能另起一行。这条断言把该契约钉死：它一旦被改回宽松匹配，下面的行会
    # 解析成功，而我们就再也发现不了「给严格行加字段会静默吞掉整行遥测」。
    print("[8] [ORB_MAP_DIAG] 字段集精确匹配（故 [ORB_HEAP] 必须独立成行）")
    from core.gripper.slam.protocol import (                # noqa: E402
        parse_orb_map_diagnostic_line,
    )
    check("标准 10 字段行可解析",
          parse_orb_map_diagnostic_line(ORB_MAP_DIAG_OK) is not None)
    check("多一个 heap 字段即整行被丢弃",
          parse_orb_map_diagnostic_line(
              ORB_MAP_DIAG_OK + " heap_in_use_kb=1") is None)

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 — {_FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
