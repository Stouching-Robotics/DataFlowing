"""
设备检测模块 —— 统一枚举已连接设备（UVC + RealSense D400 + S80M + 蓝牙）。

列表轮询走 sysfs 只读扫描（不 open 设备、不 test_read）——轮询 <5ms、
录制中安全、被占用的相机不会从列表闪烁消失；open 测试只发生在开关打开后
（CameraWorker / D435Worker / S80M 子进程 / SensorBLEEngine 各自带重连）。

蓝牙两个来源：bluetoothctl 已配对列表（快、只读、每轮可用）+ bleak 主动
发现（慢 ~5s，节流缓存；手套连接中建议 set_ble_scan_suppressed(True) 抑制
扫描，避免挤占 BLE 数据吞吐）。

用法:
    from core.device_detector import DeviceScanner, detect_devices

    scanner = DeviceScanner()
    scanner.scan_finished.connect(on_devices)   # list[DeviceInfo]
    scanner.request_scan()                      # 后台线程扫描
"""

from __future__ import annotations
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, List

from PyQt5.QtCore import QObject, pyqtSignal

from config import settings
from core.camera import list_v4l_devices, _is_sdk_device


@dataclass
class DeviceInfo:
    """已连接设备的一条描述（跨线程经信号传回主线程）。"""
    key: str                          # "uvc:{by-id前缀或索引}" | "d435:{rs serial}" | "s80m:{sn}" | "ble:{MAC}" | "usbglove:{serial}" | "gripper:{serial}"
    kind: str                         # "uvc" | "d435" | "s80m" | "data_ble" | "ble" | "usb_glove" | "gripper"
    display_name: str                 # 设备内部命名（by-id 解码 / rs 权威名 / 蓝牙广播名 / USB 手套）
    serial: str = ""                  # 设备序号（无则为空串）
    video_index: int = -1             # UVC 设备的 /dev/videoN 索引（其它为 -1）
    by_id_path: Optional[str] = None  # /dev/v4l/by-id 永久路径（有则填）
    backend: str = ""                 # 打开后端（开关打开后由 CameraWorker 决定，枚举时为空）
    address: str = ""                 # BLE MAC 地址 / USB 手套串口路径（非 BLE 为空串）
    usb_path: str = ""                # S80 相机的 USB 设备拓扑路径（如 1-3.4.1，spawn 选相机用；其余为空）
    rssi: int = 0                     # BLE 信号强度（排序用）
    user_name: str = ""               # 用户命名（枚举后由 MainWindow 从 device_names.json 填充）

    @property
    def stable_key(self) -> str:
        """持久化用 key：跨插拔稳定（=key）。"""
        return self.key

    @property
    def group(self) -> str:
        """面板分组: "camera" | "glove" | "gripper" | "other_ble"。"""
        if self.kind in ("data_ble", "usb_glove"):
            return "glove"
        if self.kind == "gripper":
            return "gripper"
        if self.kind == "ble":
            return "other_ble"
        return "camera"

    @property
    def label(self) -> str:
        """列表/画面叠加显示名：用户命名优先，回落内部命名。"""
        return self.user_name or self.display_name


def _parse_by_id_entry(entry: str) -> Optional[dict]:
    """解析 /dev/v4l/by-id 条目名 → {prefix, serial, index}。

    形如 usb-X_Y_Z-serial-video-index0；serial 为启发式（最后一段、
    纯字母数字且 ≥8 字符），无法解析返回 None。
    """
    if not entry:
        return None
    parts = entry.rsplit("-video-index", 1)
    prefix = parts[0] if len(parts) == 2 else entry
    index = int(parts[1]) if (len(parts) == 2 and parts[1].isdigit()) else None
    serial = ""
    if prefix.startswith("usb-"):
        segments = [s for s in re.split(r"[-_]", prefix[len("usb-"):]) if s]
        if segments and segments[-1].isalnum() and len(segments[-1]) >= 8:
            serial = segments[-1]
    return {"prefix": prefix, "serial": serial, "index": index}


_REALSENSE_NAME_KW = ("realsense",)


def _is_realsense_name(name: str) -> bool:
    """Windows DShow 设备名命中 RealSense 关键词（D400 全家族走
    pyrealsense2 专用通道，其 UVC 节点不能当普通相机列出）。"""
    nl = (name or "").lower()
    return any(kw in nl for kw in _REALSENSE_NAME_KW)


