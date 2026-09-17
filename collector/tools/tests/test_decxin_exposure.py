#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DECXIN 曝光归一化 —— 离线自检（不碰真相机、不需要 root）。

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_decxin_exposure.py

背景：暗下来的原因不是采集链，而是**相机机身里存的** auto_exposure=1（手动）
+ AWB=0（出厂 156/10000 积分）。值存在相机里 ⇒ 修好一台不影响另一台，主程序
侧又没有写入口 ⇒ 每接一台新夹爪都要人工 v4l2-ctl 一次。本模块把那次人工动作
自动化，挂在 core/gripper/bridge.py 的 _open_run、libuvc 服务启动**之前**。

覆盖:
  1. V4L2 常量与 ioctl 号：与 /usr/include/linux/v4l2-controls.h、内核
     _IOWR 宏算出来的值逐字节一致（号错一个 bit 就是 EINVAL 静默不生效）
  2. 枚举只认 1bcf:2d4f：Sightac（0c45:636f）与别的 VID/PID 一律不进候选
  3. 已是自动曝光 + 自动白平衡 → 一个字节都不写
  4. 手动档 → 写 3（光圈优先）并读回确认，AWB 一并写 1
  5. DECXIN 菜单没有 3 → 退到 0；再不行退 2
  6. 候选全写不进去 → 报 unsupported，且**绝不写手动档**（不许把亮的写暗）
  7. 次级节点没有控制项 → 自动找带控制项的那个节点
  8. 节点被别的进程占着（主程序正录着这台）→ 跳过，零写入
  9. 任何异常都不外泄：连不上相机不能拖垮夹爪连接
 10. --dry-run 只读不写
 11. 静态时序：bridge 的调用点必须在 UvcCameraServiceManager.select() 之前
     （写到服务启动之后＝白写：那一刻 uvcvideo 已被摘掉）

