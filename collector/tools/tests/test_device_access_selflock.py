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

第 9 条以外，以下三条钉的是提交 00612cb1「UVC 发现结果不再丢弃」的三处改动
（改前它们**零测试覆盖**：唯一触边的第 9 条用它自己的 fixture 键 `physical_usb_path`
—— 那键在 UVC 报告里根本不存在 —— 且不打标签，所以标签改错了它照样 PASS）：
  10. 逐组拒绝原因的标签取报告真键 `outer_path`（键名写错就退化成 group#N；
      报告里真缺这个键时才回退 group#N）
  11. Flash 身份失败报错带 `sightac[i]` + side/flash_ok/device_type/serial/error
      五项（否则三种不同故障报同一句话），且**「side 空/无效」与「左右重复」两条
      分支的抬头不同**（前者会被误指成「拓扑重复」，真因却是厂商没写 side）
  12. 发现程序 rc==0 时 stderr 也逐行进 `[UVC-DISCOVER]` 日志（rc!=0 的既有行为
      不变：stderr 尾部进异常文案、不重复转发）

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


def fake_manager(**attrs):
    """object.__new__ 造的假 manager：只装本次要用的属性（不起设备/子进程）。

    `_resolve_current_group` / `_associate_group` / `_run_discovery` 都是拿
    self 当命名空间用的纯逻辑，构造真 manager 会去碰设备，所以照旧绕过。
    """
    from core.gripper.devices.uvc_camera_service import UvcCameraServiceManager

    service = object.__new__(UvcCameraServiceManager)
    for key, value in attrs.items():
        setattr(service, key, value)
    return service


def test_pairing_message_carries_reason():
    """配对失败必须把逐组拒绝原因带出来，不能只剩 matches/groups 计数。

    组里用的是报告里**真有的** `outer_path`（C 侧 `discover_uvc_config.c:1239`
    写出 `"bus":N,"outer_path":...`）—— 这个 fixture 原先写的是
    `physical_usb_path`，那个键在报告里根本不存在，于是标签一直退化成
    `group#0`，把「标签对不对」这件事测成了假绿（见 test_rejection_label_...）。
    """
    from core.gripper.devices.uvc_camera_service import UvcCameraServiceError

    service = fake_manager()
    reason = "sightac mode=failed error='MJPEG 640x480@30: Invalid mode'"
    group = {"outer_path": "1.2", "valid": False}
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
            check("1.2: " in text,
                  "拒绝原因的标签是报告里真有的 outer_path（不是 group#N）")
            check("group#0" not in text,
                  "标签没有退化成 group#0（键名写错就会退化，见下一条测试）")


def test_rejection_label_uses_outer_path():
    """改动③：逐组拒绝原因的标签必须是报告里真有的键。

    真键由 C 侧定：`discover_uvc_config.c:1239` 写的是
    `{"bus":N,"outer_path":...}`；`physical_usb_path` 在整个 C 源码里
    一次都没出现（那是 ESP32 清单 side 的键，不是 UVC 报告的）。
    标签写错键名 → 真实失败下每一组都退化成 `group#0`/`group#1`，
    用户看不出是哪条 USB 路径出的问题。
    """
    from core.gripper.devices.uvc_camera_service import UvcCameraServiceError

    def rejections_for(groups):
        service = fake_manager()
        with mock.patch.object(service, "_discovery_scope",
                              return_value=(3, "1")), \
                mock.patch.object(service, "_run_discovery",
                                  return_value=groups), \
                mock.patch.object(service, "_associate_group",
                                  side_effect=UvcCameraServiceError("拒绝原因")), \
                mock.patch.object(service, "_assignment_identity",
                                  return_value=("ESP-SN", "FAYS-SN")):
            try:
                service._resolve_current_group({"id": "umi"})
            except UvcCameraServiceError as exc:
                return str(exc)
        return ""

    # 两把键都在、且值不同：标签必须取 outer_path（拿错键就会取到 3-1）——
    # 这是本条的判据，只断言「有标签」会两种实现都通过。
    text = rejections_for([{"outer_path": "1.2", "physical_usb_path": "3-1",
                            "valid": False}])
    check("1.2: 拒绝原因" in text,
          "标签取 outer_path（报告真键）")
    check("3-1: 拒绝原因" not in text,
          "标签没有误取 physical_usb_path（那个键在 UVC 报告里不存在）")

    # 多组时逐组一条，下标与标签一一对应（别把两条压成同一条）。
    text = rejections_for([{"outer_path": "1.2"}, {"outer_path": "2.3"}])
    check("1.2: 拒绝原因" in text and "2.3: 拒绝原因" in text,
          "逐组各记一条拒绝原因，标签各不相同")

    # 报告里真的没有 outer_path 时按 group#N 回退（不是静默变空）——
    # 这是有意的兜底：C 侧若某天不再写这个键，日志里仍能定位到第几组。
    text = rejections_for([{"bus": 3}])
    check("group#0: 拒绝原因" in text,
          "报告里没有 outer_path 时回退到 group#N（不静默变空）")


