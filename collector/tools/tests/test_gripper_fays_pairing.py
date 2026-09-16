#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""夹爪扫描新逻辑 —— 离线自检（不碰真设备/真 SDK/真串口）。

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_gripper_fays_pairing.py

背景：2026-09-14 厂商交付了新的扫描逻辑（`设备快速扫描与新扫描逻辑.md`），
Fays 与 ESP32 的配对不再按 USB 根端口猜归属：

    UVC 组 → 唯一 ESP32（bus + outer_path）→ 读 ESP32 NVS 里的绑定序列号
    （串口命令 QF）→ 按官方 SDK 读到的产品序列号唯一匹配在线 Fays

本测试用假的设备枚举/SDK 探针/串口，逐条验证「失败关闭」：

覆盖:
  1. GripperSerial 的 QF/WF：响应前缀表、STATE 串线不误判、写绑定校验
  2. _match_esp：按 USB serial 唯一匹配；空 serial 拒绝；重复拒绝
  3. _query_esp_bound_fays_serial：正常读取；STATE 后重试；未绑定/ERR 拒绝
  4. _resolve_fays_group：**ESP 与 Fays 根端口故意不同**也必须配上
     （旧逻辑按根端口关联，这里显式回归）
  5. 在线 Fays 的 SDK serial 重复 → 拒绝猜测
  6. 绑定序列号不在线 → 拒绝，且文案带上「正在被占用」的那台
  7. 已被运行中会话占用的 Fays 不参与探测（双夹爪时另一套 rig 在跑）
  8. SuperSpeed 预检在**启动 SDK 之前**做，预检不过不碰 SDK
  9. 完整生命周期探针的序列号与绑定值不一致 → 拒绝连接
 10. acquire() 端到端：新配对链 → 租约字段齐全、release 清理干净
 11. UVC 组关联不再比较 Fays 根端口（跨总线/跨根端口仍唯一关联）
 12. 部署的原生程序确实带上了新参数（--serial-only-fast / --identity-only /
     --topology-only）：Python 传了参数而二进制不认，会让整条扫描链失败
 13. 静态查解包元数：调用点解包的个数必须与被调函数返回的个数一致
     （移植时改小返回值却漏改调用点，只有真机走到那行才炸）

退出码 0 = 全部通过。
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock                                       # noqa: E402

from core.gripper import paths                                  # noqa: E402
from core.gripper import fays_single                            # noqa: E402
from core.gripper.devices import gripper_serial                 # noqa: E402
from core.gripper.devices.gripper_serial import GripperSerial   # noqa: E402
from core.gripper.fays_single import (                          # noqa: E402
    GripperFaysError, SingleFaysLease,
)
from core.gripper.devices.uvc_camera_service import (            # noqa: E402
    UvcCameraServiceError, UvcCameraServiceManager,
)

FAILS = []
ESP_SERIAL = "68:EE:8F:C6:0D:20"
FAYS_SERIAL = "3500000262300089"
OTHER_FAYS_SERIAL = "3500000262300097"


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def section(title):
    print(f"\n[{title}]")


def _esp(serial=ESP_SERIAL, device="/dev/ttyACM0",
         physical="1-2.2.1"):
    return {
        "serial": serial,
        "device": device,
        "physical_usb_path": physical,
        "vendor_id": "303a",
        "product_id": "1001",
    }


def _fays_group(physical, serial_port, imu_port):
    """一台完整 S80M：stereo 接口 00 + IMU 接口 02。"""
    return {
        "physical_usb_path": physical,
        "ports": {
            "stereo_dev_port": serial_port,
            "imu_dev_port": imu_port,
        },
        "stereo": {
            "device": serial_port,
            "physical_usb_path": physical,
            "usb_device_sysfs_path": f"/sys/bus/usb/devices/{physical}",
            "interface_number": "00",
        },
        "imu": {
            "device": imu_port,
            "physical_usb_path": physical,
            "usb_device_sysfs_path": f"/sys/bus/usb/devices/{physical}",
            "interface_number": "02",
        },
        "config_updated": False,
    }


# ═══════════════════════════════════════════════════════════════════
# 1. GripperSerial 的 QF / WF
# ═══════════════════════════════════════════════════════════════════

