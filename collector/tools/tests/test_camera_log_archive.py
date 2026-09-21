#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""camera-service 原生日志留档 + 告警摘录自检。

    venv/bin/python tools/tests/test_camera_log_archive.py

背景：2026-09-18 排查 episode-099 那 4.68 秒视频空洞时，客户端侧能看到的
只有「read() 失败」；相机侧的停摆 / 降档 / USB 复位**只**写 camera-service
自己的日志，而它活在 runtime_dir 里、随 _stop_entry_locked 的 rmtree 一起
消失——一次干净退出的会话（正是最该留证据的那种）日志等于没写过。

覆盖:
  1. _archive_camera_log 逐字节留档、源文件不动、文件名带 tag 与时刻
  2. 没有 camera-service.log 时静默返回 None，不抛异常
  3. 留档目录不可用时只记一行日志，绝不打断清理
  4. _prune_camera_log_archive 只留最近 20 份（按文件名时间序）
  5. 摘录只认告警行；**健康的 5 秒 stats 行与退出汇总行不得命中**
  6. 摘录取末尾（一次会话里最要紧的是结束前发生了什么）并限量
  7. _stop_entry_locked 端到端：先留档再 rmtree、摘录与路径按序进日志、
     健康会话不占 main.log
  8. _service_tag 身份拼接与降级；恶意 tag 不能逃出留档目录
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

from core.gripper.devices import uvc_camera_service as uvc        # noqa: E402

FAILS = []

# 一次**健康**会话的完整日志（取自 camera_service.c 里的实际格式）：
# 每 5 秒一行的 stats、开流协商行、libuvc 的档位权威行、IPC 行、退出汇总行
HEALTHY_LOG = "\n".join([
    "[unit_1_left] IPC socket=/tmp/x/unit_1_left.sock format=MJPEG "
    "size=640x480 fps=30",
    "[unit_1_left] vidpid=0c45:636f bus=1 port=2.2 interface=1 alt=3 "
    "payload=800 format=MJPEG size=640x480 fps=30 socket=/tmp/x.sock "
    "negotiated_payload=800",
    "[LIBUVC-QUIRK] VID:PID 0c45:636f alt=3 payload=800 endpoint=800 "
    "frame=640x480 interval=333333 xfers=4 xfer_bytes=3200",
    "[unit_1_left] fps_in=30.00 fps_out=29.98 received=1500 output=1490 "
    "dropped=10 (+0) bad_jpeg=0 (+0) bad_replaced=0 write_errors=0 queue=2",
    "[unit_1_left] fps_in=30.00 fps_out=30.00 received=1800 output=1795 "
    "dropped=10 (+0) bad_jpeg=0 (+0) bad_replaced=0 write_errors=0 queue=1",
    "[unit_1_left] received=1800 output=1795 dropped=10 bad_jpeg=0 "
    "bad_replaced=0 write_errors=0 retries=0 stalls=0 usb_resets=0",
]) + "\n"

# 行号即「出事」的顺序，断言按原文引用，免得改文案时测试跟着漂
EVIDENCE_LINES = [
    "[unit_1_left] stream stalled: no frame for 3.0s (received=900); "
    "restarting UVC stream",
    "[unit_1_left] 2 stalls at alt=7; dropping to alt=6 payload=944",
    "[unit_1_left] remembered altsetting=6 payload=944 for this "
    "camera/controller in /home/u/.ksq/x.mode",
    "[unit_1_left] usb reset: rc=0 (Success)",
    "[unit_1_left] device 1bcf:2d4f bus=1 port=2.3 unavailable: "
    "LIBUSB_ERROR_NO_DEVICE",
    "[LIBUVC-QUIRK] VID:PID 1bcf:2d4f alt=6 payload=944 unavailable "
    "endpoint=512",
    "[unit_1_left] dropped incomplete MJPEG sequence=42 bytes=1234 bad=1",
    "[unit_1_left] uvc_start_streaming failed: LIBUSB_ERROR_BUSY",
]
TROUBLE_LOG = HEALTHY_LOG + "\n".join(EVIDENCE_LINES) + "\n"


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