def _list_uvc_devices_windows(max_index: int) -> List[DeviceInfo]:
    """Windows UVC 枚举：pygrabber DirectShow 设备名。

    索引与 OpenCV CAP_DSHOW 顺序一致（两者都枚举同一个 DirectShow
    视频输入设备类），面板 video_index 可直接用于开关打开。
    pygrabber 缺失时退化按索引 DShow open 探测（只验证可打开不读帧）。
    注意：Windows 无 by-id/sysfs，key 用索引（重插拔索引可能变化）。
    """
    infos: List[DeviceInfo] = []
    names = None
    try:
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()
    except Exception:
        names = None
    if names is not None:
        for i, name in enumerate(names):
            if i >= max_index:
                break
            if _is_realsense_name(name):
                continue
            if not (name or "").strip():
                name = f"USB Camera {i}"
            infos.append(DeviceInfo(
                key=f"uvc:{i}",
                kind="uvc",
                display_name=name.strip(),
                video_index=i,
            ))
        return infos
    # 兜底：pygrabber 未安装 → DShow 索引探测（只开不读，快速释放）
    import cv2
    for i in range(max_index):
        try:
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            ok = cap.isOpened()
            cap.release()
        except Exception:
            ok = False
        if ok:
            infos.append(DeviceInfo(
                key=f"uvc:{i}",
                kind="uvc",
                display_name=f"USB Camera {i}",
                video_index=i,
            ))
    return infos


def _list_uvc_devices(max_index: int = settings.DEVICE_SCAN_MAX_INDEX,
                      *, gripper_hubs=None) -> List[DeviceInfo]:
    """UVC 网络摄像头（排除 RealSense UVC 节点、FTDI SDK 设备与夹爪组件）。

    夹爪组件的 Sightac/DECXIN/FTDI 相机由 libuvc 服务或 Fays 桥接独占，
    绝不作为通用 UVC 相机出现；但只有与控制板同根端口的那颗才算 rig 的，
    单插的 DECXIN 按普通 UVC 放出来（见 _is_gripper_component_camera）。
    Linux: sysfs 只读枚举（/dev/v4l/by-id 分组，轮询安全）。
    Windows: pygrabber DirectShow 枚举。
    """
    if os.name == "nt":
        return _list_uvc_devices_windows(max_index)
    infos: List[DeviceInfo] = []
    for d in list_v4l_devices(max_index):
        if d.get("is_sdk"):
            continue
        if d.get("is_realsense"):
            continue
        if _is_gripper_component_camera(d, gripper_hubs=gripper_hubs):
            continue
        # key 用 by-id 前缀（跨插拔稳定）；同型号同序列号两台并存时 udev 的
        # by-id 链接名唯一、被对方顶掉，退化为 USB 拓扑路径（同样跨重启
        # 稳定），最后才退到可能漂移的 video 索引。
        # by_id_ambiguous＝该前缀被多台共用，**链接归属会在重新枚举时翻转**
        # （谁后注册谁拿到），此时前缀本身就不再稳定 —— 拿到链接的那台也得
        # 放弃它，否则同一台相机在两个 key 之间跳、面板上设备消失又出现。
        prefix = str(d["video_index"])
        if d.get("by_id_path") and not d.get("by_id_ambiguous"):
            prefix = os.path.basename(d["by_id_path"]).rsplit("-video-index", 1)[0]
        elif d.get("usb_path"):
            prefix = f"usb-{d['usb_path']}"
        infos.append(DeviceInfo(
            key=f"uvc:{prefix}",
            kind="uvc",
            display_name=d["name"] or f"USB Camera {d['video_index']}",
            serial=d.get("serial", ""),
            video_index=d["video_index"],
            by_id_path=d.get("by_id_path"),
        ))
    return infos


def _list_d435_devices() -> List[DeviceInfo]:
    """pyrealsense2 权威枚举（名称 + 序列号）；无设备/未安装返回 []。"""
    try:
        import pyrealsense2 as rs
        ctx = rs.context()
        infos: List[DeviceInfo] = []
        for dev in ctx.query_devices():
            try:
                if "D400" not in dev.get_info(rs.camera_info.product_line):
                    continue
                name = dev.get_info(rs.camera_info.name)
                serial = dev.get_info(rs.camera_info.serial_number)
                infos.append(DeviceInfo(
                    key=f"d435:{serial}",
                    kind="d435",
                    display_name=name or "Intel RealSense D435",
                    serial=serial,
                ))
            except Exception:
                continue
        return infos
    except Exception:
        return []