class _FakeSerial:
    """假串口：``?`` 握手固定回 STATE，其余命令按脚本逐条喂响应。

    None 表示「这一行读到空」（超时/空行），用来复现串口噪声。
    握手必须单独建模 —— 否则 connect() 会把给命令准备的脚本吃掉，
    后面的断言就变成了「其实没连上」的空转。
    """

    def __init__(self, lines):
        self.lines = list(lines)
        self.is_open = True
        self.writes = []
        self._handshake = False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)
        self._handshake = (data == b"?\r\n")

    def readline(self):
        if self._handshake:
            self._handshake = False
            return b"STATE ST=0 RAW=0 PCT=0 Z=0 L=0\r\n"
        if not self.lines:
            return b""
        line = self.lines.pop(0)
        return b"" if line is None else (line + "\r\n").encode()

    def cancel_write(self):
        pass

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False


def _serial_with(lines):
    holder = {}

    def factory(port, baud, **kwargs):
        holder["kwargs"] = kwargs
        holder["serial"] = _FakeSerial(lines)
        return holder["serial"]

    bridge = GripperSerial(serial_factory=factory,
                           logger=lambda *_a, **_k: None)
    return bridge, holder


def test_serial_qf_wf():
    section("1. GripperSerial 的 QF/WF")
    bridge, holder = _serial_with(
        ["STATE ST=1", f"FAYS_SERIAL:{FAYS_SERIAL}"])
    check(bridge.connect("/dev/ttyACM0"), "握手成功（? → STATE）")
    check(bridge.query_fays_serial() == FAYS_SERIAL,
          "QF 跳过插进来的 STATE 行，取到 FAYS_SERIAL:<serial>")
    check(holder["kwargs"].get("exclusive") is True,
          "串口以 exclusive=True 独占打开（被占用要立刻失败而不是抢读）")

    bridge, _ = _serial_with([None, "ERR FAYS_SERIAL_NOT_SET"])
    check(bridge.connect("/dev/ttyACM0"), "握手成功")
    check(bridge.query_fays_serial() == "",
          "未绑定返回空串（不抛错、更不猜）")

    bridge, holder = _serial_with(["ERR"])
    check(bridge.connect("/dev/ttyACM0"), "握手成功")
    check(bridge.query_fays_serial() == "",
          "老固件对未知命令回 ERR 也不当成序列号")

    bridge, holder = _serial_with(
        [None, "OK FAYS_SERIAL_SET:" + FAYS_SERIAL])
    check(bridge.connect("/dev/ttyACM0"), "握手成功")
    check(bridge.set_fays_serial(FAYS_SERIAL) is True, "WF 写入成功返回 True")
    check(holder["serial"].writes[-1]
          == f"WF:{FAYS_SERIAL}\r\n".encode(),
          "写入的是 WF:<serial>")

    bridge, _ = _serial_with(["ERR FAYS_SERIAL_INVALID"])
    check(bridge.connect("/dev/ttyACM0"), "握手成功")
    try:
        bridge.set_fays_serial(FAYS_SERIAL)
        check(False, "ERR 响应应抛错")
    except RuntimeError as exc:
        check("FAYS_SERIAL_INVALID" in str(exc), "ERR 响应抛出带原因的错误")

    bridge, holder = _serial_with([])
    check(bridge.connect("/dev/ttyACM0"), "握手成功")
    for bad in ("", "  ", "has space", "a" * 65, "semi;colon"):
        try:
            bridge.set_fays_serial(bad)
            check(False, f"非法序列号 {bad!r} 应被拒绝")
        except ValueError:
            pass
    check(not any(w.startswith(b"WF:") for w in holder["serial"].writes),
          "非法序列号（空/超长/带空格与分号）在发出去之前就拒绝")


# ═══════════════════════════════════════════════════════════════════
# 2. _match_esp
# ═══════════════════════════════════════════════════════════════════

def test_match_esp():
    section("2. ESP32 只按 USB serial 匹配")
    lease = SingleFaysLease(logger=lambda _t: None)
    with mock.patch.object(fays_single, "discover_esp32_devices",
                           return_value=[_esp()]):
        check(lease._match_esp(ESP_SERIAL)["device"] == "/dev/ttyACM0",
              "唯一 serial 命中")
        try:
            lease._match_esp("")
            check(False, "空 serial 应拒绝")
        except GripperFaysError as exc:
            check("拒绝按端口号或枚举顺序猜测" in str(exc),
                  "空 serial 拒绝，并说明不许按端口/枚举顺序猜")
    with mock.patch.object(
            fays_single, "discover_esp32_devices",
            return_value=[_esp(device="/dev/ttyACM0"),
                          _esp(device="/dev/ttyACM1")]):
        try:
            lease._match_esp(ESP_SERIAL)
            check(False, "重复 serial 应拒绝")
        except GripperFaysError as exc:
            check("未唯一匹配" in str(exc), "重复 serial 拒绝，不做二选一")


