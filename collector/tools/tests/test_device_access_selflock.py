#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备 guard 的「自己挡自己」误判 —— 离线自检（不碰真设备/真 SDK/真串口）。

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_device_access_selflock.py

背景：2026-09-24 现场日志里反复出现

    ESP32 … 绑定的 Fays 序列号 3500000262300100 不在当前在线的 Fays 中，
    拒绝按端口或枚举顺序猜测配对：在线 serial=[<无>]；
    另有正在被占用的 Fays 未能探测: [4-1.1(/dev/video0)]

而同一份日志在 20 秒前刚打印过「租约已锁定: stereo=/dev/video0」——占用它的
就是本进程自己。`fays_single._device_in_use` 原先只看 flock「加锁失败」就判定
「别的会话在用」，但 **flock 绑在 open file description 上，不是进程上**：同一
进程再 open 一次同一文件，非阻塞加锁照样 EWOULDBLOCK。于是本机唯一那台 Fays
被自己跳过，「在线 serial」为空，配对失败关闭。

覆盖:
  1. 前提本身：裸 fcntl 下同一进程两个 fd 也互斥（这条 OS 语义是修法的基础）
  2. device_guard_held_by_process 随持有/释放正确翻转
  3. 本进程持有时 _device_in_use **不**判为占用（本次修的 bug）
  4. 同线程嵌套持有：只加深度，不再开 fd（否则同一操作自己挡死自己）
  5. 同进程另一线程持有时：带 timeout 的调用**排队等到**，timeout=0 立刻失败
     （现场形态：读出厂标定与启动身份探测在同一进程里抢同一把设备锁）
  6. 别的进程持有时 _device_in_use 仍判为占用（跨进程独占语义没被削弱）
  7. 节点无效时 _device_in_use 返回 False（留给 SDK 预检报错，行为不变）
  8. fays_device_guard_path 拒绝非 /dev/videoN
  9. UVC 配对失败文案带上逐组拒绝原因（原先只报 matches/groups 计数）

退出码 0 = 全部通过。
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock                                       # noqa: E402

from core.gripper.fays_single import SingleFaysLease            # noqa: E402
from core.gripper.runtime.device_access import (                # noqa: E402
    device_guard_held_by_process,
    device_access_guard,
    fays_device_guard,
    fays_device_guard_path,
)

FAILS = []

# 用 video997 这种不可能存在的节点名：走的是同一条按节点名取锁文件的代码路径，
# 但绝不会和真机上的会话抢同一把锁（测试跑的时候用户可能正在启动夹爪）。
FAKE_PORT = "/dev/video997"
LOCK_PATH = fays_device_guard_path(FAKE_PORT)