def _ftdi_camera_groups() -> List[dict]:
    """sysfs 只读扫描 FTDI Superspeed Video Bridge 节点 → 按物理相机分组。

    每台 S80 相机 = 一个 FTDI USB 设备（接口 1.0 = 双目对、1.2 = IMU），
    多台接入时按 USB 设备拓扑路径分开。返回每台
    {"usb_path", "serial", "stereo_index"}（按节点名排序，不 open 设备，
    轮询安全）；无 FTDI 返回 []。
    """
    v4l_root = "/sys/class/video4linux"
    if not os.path.isdir(v4l_root):
        return []
    groups: dict = {}
    order: List[str] = []
    for name in sorted(os.listdir(v4l_root)):
        vp = os.path.join(v4l_root, name)
        try:
            with open(os.path.join(vp, "name"), encoding="utf-8") as f:
                if "FTDI Superspeed Video Bridge" not in f.read().strip():
                    continue
            # device 符号链接指向 USB 接口（…/1-3.4.1:1.0），basename 即
            # 接口名；父级 1-3.4.1 为 USB 设备节点，serial 文件在其下
            iface = os.path.basename(os.readlink(os.path.join(vp, "device")))
        except OSError:
            continue
        if not re.match(r".+:(\d+\.\d+)$", iface):
            continue
        usb_path = iface.rsplit(":", 1)[0]
        serial = ""
        try:
            with open(os.path.join("/sys/bus/usb/devices", usb_path, "serial"),
                      encoding="utf-8") as f:
                serial = f.read().strip()
        except OSError:
            pass
        idx = int(name[5:]) if name.startswith("video") and name[5:].isdigit() else -1
        if usb_path not in groups:
            groups[usb_path] = {"usb_path": usb_path, "serial": serial,
                                "stereo_index": -1}
            order.append(usb_path)
        # 双目对取接口 1.0 的第一个节点（与 read_stereo_rgb.py 口径一致）
        if iface.endswith(":1.0") and groups[usb_path]["stereo_index"] < 0:
            groups[usb_path]["stereo_index"] = idx
    return [groups[p] for p in order]


def _find_s80m_by_id(video_index: int) -> Optional[str]:
    """S80M 双目节点的 /dev/v4l/by-id 永久路径（无则 None）。"""
    by_id_dir = "/dev/v4l/by-id"
    if not os.path.isdir(by_id_dir):
        return None
    for entry in sorted(os.listdir(by_id_dir)):
        p = os.path.join(by_id_dir, entry)
        try:
            if os.readlink(p) == f"../../video{video_index}":
                return p
        except OSError:
            continue
    return None


def _list_s80m_devices(max_index: int = settings.DEVICE_SCAN_MAX_INDEX) -> List[DeviceInfo]:
    """每台 FTDI 命中一条 S80M 条目（多台相机按 USB 序列号区分）。

    序列号从 USB 设备节点 serial 文件读取（FTDI 出厂默认 000000000001，
    可能多台同号 → key 追加 USB 拓扑路径兜底唯一）；无序列号时同样以
    USB 路径兜底。usb_path 供 spawn 传 --device-path 精确选中该相机。
    sysfs 扫描无果时退回旧版 _is_sdk_device 单条逻辑（老环境兜底）。
    """
    cams = _ftdi_camera_groups()
    if cams:
        # 同序列号多台 → key 用 "s80m:{sn}@{usb_path}" 兜底唯一
        seen: dict = {}
        for c in cams:
            seen[c["serial"]] = seen.get(c["serial"], 0) + 1
        infos: List[DeviceInfo] = []
        for c in cams:
            if not (0 <= c["stereo_index"] < max_index):
                continue
            sn, path = c["serial"], c["usb_path"]
            if sn and seen[sn] == 1:
                key = f"s80m:{sn}"
            else:
                key = f"s80m:{sn}@{path}" if sn else f"s80m:usb-{path}"
            infos.append(DeviceInfo(
                key=key,
                kind="s80m",
                display_name=f"FaysSense S80M ({sn or path})",
                serial=sn,
                video_index=c["stereo_index"],
                by_id_path=_find_s80m_by_id(c["stereo_index"]),
                usb_path=path,
            ))
        return infos
    # 兜底：sysfs 名称不符的老环境退回旧版单条逻辑（key 保持 s80m:ftdi）
    for i in range(max_index):
        if not _is_sdk_device(i):
            continue
        return [DeviceInfo(
            key="s80m:ftdi",
            kind="s80m",
            display_name="FaysSense S80M",
            serial="",
            video_index=i,
            by_id_path=_find_s80m_by_id(i),
        )]
    return []