# ═══════════════════════════════════════════════════════════════════
# 3. _query_esp_bound_fays_serial
# ═══════════════════════════════════════════════════════════════════

def _bridge_stub(responses, connect_ok=True, last_error=None):
    """假 GripperSerial：send 按调用次数吐 responses。"""
    instance = mock.MagicMock()
    instance.connect.return_value = connect_ok
    instance.last_error = last_error
    instance.send.side_effect = list(responses)
    return instance


def test_query_bound_serial():
    section("3. 读 ESP32 绑定的 Fays 序列号（QF）")
    lease = SingleFaysLease(logger=lambda _t: None)

    with mock.patch.object(fays_single, "GripperSerial",
                           return_value=_bridge_stub([f"FAYS_SERIAL:{FAYS_SERIAL}"])) as cls:
        got = lease._query_esp_bound_fays_serial(_esp())
    check(got == FAYS_SERIAL, "正常返回绑定序列号")
    check(cls.return_value.disconnect.called,
          "查完立刻归还串口（运行时桥接随后还要独占打开）")
    check(cls.call_args.kwargs.get("logger") is not None, "透传日志器")

    with mock.patch.object(fays_single, "GripperSerial",
                           return_value=_bridge_stub(["STATE ST=1",
                                                      "STATE ST=1",
                                                      f"FAYS_SERIAL:{FAYS_SERIAL}"])):
        check(lease._query_esp_bound_fays_serial(_esp()) == FAYS_SERIAL,
              "先收到周期性 STATE → 重试 QF，不误判成未绑定")

    with mock.patch.object(fays_single, "GripperSerial",
                           return_value=_bridge_stub(["STATE ST=1", "STATE ST=1",
                                                      "STATE ST=1"])):
        try:
            lease._query_esp_bound_fays_serial(_esp())
            check(False, "持续只有 STATE 应拒绝")
        except GripperFaysError as exc:
            check("查询 Fays 序列号失败" in str(exc), "持续只有 STATE → 失败关闭")

    with mock.patch.object(fays_single, "GripperSerial",
                           return_value=_bridge_stub(["ERR FAYS_SERIAL_NOT_SET"])):
        try:
            lease._query_esp_bound_fays_serial(_esp())
            check(False, "未绑定应拒绝")
        except GripperFaysError as exc:
            check("WF:<serial>" in str(exc),
                  "未绑定 → 拒绝并给出写入办法（WF:<serial>）")

    with mock.patch.object(fays_single, "GripperSerial",
                           return_value=_bridge_stub([], connect_ok=False,
                                                     last_error="serial open/handshake failed: [Errno 16] busy")):
        try:
            lease._query_esp_bound_fays_serial(_esp())
            check(False, "串口连不上应拒绝")
        except GripperFaysError as exc:
            check("busy" in str(exc), "串口打不开时带上底层原因")

    with mock.patch.object(fays_single, "GripperSerial") as cls:
        try:
            lease._query_esp_bound_fays_serial(_esp(device=""))
            check(False, "缺串口节点应拒绝")
        except GripperFaysError as exc:
            check("缺少串口节点" in str(exc), "缺串口节点拒绝")
        check(not cls.called, "缺节点时根本不构造串口对象")


# ═══════════════════════════════════════════════════════════════════
# 4~8. _resolve_fays_group
# ═══════════════════════════════════════════════════════════════════