class _Sandbox:
    """把留档目录重定向到临时目录（settings.LOGS_DIR 是调用时读取的）。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ksq-camera-log-test-")
        self._saved = uvc.settings
        uvc.settings = SimpleNamespace(
            LOGS_DIR=os.path.join(self.root, "logs"))

    def archive_dir(self):
        return uvc._camera_log_archive_dir()

    def make_runtime_dir(self, payload=HEALTHY_LOG, name=uvc._CAMERA_LOG_NAME):
        runtime_dir = tempfile.mkdtemp(prefix="ksq-runtime-", dir=self.root)
        if payload is not None:
            with open(os.path.join(runtime_dir, name), "w",
                      encoding="utf-8") as handle:
                handle.write(payload)
        return runtime_dir

    def close(self):
        uvc.settings = self._saved
        shutil.rmtree(self.root, ignore_errors=True)


def _archived(box):
    directory = box.archive_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(name for name in os.listdir(directory)
                  if name.endswith(uvc._CAMERA_LOG_NAME))


def check_archive_basic(box):
    print("\n[1] 留档基本行为")
    runtime_dir = box.make_runtime_dir()
    source = os.path.join(runtime_dir, uvc._CAMERA_LOG_NAME)

    target = uvc._archive_camera_log(runtime_dir, "bus1-port2.2")
    check(target is not None and os.path.isfile(target),
          f"留档文件已生成（{os.path.basename(target or 'None')}）")
    check(os.path.isfile(source), "源文件仍留在 runtime_dir（先拷后删）")
    with open(target, "r", encoding="utf-8") as handle:
        check(handle.read() == HEALTHY_LOG, "留档内容与源逐字节一致")
    check("bus1-port2.2" in os.path.basename(target), "留档文件名带 tag")
    check(os.path.dirname(target) == box.archive_dir(),
          "留档落在 logs/camera_service/（与 main.log 同级、独立子目录）")
    check(len(os.path.basename(target)) > len("_camera-service.log") + 8,
          "留档文件名带时刻前缀（同名不同次会话不会互相覆盖）")
    shutil.rmtree(runtime_dir, ignore_errors=True)


def check_missing_source(box):
    print("\n[2] 没有 camera-service.log 时不报错")
    runtime_dir = box.make_runtime_dir(payload=None)
    logged = []
    result = uvc._archive_camera_log(runtime_dir, "bus1-port2.2", logged.append)
    check(result is None, "返回 None（静默跳过，不抛异常）")
    check(logged == [], "连日志都不记（这不是异常，只是没证据可留）")
    shutil.rmtree(runtime_dir, ignore_errors=True)

    only_other = box.make_runtime_dir(payload="x", name="other.log")
    check(uvc._archive_camera_log(only_other, "t") is None,
          "目录里有别的文件但没有 camera-service.log → 仍返回 None")
    shutil.rmtree(only_other, ignore_errors=True)


def check_archive_unwritable(box):
    print("\n[3] 留档目录不可用时不打断清理")
    blocker = os.path.join(box.root, "logs", "camera_service")
    os.makedirs(os.path.dirname(blocker), exist_ok=True)
    shutil.rmtree(blocker, ignore_errors=True)            # 前面几项可能已建出目录
    with open(blocker, "w", encoding="utf-8") as handle:  # 目录位置被一个文件占住
        handle.write("not a directory")

    runtime_dir = box.make_runtime_dir()
    logged = []
    result = uvc._archive_camera_log(runtime_dir, "bus1-port2.2", logged.append)
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
    names = [f"20260901_0000{index:02d}_bus1-port2.2_camera-service.log"
             for index in range(25)]
    for name in names:
        with open(os.path.join(directory, name), "w", encoding="utf-8") as h:
            h.write(name)

    uvc._prune_camera_log_archive()
    remaining = sorted(os.listdir(directory))
    check(len(remaining) == uvc._CAMERA_LOG_ARCHIVE_KEEP,
          f"25 份被裁到 {uvc._CAMERA_LOG_ARCHIVE_KEEP} 份"
          f"（实际 {len(remaining)}）")
    check(remaining == sorted(names)[-uvc._CAMERA_LOG_ARCHIVE_KEEP:],
          "被删的是最旧的 5 份，保留的是最新的 20 份")
    shutil.rmtree(directory, ignore_errors=True)


def check_evidence_matcher(box):
    print("\n[5] 摘录判据：健康的会话必须一条都不命中")
    healthy = os.path.join(box.root, "healthy.log")
    with open(healthy, "w", encoding="utf-8") as handle:
        handle.write(HEALTHY_LOG)
    hits = uvc._camera_log_evidence(healthy)
    check(hits == [], f"健康日志零命中（实际 {hits}）")
    for label, line in (
        ("每 5 秒 stats 行", "[unit_1_left] fps_in=30.00 fps_out=29.98 "
         "received=1500 output=1490 dropped=10 (+0) bad_jpeg=0 (+0) "
         "bad_replaced=0 write_errors=0 queue=2"),
        ("退出汇总行", "[unit_1_left] received=1800 output=1795 dropped=10 "
         "bad_jpeg=0 bad_replaced=0 write_errors=0 retries=0 stalls=0 "
         "usb_resets=0"),
        ("档位权威行 xfers=", "[LIBUVC-QUIRK] VID:PID 0c45:636f alt=3 "
         "payload=800 endpoint=800 frame=640x480 interval=333333 xfers=4 "
         "xfer_bytes=3200"),
    ):
        check(uvc._CAMERA_LOG_EVIDENCE.search(line) is None,
              f"{label}不被当作证据（裸 dropped / stalls= 是噪声来源）")

    print("\n[6] 告警行逐条命中，取末尾且限量")
    trouble = os.path.join(box.root, "trouble.log")
    with open(trouble, "w", encoding="utf-8") as handle:
        handle.write(TROUBLE_LOG)
    hits = uvc._camera_log_evidence(trouble, limit=99)
    check(hits == EVIDENCE_LINES,
          f"八条告警逐条命中且顺序不变（实际 {len(hits)} 条）")

    limited = uvc._camera_log_evidence(trouble)
    check(len(limited) == 6, f"默认只摘 6 条（实际 {len(limited)}）")
    check(limited == EVIDENCE_LINES[-6:],
          "摘的是最末 6 条（会话结束前发生了什么最要紧）")
    check(uvc._camera_log_evidence(os.path.join(box.root, "nope.log")) == [],
          "文件不存在时返回空表（不抛异常）")


def _manager(logged):
    return uvc.UvcCameraServiceManager(
        logger=logged.append, discovery_binary="/nonexistent/discover",
        service_binary="/nonexistent/service")


def check_stop_entry_end_to_end(box):
    print("\n[7] _stop_entry_locked 端到端")
    logged = []
    manager = _manager(logged)

    runtime_dir = box.make_runtime_dir(TROUBLE_LOG)
    entry = {"process": None, "runtime_dir": runtime_dir,
             "sockets": {}, "leases": 0, "tag": "bus1-port2.2"}
    manager._stop_entry_locked(entry)

    check(not os.path.exists(runtime_dir), "runtime_dir 已被 rmtree")
    archived = _archived(box)
    check(len(archived) == 1, f"日志被留档（{archived}）")
    check(any("告警摘录" in line for line in logged),
          "有告警时打摘录标题")
    check(sum(1 for line in logged if line.startswith("  [")) == 6,
          f"摘录行按原样带前缀缩进（{len(logged)} 行日志）")
    check(any("日志留档:" in line for line in logged),
          "摘录之后给出留档路径")
    check(logged.index(next(l for l in logged if "告警摘录" in l))
          < logged.index(next(l for l in logged if "日志留档:" in l)),
          "顺序：先摘录、后路径")

    logged.clear()
    healthy_dir = box.make_runtime_dir(HEALTHY_LOG)
    manager._stop_entry_locked(
        {"process": None, "runtime_dir": healthy_dir, "sockets": {},
         "leases": 0, "tag": "bus1-port2.2"})
    check(logged == [], f"健康会话不占 main.log（实际 {logged}）")
    # 同一秒内同 tag 的第二份：绝不覆盖上一份（留档的意义就是证据不会没了）
    check(len(_archived(box)) == 2,
          f"同秒同 tag 两次留档都不丢（{_archived(box)}）")

    missing = os.path.join(box.root, "gone")
    logged.clear()
    manager._stop_entry_locked(
        {"process": None, "runtime_dir": missing, "sockets": {},
         "leases": 0, "tag": "bus1-port2.2"})
    check(logged == [], "没有日志文件时全程静默（老条目/异常路径都走这里）")


def check_service_tag(box):
    print("\n[8] 留档身份 tag")
    check(uvc._service_tag([{"bus": 1, "outer_path": "2.2"}]) == "bus1-port2.2",
          "双 rig 靠 bus + 外口路径区分")
    check(uvc._service_tag([{"bus": "3", "outer_path": "1.4"}])
          == "bus3-port1.4", "bus 是字符串也能拼（扫描结果里并非恒为 int）")
    check(uvc._service_tag([]) == "bus0-portunknown", "空 records 降级不抛")
    check(uvc._service_tag([{"bus": None, "outer_path": ""}])
          == "bus0-portunknown", "字段缺失降级成 unknown")

    # 恶意/异常 tag 不能逃出留档目录（tag 来自设备拓扑，仍按不可信处理）
    escaped = uvc._archive_camera_log(
        box.make_runtime_dir(), "../../evil/../../x")
    check(escaped is not None
          and os.path.dirname(os.path.abspath(escaped))
          == os.path.abspath(box.archive_dir()),
          f"路径分隔符被替掉，留档仍在 logs/camera_service/（{escaped}）")
    check("/" not in os.path.basename(escaped), "文件名里不含路径分隔符")


def main():
    box = _Sandbox()
    try:
        check_archive_basic(box)
        check_missing_source(box)
        check_archive_unwritable(box)
        check_prune(box)
        check_evidence_matcher(box)
        check_stop_entry_end_to_end(box)
        check_service_tag(box)
    finally:
        box.close()

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        for msg in FAILS:
            print(f"  - {msg}")
        return 1
    print("PASS: camera-service 日志留档回归通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