退出码 0 = 全部通过。
"""

from __future__ import annotations

import ast
import errno
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock                                       # noqa: E402

from core.gripper import decxin_exposure as dx                  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def section(title):
    print(f"\n[{title}]")


EINVAL = OSError(errno.EINVAL, "Invalid argument")


class FakeNode:
    """假 /dev/videoN：scripted 控制项 + 记录写调用。"""

    def __init__(self, path, controls, *, unsupported=(), set_rejects=()):
        self.path = path
        self.controls = dict(controls)          # cid -> 当前值
        self.unsupported = set(unsupported)     # 读就 EINVAL 的控制项
        self.set_rejects = set(set_rejects)     # 写就 EINVAL 的 (cid, value)
        self.writes = []
        self.closed = False

    def get(self, cid):
        if cid in self.unsupported:
            raise EINVAL
        return self.controls[cid]

    def set(self, cid, value):
        if (cid, value) in self.set_rejects:
            raise EINVAL
        self.writes.append((cid, value))
        self.controls[cid] = value

    def query(self, cid):
        if cid in self.unsupported:
            return None
        return {"type": 3, "name": "Exposure, Auto",
                "min": 1, "max": 3, "step": 1, "default": 3}

    def close(self):
        self.closed = True


class FakeCamera:
    """一台假 DECXIN：一组节点 + 一个 opener，记录被打开过哪些节点。"""

    def __init__(self, nodes):
        self.nodes = {path: node for path, node in nodes.items()}
        self.opened = []

    def opener(self, path):
        if path not in self.nodes:
            raise OSError(errno.ENOENT, "No such file or directory")
        self.opened.append(path)
        return self.nodes[path]


AE = dx.V4L2_CID_EXPOSURE_AUTO
AWB = dx.V4L2_CID_AUTO_WHITE_BALANCE


def _run(camera, groups=(("1-2.1.2", [3, 4]),), **kwargs):
    logs = []
    with mock.patch.object(dx, "_decxin_groups", return_value=list(groups)):
        results = dx.normalize_decxin_exposure(
            logger=logs.append, opener=camera.opener, **kwargs)
    return results, logs


def test_constants_and_ioctl_numbers():
    section("V4L2 常量 / ioctl 号")
    check(dx.V4L2_CID_EXPOSURE_AUTO == 0x009A0901,
          "V4L2_CID_EXPOSURE_AUTO == 0x009a0901（v4l2-controls.h）")
    check(dx.V4L2_CID_AUTO_WHITE_BALANCE == 0x0098090C,
          "V4L2_CID_AUTO_WHITE_BALANCE == 0x0098090c")
    check(dx.V4L2_EXPOSURE_MANUAL == 1 and dx.V4L2_EXPOSURE_APERTURE_PRIORITY == 3,
          "菜单语义：1=手动、3=光圈优先（DECXIN 菜单 1~3、无 0）")
    check(dx._VIDIOC_G_CTRL == 0xC008561B,
          "VIDIOC_G_CTRL == 0xC008561B")
    check(dx._VIDIOC_S_CTRL == 0xC008561C,
          "VIDIOC_S_CTRL == 0xC008561C")
    check(dx._VIDIOC_QUERYCTRL == 0xC0445624,
          "VIDIOC_QUERYCTRL == 0xC0445624（68 字节 struct v4l2_queryctrl）")
    check(dx._EXPOSURE_AUTO_CANDIDATES[0] == 3
          and dx.V4L2_EXPOSURE_MANUAL not in dx._EXPOSURE_AUTO_CANDIDATES,
          "候选表以 3 打头，且不含手动档")


def test_enumeration_only_decxin():
    section("枚举只认 1bcf:2d4f")
    listing = ["video0", "video1", "video3", "video4", "video17", "videoX"]
    vids = {"video0": ("0c45", "636f"),    # Sightac 触觉 —— 绝不碰
            "video1": ("1bcf", "2d4f"),
            "video3": ("1bcf", "2d4f"),
            "video4": ("1bcf", "2d4f"),
            "video17": ("1bcf", "2d4f"),   # 超出 max_index
            "videoX": ("1bcf", "2d4f")}    # 名字都读不出索引
    paths = {"video1": "1-2.1.2", "video3": "1-2.1.2", "video4": "1-2.1.2",
             "video0": "1-4", "video17": "1-9"}
    fake_os = mock.Mock()
    fake_os.listdir.return_value = listing
    with mock.patch.object(dx, "os", fake_os), \
            mock.patch("core.camera._usb_vid_pid",
                       side_effect=lambda idx: vids.get(f"video{idx}")), \
            mock.patch("core.camera._physical_usb_path",
                       side_effect=lambda idx: paths.get(f"video{idx}")):
        groups = dx._decxin_groups(max_index=16)
    check(groups == [("1-2.1.2", [1, 3, 4])],
          f"只剩同一台 DECXIN 的三个节点、按索引排序: {groups}")
    check(0 not in [i for _key, idx_list in groups for i in idx_list],
          "Sightac 0c45:636f（video0）不在候选里")


def test_already_auto_is_untouched():
    section("已是自动 → 不写")
    camera = FakeCamera({
        "/dev/video3": FakeNode("/dev/video3", {AE: 3, AWB: 1}),
    })
    results, logs = _run(camera)
    check(len(results) == 1 and results[0].state == "ok",
          f"状态 ok: {results[0].state}")
    check(camera.nodes["/dev/video3"].writes == [],
          "零写入（不无谓地每连一次动一次相机）")
    check(any("已是自动曝光" in line for line in logs), "日志说明无需处理")


def test_manual_stuck_is_fixed():
    section("手动档 → 写回自动")
    node = FakeNode("/dev/video3", {AE: 1, AWB: 0})
    camera = FakeCamera({"/dev/video3": node})
    results, logs = _run(camera)
    check(results[0].state == "changed",
          f"状态 changed: {results[0].state}")
    check(node.controls[AE] == 3 and node.controls[AWB] == 1,
          f"最终 AE={node.controls[AE]} AWB={node.controls[AWB]}（3=光圈优先）")
    check((AE, 3) in node.writes and (AWB, 1) in node.writes,
          f"写入序列 {node.writes}")
    check(results[0].before == (1, 0) and results[0].after == (3, 1),
          f"before/after 记录完整: {results[0].before} → {results[0].after}")
    check(node.closed, "句柄收尾关闭")


def test_menu_without_three():
    section("菜单没有 3 → 退到 0")
    node = FakeNode("/dev/video3", {AE: 1, AWB: 1},
                    set_rejects={(AE, 3), (AE, 2)})
    camera = FakeCamera({"/dev/video3": node})
    results, _logs = _run(camera)
    check(node.controls[AE] == 0, f"落到标准 UVC 的 0=auto: AE={node.controls[AE]}")
    check(results[0].state == "changed", f"状态 changed: {results[0].state}")


def test_all_candidates_fail_never_writes_manual():
    section("候选全失败 → 不改暗、不写手动档")
    node = FakeNode("/dev/video3", {AE: 1, AWB: 1},
                    set_rejects={(AE, 3), (AE, 0), (AE, 2)})
    camera = FakeCamera({"/dev/video3": node})
    results, logs = _run(camera)
    check(results[0].state == "unsupported",
          f"状态 unsupported: {results[0].state}")
    check(node.controls[AE] == 1, "相机仍是手动（没被写坏）")
    check(all(value != dx.V4L2_EXPOSURE_MANUAL for _cid, value in node.writes),
          f"写入里从不出现手动档: {node.writes}")
    check(any("全部写不进去" in line for line in logs), "日志点明写不进去的原因")


def test_picks_control_capable_node():
    section("次级节点无控制项 → 找带控制项的节点")
    camera = FakeCamera({
        "/dev/video3": FakeNode("/dev/video3", {AE: 1, AWB: 0},
                                unsupported={AE, AWB}),
        "/dev/video4": FakeNode("/dev/video4", {AE: 1, AWB: 0}),
    })
    results, _logs = _run(camera)
    check(camera.opened == ["/dev/video3", "/dev/video4"],
          f"按索引依次试: {camera.opened}")
    check(results[0].node == "/dev/video4" and results[0].state == "changed",
          f"落在 {results[0].node} ({results[0].state})")
    check(camera.nodes["/dev/video3"].closed, "试失败的节点也关掉")


def test_busy_node_is_skipped():
    section("节点被占用 → 跳过")
    node = FakeNode("/dev/video3", {AE: 1, AWB: 0})
    camera = FakeCamera({"/dev/video3": node})
    logs = []
    result = dx._normalize_one(
        "1-2.1.2", [3, 4], log=logs.append, dry_run=False, opener=camera.opener,
        holder_probe=lambda path: ["4242:main.py"] if path == "/dev/video4" else [])
    check(result.state == "skipped", f"状态 skipped: {result.state}")
    check(node.writes == [] and camera.opened == [],
          "零写入、且根本没 open（占用检查在 open 之前，否则查到的占用者是自己）")
    check("4242:main.py" in result.detail, f"说明里带上占用者: {result.detail}")


def test_holders_scan_sees_own_process():
    section("占用扫描：本进程自己开着的/字符设备")
    check(dx._node_holders("/dev/definitely-not-here") == [],
          "不存在的路径 → 空表（不抛）")
    fd = os.open("/dev/null", os.O_RDONLY)
    try:
        holders = dx._node_holders("/dev/null")
    finally:
        os.close(fd)
    check(any(h.startswith(f"{os.getpid()}:") for h in holders),
          f"本进程（夹爪桥就在主程序进程里）也算占用者: {holders}")


def test_awb_rejected_is_partial():
    section("AWB 写不进去但曝光已自动 → partial")
    node = FakeNode("/dev/video3", {AE: 3, AWB: 0}, set_rejects={(AWB, 1)})
    camera = FakeCamera({"/dev/video3": node})
    results, _logs = _run(camera)
    check(results[0].state == "partial", f"状态 partial: {results[0].state}")
    check(results[0].healthy, "亮度契约仍成立（AE 已是自动）")
    check(node.controls[AE] == 3, "AE 没被带坏")


def test_never_raises_and_dry_run():
    section("异常不外泄 / dry-run")
    logs = []
    with mock.patch.object(dx, "_decxin_groups", side_effect=RuntimeError("boom")):
        results = dx.normalize_decxin_exposure(logger=logs.append)
    check(results == [] and any("枚举失败" in line for line in logs),
          "枚举异常 → 空结果 + 日志，不抛")

    node = FakeNode("/dev/video3", {AE: 1, AWB: 0})
    camera = FakeCamera({"/dev/video3": node})
    results, logs = _run(camera, dry_run=True)
    check(results[0].state == "dry-run" and node.writes == [],
          "dry-run 只读不写")
    check(any("dry-run" in line for line in logs), "dry-run 日志可辨认")

    camera2 = FakeCamera({})            # 所有节点都 open 失败
    results, logs = _run(camera2)
    check(results and results[0].state == "unsupported",
          f"open 全失败 → unsupported（不抛）: {results[0].detail}")
    check(dx._node_holders("/dev/definitely-not-here") == [],
          "_node_holders 对不存在的节点返回空表（不抛）")


def test_bridge_call_precedes_service_start():
    section("bridge 静态时序")
    bridge_path = os.path.join(REPO_ROOT, "core", "gripper", "bridge.py")
    with open(bridge_path, encoding="utf-8") as fh:
        source = fh.read()
    check("from core.gripper.decxin_exposure import normalize_decxin_exposure"
          in source, "bridge 导入了归一化入口")
    tree = ast.parse(source)
    open_run = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_open_run"),
                    None)
    check(open_run is not None, "找得到 _open_run")
    if open_run is None:
        return
    calls = []
    for node in ast.walk(open_run):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            calls.append((node.lineno, func.id))
        elif isinstance(func, ast.Attribute):
            calls.append((node.lineno, func.attr))
    calls.sort()
    names = [name for _line, name in calls]
    check("normalize_decxin_exposure" in names, "在 _open_run 里调用")
    if "normalize_decxin_exposure" not in names:
        return
    normalize_at = names.index("normalize_decxin_exposure")
    select_at = next((i for i, name in enumerate(names) if name == "select"), None)
    check(select_at is not None and normalize_at < select_at,
          "调用点在 UvcCameraServiceManager.select()（＝服务启动）之前"
          " —— 之后 uvcvideo 已被 libusb 摘掉，写不进去就白写")


def main():
    test_constants_and_ioctl_numbers()
    test_enumeration_only_decxin()
    test_already_auto_is_untouched()
    test_manual_stuck_is_fixed()
    test_menu_without_three()
    test_all_candidates_fail_never_writes_manual()
    test_picks_control_capable_node()
    test_busy_node_is_skipped()
    test_holders_scan_sees_own_process()
    test_awb_rejected_is_partial()
    test_never_raises_and_dry_run()
    test_bridge_call_precedes_service_start()
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