def _resolve(lease, esp, groups, serials, precheck_error=None,
             busy=(), bound=FAYS_SERIAL):
    """跑一次 _resolve_fays_group，返回 (结果, 被探测过的端口列表)。"""
    probed = []

    def fake_probe(ports, **kwargs):
        probed.append(ports["stereo_dev_port"])
        value = serials.get(ports["stereo_dev_port"])
        if isinstance(value, Exception):
            raise value
        check(kwargs.get("serial_only_fast") is True,
              f"身份扫描用 serial-only-fast: {ports['stereo_dev_port']}")
        return value

    def fake_guard(port, *, timeout=0.0):
        if port in busy:
            raise RuntimeError(f"设备正在使用或初始化，请稍后重试: {port}")
        return mock.MagicMock()

    with mock.patch.object(fays_single, "discover_esp32_devices",
                           return_value=[esp]), \
         mock.patch.object(fays_single, "discover_fays_device_groups",
                           return_value=list(groups)), \
         mock.patch.object(fays_single, "probe_product_serial",
                           side_effect=fake_probe), \
         mock.patch.object(fays_single, "fays_device_guard",
                           side_effect=fake_guard), \
         mock.patch.object(fays_single, "build_fays_probe_env",
                           return_value={}), \
         mock.patch.object(fays_single, "validate_fays_superspeed",
                           side_effect=precheck_error), \
         mock.patch.object(fays_single, "GripperSerial",
                           return_value=_bridge_stub([f"FAYS_SERIAL:{bound}"])):
        try:
            result = lease._resolve_fays_group(esp)
        except GripperFaysError as exc:
            return exc, probed
    return result, probed


def test_resolve_by_serial():
    section("4. 按 SDK 序列号配对（不按根端口）")
    lease = SingleFaysLease(logger=lambda _t: None)
    # ESP 在控制器 A 的根端口 2，Fays 在**另一个根端口/另一条总线**上：
    # 旧逻辑（同控制器同根端口）在这里必然配不上，新逻辑必须配上。
    group = _fays_group("9-5.1.2", "/dev/video4", "/dev/video5")
    result, probed = _resolve(lease, _esp(physical="1-2.2.1"), [group],
                              {"/dev/video4": FAYS_SERIAL})
    check(not isinstance(result, Exception),
          "ESP 与 Fays 根端口/bus 不同也配对成功（旧根端口规则已移除）")
    if not isinstance(result, Exception):
        matched, bound = result
        check(matched["physical_usb_path"] == "9-5.1.2", "配到的是那台 Fays")
        check(bound == FAYS_SERIAL, "同时返回 ESP32 里的绑定值供复核")

    section("5. 重复 SDK 序列号 → 拒绝猜测")
    two = [_fays_group("1-2.3", "/dev/video0", "/dev/video1"),
           _fays_group("1-5.3", "/dev/video2", "/dev/video3")]
    result, probed = _resolve(lease, _esp(), two,
                              {"/dev/video0": FAYS_SERIAL,
                               "/dev/video2": FAYS_SERIAL})
    check(isinstance(result, Exception), "两台 Fays 报同一序列号 → 失败")
    check("重复产品序列号" in str(result), "文案点明是重复序列号")

    section("6. 绑定序列号不在线 → 拒绝")
    other = _fays_group("1-2.3", "/dev/video0", "/dev/video1")
    result, probed = _resolve(lease, _esp(), [other],
                              {"/dev/video0": OTHER_FAYS_SERIAL})
    check(isinstance(result, Exception), "ESP 绑的那台不在线 → 失败关闭")
    check("拒绝按端口或枚举顺序猜测配对" in str(result), "文案说明不猜归属")
    check(OTHER_FAYS_SERIAL in str(result), "文案列出在线的序列号")

    section("7. 被占用的 Fays 不参与探测（另一套 rig 在跑）")
    busy_group = _fays_group("1-2.3", "/dev/video0", "/dev/video1")
    free_group = _fays_group("1-5.3", "/dev/video6", "/dev/video7")
    result, probed = _resolve(
        lease, _esp(), [busy_group, free_group],
        {"/dev/video6": FAYS_SERIAL}, busy={"/dev/video0"})
    check(probed == ["/dev/video6"],
          f"只探测没被占用的那台（实际探测 {probed}）")
    check(not isinstance(result, Exception), "占用的那台不影响另一台配对")

    result, probed = _resolve(
        lease, _esp(), [busy_group, free_group],
        {"/dev/video6": OTHER_FAYS_SERIAL}, busy={"/dev/video0"})
    check("正在被占用的 Fays" in str(result),
          "配不上时把「被占用、没能探测」的那台写进错误文案")
    check("/dev/video0" in str(result), "文案带上被占用设备的 stereo 节点")

    section("8. SuperSpeed 预检在启动 SDK 之前")
    result, probed = _resolve(
        lease, _esp(), [free_group], {"/dev/video6": FAYS_SERIAL},
        precheck_error=RuntimeError("Fays USB 链路不是 SuperSpeed"))
    check(isinstance(result, Exception), "预检不过 → 失败")
    check(probed == [], "预检不过时**根本没启动 SDK**（不再进厂商探针）")
    check("拒绝启动 SDK" in str(result), "文案说明是预检拦下的")

    result, probed = _resolve(lease, _esp(), [], {})
    check(isinstance(result, Exception) and "未发现完整的 Fays" in str(result),
          "一台完整 S80M 都没有 → 失败")

    section("8b. SDK 探测失败不做拓扑回退")
    result, probed = _resolve(lease, _esp(), [free_group],
                              {"/dev/video6": RuntimeError("probe 崩了")})
    check(isinstance(result, Exception)
          and "拒绝回退到拓扑猜测" in str(result),
          "SDK 探测失败 → 失败关闭，不拿拓扑兜底")