# ── 蓝牙枚举 ──────────────────────────────────────────
BLE_DISCOVERY_INTERVAL_S = 20.0        # bleak 主动发现节流间隔
BLE_DISCOVERY_TIMEOUT_S = 5.0          # 单次 discover 超时
_ble_discovery_cache = {"ts": 0.0, "devices": []}   # [(name, mac, rssi)]
_ble_discovery_lock = threading.Lock()
_ble_scan_suppressed = False            # 手套连接中由 MainWindow 置 True


def set_ble_scan_suppressed(on: bool):
    """抑制 bleak 主动发现（手套连接中防扫描挤占数据吞吐）。

    只影响主动发现；bluetoothctl 已配对列表是只读系统调用，不受影响。
    """
    global _ble_scan_suppressed
    _ble_scan_suppressed = bool(on)


def _mac_norm(mac: str) -> str:
    """MAC 归一化：大写、冒号分隔（bluetoothctl 与 bleak 格式统一）。"""
    return re.sub(r"[^0-9A-Fa-f]", ":", mac or "").replace("::", ":").strip(":").upper()


def _ble_discover() -> List[tuple]:
    """bleak 主动发现（节流缓存；线程内调用，5s 阻塞）。"""
    now = time.monotonic()
    with _ble_discovery_lock:
        if now - _ble_discovery_cache["ts"] <= BLE_DISCOVERY_INTERVAL_S:
            return list(_ble_discovery_cache["devices"])
        _ble_discovery_cache["ts"] = now
    result: List[tuple] = []
    try:
        import asyncio
        from bleak import BleakScanner
        loop = asyncio.new_event_loop()
        try:
            found = loop.run_until_complete(
                BleakScanner.discover(timeout=BLE_DISCOVERY_TIMEOUT_S))
            for d in found:
                rssi = None
                try:
                    rssi = d.rssi
                except Exception:
                    rssi = getattr(getattr(d, "details", None), "rssi", None)
                result.append((d.name or "", _mac_norm(d.address),
                               int(rssi) if rssi is not None else -999))
        finally:
            loop.close()
    except Exception:
        result = []
    with _ble_discovery_lock:
        _ble_discovery_cache["devices"] = result
    return list(result)


def _bluetoothctl_paired() -> dict:
    """bluetoothctl devices 已配对列表（只读；不可用返回 {}）。"""
    paired: dict = {}
    try:
        out = subprocess.run(["bluetoothctl", "devices"], capture_output=True,
                             text=True, timeout=5)
        for line in out.stdout.splitlines():
            m = re.match(r"Device\s+(\S+)\s+(.+)$", line.strip())
            if m:
                paired[_mac_norm(m.group(1))] = m.group(2).strip()
    except Exception:
        pass
    return paired


_GLOVE_NAMES = {"l", "r", "left", "right", "l_glove", "r_glove",
                "left_glove", "right_glove"}


def _is_glove_name(name: str) -> bool:
    """广播名判手套：历史 "Matrix…" 或单字母 L/R（现役手套固件）。"""
    n = (name or "").strip().lower()
    return "matrix" in n or n in _GLOVE_NAMES