def test_pairing_uniqueness_message_carries_identity():
    """改动②：「Flash 身份」失败必须带上 sightac[i] 的元素身份，且**两种故障分开报**。

    不加身份字段时，「Flash 读不出 / 读出来是空串 / device_type 不对」
    三种完全不同的故障报的是**同一句话**（`side=''`），现场又要从头猜。
    字段名都是 C 侧 `write_json_candidate` 真写出来的键
    （discover_uvc_config.c:1092 起：side / flash_ok / device_type /
    flash_serial / error）。

    另一半是**抬头本身**：同一个抬头 `身份不唯一` 同时盖住①这台没报出左右
    （side 空/无效）与②两台报了同一个左右 —— ①的真因是 Flash 没写 side
    （只能厂商补写，仓库无写路径），照"左右重复"去查拓扑是白转一圈。所以两条
    分支的抬头必须不同。
    """
    from core.gripper.devices.uvc_camera_service import UvcCameraServiceError

    def message_for(sightac):
        service = fake_manager()
        group = {"bus": 3, "outer_path": "1", "valid": True,
                 "sightac": sightac,
                 "decxin": {"mode_ok": True, "port": "1.3"}}
        live_esp = [{"serial": "ESP-SN", "physical_usb_path": "3-1.2"}]
        with mock.patch.object(service, "_assignment_identity",
                               return_value=("ESP-SN", "FAYS-SN")):
            try:
                service._associate_group(group, {"id": "umi"}, live_esp)
            except UvcCameraServiceError as exc:
                return str(exc)
        return ""

    # 真实的 field failure：side 读出来是空串，JSON 里有 error 说明为什么。
    bad = {"side": "", "flash_ok": False, "device_type": "",
           "flash_serial": "",
           "error": "LIBUSB_ERROR_TIMEOUT reading Flash side"}
    text = message_for([{"side": "left", "flash_ok": True,
                         "device_type": "sightac", "flash_serial": "L-SN"},
                        bad])
    check("sightac[1]" in text, "带上出问题的那个下标（sightac[1]，不是笼统的组）")
    check("side=''" in text, "带上 side 原文")
    check("flash_ok=False" in text, "带上 flash_ok（区分「Flash 读失败」）")
    check("device_type=''" in text, "带上 device_type（区分「类型不对」）")
    check("serial=''" in text, "带上 flash_serial（区分「读出来是空串」）")
    check("LIBUSB_ERROR_TIMEOUT" in text,
          "带上 error 原文（真因不再被压成同一句话）")
    check("身份不唯一" not in text,
          "side 空/无效报的是「没报出有效左右」，**不是**「身份不唯一」"
          "（照「重复」去查拓扑会把方向指错）")

    # 左右重了：报的是**第二个**（i=1）那个，不是第一个；抬头是「不唯一」。
    text = message_for([{"side": "left", "flash_ok": True},
                        {"side": "left", "flash_ok": True}])
    check("sightac[1]" in text and "side='left'" in text,
          "左右重复时报出重复的那个下标与值（sightac[1] side='left'）")
    check("身份不唯一" in text,
          "真的左右重复时仍然报「身份不唯一」（两分支抬头不同）")