# ═══════════════════════════════════════════════════════════════════
# 9. 完整生命周期复核
# ═══════════════════════════════════════════════════════════════════

def test_full_probe_must_match_binding():
    section("9. 完整探针序列号必须等于 ESP32 绑定值")
    lease = SingleFaysLease(logger=lambda _t: None)
    group = _fays_group("1-2.3", "/dev/video0", "/dev/video1")
    with mock.patch.object(fays_single, "validate_fays_sdk_access",
                           return_value=5000.0), \
         mock.patch.object(fays_single, "probe_product_serial",
                           return_value=OTHER_FAYS_SERIAL), \
         mock.patch.object(fays_single, "build_fays_probe_env",
                           return_value={}):
        try:
            lease._probe_serial(group, expected_serial=FAYS_SERIAL)
            check(False, "序列号不一致应拒绝连接")
        except GripperFaysError as exc:
            check("与 ESP32 绑定值不一致" in str(exc),
                  "完整生命周期读到的序列号与绑定值不符 → 拒绝连接")
            check(OTHER_FAYS_SERIAL in str(exc) and FAYS_SERIAL in str(exc),
                  "文案同时给出绑定值与实际值")
        serial, speed = lease._probe_serial(group,
                                            expected_serial=OTHER_FAYS_SERIAL)
    check(serial == OTHER_FAYS_SERIAL and speed == 5000.0,
          "一致时正常返回 (serial, speed)")


# ═══════════════════════════════════════════════════════════════════
# 10. acquire() 端到端
# ═══════════════════════════════════════════════════════════════════

def test_acquire_end_to_end():
    section("10. acquire() 端到端（新配对链 → 租约）")
    lease = SingleFaysLease(logger=lambda _t: None)
    group = _fays_group("9-5.1.2", "/dev/video4", "/dev/video5")
    workdir = tempfile.mkdtemp(prefix="ksq-test-fays-")
    ipc_dir = None
    fake_tempfile = mock.MagicMock()
    fake_tempfile.mkdtemp.return_value = workdir

    def fake_materialize(ports, destination, sdk_yaml):
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write("stereo_dev_port: %s\n" % ports["stereo_dev_port"])
        return destination

    yamls = ("/tmp/fays_vikit_%s.yaml" % FAYS_SERIAL,
             "/tmp/s80m_%s_stereo_inertial.yaml" % FAYS_SERIAL)
    with mock.patch.object(fays_single, "discover_esp32_devices",
                           return_value=[_esp()]), \
         mock.patch.object(fays_single, "discover_fays_device_groups",
                           return_value=[group]), \
         mock.patch.object(fays_single, "probe_product_serial",
                           side_effect=lambda ports, **kw: FAYS_SERIAL), \
         mock.patch.object(fays_single, "fays_device_guard",
                           return_value=mock.MagicMock()), \
         mock.patch.object(fays_single, "build_fays_probe_env",
                           return_value={}), \
         mock.patch.object(fays_single, "validate_fays_superspeed"), \
         mock.patch.object(fays_single, "validate_fays_sdk_access",
                           return_value=5000.0), \
         mock.patch.object(fays_single, "GripperSerial",
                           return_value=_bridge_stub([f"FAYS_SERIAL:{FAYS_SERIAL}"])), \
         mock.patch.object(fays_single, "materialize_fays_device_config",
                           side_effect=fake_materialize), \
         mock.patch.object(fays_single, "tempfile", fake_tempfile), \
         mock.patch.object(paths, "FAYS_LOCK_DIR",
                           os.path.join(workdir, "locks")), \
         mock.patch.object(paths, "per_device_fays_yamls",
                           return_value=yamls):
        try:
            selected = lease.acquire(ESP_SERIAL)
            check(selected["product_serial"] == FAYS_SERIAL,
                  "租约拿到的是 ESP32 绑定的那台 Fays")
            check(selected["esp_serial"] == ESP_SERIAL, "带上 ESP32 serial")
            check(selected["usb_speed_mbps"] == 5000.0, "带上 USB 链路速度")
            check(selected["sdk_yaml"] == yamls[0]
                  and selected["orb_yaml"] == yamls[1],
                  "标定文件来自 per_device_fays_yamls（逐设备，不复用）")
            check(selected["esp_tty"] == "/dev/ttyACM0", "带上 ESP32 串口节点")
            check(selected["mapping_source"] == "runtime_probe_sdk_serial",
                  "记录身份来源")
            check(os.path.isfile(selected["runtime_config"]),
                  "运行时 yaml 已 materialize 到工作目录")
            check(lease.is_acquired(), "租约已持有")
            ipc_dir = selected["ipc_dir"]
            check(os.path.isdir(ipc_dir), "IPC 目录已建立")
            again = lease.acquire(ESP_SERIAL)
            check(again["product_serial"] == FAYS_SERIAL, "重复 acquire 幂等")
            check(lease.snapshot()["product_serial"] == FAYS_SERIAL,
                  "snapshot 返回当前租约")
        finally:
            lease.release()
            check(not lease.is_acquired(), "release 后租约已释放")
            check(not os.path.isdir(ipc_dir), "release 清掉 IPC 目录")
            check(lease.snapshot() is None, "release 后 snapshot 为空")
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
# 11. UVC 组关联不再看 Fays 根端口
# ═══════════════════════════════════════════════════════════════════