def _list_ble_devices() -> List[DeviceInfo]:
    """蓝牙设备：bluetoothctl 已配对 + bleak 主动发现，按 MAC 去重合并。

    广播名含 "Matrix"/"L"/"R" 判为手套（data_ble），其余为普通 BLE
    （other_ble）；device_names.json 里已绑定 sensor 列的 MAC 永远按手套
    对待（连过一次即持久化，改名/空名不丢）。手套连接中
    （_ble_scan_suppressed）跳过主动发现，防扫描挤占数据吞吐。
    """
    merged: dict = {}
    for mac, name in _bluetoothctl_paired().items():
        merged[_mac_norm(mac)] = {"name": name, "rssi": 0}
    if not _ble_scan_suppressed:
        for name, mac, rssi in _ble_discover():
            mac = _mac_norm(mac)
            if mac in merged:
                # 配对名优先；无配对名时用广播名
                if not merged[mac]["name"]:
                    merged[mac]["name"] = name
            else:
                merged[mac] = {"name": name, "rssi": rssi}
    infos: List[DeviceInfo] = []
    for mac, d in merged.items():
        name = d["name"] or "BLE Device"
        is_glove = _is_glove_name(name) or bool(
            settings.device_sensor_role(f"ble:{mac}"))
        infos.append(DeviceInfo(
            key=f"ble:{mac}",
            kind="data_ble" if is_glove else "ble",
            display_name=name,
            serial=mac,
            address=mac,
            rssi=d["rssi"],
        ))
    return infos


def detect_devices(max_index: int = settings.DEVICE_SCAN_MAX_INDEX) -> List[DeviceInfo]:
    """六段枚举（UVC + D435 + S80M + BLE + USB 手套 + UMI 夹爪），各自容错。"""
    devices: List[DeviceInfo] = []
    gripper_devices: List[DeviceInfo] = []
    try:
        gripper_devices = _list_gripper_devices()
    except Exception:
        gripper_devices = []
    try:
        # rig 的组件相机（DECXIN/Sightac/FT602）归 libuvc 服务独占，不进通用
        # 列表；只有与控制板同根端口的那颗才算 rig 的，单插的 DECXIN 放行。
        devices += _list_uvc_devices(
            max_index, gripper_hubs=gripper_root_hubs(gripper_devices)
        )
    except Exception:
        pass
    try:
        devices += _list_d435_devices()
    except Exception:
        pass
    if not gripper_devices:
        # 单设备架设：夹爪在场时其 s80m 只经 ORB 桥接进程打开，
        # 隐藏通用 S80M 条目，避免同一 FT602 被两个通道双开。
        try:
            devices += _list_s80m_devices(max_index)
        except Exception:
            pass
    try:
        devices += _list_ble_devices()
    except Exception:
        pass
    try:
        devices += _list_usb_glove_devices()
    except Exception:
        pass
    devices += gripper_devices
    return devices


# ── USB (Type-C) 手套 ──────────────────────────────────

_GLOVE_USB_VID = 0x0483
_GLOVE_USB_PID = 0x5740
_GLOVE_SIDE_NAMES = {"left_glove": "USB 手套·左手",
                     "right_glove": "USB 手套·右手"}
_glove_side_cache: Optional[dict] = None
_glove_side_cache_time = 0.0


def _glove_side_by_serial() -> dict:
    """读工具包 glove_devices.json → {usb_serial小写: "left_glove"/"right_glove"}。

    优先项目根下同级的 stouch_glove_toolkit* 目录，其次项目根自身；
    文件缺失/解析失败返回 {}（左右手仍可经首连分配后持久化）。
    除 left/right 的 usb_serial 外，还合并可选的 extra_usb_serials
    映射（{序列号: "left_glove"/"right_glove"}）——支持同一手侧多只
    手套（换机/固件序列号变更后旧号仍能认出来）。
    结果缓存 10s，避免 2s 轮询反复读盘。
    """
    global _glove_side_cache, _glove_side_cache_time
    now = time.monotonic()
    if _glove_side_cache is not None and now - _glove_side_cache_time < 10.0:
        return _glove_side_cache
    result: dict = {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(root, "glove_devices.json")]
    try:
        candidates += sorted(
            os.path.join(root, name, "glove_devices.json")
            for name in os.listdir(root)
            if name.lower().startswith("stouch_glove_toolkit")
        )
    except OSError:
        pass
    import json
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for side, role in (("left", "left_glove"), ("right", "right_glove")):
            serial = (data.get(side) or {}).get("usb_serial", "")
            if serial:
                result[str(serial).strip().lower()] = role
        for serial, role in (data.get("extra_usb_serials") or {}).items():
            if role in ("left_glove", "right_glove") and serial:
                result.setdefault(str(serial).strip().lower(), role)
        if result:
            break
    _glove_side_cache = result
    _glove_side_cache_time = now
    return result


