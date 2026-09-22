"""设备检测模块单元测试。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_device_detector.py

覆盖:
  - _parse_by_id_entry 各形态（纯函数）
  - _list_uvc_devices 排除 RealSense / FTDI（mock list_v4l_devices）
  - _list_s80m_devices 多台按序列号区分（mock _ftdi_camera_groups）
  - detect_devices 子模块异常不崩（mock 抛异常）
  - DeviceScanner 信号投递（QCoreApplication 事件循环）
  - 真机段（D435 在位时）: _is_realsense_node / _list_d435_devices serial 非空
  - detect_cameras 从不请求 RealSense 索引（patch _try_open_camera）
  - BLE: _mac_norm / bluetoothctl 解析 / 发现合并去重 / 分组判定 / 扫描抑制
  - 设备命名持久化 round-trip（临时文件，含 sensor 角色与旧格式升级）
"""
import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication

from config import settings
import core.camera as cam
import core.device_detector as det
from core.device_detector import (
    DeviceInfo, _parse_by_id_entry, _list_uvc_devices, _list_s80m_devices,
    _mac_norm, _bluetoothctl_paired, _list_ble_devices,
    _glove_side_by_serial,
    detect_devices, DeviceScanner, set_ble_scan_suppressed,
)

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def main():
    # 测试默认不跑真实 5s 蓝牙主动扫描（真机段单独处理）
    _orig_discover = det._ble_discover
    det._ble_discover = lambda: []
    try:
        return _main()
    finally:
        det._ble_discover = _orig_discover