def _sightac(side, port):
    return {
        "side": side,
        "port": port,
        "vid": 0x1234,
        "pid": 0x5678,
        "flash_ok": True,
        "flash_params_ok": True,
        "flash_serial": f"{side}-serial",
        "device_type": "planar",
        "flash_params": {
            "device_type": "planar",
            "fx_params": [1.0] * 5,
            "fy_params": [1.0] * 5,
            "fz_params": [1.0] * 21,
            "fw_params": [1.0] * 9,
            "ft_params": [1.0] * 20,
        },
    }


def test_uvc_association():
    section("11. UVC 组只按 ESP32 关联")
    manager = UvcCameraServiceManager(
        logger=lambda _t: None,
        discovery_binary="/bin/true", service_binary="/bin/true")
    group = {
        "bus": 1,
        "outer_path": "2",
        "valid": True,
        "sightac": [_sightac("left", "1-2.3"), _sightac("right", "1-2.4")],
        "decxin": {"mode_ok": True},
    }
    live_esp = [_esp(physical="1-2.2.1")]
    assignment = {
        "esp32": {"serial": ESP_SERIAL},
        # Fays 在另一条总线/另一个根端口：旧逻辑会在这里报「不在同一根端口」
        "fays": {"product_serial": FAYS_SERIAL,
                 "physical_usb_path": "9-5.1.2"},
    }
    try:
        normalized = manager._associate_group(group, assignment, live_esp)
        check(normalized["esp32_serial"] == ESP_SERIAL, "按 ESP32 serial 关联成功")
        check(normalized["fays_serial"] == FAYS_SERIAL, "assignment 里的 Fays 身份原样带上")
        check(set(normalized["sightac"]) == {"left", "right"},
              "左右由 Flash side 决定")
    except UvcCameraServiceError as exc:
        check(False, f"Fays 根端口不同不该影响 UVC 组关联: {exc}")

    wrong = {"esp32": {"serial": "other-esp"},
             "fays": {"product_serial": FAYS_SERIAL}}
    try:
        manager._associate_group(group, wrong, live_esp)
        check(False, "ESP32 serial 不匹配应拒绝")
    except UvcCameraServiceError as exc:
        check("无法按 ESP32 serial 唯一关联" in str(exc),
              "ESP32 对不上 → 拒绝该组")

    for bad in ({}, {"esp32": {"serial": ESP_SERIAL}},
                {"esp32": {"serial": ESP_SERIAL},
                 "fays": {"product_serial": ""}}):
        try:
            manager._associate_group(group, bad, live_esp)
            check(False, f"残缺 assignment {bad} 应拒绝")
        except UvcCameraServiceError:
            pass
    check(True, "残缺 assignment（缺 ESP serial / Fays serial）全部拒绝")