def usb_glove_prefer_side(serial: str) -> str:
    """USB 手套序列号 → 期望传感器列名（未知返回空串）。"""
    return _glove_side_by_serial().get((serial or "").strip().lower(), "")


def _list_usb_glove_devices() -> List[DeviceInfo]:
    """枚举 STM32 USB CDC 手套（0483:5740，串口工具按 VID/PID 过滤）。"""
    try:
        import serial.tools.list_ports
    except ImportError:
        return []
    side_map = _glove_side_by_serial()
    infos: List[DeviceInfo] = []
    for port in serial.tools.list_ports.comports():
        if port.vid != _GLOVE_USB_VID or port.pid != _GLOVE_USB_PID:
            continue
        serial = (port.serial_number or "").strip()
        key = f"usbglove:{serial}" if serial else f"usbglove:{port.device}"
        side = side_map.get(serial.lower(), "")
        infos.append(DeviceInfo(
            key=key,
            kind="usb_glove",
            display_name=_GLOVE_SIDE_NAMES.get(side, "USB 手套"),
            serial=serial,
            address=port.device,
        ))
    return infos


# ── UMI 夹爪 ──────────────────────────────────────────

_GRIPPER_USB_VID = 0x303A   # Espressif ESP32-S3（CDC-ACM 控制板）
_GRIPPER_USB_PID = 0x1001
# 夹爪组件相机（Sightac 触觉 ×2 / DECXIN RGB / Fays FT602），libuvc 服务与
# Fays 桥接独占，绝不进通用 UVC 列表
_GRIPPER_COMPONENT_UVC = {("0c45", "636f"), ("1bcf", "2d4f"), ("0403", "602e")}
_GRIPPER_COMPONENT_TOKENS = ("0c45_636f", "1bcf_2d4f", "0403_602e",
                             "sightac", "decxin", "ftdi superspeed")
# 组件里唯一有独立用途的一颗：DECXIN 单插时跑 USB-DECXIN--- 手部关键点任务
# （v1.3.0 前它是普通 UVC 相机，device_names.json 里还留着 "DECXIN_head"）。
# rig 上那颗与 Sightac 一起挂在控制板的根端口下，单插那颗在别的根端口 ——
# 真机实测：控制板 1-2.2.1、rig 的 DECXIN 1-2.2.2（同属根端口 1-2），
# 单插的 DECXIN 1-5。原生 discover-uvc-config 按同一拓扑分组，把单插那颗
# 标成 ungrouped。Sightac/FT602 不放开：前者单插无用，后者另有 is_sdk 兜底。
_GRIPPER_STANDALONE_UVC = {("1bcf", "2d4f")}
_GRIPPER_STANDALONE_TOKENS = ("1bcf_2d4f", "decxin")


def _usb_root_hub_from_sysfs(sysfs_node: str) -> Optional[str]:
    """从 sysfs 设备节点取 USB 根端口（"1-2.2.1" → "1-2"）；取不到返回 None。

    与 core/gripper/devices/usb_camera_sets.py 的 _root_hub_from_physical
    同一约定。此处自带一份，是因为本模块必须能在 Windows 上 import（见
    core/gripper_codec.py 的 fcntl 陷阱），不反向依赖 core.gripper。
    """
    try:
        resolved = os.path.realpath(sysfs_node)
    except OSError:
        return None
    for component in reversed(resolved.split(os.sep)):
        physical = component.split(":", 1)[0]
        match = re.fullmatch(r"(\d+-\d+)(?:\.\d+)*", physical)
        if match is not None:
            return match.group(1)
    return None


def _v4l_root_hub(video_index) -> Optional[str]:
    """V4L2 节点（/dev/videoN）所在的 USB 根端口。"""
    if video_index is None:
        return None
    return _usb_root_hub_from_sysfs(
        f"/sys/class/video4linux/video{video_index}/device"
    )


def _tty_root_hub(device_path: str) -> Optional[str]:
    """ttyACM 控制板所在的 USB 根端口；用来定位夹爪 rig 占了哪个根端口。"""
    basename = os.path.basename(str(device_path or "").strip())
    if not re.fullmatch(r"ttyACM\d+", basename):
        return None
    return _usb_root_hub_from_sysfs(f"/sys/class/tty/{basename}/device")