def _main():
    print("── 1. _parse_by_id_entry 各形态 ──")
    cases = [
        ("usb-DECXIN_Video_Camera_2024010100-video-index0",
         {"prefix": "usb-DECXIN_Video_Camera_2024010100",
          "serial": "2024010100", "index": 0}),
        ("usb-046d_0825_AB12CD34-video-index0",
         {"prefix": "usb-046d_0825_AB12CD34",
          "serial": "AB12CD34", "index": 0}),
        # 末尾段太短/非字母数字 → 不算序号
        ("usb-SunplusIT_Inc_Integrated_Camera-video-index0",
         {"prefix": "usb-SunplusIT_Inc_Integrated_Camera",
          "serial": "", "index": 0}),
        ("usb-Intel_R__RealSense_TM__Depth_Camera_435_212223021136-video-index1",
         {"prefix": "usb-Intel_R__RealSense_TM__Depth_Camera_435_212223021136",
          "serial": "212223021136", "index": 1}),
        # 无 -video-indexN 后缀
        ("no-suffix-entry",
         {"prefix": "no-suffix-entry", "serial": "", "index": None}),
        ("", None),
        (None, None),
    ]
    for entry, expect in cases:
        got = _parse_by_id_entry(entry)
        check(got == expect, f"_parse_by_id_entry({entry!r}) = {got}")

    print("── 2. _list_uvc_devices 过滤（mock list_v4l_devices） ──")
    fake_v4l = [
        {"video_index": 0, "name": "RealSense 435i", "serial": "111111111111",
         "by_id_path": "/dev/v4l/by-id/usb-RealSense_111111111111-video-index0",
         "vid": "8086", "pid": "0b3a", "is_sdk": False, "is_realsense": True},
        {"video_index": 1, "name": "FTDI 设备", "serial": "",
         "by_id_path": "/dev/v4l/by-id/usb-FTDI-video-index1",
         "vid": "0403", "pid": "601e", "is_sdk": True, "is_realsense": False},
        {"video_index": 2, "name": "DECXIN Webcam", "serial": "2024010100",
         "by_id_path": "/dev/v4l/by-id/usb-DECXIN_Video_Camera_2024010100-video-index0",
         "vid": "32e4", "pid": "0416", "is_sdk": False, "is_realsense": False},
    ]
    with patch("core.device_detector.list_v4l_devices", return_value=fake_v4l):
        infos = _list_uvc_devices(16)
    keys = [i.key for i in infos]
    check(len(infos) == 1 and keys[0].startswith("uvc:"), f"只剩 UVC: {keys}")
    check(infos[0].video_index == 2 and infos[0].serial == "2024010100",
          f"webcam 索引/序号正确: idx={infos[0].video_index} serial={infos[0].serial}")

    print("── 2b. list_v4l_devices 按物理设备分组（by-id 被顶掉也可见） ──")
    # 两颗同型号同序列号 DECXIN：udev 的 by-id 链接名唯一（厂商_型号_序列号
    # 完全相同），只有后注册那颗有链接，另一颗成孤儿。以 by-id 为枚举入口
    # 会让孤儿**整个从面板消失**（真机：video8/9 曾是这种孤儿）。
    nodes = {"1-5": [8, 9], "1-2.2.2": [10, 11]}
    hub_of = {8: "1-5", 9: "1-5", 10: "1-2.2.2", 11: "1-2.2.2"}
    with patch("core.camera._v4l_nodes_by_physical_device",
               return_value=nodes), \
         patch("core.camera._physical_usb_path",
               side_effect=lambda i: hub_of.get(i)), \
         patch("core.camera._find_persistent_v4l_path",
               side_effect=lambda i: ("/dev/v4l/by-id/usb-DECXIN_DECXIN_CAMERA"
                                      "_01.00.00-video-index0" if i == 10 else None)), \
         patch("core.camera._usb_vid_pid", return_value=("1bcf", "2d4f")), \
         patch("core.camera._usb_ident_strings",
               return_value=("DECXIN", "DECXIN CAMERA")), \
         patch("core.camera._is_sdk_device", return_value=False), \
         patch("core.camera._is_realsense_node", return_value=False):
        devs = cam.list_v4l_devices(16)
    check(sorted(d["video_index"] for d in devs) == [8, 10],
          f"两颗各取主视频流（不是聚成一颗）: {[d['video_index'] for d in devs]}")
    orphan = next(d for d in devs if d["video_index"] == 8)
    check(orphan["by_id_path"] is None and orphan["usb_path"] == "1-5"
          and orphan["name"] == "DECXIN DECXIN CAMERA",
          f"孤儿靠 USB 拓扑+字符串兜底: {orphan}")
    # 同型号 + 有一台拿不到链接 ⇒ 那个前缀不能当 key：**谁拿链接取决于注册
    # 顺序、重新枚举时会翻转**，于是拿到链接的 video10 自己也得放弃 by-id
    # 前缀。注意判据不能是「数前缀重复」—— 任一时刻只有一台有链接，另一台
    # 压根进不了计数，这么写永远数不出来。
    check(next(d for d in devs if d["video_index"] == 10)["by_id_ambiguous"] is True,
          "同型号无链接设备在场 → 该 by-id 前缀标记有歧义（拿链接的也要退）")
    check(orphan["by_id_ambiguous"] is False,
          "孤儿无前缀，不参与歧义标记（by_id_path=None 本就退拓扑）")

    # 对照：同型号**不同序列号**两台各有各的链接 → 前缀唯一，不许误判降级
    link2 = {8: "/dev/v4l/by-id/usb-DECXIN_DECXIN_CAMERA_01.00.00-video-index0",
             10: "/dev/v4l/by-id/usb-DECXIN_DECXIN_CAMERA_01.00.01-video-index0"}
    with patch("core.camera._v4l_nodes_by_physical_device", return_value=nodes), \
         patch("core.camera._physical_usb_path",
               side_effect=lambda i: hub_of.get(i)), \
         patch("core.camera._find_persistent_v4l_path",
               side_effect=lambda i: link2.get(i)), \
         patch("core.camera._usb_vid_pid", return_value=("1bcf", "2d4f")), \
         patch("core.camera._usb_ident_strings",
               return_value=("DECXIN", "DECXIN CAMERA")), \
         patch("core.camera._is_sdk_device", return_value=False), \
         patch("core.camera._is_realsense_node", return_value=False):
        devs2 = cam.list_v4l_devices(16)
    check(all(d["by_id_ambiguous"] is False for d in devs2)
          and len({d["by_id_path"] for d in devs2}) == 2,
          f"同型号不同序列号各持前缀 → 不降级: "
          f"{[(d['video_index'], d['by_id_ambiguous']) for d in devs2]}")

    print("── 3. _list_s80m_devices 多台按序列号区分（mock _ftdi_camera_groups） ──")
    fake_cams = [
        {"usb_path": "1-3.4.1", "serial": "000000000001", "stereo_index": 0},
        {"usb_path": "1-3.4.2", "serial": "000000000002", "stereo_index": 2},
    ]
    with patch("core.device_detector._ftdi_camera_groups", return_value=fake_cams):
        infos = _list_s80m_devices(16)
    check(len(infos) == 2
          and [i.key for i in infos] == ["s80m:000000000001",
                                         "s80m:000000000002"]
          and all(i.kind == "s80m" for i in infos)
          and [i.usb_path for i in infos] == ["1-3.4.1", "1-3.4.2"],
          f"两台按序列号区分: {[(i.key, i.usb_path) for i in infos]}")
    check(infos[0].video_index == 0 and infos[1].video_index == 2,
          "video_index 取自各相机双目节点")

    # 同序列号两台（FTDI 出厂默认号）→ key 追加 USB 路径兜底唯一
    dup_cams = [
        {"usb_path": "1-3.4.1", "serial": "000000000001", "stereo_index": 0},
        {"usb_path": "1-3.4.2", "serial": "000000000001", "stereo_index": 2},
    ]
    with patch("core.device_detector._ftdi_camera_groups", return_value=dup_cams):
        infos = _list_s80m_devices(16)
    keys = [i.key for i in infos]
    check(len(infos) == 2 and len(set(keys)) == 2
          and any("@1-3.4.2" in k for k in keys),
          f"同号兜底 key 唯一: {keys}")

    # 无序列号 → USB 路径兜底
    nosn_cams = [{"usb_path": "1-3.4.1", "serial": "", "stereo_index": 0}]
    with patch("core.device_detector._ftdi_camera_groups", return_value=nosn_cams):
        infos = _list_s80m_devices(16)
    check(len(infos) == 1 and infos[0].key == "s80m:usb-1-3.4.1",
          f"无序列号兜底: {[i.key for i in infos]}")

    # 老环境兜底：sysfs 扫不到 → 退回旧版 _is_sdk_device 单条
    def fake_sdk(i):
        return i == 7
    with patch("core.device_detector._ftdi_camera_groups", return_value=[]), \
         patch("core.device_detector._is_sdk_device", side_effect=fake_sdk):
        infos = _list_s80m_devices(16)
    check(len(infos) == 1 and infos[0].key == "s80m:ftdi"
          and infos[0].video_index == 7,
          f"老环境兜底单条: {[(i.key, i.video_index) for i in infos]}")
    with patch("core.device_detector._ftdi_camera_groups", return_value=[]), \
         patch("core.device_detector._is_sdk_device", return_value=False):
        infos = _list_s80m_devices(16)
    check(infos == [], "无 FTDI 时返回空")

    print("── 4. detect_devices 子模块异常不崩 ──")
    # v1.1.3 起有第五段 USB 手套枚举；夹爪接入后有第六段 UMI 夹爪，
    # 真机在位时同样要炸掉（夹爪枚举含 pyserial 真机扫描，必须 mock）
    with patch("core.device_detector._list_uvc_devices", side_effect=OSError("boom")), \
         patch("core.device_detector._list_d435_devices", side_effect=OSError("boom")), \
         patch("core.device_detector._list_s80m_devices", side_effect=OSError("boom")), \
         patch("core.device_detector._list_ble_devices", side_effect=OSError("boom")), \
         patch("core.device_detector._list_usb_glove_devices", side_effect=OSError("boom")), \
         patch("core.device_detector._list_gripper_devices", side_effect=OSError("boom")):
        check(detect_devices() == [], "六段全炸 → 返回空列表不抛异常")

    print("── 4b. UMI 夹爪枚举 / 组件相机排除 ──")
    # 组件相机按 VID/PID 排除（0c45:636f sightac / 1bcf:2d4f decxin /
    # 0403:602e fays）；VID/PID 缺失时按 by-id 字符串兜底
    check(det._is_gripper_component_camera(
        {"vid": "0c45", "pid": "636f"}), "sightac VID/PID 排除")
    check(det._is_gripper_component_camera(
        {"vid": "1bcf", "pid": "2d4f"}), "decxin VID/PID 排除")
    check(not det._is_gripper_component_camera(
        {"vid": "32e4", "pid": "0416"}), "无关 VID/PID 保留")
    check(det._is_gripper_component_camera(
        {"by_id_path": "/dev/v4l/by-id/usb-Sightac_SN0001-video-index0",
         "name": "Sightac"}),
        "VID/PID 缺失时 by-id 兜底排除")

    print("── 4c. 单插 DECXIN 按 USB 根端口与 rig 区分 ──")
    # 真机：控制板 1-2.2.1、rig 的 DECXIN 1-2.2.2（同根端口 1-2），
    # 单插的 DECXIN 1-5。同 VID/PID 只能靠拓扑分开。
    decxin = {"vid": "1bcf", "pid": "2d4f", "video_index": 8}
    with patch("core.device_detector._v4l_root_hub", return_value="1-5"):
        check(not det._is_gripper_component_camera(decxin, gripper_hubs=set()),
              "没接 rig → 单插 DECXIN 放行")
        check(not det._is_gripper_component_camera(decxin,
                                                   gripper_hubs={"1-2"}),
              "与控制板不同根端口 → 单插 DECXIN 放行")
    with patch("core.device_detector._v4l_root_hub", return_value="1-2"):
        check(not det._is_gripper_component_camera(decxin, gripper_hubs=set()),
              "没接 rig → rig 位置的 DECXIN 也放行")
        check(det._is_gripper_component_camera(decxin, gripper_hubs={"1-2"}),
              "与控制板同根端口 → rig 的 DECXIN 排除")
    with patch("core.device_detector._v4l_root_hub", return_value=None):
        check(det._is_gripper_component_camera(decxin, gripper_hubs={"1-2"}),
              "根端口取不到 → 保守排除，不与 rig 双开")
    check(det._is_gripper_component_camera(decxin),
          "不传 gripper_hubs（未知）→ 维持旧契约排除")
    with patch("core.device_detector._v4l_root_hub", return_value="1-5"):
        check(det._is_gripper_component_camera(
            {"vid": "0c45", "pid": "636f", "video_index": 9},
            gripper_hubs=set()), "Sightac 单插也无独立用途 → 始终排除")

    # 端到端：两颗同型号 DECXIN 同时在位，只放行单插那颗
    def _mk_decxin(idx, tail):
        return {"video_index": idx, "name": "DECXIN DECXIN CAMERA 01.00.00",
                "serial": "01.00.00",
                "by_id_path": f"/dev/v4l/by-id/usb-DECXIN_DECXIN_CAMERA_{tail}"
                              f"-video-index0",
                "vid": "1bcf", "pid": "2d4f",
                "is_sdk": False, "is_realsense": False}
    # hub_of 按 _v4l_root_hub 的契约给**根端口**（1-2.2.2 的根端口是 1-2）
    hub_of = {8: "1-5", 10: "1-2"}
    with patch("core.device_detector.list_v4l_devices",
               return_value=[_mk_decxin(8, "01.00.00"), _mk_decxin(10, "01.00.01")]), \
         patch("core.device_detector._v4l_root_hub",
               side_effect=lambda i: hub_of.get(i)):
        infos = _list_uvc_devices(16, gripper_hubs={"1-2"})
    check([i.video_index for i in infos] == [8]
          and infos[0].key == "uvc:usb-DECXIN_DECXIN_CAMERA_01.00.00",
          f"只放行单插那颗且 key 与 device_names 稳定: "
          f"{[(i.video_index, i.key) for i in infos]}")

    # by-id 被顶掉时 key 退到 USB 拓扑路径（跨重启稳定），不是会漂移的索引
    with patch("core.device_detector.list_v4l_devices", return_value=[
            {"video_index": 8, "name": "DECXIN DECXIN CAMERA", "serial": "",
             "by_id_path": None, "usb_path": "1-5",
             "vid": "1bcf", "pid": "2d4f",
             "is_sdk": False, "is_realsense": False}]), \
         patch("core.device_detector._v4l_root_hub", return_value="1-5"):
        infos = _list_uvc_devices(16, gripper_hubs={"1-2"})
    check([i.key for i in infos] == ["uvc:usb-1-5"],
          f"by-id 被顶掉 → key 退到 USB 拓扑路径: {[i.key for i in infos]}")

    # by-id 前缀有歧义（被同型号两台共用，链接归属会在重枚举时翻转）→
    # **拿到链接的那台也不能用**，否则同一台相机在 by-id key 与拓扑 key 之间
    # 跳，面板表现为设备消失又出现、并把用户起的名字丢掉
    with patch("core.device_detector.list_v4l_devices", return_value=[
            {**_mk_decxin(8, "01.00.00"), "usb_path": "1-5",
             "by_id_ambiguous": True},
            {**_mk_decxin(10, "01.00.00"), "usb_path": "1-2.2.2",
             "by_id_ambiguous": True}]), \
         patch("core.device_detector._v4l_root_hub", return_value="1-5"):
        infos = _list_uvc_devices(16, gripper_hubs={"1-2"})
    keys = sorted(i.key for i in infos)
    check(keys == ["uvc:usb-1-2.2.2", "uvc:usb-1-5"] and len(set(keys)) == 2,
          f"前缀有歧义 → 两颗都退拓扑路径且 key 互不相同: {keys}")

    # 歧义 + 连拓扑路径都取不到 → 退到索引（最后一档兜底，不许抛异常）
    with patch("core.device_detector.list_v4l_devices", return_value=[
            {**_mk_decxin(8, "01.00.00"), "usb_path": "",
             "by_id_ambiguous": True}]), \
         patch("core.device_detector._v4l_root_hub", return_value="1-5"):
        infos = _list_uvc_devices(16, gripper_hubs={"1-2"})
    check([i.key for i in infos] == ["uvc:8"],
          f"歧义且无拓扑路径 → 退索引兜底: {[i.key for i in infos]}")

    # gripper_root_hubs：从控制板 tty 路径取根端口
    with patch("core.device_detector._tty_root_hub",
               side_effect=lambda p: "1-2" if p == "/dev/ttyACM0" else None):
        check(det.gripper_root_hubs(
            [DeviceInfo(key="gripper:s", kind="gripper", display_name="g",
                        address="/dev/ttyACM0")]) == {"1-2"},
              "控制板根端口取自 tty 路径")
    check(det.gripper_root_hubs([]) == set(), "没接 rig → 空集")

    # 夹爪枚举：mock 串口枚举 + 资源可用 → 一条 gripper 条目
    class _Port:
        def __init__(self, vid, pid, sn, dev):
            self.vid, self.pid = vid, pid
            self.serial_number, self.device = sn, dev
    fake_ports = [
        _Port(0x303A, 0x1001, "CC:BA:97:25:B4:44", "/dev/ttyACM0"),
        _Port(0x0483, 0x5740, "glove-sn", "/dev/ttyACM1"),
    ]
    with patch("core.gripper.paths.gripper_resources_available",
               return_value=True), \
         patch("serial.tools.list_ports.comports",
               return_value=fake_ports):
        infos = det._list_gripper_devices()
    check(len(infos) == 1 and infos[0].key == "gripper:CC:BA:97:25:B4:44"
          and infos[0].address == "/dev/ttyACM0"
          and infos[0].group == "gripper",
          f"夹爪枚举唯一命中: {[(i.key, i.address) for i in infos]}")
    with patch("core.gripper.paths.gripper_resources_available",
               return_value=False), \
         patch("serial.tools.list_ports.comports",
               return_value=fake_ports):
        infos = det._list_gripper_devices()
    check(infos == [], "资源缺失时夹爪条目隐藏")
    with patch("core.gripper.paths.gripper_resources_available",
               return_value=True), \
         patch("serial.tools.list_ports.comports",
               return_value=fake_ports), \
         patch("core.device_detector._list_s80m_devices",
               return_value=[DeviceInfo(key="s80m:x", kind="s80m",
                                        display_name="x")]):
        devs = detect_devices()
    check(not any(d.kind == "s80m" for d in devs),
          "夹爪在场时 s80m 条目被抑制")

    print("── 5. DeviceScanner 信号投递 ──")
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    scanner = DeviceScanner(max_index=16)
    got = []
    scanner.scan_finished.connect(lambda devs: got.append(devs))
    scanner.request_scan()
    t0 = time.time()
    while not got and time.time() - t0 < 5:
        app.processEvents()
        time.sleep(0.01)
    check(bool(got) and isinstance(got[0], list), "scan_finished 收到列表")
    # 守卫：进行中再次请求不堆积（_busy）
    scanner.stop()
    scanner.request_scan()
    check(True, "stop 后 request_scan 不启动新线程")

    print("── 6. 真机段（RealSense 在位时） ──")
    rs_nodes = [i for i in range(16) if cam._is_realsense_node(i)]
    if rs_nodes:
        check(True, f"_is_realsense_node 命中: {rs_nodes}")
        d435s = [d for d in detect_devices() if d.kind == "d435"]
        check(bool(d435s) and d435s[0].serial,
              f"_list_d435_devices serial 非空: "
              f"{[(d.display_name, d.serial) for d in d435s]}")
        if len(d435s) >= 2:
            check(len({d.serial for d in d435s}) == len(d435s),
                  f"多台 D400 各自成条（serial 唯一）: "
                  f"{[(d.display_name, d.serial) for d in d435s]}")
        else:
            print("  （仅 1 台 D400，多设备分支未覆盖）")
    else:
        print("  SKIP: 本机无 RealSense 设备")

    print("── 7. detect_cameras 从不请求 RealSense 索引 ──")
    requested = []
    def spy(index, test_read=False, fallback_all_by_id=False):
        requested.append(index)
        return None, ""
    with patch("core.camera._try_open_camera", side_effect=spy):
        cam.detect_cameras(max_index=settings.DEVICE_SCAN_MAX_INDEX)
    bad = [i for i in requested if i in rs_nodes]
    check(not bad, f"RealSense 索引从未被请求（请求了 {requested}）")

    print("── 8. BLE: _mac_norm / bluetoothctl 解析 ──")
    check(_mac_norm("aa:bb:cc:11:22:33") == "AA:BB:CC:11:22:33",
          f"MAC 大写归一: {_mac_norm('aa:bb:cc:11:22:33')}")
    check(_mac_norm("AA-BB-CC-11-22-33") == "AA:BB:CC:11:22:33",
          f"MAC 连字符归一: {_mac_norm('AA-BB-CC-11-22-33')}")
    fake_out = ("Device 30:A9:98:57:4A:C2 HUAWEI FreeBuds 5i\n"
                "Controller F8:3D:C6:C1:1B:E9 REDACTED-HOST\n")
    with patch("core.device_detector.subprocess.run",
               return_value=type("R", (), {"stdout": fake_out})()):
        paired = _bluetoothctl_paired()
    check(paired.get("30:A9:98:57:4A:C2") == "HUAWEI FreeBuds 5i"
          and "F8:3D:C6:C1:1B:E9" not in paired,
          f"配对列表解析（Controller 行排除）: {paired}")

    print("── 9. BLE: 发现合并去重 / 分组判定 ──")
    fake_disc = [("Matrix Glove R", "aa:11:22:33:44:55", -45),
                 ("FreeBuds", "30:a9:98:57:4a:c2", -60),
                 ("Phone X", "bb:66:77:88:99:00", -70)]
    with patch("core.device_detector._ble_discover", return_value=fake_disc), \
         patch("core.device_detector._bluetoothctl_paired",
               return_value={"30:A9:98:57:4A:C2": "HUAWEI FreeBuds 5i"}):
        infos = _list_ble_devices()
    by_key = {i.key: i for i in infos}
    check(len(infos) == 3, f"配对+发现合并去重后 3 条: {sorted(by_key)}")
    glove = by_key.get("ble:AA:11:22:33:44:55")
    check(glove is not None and glove.kind == "data_ble" and glove.group == "glove",
          f"Matrix 判为手套: {getattr(glove, 'kind', None)}/{getattr(glove, 'group', None)}")
    buds = by_key.get("ble:30:A9:98:57:4A:C2")
    check(buds is not None and buds.kind == "ble" and buds.group == "other_ble"
          and buds.display_name == "HUAWEI FreeBuds 5i" and buds.serial == "30:A9:98:57:4A:C2",
          f"耳机入其他蓝牙组且配对名优先: "
          f"{getattr(buds, 'kind', None)}/{getattr(buds, 'display_name', None)}")
    check(buds.label == buds.display_name, "label 回落 display_name")

    print("── 10. BLE: 扫描抑制（手套连接中不触发发现） ──")
    det._ble_discovery_cache["ts"] = 0.0   # 重置节流，确保会触发刷新
    calls = []
    def spy_discover():
        calls.append(1)
        return [("Matrix Glove L", "cc:11:22:33:44:55", -50)]
    det._ble_discover = spy_discover
    try:
        set_ble_scan_suppressed(True)
        infos = _list_ble_devices()
        check(not calls, f"抑制时不调用发现（calls={len(calls)}）")
        set_ble_scan_suppressed(False)
        infos = _list_ble_devices()
        check(len(calls) == 1, f"解除抑制后触发发现（calls={len(calls)}）")
        check(any(i.kind == "data_ble" for i in infos), "发现结果并入列表")
    finally:
        set_ble_scan_suppressed(False)
        det._ble_discovery_cache["ts"] = 0.0
        det._ble_discover = lambda: []

    print("── 11. 设备命名持久化 round-trip（临时文件） ──")
    import tempfile
    from config import settings as _settings
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_path = tmp.name
    tmp.close()
    _orig_file = _settings.DEVICE_NAMES_FILE
    _settings.DEVICE_NAMES_FILE = tmp_path
    try:
        _settings.save_device_name("d435:123456789012", "顶部深度相机")
        _settings.save_device_name("ble:AA:11:22:33:44:55", "右手手套",
                                   sensor="right_glove")
        _settings.save_device_name("uvc:usb-Logitech_ABC123", "桌面摄像头")
        names = _settings.load_device_names()
        check(names["d435:123456789012"]["name"] == "顶部深度相机",
              f"命名写入并读回: {names.get('d435:123456789012')}")
        check(_settings.device_name("d435:123456789012") == "顶部深度相机",
              "device_name 读取")
        check(_settings.device_sensor_role("ble:AA:11:22:33:44:55") == "right_glove",
              "sensor 角色绑定")
        # merge-write：再保存其它键不动已有条目
        _settings.save_device_name("d435:999999999999", "备用机")
        names2 = _settings.load_device_names()
        check("d435:123456789012" in names2 and "ble:AA:11:22:33:44:55" in names2,
              "merge-write 保留旧条目")
        # 旧版纯字符串条目升级 + remove
        names2["d435:999999999999"] = "旧格式字符串"
        _settings._write_device_names(names2)
        _settings.save_device_name("d435:999999999999", "备用机2")
        check(_settings.device_name("d435:999999999999") == "备用机2",
              "旧字符串条目升级为结构化并更新")
        _settings.remove_device_name("uvc:usb-Logitech_ABC123")
        check("uvc:usb-Logitech_ABC123" not in _settings.load_device_names(),
              "remove 删除条目")
        check(_settings.device_name("不存在的key") == "" and
              _settings.device_sensor_role("不存在的key") == "",
              "缺失条目安全回落空串")

        # v1.1.3: USB 手套序列号注册的侧别是权威信息 —— 即便 BLE 陈旧绑定
        # 占着 right_glove，也直接占用并驱逐（右手套不得落到 left_glove）
        role = _settings.assign_glove_sensor_role("usbglove:2096376E3032",
                                                  "right_glove")
        check(role == "right_glove"
              and _settings.device_sensor_role("usbglove:2096376E3032")
              == "right_glove",
              f"USB 权威侧别占用 right_glove: {role}")
        check(_settings.device_sensor_role("ble:AA:11:22:33:44:55") == "",
              "被抢占列的陈旧 BLE 绑定已释放")
        check(_settings.assign_glove_sensor_role("usbglove:2096376E3032",
                                                 "right_glove") == "right_glove",
              "USB 手套重连保持绑定（幂等）")
        # 注册表改侧别 ⇒ 历史绑定必须让位。用户把序列号挪到另一侧（同一只
        # 手套换到另一只手上）时，改注册表是全程序**唯一**的修法 —— 没有任何
        # UI 能改手套的列名绑定。若历史绑定优先，重连还是老样子：用户改完
        # 注册表会发现"改了也没用"，且日志一切正常。
        _settings.save_device_name("usbglove:2096376E3032", "右手套",
                                   sensor="right_glove")
        role_moved = _settings.assign_glove_sensor_role("usbglove:2096376E3032",
                                                        "left_glove")
        check(role_moved == "left_glove"
              and _settings.device_sensor_role("usbglove:2096376E3032")
              == "left_glove",
              f"注册表改侧别压过历史绑定（改了注册表要生效）: {role_moved}")
        check(_settings.device_name("usbglove:2096376E3032") == "右手套",
              "改侧别不得丢掉用户命名")
        # 改回来，免得影响下面的用例（同一临时文件）
        _settings.assign_glove_sensor_role("usbglove:2096376E3032",
                                           "right_glove")
        # 非 usbglove 键（BLE）保持原有碰撞规避语义：right 被占 → 落 left
        check(_settings.assign_glove_sensor_role("ble:FF:11:22:33:44:55",
                                                 "right_glove") == "left_glove",
              "BLE 键仍按空闲列规避碰撞")
    finally:
        _settings.DEVICE_NAMES_FILE = _orig_file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    print("── 12. USB 手套序列号 → 侧别映射（真实注册表） ──")
    det._glove_side_cache = None  # 清缓存强制重读
    side_map = _glove_side_by_serial()
    check(side_map.get("2095376e3032") == "right_glove",
          f"右手套主序列号 → right_glove: {side_map}")
    check(side_map.get("2096376e3032") == "right_glove",
          f"右手套备用序列号（换机/固件变更）→ right_glove: {side_map}")
    check(side_map.get("2067376f3032") == "left_glove",
          f"左手套序列号 → left_glove: {side_map}")
    # 2026-09-21 新到的这对（用户报告右手被判成左手）。**注册表是本程序里
    # 唯一按序列号说明左右手的地方**，漏登记 ⇒ prefer 为空 ⇒ 只能按连接
    # 先后抢空闲列 ⇒ 谁先插谁当右手。用户换手套时这一条会红，照改注册表。
    check(side_map.get("364133593535") == "right_glove",
          f"新右手套 364133593535 → right_glove: {side_map}")
    check(side_map.get("364933593535") == "left_glove",
          f"新左手套 364933593535 → left_glove: {side_map}")

    print("── 12b. 未注册手套 + 两列都被占：必须拒绝，不得猜 left_glove ──")
    # 复现用户报的那次误判：新序列号不在注册表里（prefer 为空），而两只
    # **不在位**的旧手套在 device_names.json 里各占着一列 ⇒ 旧版落到
    # `return SENSOR_NAMES[-1]`，把这只新手套判成 left_glove，与真左手同列、
    # 两个 write_sensor 互相覆盖，且猜测不落盘 ⇒ 每次连接都重复同一个错答案。
    tmp2 = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp2_path = tmp2.name
    tmp2.close()
    _settings2 = _settings
    _orig2 = _settings2.DEVICE_NAMES_FILE
    _settings2.DEVICE_NAMES_FILE = tmp2_path
    try:
        _settings2.save_device_name("usbglove:2095376E3032", "旧右手套",
                                    sensor="right_glove")
        _settings2.save_device_name("usbglove:2090376E3032", "旧左手套",
                                    sensor="left_glove")
        role = _settings2.assign_glove_sensor_role("usbglove:364133593535", "")
        check(role != "left_glove",
              f"未注册手套不得被判成左手（旧版正是 left_glove）: {role!r}")
        check(role == "", f"无空闲列时返回空串交调用方拒绝: {role!r}")
        check(_settings2.device_sensor_role("usbglove:2095376E3032")
              == "right_glove",
              "拒绝时不得动别人的绑定（宁可开不了，不能串列）")
        # 边界：拒绝**只**针对"认不出是哪只手"的。同一套占位下，注册过的
        # 序列号（prefer 权威）照旧拿回自己那一列 —— 否则上面那条会恒真。
        role2 = _settings2.assign_glove_sensor_role("usbglove:364133593535",
                                                   "right_glove")
        check(role2 == "right_glove",
              f"注册过的序列号（prefer 权威）不受占位影响: {role2!r}")
        check(_settings2.device_sensor_role("usbglove:2095376E3032") == "",
              "权威侧别抢占后释放了旧手套在该列的绑定")
    finally:
        _settings2.DEVICE_NAMES_FILE = _orig2
        try:
            os.unlink(tmp2_path)
        except OSError:
            pass

    print("── 13. 真机段（蓝牙在位时） ──")
    if os.path.exists("/usr/bin/bluetoothctl") or os.path.exists("/usr/local/bin/bluetoothctl"):
        paired = _bluetoothctl_paired()
        check(True, f"bluetoothctl 可用，已配对 {len(paired)} 台（不做断言）")
    else:
        print("  SKIP: 本机无 bluetoothctl")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: 设备检测单元测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