def _binary_has_usage(path, needle):
    """只在二进制里找用法串，不执行它。

    探针一跑就会初始化厂商 SDK（全局资源，还会跟运行中的采集抢锁），
    而这里要防的回归是「Python 传了参数、部署的二进制根本没见过」——
    参数一旦编译进去就必然留在 .rodata 的 usage 里，读文件足够证明。
    """
    try:
        with open(path, "rb") as stream:
            return needle.encode() in stream.read()
    except OSError:
        return None


def test_deployed_binaries_accept_new_flags():
    section("12. 部署的原生扫描程序支持新参数")

    probe = paths.FAYS_CALIBRATION_PROBE
    found = _binary_has_usage(probe, "[--serial-only-fast] <fays_vikit.yaml>")
    if found is None:
        print(f"   （跳过：{probe} 不存在，native 资源未部署）")
    else:
        # 身份扫描现在每次都带 --serial-only-fast；旧二进制只认 argc==2，
        # 加了参数就退化成 usage + 退出码 1，整条配对链会全线失败。
        check(found, f"探针支持 --serial-only-fast: {probe}")
        check(_binary_has_usage(
            probe, "--serial-only-fast") is True,
            "探针二进制内嵌该参数（不只是用法文案）")

    discovery = paths.DISCOVER_UVC_CONFIG_BINARY
    identity = _binary_has_usage(
        discovery, "[--skip-flash|--identity-only|--topology-only]")
    if identity is None:
        print(f"   （跳过：{discovery} 不存在，native 资源未部署）")
    else:
        # 重新扫描走 identity-only、快速诊断走 topology-only；旧二进制把
        # 这两个参数当未知参数直接 usage + 退出码 2。
        check(identity, f"UVC 发现程序支持三种扫描深度: {discovery}")
        for flag in ("--identity-only", "--topology-only"):
            check(_binary_has_usage(discovery, flag) is True,
                  f"UVC 发现程序内嵌 {flag}")


def test_unpack_arity_matches_returns():
    section("13. 解包元数与被调函数返回元数一致")

    # 移植时把 _assignment_identity 从「ESP serial + Fays serial + Fays 路径」
    # 缩成两元（路径是根端口配对的遗留物），却漏改了两处调用点：静态看没
    # 问题、导入也没问题，只有真机 Connect 走到那一行才炸
    # `not enough values to unpack (expected 3, got 2)`。
    # 这类错在离线环境下没法用真设备发现，只能静态查。
    package = os.path.join(REPO_ROOT, "core", "gripper")
    sources = []
    for dirpath, _dirnames, filenames in os.walk(package):
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                sources.append(os.path.join(dirpath, filename))

    # 函数名 -> 元数集合；同名函数（不同类里的同名方法）合并取值，
    # 只有「全都返回同一个元数」时才拿来判错，避免误报。
    arities = {}
    trees = {}
    for path in sources:
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as exc:                       # pragma: no cover
            check(False, f"{path} 语法错误: {exc}")
            continue
        trees[path] = tree
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            found, uniform = set(), True
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return):
                    if isinstance(sub.value, ast.Tuple) and not any(
                            isinstance(e, ast.Starred) for e in sub.value.elts):
                        found.add(len(sub.value.elts))
                    else:
                        uniform = False          # 返回非元组/None/转发调用
            if uniform and len(found) == 1:
                arities.setdefault(node.name, set()).update(found)

    mismatches = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target, value = node.targets[0], node.value
            if not isinstance(target, ast.Tuple) or not isinstance(
                    value, ast.Call):
                continue
            func = value.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None)
            expected = arities.get(name)
            if expected and len(expected) == 1 and \
                    len(target.elts) not in expected:
                mismatches.append(
                    "{}:{} {}() 返回 {} 值，此处解包 {} 个".format(
                        os.path.relpath(path, REPO_ROOT), node.lineno, name,
                        sorted(expected)[0], len(target.elts)))

    check(not mismatches,
          f"core/gripper 全量 {len(trees)} 个文件解包元数全对"
          + ("" if not mismatches else "：" + "；".join(mismatches)))


def main():
    test_serial_qf_wf()
    test_match_esp()
    test_query_bound_serial()
    test_resolve_by_serial()
    test_full_probe_must_match_binding()
    test_acquire_end_to_end()
    test_uvc_association()
    test_deployed_binaries_accept_new_flags()
    test_unpack_arity_matches_returns()
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