def gripper_root_hubs(gripper_devices) -> set:
    """夹爪控制板占用的 USB 根端口集合；空集＝本次没接 rig。

    rig 的 DECXIN/Sightac 与控制板同根端口，据此把单插的 DECXIN 从夹爪
    组件的排除名单里摘出来（见 _is_gripper_component_camera）。
    """
    hubs = set()
    for info in gripper_devices or ():
        hub = _tty_root_hub(getattr(info, "address", "") or "")
        if hub:
            hubs.add(hub)
    return hubs


def _is_gripper_component_camera(d: dict, *, gripper_hubs=None) -> bool:
    """按 VID/PID 识别夹爪组件相机；VID/PID 缺失时用 by-id 字符串兜底。

    gripper_hubs 是控制板占用的根端口集合（gripper_root_hubs()）:
      None     → 未知，一律按组件处理（保守，维持旧契约）
      set()    → 没接 rig，没有 libuvc 服务独占 → 可独立使用的 DECXIN 放行
      {"1-2"}  → 只放行不在这些根端口下的 DECXIN，即单插的那颗
    相机自身根端口取不到时同样按组件处理 —— 宁可藏，不可与 rig 双开。
    """
    releasable = gripper_hubs is not None
    hubs = set(gripper_hubs or ())
    vid = str(d.get("vid") or "").lower()
    pid = str(d.get("pid") or "").lower()
    if not (vid and pid):
        by_id = (d.get("by_id_path") or "").lower()
        name = (d.get("name") or "").lower()
        if releasable and any(
            token in by_id or token in name
            for token in _GRIPPER_STANDALONE_TOKENS
        ):
            hub = _v4l_root_hub(d.get("video_index"))
            if hub is not None and hub not in hubs:
                return False
        return any(
            token in by_id or token in name
            for token in _GRIPPER_COMPONENT_TOKENS
        )
    key = (vid, pid)
    if key in _GRIPPER_STANDALONE_UVC and releasable:
        hub = _v4l_root_hub(d.get("video_index"))
        if hub is not None and hub not in hubs:
            return False
    return key in _GRIPPER_COMPONENT_UVC


def _list_gripper_devices() -> List[DeviceInfo]:
    """枚举 UMI 夹爪控制板（303A:1001）；原生资源缺失时隐藏整类。

    资源缺失（core/gripper/native/ 未随包交付）返回 []，夹爪条目与组件
    相机排除同时消失，主程序退化为纯手套模式（P0 验证：改名 native/
    后条目消失）。
    """
    try:
        from core.gripper import paths
        if not paths.gripper_resources_available():
            return []
    except Exception:
        return []
    try:
        import serial.tools.list_ports
    except ImportError:
        return []
    infos: List[DeviceInfo] = []
    for port in serial.tools.list_ports.comports():
        if port.vid != _GRIPPER_USB_VID or port.pid != _GRIPPER_USB_PID:
            continue
        serial = (port.serial_number or "").strip()
        key = f"gripper:{serial}" if serial else f"gripper:{port.device}"
        infos.append(DeviceInfo(
            key=key,
            kind="gripper",
            display_name="UMI 夹爪",
            serial=serial,
            address=port.device,
        ))
    return infos


class DeviceScanner(QObject):
    """后台线程扫描设备（sysfs 只读），经排队信号回主线程。

    带 _busy 守卫：扫描未完成时 request_scan 直接返回，防止轮询堆积。
    """

    scan_finished = pyqtSignal(list)   # list[DeviceInfo]

    def __init__(self, parent=None, max_index: int = None):
        super().__init__(parent)
        self._max_index = (max_index if max_index is not None
                           else settings.DEVICE_SCAN_MAX_INDEX)
        self._busy = False
        self._busy_lock = threading.Lock()
        self._stop = False

    def request_scan(self):
        """请求一次扫描；进行中则忽略（防轮询堆积）。"""
        with self._busy_lock:
            if self._busy or self._stop:
                return
            self._busy = True
        threading.Thread(target=self._run_scan, daemon=True,
                         name="device-scan").start()

    def _run_scan(self):
        try:
            devices = detect_devices(self._max_index)
            if not self._stop:
                self.scan_finished.emit(devices)
        finally:
            with self._busy_lock:
                self._busy = False

    def stop(self):
        """停止接受新扫描（在途扫描完成后自然退出）。"""
        with self._busy_lock:
            self._stop = True