def test_discover_stderr_kept_on_success():
    """改动①：rc==0 时 stderr 也要进 [UVC-DISCOVER] 日志。

    发现程序（core/gripper/camera_service_src/tools/discover_uvc_config.c）
    把每台相机的现场打到 stderr 的 [DISCOVERY] 行。JSON 里虽然有 error
    字段，但并非全都在：`mode=unknown` 与 `mode=failed` 在 JSON 里都被压成
    `mode_ok:false`，flash/calibration 的 ok|failed|skipped 三态也只剩布尔
    —— 而 rc==0 的「配对失败」现场恰恰就是这些差别。过去只在 rc!=0 时读
    stderr，rc=0 的那次等于整份丢掉，只剩一句 side=''。
    """
    import json as _json
    from pathlib import Path

    from core.gripper.devices.uvc_camera_service import (
        UvcCameraServiceError, UvcCameraServiceManager,
    )

    reports = _json.dumps({"groups": [{"bus": 3, "outer_path": "1",
                                       "sightac": [{}, {}], "decxin": {},
                                       "valid": True}]})
    lines = [
        "[DISCOVERY] Sightac vidpid=2207:0019 bus=3 port=1.2 "
        "usb_serial=<none> control_if=0 stream_if=1 alt=7 "
        "descriptor_payload=327680 negotiated_payload=241664 mode=unknown "
        "flash=failed calibration=failed side=<none> serial=<none> "
        "type=<none> error=XU6 prepare failed",
        "[DISCOVERY] complete group(s)=1",
    ]

    def run_with(returncode, stdout, stderr):
        logs = []
        service = fake_manager(_logger=logs.append,
                               _discovery_binary=Path("/bin/true"))
        completed = subprocess.CompletedProcess(
            ["/bin/true"], returncode, stdout=stdout, stderr=stderr)
        with mock.patch.object(subprocess, "run", return_value=completed):
            try:
                result, error = service._run_discovery(), None
            except UvcCameraServiceError as exc:
                result, error = None, str(exc)
        return logs, result, error

    logs, result, error = run_with(0, reports, "\n".join(lines) + "\n")
    check(error is None and result is not None,
          "rc==0 仍正常解析出 groups（改动没干扰成功路径）")
    check(logs == ["[UVC-DISCOVER] " + line for line in lines],
          f"rc==0 的 stderr 逐行进日志（{len(logs)} 行；改动前这里一行都没有）")

    logs, _, _ = run_with(0, reports, "")
    check(logs == [], "stderr 为空时不打空行（不制造噪声）")

    # rc!=0 的既有行为不变：stderr 尾部进异常文案（不是进日志），
    # 且异常发生在转发之前 —— 别把失败路径的 stderr 打两遍。
    many = [f"[DISCOVERY] line {i}" for i in range(30)]
    logs, _, error = run_with(1, "", "\n".join(many))
    check(error is not None and "rc=1" in error,
          "rc!=0 仍抛错并带上 rc")
    check("line 29" in error and "line 0" not in error,
          "rc!=0 只带 stderr 尾部 20 行（既有行为不变）")
    check(logs == [], "rc!=0 时不重复转发（stderr 已进异常文案）")

    # 防御：_logger 是实例属性（构造时注入），不是全局 print。
    check(UvcCameraServiceManager.__init__.__code__.co_varnames[:6]
          and "logger" in UvcCameraServiceManager.__init__.__code__
          .co_varnames[:6], "_logger 由构造参数注入（真机上打给 SLAM/设备面板）")


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
    print("\n[8. 逐组拒绝原因的标签用真键 outer_path（改动③）]")
    test_rejection_label_uses_outer_path()
    print("\n[9. 「Flash 身份不唯一」带 sightac[i] 身份字段（改动②）]")
    test_pairing_uniqueness_message_carries_identity()
    print("\n[10. rc==0 也留发现程序 stderr（改动①）]")
    test_discover_stderr_kept_on_success()

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