HOLDER_SRC = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
sys.stdout.write("HELD\\n")
sys.stdout.flush()
time.sleep(float(sys.argv[2]))
"""


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def test_flock_premise():
    """前提：flock 绑在 open file description 上 —— 同一进程两个 fd 也互斥。"""
    path = f"{LOCK_PATH}.premise"
    fd1 = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    fd2 = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        second_blocked = False
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            second_blocked = True
        check(second_blocked,
              "flock 前提：同一进程第二个 fd 非阻塞加锁被拒（这就是误判的来源）")
        check(not device_guard_held_by_process(path),
              "裸 fcntl 加锁不进登记表（登记表只由 device_access_guard 维护）")
    finally:
        os.close(fd2)
        os.close(fd1)


def test_registry_tracks_hold():
    check(not device_guard_held_by_process(LOCK_PATH),
          "未持有时登记表为 False")
    with fays_device_guard(FAKE_PORT, timeout=0.0):
        check(device_guard_held_by_process(LOCK_PATH),
              "持有时登记表为 True")
        check(SingleFaysLease._device_in_use(
                  {"ports": {"stereo_dev_port": FAKE_PORT}}) is False,
              "本进程自己持有 → 不判为「别的会话占用」（修的就是这条）")
    check(not device_guard_held_by_process(LOCK_PATH),
          "释放后登记表回到 False")


def test_nested_hold_depth():
    with fays_device_guard(FAKE_PORT, timeout=0.0):
        with fays_device_guard(FAKE_PORT, timeout=0.0):
            check(device_guard_held_by_process(LOCK_PATH),
                  "嵌套持有期间仍登记为 True")
        check(device_guard_held_by_process(LOCK_PATH),
              "内层退出没解掉外层的持有（深度计数正确）")
    check(not device_guard_held_by_process(LOCK_PATH),
          "全部退出后登记表清空")


def test_cross_thread_waits():
    """同进程另一线程正在用：带 timeout 的调用排队等到，timeout=0 立刻失败。

    现场形态就是这个：读出厂标定（calibration.py，持锁十几秒）与启动时的身份
    探测（fays_serial_probe.py）在同一个进程里跑，后者必须等，不能失败。
    """
    holding = threading.Event()
    released = threading.Event()

    def holder():
        with fays_device_guard(FAKE_PORT, timeout=0.0):
            holding.set()
            released.wait(timeout=10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    check(holding.wait(timeout=5), "持锁线程已进入 guard")
    check(device_guard_held_by_process(LOCK_PATH),
          "另一线程持有 → 登记表仍为 True（guard 是全进程的）")

    busy_failed = False
    try:
        with fays_device_guard(FAKE_PORT, timeout=0.0):
            pass
    except RuntimeError:
        busy_failed = True
    check(busy_failed, "同进程另一线程持有时 timeout=0 立刻失败（探测语义不变）")

    waited = {"ok": False}

    def waiter():
        with fays_device_guard(FAKE_PORT, timeout=10.0):
            waited["ok"] = True

    thread2 = threading.Thread(target=waiter, daemon=True)
    thread2.start()
    time.sleep(0.3)
    released.set()
    thread.join(timeout=10)
    thread2.join(timeout=10)
    check(waited["ok"], "带 timeout 的调用排到队（等待而不是失败）")
    check(not device_guard_held_by_process(LOCK_PATH), "全部退出后登记表清空")


def test_other_process_still_blocks():
    """别的进程持有时必须仍然判为占用 —— 否则修法等于把独占语义关掉。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER_SRC, LOCK_PATH, "10"],
        stdout=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline().strip()
        check(line == "HELD", "子进程成功持有 guard")
        check(SingleFaysLease._device_in_use(
                  {"ports": {"stereo_dev_port": FAKE_PORT}}) is True,
              "别的进程持有 → 仍判为占用（跨进程独占没被削弱）")
        check(not device_guard_held_by_process(LOCK_PATH),
              "别的进程的持有不进本进程登记表")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_invalid_node():
    check(SingleFaysLease._device_in_use(
              {"ports": {"stereo_dev_port": "/dev/ttyACM0"}}) is False,
          "节点不是 /dev/videoN → 不判占用（留给 SDK 预检报错，行为不变）")
    check(SingleFaysLease._device_in_use({"ports": {}}) is False,
          "缺 stereo 端口 → 不判占用")
    raised = False
    try:
        fays_device_guard_path("/dev/ttyACM0")
    except RuntimeError:
        raised = True
    check(raised, "fays_device_guard_path 拒绝非 /dev/videoN")


def test_pairing_message_carries_reason():
    """配对失败必须把逐组拒绝原因带出来，不能只剩 matches/groups 计数。"""
    from core.gripper.devices.uvc_camera_service import (
        UvcCameraServiceError, UvcCameraServiceManager,
    )

    service = object.__new__(UvcCameraServiceManager)
    reason = "sightac mode=failed error='MJPEG 640x480@30: Invalid mode'"
    group = {"physical_usb_path": "3-1", "valid": False}
    with mock.patch.object(service, "_discovery_scope", return_value=(3, "1")), \
            mock.patch.object(service, "_run_discovery", return_value=[group]), \
            mock.patch.object(service, "_associate_group",
                              side_effect=UvcCameraServiceError(reason)), \
            mock.patch.object(service, "_assignment_identity",
                              return_value=("68:EE:8F:C6:0D:20", "3500000262300100")):
        try:
            service._resolve_current_group({"id": "umi"})
            check(False, "配对不唯一时必须抛错")
        except UvcCameraServiceError as exc:
            text = str(exc)
            check("matches=0" in text and "groups=1" in text,
                  "文案仍保留 matches/groups 计数")
            check(reason in text, "文案带上了逐组拒绝原因（真因不再被吞）")


def main():
    print("[1. flock 前提]")
    test_flock_premise()
    print("\n[2. 登记表随持有/释放翻转]")
    test_registry_tracks_hold()
    print("\n[3. 嵌套持有深度]")
    test_nested_hold_depth()
    print("\n[4. 同进程跨线程排队]")
    test_cross_thread_waits()
    print("\n[5. 跨进程独占仍然生效]")
    test_other_process_still_blocks()
    print("\n[6. 无效节点行为不变]")
    test_invalid_node()
    print("\n[7. 配对失败文案带原因]")
    test_pairing_message_carries_reason()

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败:")
        for item in FAILS:
            print(f"   - {item}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
