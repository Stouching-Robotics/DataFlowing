#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""夹爪配对与串口诊断 —— 离线自检（不碰真设备/真 SDK/真串口）。

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_gripper_esp_binding.py

背景：接新的夹爪时主程序报
``ESP32 AC:27:6E:C7:BA:68 串口连接失败，无法查询绑定的 Fays 序列号:
serial open/handshake failed: Write timeout``，用户无法判断板子到底是坏了、
没烧固件还是串口被别的进程占着。为此主程序夹爪右键菜单加了两个入口：

  * ESP32 串口诊断 —— 只读地把板上原样输出打出来，回答「板子在不在说话」
  * 读取/写入 Fays 绑定序列号 —— 读 ``QF`` / 写 ``WF:``，新板子不必再开
    厂商工具包 GUI

本测试用假串口/假设备枚举/假单板租约，逐条钉住「失败关闭 + 如实汇报」：

覆盖:
  1. GripperSerial.read_lines：只读收集、跳过空行、上限、串口没开的空返回
  1b. open_readonly / handshake：只读打开一个字节都不写、能收 TX 侧输出、
     随后可补握手；没打开就握手如实报错
  2. esp_binding_status：已绑定 / 未绑定（不算失败）/ 读不通带底层原文 /
     缺串口节点；任何路径都不抛异常，且总是归还串口
  3. online_fays_serials：枚举口径与 acquire 一致；无完整设备组时明确报错
  4. write_bound_serial：缺节点/连不上/写失败都带原因抛出；回读空值不谎报成功
  5. esp_console_report：连上才发 ? 与 QF（不多发命令）；连不上如实带串口层原文，
     并且**仍然只读复检一次**（写阻塞时也只有这条路能拿到 TX 侧证据）
  6. 主窗口离线骨架：前置检查、暂时关闭与开回、录制中不给入口
  7. 主窗口绑定流程：探板失败不开回；没找到在线 Fays 就停手；挑目标 → 写入 →
     回读一致才说成功
  8. 主窗口串口诊断：原文进日志、摘要进弹窗；只读操作一律开回
  9. 面板信号名与主窗口 connect 的名字一致（信号名打错就永远不触发）
  10. 「板子没在服务串口」的处置提示：写超时/无应答族才追加，口被占/没权限/
     缺节点不加（主机侧原因不该被引到重烧固件上去）

退出码 0 = 全部通过。
"""

from __future__ import annotations

import os
import sys
from functools import partial

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock                                       # noqa: E402

import serial                                                   # noqa: E402

from core.gripper import fays_single                            # noqa: E402
from core.gripper.devices.gripper_serial import GripperSerial   # noqa: E402
from core.gripper.fays_single import (                          # noqa: E402
    GripperFaysError, SingleFaysLease,
)

FAILS = []
ESP_SERIAL = "AC:27:6E:C7:BA:68"
FAYS_SERIAL = "3500000262300089"
OTHER_FAYS_SERIAL = "3500000262300097"
WRITE_TIMEOUT = "serial open/handshake failed: Write timeout"


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def section(title):
    print(f"\n[{title}]")


def _esp(serial=ESP_SERIAL, device="/dev/ttyACM0", physical="1-2.2.1"):
    return {
        "serial": serial,
        "device": device,
        "physical_usb_path": physical,
        "vendor_id": "303a",
        "product_id": "1001",
    }


def _fays_group(physical="1-2.3", stereo="/dev/video4", imu="/dev/video5"):
    """一台完整 S80M（stereo 接口 00 + IMU 接口 02）。"""
    return {
        "physical_usb_path": physical,
        "ports": {"stereo_dev_port": stereo, "imu_dev_port": imu},
        "stereo": {"device": stereo, "physical_usb_path": physical,
                   "usb_device_sysfs_path": f"/sys/bus/usb/devices/{physical}",
                   "interface_number": "00"},
        "imu": {"device": imu, "physical_usb_path": physical,
                "usb_device_sysfs_path": f"/sys/bus/usb/devices/{physical}",
                "interface_number": "02"},
        "config_updated": False,
    }


class _Clock:
    """可推进的假时钟：read_lines 的截止时间是按秒算的，真等会拖慢测试。"""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class _ScriptedSerial:
    """假串口：握手固定回 STATE，其余行按脚本逐条喂。

    None 表示「这一行读到空」（CDC 超时/空行）；``boom_at`` 之后 readline
    抛 IOError，用来复现「板子中途掉线」。每次读都推进假时钟，所以脚本耗尽
    后 read_lines 会在假时间里走到截止，而不是死循环。
    """

    def __init__(self, lines, clock, *, step=0.1, boom_at=None):
        self._lines = list(lines)
        self._clock = clock
        self._step = step
        self._boom_at = boom_at
        self._reads = 0
        self.is_open = True
        self.writes = []
        self._handshake = False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)
        self._handshake = (data == b"?\r\n")

    def readline(self):
        self._clock.advance(self._step)
        self._reads += 1
        if self._boom_at is not None and self._reads > self._boom_at:
            raise IOError("device disconnected")
        if self._handshake:
            self._handshake = False
            return b"STATE ST=0 RAW=0 PCT=0 Z=0 L=0\r\n"
        if not self._lines:
            return b""
        line = self._lines.pop(0)
        return b"" if line is None else (line + "\r\n").encode()

    def cancel_write(self):
        pass

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False


def _read_bridge(lines, *, step=0.1, boom_at=None):
    """返回 (已握手的 GripperSerial, 假串口, 假时钟)。"""
    clock = _Clock()
    holder = {}
    fake = _ScriptedSerial(lines, clock, step=step, boom_at=boom_at)

    def factory(port, baud, **kwargs):
        holder["port"] = port
        return fake

    bridge = GripperSerial(serial_factory=factory, clock=clock,
                           sleep=lambda _s: None,
                           logger=lambda *_a, **_k: None)
    check(bridge.connect("/dev/ttyACM0"), "先决条件：假串口握手成功")
    return bridge, fake, clock


# ═══════════════════════════════════════════════════════════════════
# 1. GripperSerial.read_lines
# ═══════════════════════════════════════════════════════════════════

def test_read_lines():
    section("1. GripperSerial.read_lines（只读诊断）")
    bridge, fake, _clock = _read_bridge(
        ["STATE ST=0 RAW=1", None, "DATA 1 2 3", ""])
    before = list(fake.writes)
    lines = bridge.read_lines(3.0)
    check(lines == ["STATE ST=0 RAW=1", "DATA 1 2 3"],
          f"收集到板上主动输出的整行、跳过空行：{lines}")
    check(fake.writes == before,
          "只读：除握手那条 ? 外不再往板子写任何命令")

    bridge, _fake, _c = _read_bridge([None, None])
    check(bridge.read_lines(1.0) == [],
          "板子一行都不吐 → 空列表（＝没有程序在写这个口）")

    bridge, _fake, _c = _read_bridge([f"L{i}" for i in range(50)])
    check(len(bridge.read_lines(10.0, limit=5)) == 5,
          "limit 封顶：行数上限生效，不会把内存读爆")

    bridge, _fake, _c = _read_bridge(["A", "B", "C"], boom_at=3)
    check(bridge.read_lines(3.0) == ["A", "B"],
          "中途掉线：已收到的行仍然如实返回（不吞掉证据）")

    bridge, _fake, _c = _read_bridge([])
    bridge.disconnect()
    check(bridge.read_lines(1.0) == [],
          "串口没打开时返回空列表（不抛异常、不偷偷开端口）")


def test_open_readonly():
    section("1b. open_readonly / handshake（写阻塞时仍能拿到 TX 侧证据）")
    clock = _Clock()
    fake = _ScriptedSerial(["OTA AP: ESP32_OTA  IP: 192.168.4.1", "READY"],
                           clock)

    def factory(port, baud, **kwargs):
        return fake

    bridge = GripperSerial(serial_factory=factory, clock=clock,
                           sleep=lambda _s: None, logger=lambda *_a, **_k: None)
    check(bridge.open_readonly("/dev/ttyACM0"), "只读打开成功")
    check(fake.writes == [],
          f"一个字节都没往板子写（写阻塞的板子也因此读得到）：{fake.writes}")
    lines = bridge.read_lines(3.0)
    check(lines == ["OTA AP: ESP32_OTA  IP: 192.168.4.1", "READY"],
          f"只读打开后能收板上原生输出：{lines}")
    check(fake.writes == [], "收完还是没写过任何命令")
    check(bridge.handshake(), "随后可以在这条已打开的口上补握手")
    check(fake.writes == [b"?\r\n"], f"握手才写出那一条 ?：{fake.writes}")

    # 没打开就握手：如实报错，不去偷偷开端口
    bridge = GripperSerial(serial_factory=factory, clock=clock,
                           sleep=lambda _s: None, logger=lambda *_a, **_k: None)
    check(bridge.handshake() is False
          and "not open" in str(bridge.last_error),
          f"没打开就握手：报错而不是假装成功：{bridge.last_error}")

    # 打开失败：与 connect 同一口径（口被占/权限/节点不在都走这里）
    def busy_factory(port, baud, **kwargs):
        raise IOError(16, "Device or resource busy")

    bridge = GripperSerial(serial_factory=busy_factory, clock=clock,
                           sleep=lambda _s: None, logger=lambda *_a, **_k: None)
    check(bridge.open_readonly("/dev/ttyACM0") is False
          and "Device or resource busy" in str(bridge.last_error),
          f"口被占时只读打开也如实失败：{bridge.last_error}")


# ═══════════════════════════════════════════════════════════════════
# 2-5. SingleFaysLease 的四个对外方法
# ═══════════════════════════════════════════════════════════════════

def _lease_with(responses, *, connect_ok=True, last_error=None, lines=(),
                esp=None, raise_on=None):
    """(假单板租约, 假 GripperSerial 实例)。

    ``raise_on`` 是 ``{方法名: 异常}``：让某一步抛错，验证异常原样上传。
    """
    instance = mock.MagicMock()
    instance.connect.return_value = connect_ok
    instance.last_error = last_error
    instance.send.side_effect = list(responses)
    instance.read_lines.return_value = list(lines)
    for name, exc in (raise_on or {}).items():
        getattr(instance, name).side_effect = exc
    lease = SingleFaysLease(logger=lambda _t: None)
    return lease, instance


def _patch_lease(instance, esp=None):
    """把 fays_single 的板子枚举与 GripperSerial 一起换掉。"""
    return (
        mock.patch.object(fays_single, "discover_esp32_devices",
                          return_value=list(esp if esp is not None
                                            else [_esp()])),
        mock.patch.object(fays_single, "GripperSerial",
                          return_value=instance),
    )


def test_esp_binding_status():
    section("2. esp_binding_status（读现状，未绑定不算失败）")
    lease, instance = _lease_with([f"FAYS_SERIAL:{FAYS_SERIAL}"])
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        status = lease.esp_binding_status(ESP_SERIAL)
    check(status["bound"] == FAYS_SERIAL and status["reason"] == "",
          f"已绑定：{status}")
    check(status["device"] == "/dev/ttyACM0",
          "回报的是实际打开的那个串口节点")
    check(instance.disconnect.called, "读完就把串口还回去")

    lease, instance = _lease_with(["ERR FAYS_SERIAL_NOT_SET"])
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        status = lease.esp_binding_status(ESP_SERIAL)
    check(status["bound"] == "" and status["reason"] == "",
          f"未绑定是正常状态（新板子）：{status}")
    check(status["response"] == "ERR FAYS_SERIAL_NOT_SET",
          "板上原话照样带回，界面可显示")

    lease, instance = _lease_with(["STATE ST=0", "FAYS_SERIAL:" + FAYS_SERIAL])
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p, mock.patch.object(fays_single.time, "sleep"):
        status = lease.esp_binding_status(ESP_SERIAL)
    check(status["bound"] == FAYS_SERIAL,
          "固件插进来的 STATE 行不误判成「未绑定」（重试取真响应）")

    lease, instance = _lease_with(["ERR"])
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        status = lease.esp_binding_status(ESP_SERIAL)
    check(status["bound"] == "" and "查询绑定序列号失败" in status["reason"],
          f"老固件不认 QF → 当读不通（不猜）：{status['reason']}")

    lease, instance = _lease_with([], connect_ok=False,
                                  last_error=WRITE_TIMEOUT)
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        status = lease.esp_binding_status(ESP_SERIAL)
    check(status["reason"] == f"串口连接失败: {WRITE_TIMEOUT}",
          f"连不上时带上串口层原文：{status['reason']}")
    check(not instance.send.called,
          "没连上就不发命令（板子没在服务串口，多写只会更堵）")
    check(status["esp_serial"] == ESP_SERIAL,
          "连不上也回报是哪个板子，用户才知道该拔哪根线")

    lease, instance = _lease_with([])
    esp_p, serial_p = _patch_lease(instance, esp=[_esp(device="")])
    with esp_p, serial_p:
        status = lease.esp_binding_status(ESP_SERIAL)
    check("缺少串口节点" in status["reason"],
          f"节点缺失如实报：{status['reason']}")

    lease, instance = _lease_with([])
    esp_p, serial_p = _patch_lease(instance, esp=[])
    with esp_p, serial_p:
        try:
            lease.esp_binding_status(ESP_SERIAL)
            check(False, "板子不在列表里应拒绝")
        except GripperFaysError as exc:
            check("未唯一匹配" in str(exc),
                  f"板子按 USB serial 唯一匹配，找不到就拒绝：{exc}")


def test_online_fays_serials():
    section("3. online_fays_serials（在线 Fays 枚举口径）")
    lease, _instance = _lease_with([])
    group = _fays_group()
    with mock.patch.object(fays_single, "discover_fays_device_groups",
                           return_value=[group]), \
            mock.patch.object(fays_single, "validate_fays_superspeed"), \
            mock.patch.object(SingleFaysLease, "_device_in_use",
                              return_value=False), \
            mock.patch.object(fays_single, "probe_product_serial",
                              return_value=FAYS_SERIAL), \
            mock.patch.object(fays_single, "build_fays_probe_env",
                              return_value={}), \
            mock.patch.object(fays_single, "validate_fays_sdk_access"):
        by_serial, skipped = lease.online_fays_serials()
    check(list(by_serial) == [FAYS_SERIAL] and skipped == [],
          f"列出在线序列号 → 给界面挑：{list(by_serial)}")
    check(by_serial[FAYS_SERIAL] is group, "序列号映射回设备组（写入后要靠它）")

    lease, _instance = _lease_with([])
    with mock.patch.object(fays_single, "discover_fays_device_groups",
                           return_value=[]):
        try:
            lease.online_fays_serials()
            check(False, "没有完整 Fays 设备组时应明确报错")
        except GripperFaysError as exc:
            check("未发现完整的 Fays S80M" in str(exc),
                  f"无设备组时明说缺什么（stereo + IMU 都要在）：{exc}")


def test_write_bound_serial():
    section("4. write_bound_serial（写 NVS + 回读）")
    lease, instance = _lease_with([])
    instance.query_fays_serial.return_value = FAYS_SERIAL
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        readback = lease.write_bound_serial(ESP_SERIAL, FAYS_SERIAL)
    check(readback == FAYS_SERIAL, f"写完立刻回读：{readback}")
    check(instance.set_fays_serial.call_args == mock.call(FAYS_SERIAL),
          "写的是用户挑的那一台（WF:<serial>）")
    check(instance.disconnect.called, "写完就把串口还回去")

    lease, instance = _lease_with([])
    instance.query_fays_serial.return_value = ""
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        readback = lease.write_bound_serial(ESP_SERIAL, FAYS_SERIAL)
    check(readback == "", "回读为空就是空（不谎报写入成功）")

    lease, instance = _lease_with([], connect_ok=False,
                                  last_error=WRITE_TIMEOUT)
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        try:
            lease.write_bound_serial(ESP_SERIAL, FAYS_SERIAL)
            check(False, "连不上时应抛出")
        except GripperFaysError as exc:
            check(WRITE_TIMEOUT in str(exc) and ESP_SERIAL in str(exc),
                  f"连不上带板子与串口层原文：{exc}")
    check(not instance.set_fays_serial.called,
          "没连上就不发 WF（避免半条命令留在 FIFO 里）")

    lease, instance = _lease_with([])
    esp_p, serial_p = _patch_lease(instance, esp=[_esp(device="")])
    with esp_p, serial_p:
        try:
            lease.write_bound_serial(ESP_SERIAL, FAYS_SERIAL)
            check(False, "缺串口节点时应抛出")
        except GripperFaysError as exc:
            check("缺少串口节点" in str(exc), f"缺节点如实报：{exc}")

    lease, instance = _lease_with(
        [], raise_on={"set_fays_serial": RuntimeError(
            "ESP32 写入 Fays 序列号失败: ERR FAYS_SERIAL_INVALID")})
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        try:
            lease.write_bound_serial(ESP_SERIAL, "bad serial")
            check(False, "板上拒绝写入时应抛出")
        except RuntimeError as exc:
            check("FAYS_SERIAL_INVALID" in str(exc),
                  f"板上回执原文上传给界面：{exc}")
    check(instance.disconnect.called, "写失败也要归还串口（finally 路径）")


def test_esp_console_report():
    section("5. esp_console_report（只读诊断）")
    lease, instance = _lease_with(
        ["STATE ST=0 RAW=0", f"FAYS_SERIAL:{FAYS_SERIAL}"],
        lines=["STATE ST=0 RAW=0", "DATA 1 2 3"])
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        report = lease.esp_console_report(ESP_SERIAL)
    check(report["connected"] is True, "连上标记为真")
    check(report["state"] == "STATE ST=0 RAW=0", "带回 ? 的原样响应")
    check(report["bound"] == f"FAYS_SERIAL:{FAYS_SERIAL}",
          "带回 QF 的原样响应（界面原样显示，不再解析一遍）")
    check(report["lines"] == ["STATE ST=0 RAW=0", "DATA 1 2 3"],
          "带回随后板上主动输出的整行")
    check(report["esp_serial"] == ESP_SERIAL
          and report["device"] == "/dev/ttyACM0"
          and report["physical_usb_path"] == "1-2.2.1",
          "身份三件套齐全（序列号/节点/物理口）")
    check(report["seconds"] == 3.0, "默认收 3 秒")
    check(instance.send.call_args_list == [mock.call("?"), mock.call("QF")],
          f"诊断只发 ? 与 QF 两条（不额外发命令）："
          f"{instance.send.call_args_list}")
    check(instance.disconnect.called, "诊断完归还串口")

    lease, instance = _lease_with([], connect_ok=False,
                                  last_error=WRITE_TIMEOUT,
                                  lines=["OTA AP: ESP32_OTA  IP: 192.168.4.1"])
    esp_p, serial_p = _patch_lease(instance)
    with esp_p, serial_p:
        report = lease.esp_console_report(ESP_SERIAL)
    check(report["connected"] is False, "连不上标记为假")
    check(report["last_error"] == WRITE_TIMEOUT,
          f"串口层原文原样带出（这就是用户报的那句）：{report['last_error']}")
    check(report["lines"] == [], "没连上就没有输出行")
    check(report["state"] == "" and report["bound"] == "",
          "没连上不填假的握手/绑定值")
    check(instance.open_readonly.called,
          "握手失败后仍然只读复检一次：写阻塞的板子只有这条路能拿到证据")
    check(report["read_only_lines"] == ["OTA AP: ESP32_OTA  IP: 192.168.4.1"],
          f"只读复检收到的行带回给界面（板子在说话）：{report['read_only_lines']}")
    check(report["read_only_error"] == "",
          "只读复检成功就没有额外错误")
    check(not instance.send.called,
          "没连上不发任何命令（只读复检也不写字节）")
    check(instance.disconnect.called, "失败路径也要归还串口")

    lease, instance = _lease_with([])
    esp_p, serial_p = _patch_lease(instance, esp=[_esp(device="")])
    with esp_p, serial_p:
        report = lease.esp_console_report(ESP_SERIAL)
    check(report["last_error"] == "缺少串口节点，无法打开"
          and report["connected"] is False,
          f"节点缺失如实报：{report['last_error']}")
    check(not instance.open_readonly.called,
          "连节点都没有就不去试着打开（省一次无意义的失败）")


class _WriteBlocksSerial:
    """写就抛 SerialTimeoutException 的假串口 —— 复现现场那块板子。

    USB-Serial-JTAG 的收端没人排空时，主机这侧的 write 就是这样失败的；
    此时 read 这条路仍然通（板子说了话就收得到）。
    """

    def __init__(self, lines=()):
        self.is_open = True
        self.writes = []
        self._lines = list(lines)

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)
        raise serial.SerialTimeoutException("Write timeout")

    def readline(self):
        if not self._lines:
            return b""
        return (self._lines.pop(0) + "\r\n").encode()

    def cancel_write(self):
        pass

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False


def test_esp_console_report_write_blocked():
    section("5b. 写阻塞的板子：真 GripperSerial 走通只读复检（拿得到 TX 证据）")
    clock = _Clock()
    blocked = _WriteBlocksSerial()
    # 第一次打开（握手那次）写就超时；只读复检那次换成一块在说话的板子
    replier = _ScriptedSerial(["OTA AP: ESP32_OTA  IP: 192.168.4.1", "READY"],
                              clock)
    opened = []

    def factory(port, baud, **kwargs):
        opened.append(port)
        return blocked if len(opened) == 1 else replier

    logs = []
    # lease 把 driver 的 logger 原样交给 GripperSerial，所以在这一层接日志
    lease = SingleFaysLease(
        logger=lambda *a: logs.append(" ".join(str(x) for x in a)))
    with mock.patch.object(fays_single, "discover_esp32_devices",
                           return_value=[_esp()]), \
            mock.patch.object(fays_single, "GripperSerial",
                              partial(GripperSerial, serial_factory=factory,
                                      clock=clock, sleep=lambda _s: None)):
        report = lease.esp_console_report(ESP_SERIAL)
    check(report["connected"] is False, "写阻塞 → 连不上")
    # 日志必须能自己说清「卡在哪一写」：只写进去一笔就阻塞时，上面一行
    # attempt 都不会出现，事后看日志会误判成「根本没试过握手」。
    stuck = [line for line in logs if "握手中断于第 1 次写" in line]
    check(len(stuck) == 1 and "Write timeout" in stuck[0],
          f"日志记下中断在第几次写、原因是什么：{logs}")
    check(not any("connect attempt" in line for line in logs),
          f"第一笔写就阻塞 ⇒ 一条 attempt 行都不该有：{logs}")
    check(report["last_error"] == WRITE_TIMEOUT,
          f"串口层原文就是用户报的那句：{report['last_error']}")
    check(blocked.writes == [b"?\r\n"],
          f"握手只写到一半（第一次写就超时）：{blocked.writes}")
    check(opened == ["/dev/ttyACM0", "/dev/ttyACM0"],
          f"失败后重新只读打开了同一个节点：{opened}")
    check(report["read_only_lines"] == ["OTA AP: ESP32_OTA  IP: 192.168.4.1",
                                        "READY"],
          f"只读复检拿到板子 TX 侧的原样输出：{report['read_only_lines']}")
    check(report["read_only_error"] == "",
          f"只读复检成功：{report['read_only_error']}")
    check(replier.writes == [],
          f"只读复检一个字节都没写（板子收不了，写了只会更堵）：{replier.writes}")

    # 只读复检自己都打不开（被抢口/没权限）：如实报，不冒充「板子哑了」
    clock = _Clock()
    blocked = _WriteBlocksSerial()
    opened = []

    def busy_factory(port, baud, **kwargs):
        opened.append(port)
        if len(opened) == 1:
            return blocked
        raise IOError(16, "Device or resource busy")

    lease = SingleFaysLease(logger=lambda _t: None)
    with mock.patch.object(fays_single, "discover_esp32_devices",
                           return_value=[_esp()]), \
            mock.patch.object(fays_single, "GripperSerial",
                              partial(GripperSerial, serial_factory=busy_factory,
                                      clock=clock, sleep=lambda _s: None)):
        report = lease.esp_console_report(ESP_SERIAL)
    check(report["last_error"] == WRITE_TIMEOUT,
          f"板子那条原文不被复检的失败顶掉：{report['last_error']}")
    check("Device or resource busy" in report["read_only_error"]
          and report["read_only_lines"] == [],
          f"只读复检打不开时单独带自己的原因：{report['read_only_error']}")


# ═══════════════════════════════════════════════════════════════════
# 6-8. 主窗口离线操作
# ═══════════════════════════════════════════════════════════════════

class _Recorder:
    """替身信号：直接记录 emit 参数，不经 Qt 队列（本用例只查流程与文案）。"""

    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _Boxes:
    """替身弹窗：记录标题/正文，返回值由用例预设。"""

    def __init__(self):
        self.calls = []
        # 与真 Qt 一样是可按位或的整数（实现里写 Yes|No 当按钮组合）
        self.Yes, self.No = 1, 0
        self.question_answer = self.Yes

    def _record(self, level, title, text):
        self.calls.append((level, title, text))

    def question(self, _parent, title, text, *_a, **_k):
        self._record("question", title, text)
        return self.question_answer

    def warning(self, _parent, title, text):
        self._record("warning", title, text)

    def critical(self, _parent, title, text):
        self._record("critical", title, text)

    def information(self, _parent, title, text):
        self._record("information", title, text)

    def texts(self):
        return " | ".join(text for _lvl, _t, text in self.calls)

    def titles(self):
        return " | ".join(title for _lvl, title, _t2 in self.calls)

    def levels(self):
        return [level for level, _t, _t2 in self.calls]


def _fake_window(*, recording=False, workers=None):
    """只带离线操作所需状态的主窗口（绕过 Qt 初始化）。

    三个跨线程信号换成 :class:`_Recorder`：实例属性遮蔽类上的 pyqtSignal，
    省掉线程与事件循环，用例可直接读 emit 参数。
    """
    import ui.main_window as main_window

    window = main_window.MainWindow.__new__(main_window.MainWindow)
    logs = []
    window._log = logs.append
    window._logs = logs
    window._workers = dict(workers or {})
    window._active_device_keys = set()
    window._gripper_recalib_reopen = set()
    window._pipeline = mock.MagicMock(is_recording=recording)
    window._device_panel = mock.MagicMock()
    window._device_panel.device_for_key.return_value = None
    window._gripper_recalibration_done = _Recorder()
    window._gripper_binding_probe_done = _Recorder()
    window._gripper_binding_write_done = _Recorder()
    window._gripper_diagnostics_done = _Recorder()
    window._closed = []
    window._close_gripper = window._closed.append
    # 状态栏刷新要 grid 等控件（本用例只查「有没有刷新」）
    window._status_updates = []
    window._update_status = lambda: window._status_updates.append(True)
    window._toggled = []
    window._on_device_toggled = lambda dev, on: window._toggled.append(
        (dev.key, on))
    return window


def _gripper_dev(serial=ESP_SERIAL, key="gripper:AC276EC7BA68"):
    from core.device_detector import DeviceInfo
    return DeviceInfo(key=key, kind="gripper", display_name="UMI",
                      serial=serial)


def _install_lease(window, lease):
    """把主窗口后台线程里的 SingleFaysLease 换成假租约，并同步执行线程体。"""
    starts = []

    def starter(worker, dev, label, name, extra=()):
        starts.append((name, label) + tuple(extra))   # (线程名, 标签[, 目标])
        worker(dev, label, *extra)      # 同步跑，省掉线程与 Qt 队列

    window._start_gripper_worker = starter
    window._starts = starts
    return mock.patch.object(fays_single, "SingleFaysLease",
                             return_value=lease)


def test_window_offline_bookkeeping():
    section("6. 主窗口离线骨架（前置检查 / 让出设备 / 开回）")
    window = _fake_window()
    boxes = _Boxes()
    import ui.main_window as main_window

    dev = _gripper_dev(serial="")
    with mock.patch.object(main_window, "QMessageBox", boxes):
        check(window._gripper_offline_guard(dev, "重读标定") is False,
              "没读到 ESP32 序列号 → 不给进（定位不到控制板）")
    check(boxes.levels() == ["warning"] and "夹爪无序列号" in boxes.calls[0][1],
          f"并弹窗说明原因：{boxes.calls}")

    window = _fake_window(recording=True)
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes):
        check(window._gripper_offline_guard(_gripper_dev(), "改写绑定序列号")
              is False, "录制中 → 不给进（设备要独占）")
    check("录制中不可改写绑定序列号，请先停止录制。" in boxes.texts(),
          f"录制中的文案按动作区分：{boxes.texts()}")

    window = _fake_window()
    with mock.patch.object(main_window, "QMessageBox", _Boxes()):
        check(window._gripper_offline_guard(_gripper_dev(), "做串口诊断")
              is True, "都满足时放行")

    # 本来就没开：什么都不记，也不该替用户打开
    window = _fake_window()
    window._gripper_offline_take_over(_gripper_dev(), "UMI — x", "重读标定")
    check(window._closed == [] and not window._gripper_recalib_reopen,
          "设备本来没开 → 不关也不记待开回")
    check(window._reopen_after_offline("gripper:AC276EC7BA68", False) is False,
          "没记待开回 → 善后不动设备")

    # 开着：关掉并记下待开回（与用户点开关同一口径）
    dev = _gripper_dev()
    window = _fake_window(workers={dev.key: {"kind": "gripper"}})
    window._device_panel.device_for_key.return_value = dev
    window._gripper_offline_take_over(dev, "UMI — x", "重读标定")
    check(window._closed == [dev.key], "开着 → 先关掉让出串口")
    check(window._gripper_recalib_reopen == {dev.key}, "记下完成要开回")
    check(dev.key not in window._active_device_keys, "不再算当前显示设备")
    check(window._status_updates, "状态栏同步刷新（不让用户看着还是「运行中」）")
    check(window._device_panel.set_checked.call_args == mock.call(dev.key, False),
          "面板勾选状态同步取消")
    check(any("已暂时关闭以重读标定" in line for line in window._logs),
          f"日志说明为什么关：{window._logs}")
    check(window._gripper_reopen_forget(dev.key) is True,
          "取出待开回标记")
    check(window._gripper_reopen_forget(dev.key) is False,
          "取出即消费（回调重入不会开两次）")

    window = _fake_window(workers={dev.key: {"kind": "gripper"}})
    window._device_panel.device_for_key.return_value = dev
    window._gripper_recalib_reopen = {dev.key}
    check(window._reopen_after_offline(dev.key, True) is True
          and window._toggled == [(dev.key, True)],
          "善后按用户点开关那条路开回来")

    window = _fake_window()
    window._device_panel.device_for_key.return_value = None
    check(window._reopen_after_offline(dev.key, True) is False,
          "期间设备拔了/列表重建过 → 不擅自开")

    # 探板失败：不开回（板子状态未知，交回用户决定）
    dev = _gripper_dev()
    window = _fake_window(workers={dev.key: {"kind": "gripper"}})
    window._device_panel.device_for_key.return_value = dev
    window._gripper_recalib_reopen = {dev.key}
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes):
        window._on_gripper_binding_probe_done(
            dev.key, "UMI — x", f"串口连接失败: {WRITE_TIMEOUT}", {})
    check(window._toggled == [], "探板失败不自动开回（避免二次弹错）")
    check(not window._gripper_recalib_reopen, "失败也清掉待开回记录")
    check("串口连接失败" in boxes.texts() and "夹爪仍在设备列表中" in boxes.texts(),
          f"失败弹窗带底层原因与去向：{boxes.texts()}")
    check("ESP32 串口诊断" in boxes.texts(),
          "失败弹窗指向诊断入口（用户下一步该干什么）")


def test_window_binding_flow():
    section("7. 主窗口绑定流程（探板 → 挑目标 → 写入 → 回读）")
    dev = _gripper_dev()
    boxes = _Boxes()
    import ui.main_window as main_window

    lease, instance = _lease_with([f"FAYS_SERIAL:{FAYS_SERIAL}"])
    instance.query_fays_serial.return_value = FAYS_SERIAL
    instance.set_fays_serial.return_value = True
    window = _fake_window()
    window._device_panel.device_for_key.return_value = dev
    with mock.patch.object(main_window, "QMessageBox", boxes), \
            mock.patch.object(main_window, "QInputDialog") as dialog, \
            _install_lease(window, lease), \
            mock.patch.object(fays_single, "discover_esp32_devices",
                              return_value=[_esp()]), \
            mock.patch.object(fays_single, "GripperSerial",
                              return_value=instance), \
            mock.patch.object(lease, "online_fays_serials",
                              return_value=({FAYS_SERIAL: _fays_group()}, [])):
        dialog.getItem.return_value = (FAYS_SERIAL, True)
        window._on_gripper_binding(dev)
        check(window._starts and window._starts[0][0] == "gripper-binding-probe",
              f"先起探板线程：{window._starts}")
        probe = window._gripper_binding_probe_done.calls
        check(len(probe) == 1 and probe[0][2] == "",
              f"探板成功不报错：{probe}")
        info = probe[0][3]
        check(info["bound"] == FAYS_SERIAL and info["online"] == [FAYS_SERIAL],
              f"探板带回现状 + 在线清单：{info['bound']} / {info['online']}")
        window._on_gripper_binding_probe_done(*probe[0])
        check(dialog.getItem.called, "弹选择框让用户挑（不给手输）")
        check(dialog.getItem.call_args[0][3] == [FAYS_SERIAL],
          f"候选项就是当前在线的 Fays：{dialog.getItem.call_args[0][3]}")
        check(window._starts[-1][0] == "gripper-binding-write",
              f"确认后起写入线程：{window._starts}")
        check(window._starts[-1][2] == FAYS_SERIAL,
              f"写入线程带的是用户挑的那一台：{window._starts[-1]}")
        write = window._gripper_binding_write_done.calls
        check(len(write) == 1 and write[0][2] == "",
              f"写入成功不报错：{write}")
        check(write[0][3]["readback"] == FAYS_SERIAL,
              f"回读值带回主线程：{write[0][3]}")
        window._on_gripper_binding_write_done(*write[0])
    check(instance.set_fays_serial.called, "真的发了 WF:")
    check(boxes.calls[-1][0] == "information"
          and "绑定完成" in boxes.calls[-1][1],
          f"回读一致才说成功（不是警告）：{boxes.calls[-1]}")
    check(any("已写入" in line and FAYS_SERIAL in line
              for line in window._logs), f"日志留痕：{window._logs}")

    # 回读对不上：明说不算成功（不猜）
    dev = _gripper_dev()
    window = _fake_window()
    window._device_panel.device_for_key.return_value = dev
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes):
        window._on_gripper_binding_write_done(
            dev.key, "UMI — x", "",
            {"written": FAYS_SERIAL, "readback": ""})
    check(boxes.levels() == ["warning"] and "回读不一致" in boxes.calls[0][1],
          f"回读为空 → 警告而不是成功：{boxes.calls}")
    check(window._toggled == [], "写入异常也不自动开回")

    # 没有在线 Fays：停手，并把夹爪开回来（板子没被改动）
    dev = _gripper_dev()
    window = _fake_window(workers={dev.key: {"kind": "gripper"}})
    window._device_panel.device_for_key.return_value = dev
    window._gripper_recalib_reopen = {dev.key}
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes), \
            mock.patch.object(main_window, "QInputDialog") as dialog:
        window._on_gripper_binding_probe_done(
            dev.key, "UMI — x", "",
            {"esp_serial": ESP_SERIAL, "bound": "", "online": [],
             "online_error": "未发现完整的 Fays S80M"})
    check(not dialog.getItem.called, "没有候选就不弹选择框")
    check("未找到在线 Fays" in boxes.titles() and "stereo 与 IMU" in boxes.texts(),
          f"明说缺什么、怎么补救：{boxes.calls}")
    check(window._toggled == [(dev.key, True)],
          "板子没被改动 → 把原先开着的夹爪开回来")

    # 用户放弃写入：同样开回
    dev = _gripper_dev()
    window = _fake_window(workers={dev.key: {"kind": "gripper"}})
    window._device_panel.device_for_key.return_value = dev
    window._gripper_recalib_reopen = {dev.key}
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes), \
            mock.patch.object(main_window, "QInputDialog") as dialog:
        dialog.getItem.return_value = (FAYS_SERIAL, False)
        window._on_gripper_binding_probe_done(
            dev.key, "UMI — x", "",
            {"esp_serial": ESP_SERIAL, "bound": "", "online": [FAYS_SERIAL]})
    check(window._toggled == [(dev.key, True)], "用户取消 → 开回来")
    check(any("已取消" in line for line in window._logs),
          f"日志记下用户取消：{window._logs}")

    # 确认框里选「否」：也不写
    window = _fake_window()
    window._device_panel.device_for_key.return_value = dev
    boxes = _Boxes()
    boxes.question_answer = boxes.No
    with mock.patch.object(main_window, "QMessageBox", boxes), \
            mock.patch.object(main_window, "QInputDialog") as dialog, \
            _install_lease(window, _lease_with([])[0]):
        dialog.getItem.return_value = (FAYS_SERIAL, True)
        chosen = window._ask_target_fays_serial(
            {"esp_serial": ESP_SERIAL, "bound": "", "online": [FAYS_SERIAL]},
            "UMI — x")
    check(chosen is None, "确认框选否 → 不给写入目标")
    check("覆盖原值" in boxes.texts(), f"确认框写清后果：{boxes.texts()}")

    # 已绑定另一台：默认选中当前那台（最不容易误改）
    window = _fake_window()
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes), \
            mock.patch.object(main_window, "QInputDialog") as dialog:
        dialog.getItem.return_value = (OTHER_FAYS_SERIAL, True)
        window._ask_target_fays_serial(
            {"esp_serial": ESP_SERIAL, "bound": FAYS_SERIAL,
             "online": [FAYS_SERIAL, OTHER_FAYS_SERIAL]}, "UMI — x")
    args = dialog.getItem.call_args[0]
    check(list(args[3]) == [FAYS_SERIAL, OTHER_FAYS_SERIAL]
          and args[4] == 0,
          f"候选项按序列号排序、默认落在当前绑定那台：{args[3:5]}")
    check(FAYS_SERIAL in args[2], f"提示里说明当前绑定：{args[2]}")


def test_window_diagnostics():
    section("8. 主窗口串口诊断（只读，一律开回）")
    import ui.main_window as main_window

    dev = _gripper_dev()
    lease, instance = _lease_with(
        ["STATE ST=0 RAW=0", f"FAYS_SERIAL:{FAYS_SERIAL}"],
        lines=["STATE ST=0 RAW=0", "DATA 1 2 3"])
    window = _fake_window()
    window._device_panel.device_for_key.return_value = dev
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes), \
            _install_lease(window, lease), \
            mock.patch.object(fays_single, "discover_esp32_devices",
                              return_value=[_esp()]), \
            mock.patch.object(fays_single, "GripperSerial",
                              return_value=instance):
        window._on_gripper_diagnostics(dev)
        check(window._starts and window._starts[0][0] == "gripper-diagnostics",
              f"起诊断线程：{window._starts}")
        report = window._gripper_diagnostics_done.calls
        check(len(report) == 1 and report[0][2] == "",
              f"诊断成功不报错：{report}")
        window._on_gripper_diagnostics_done(*report[0])
    raw = [line for line in window._logs if line.startswith("[夹爪串口]")]
    check(raw == ["[夹爪串口] STATE ST=0 RAW=0", "[夹爪串口] DATA 1 2 3"],
          f"板上原样输出逐行进日志：{raw}")
    check(boxes.levels() == ["information"] and "DATA 1 2 3" in boxes.texts(),
          f"摘要弹窗带上输出行：{boxes.texts()}")
    check(any("握手响应" in line for line in window._logs),
          f"日志摘要含握手响应：{window._logs}")

    # 连不上（用户报的那种）：原文 + 只读复检的证据 + 处置梯子
    dev = _gripper_dev()
    lease, instance = _lease_with([], connect_ok=False,
                                  last_error=WRITE_TIMEOUT)
    window = _fake_window()
    window._device_panel.device_for_key.return_value = dev
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes), \
            _install_lease(window, lease), \
            mock.patch.object(fays_single, "discover_esp32_devices",
                              return_value=[_esp()]), \
            mock.patch.object(fays_single, "GripperSerial",
                              return_value=instance):
        window._on_gripper_diagnostics(dev)
        window._on_gripper_diagnostics_done(
            *window._gripper_diagnostics_done.calls[0])
    check(boxes.levels() == ["critical"], "连不上是 critical，不是 information")
    check(WRITE_TIMEOUT in boxes.texts(),
          f"串口层原文原样显示（不翻译、不美化）：{boxes.texts()}")
    check("/dev/ttyACM0" in boxes.texts(),
          f"带上串口节点，用户能确认说的是哪块板：{boxes.texts()}")
    check("只读" in boxes.texts() and "收到 0 行" in boxes.texts(),
          f"报出只读复检收到几行（这才是能分辨病因的证据）：{boxes.texts()}")
    check("一行都没有" in boxes.texts(),
          f"0 行时把「板上没有程序在写这个口」说明白：{boxes.texts()}")
    check("断电" in boxes.texts() and "flash_esp32.sh" in boxes.texts(),
          f"给出从低到高的处置梯子：{boxes.texts()}")
    check(any(WRITE_TIMEOUT in line for line in window._logs),
          f"原文也进日志（可回查）：{window._logs}")
    check(any("只读复检" in line for line in window._logs),
          f"只读复检的结论也进日志：{window._logs}")

    # 板子在说话、只是不收写入：首句的归因要跟着变（不能还说板子哑了）
    dev = _gripper_dev()
    lease, instance = _lease_with([], connect_ok=False,
                                  last_error=WRITE_TIMEOUT,
                                  lines=["STATE ST=0 RAW=7"])
    window = _fake_window()
    window._device_panel.device_for_key.return_value = dev
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes), \
            _install_lease(window, lease), \
            mock.patch.object(fays_single, "discover_esp32_devices",
                              return_value=[_esp()]), \
            mock.patch.object(fays_single, "GripperSerial",
                              return_value=instance):
        window._on_gripper_diagnostics(dev)
        window._on_gripper_diagnostics_done(
            *window._gripper_diagnostics_done.calls[0])
    raw = [line for line in window._logs if line.startswith("[夹爪串口]")]
    check(raw == ["[夹爪串口] STATE ST=0 RAW=7"],
          f"只读复检收到的行逐行进日志：{raw}")
    check("收到 1 行" in boxes.texts() and "STATE ST=0 RAW=7" in boxes.texts(),
          f"摘要里带上只读复检收到的行：{boxes.texts()}")
    check("不接收主机写入" in boxes.texts(),
          f"有输出时归因改成「不收写入」，不说板子哑了：{boxes.texts()}")
    check("一行都没有" not in boxes.texts(),
          "有输出就不说「一行都没有」")

    # 只读操作的失败路径也要开回（板子没被改动）
    dev = _gripper_dev()
    window = _fake_window(workers={dev.key: {"kind": "gripper"}})
    window._device_panel.device_for_key.return_value = dev
    window._gripper_recalib_reopen = {dev.key}
    boxes = _Boxes()
    with mock.patch.object(main_window, "QMessageBox", boxes):
        window._on_gripper_diagnostics_done(
            dev.key, "UMI — x", "定位 ESP32 失败", {})
    check(window._toggled == [(dev.key, True)],
          "诊断只读 → 无论成败都按原样开回")
    check("定位 ESP32 失败" in boxes.texts(), f"失败原因照报：{boxes.texts()}")


def test_serial_stuck_hint():
    section("10. 「板子没在服务串口」的处置提示（该给的给、不该给的不给）")
    import ui.main_window as main_window

    window = _fake_window()

    # 该给：写阻塞（用户在 2026-09-23 报的那条）
    hint = window._gripper_serial_stuck_hint(
        "ESP32 AC:27:6E:C7:BA:68 串口连接失败，无法查询绑定的 Fays 序列号: "
        + WRITE_TIMEOUT)
    check("断电" in hint and "flash_esp32.sh" in hint,
          f"写超时 → 给断电梯子（最便宜的一步排最前）：{hint}")
    check("测试连接" in hint,
          "先让人证明主机能不能连上板子（下载通路），再决定要不要重烧")
    check("--erase" in hint and "NVS" in hint,
          f"提醒 --erase 会擦掉 NVS 里的绑定、烧完要补写：{hint}")
    check("权限问题" in hint,
          "明确排除权限/占用，省掉一轮查 udev 的白工")

    # 该给：写得进去、板子完全不回话（另一种「没在服务串口」的报法）
    hint = window._gripper_serial_stuck_hint(
        "ESP32 X 串口连接失败，无法查询绑定的 Fays 序列号: "
        "serial handshake timeout after 8 attempts")
    check("断电" in hint,
          f"无应答也指向板子（不是主机侧）→ 给同一套梯子：{hint}")

    # 不该给：口被别的进程占 —— 主机侧原因，引到重烧固件是误导
    busy = window._gripper_serial_stuck_hint(
        "串口连接失败: serial open/handshake failed: [Errno 16] "
        "Device or resource busy")
    check(busy == "", f"口被占 → 不加板子侧提示：{busy!r}")
    denied = window._gripper_serial_stuck_hint(
        "串口连接失败: serial open/handshake failed: [Errno 13] "
        "Permission denied: '/dev/ttyACM0'")
    check(denied == "", f"没权限 → 不加板子侧提示：{denied!r}")
    check(window._gripper_serial_stuck_hint("缺少串口节点，无法打开") == "",
          "节点不在 → 不加板子侧提示（那是没插好/插错口）")
    check(window._gripper_serial_stuck_hint("") == "",
          "空原因不加提示（骨架路径出错时不硬塞一段无关的话）")

    # 五个串口失败入口都挂上：写在弹窗正文里，跟着原文一起给
    dev = _gripper_dev()
    window = _fake_window()
    boxes = _Boxes()
    wrapped = ("ESP32 AC:27:6E:C7:BA:68 串口连接失败，无法查询绑定的 Fays "
               "序列号: " + WRITE_TIMEOUT)
    with mock.patch.object(main_window, "QMessageBox", boxes):
        window._on_gripper_error(dev.key, wrapped)
        window._on_gripper_recalibration_done(dev.key, "UMI — x", wrapped)
        window._on_gripper_binding_probe_done(
            dev.key, "UMI — x", "串口连接失败: " + WRITE_TIMEOUT, {})
        window._on_gripper_binding_write_done(
            dev.key, "UMI — x",
            "ESP32 X 串口连接失败，无法写入 Fays 序列号: " + WRITE_TIMEOUT, {})
    check(len(boxes.calls) == 4
          and all("断电" in text for _l, _t, text in boxes.calls),
          f"四条串口失败入口都给下一步（不再只有诊断那条给）："
          f"{[t for _l, t, _x in boxes.calls]}")
    check(all("flash_esp32.sh" in text for _l, _t, text in boxes.calls),
          "四条给的是同一套梯子（口径一致，不各说各的）")
    check(all(WRITE_TIMEOUT in text for _l, _t, text in boxes.calls),
          "原文仍然原样保留在提示前面")


# ═══════════════════════════════════════════════════════════════════
# 9. 信号名跨文件一致
# ═══════════════════════════════════════════════════════════════════

def test_signal_names_are_wired():
    section("9. 面板信号与主窗口的连接名一致")

    # 面板发出信号、主窗口按名 connect：名字打错不会报错，只是永远不触发
    panel_src = open(os.path.join(REPO_ROOT, "ui", "device_panel.py"),
                     encoding="utf-8").read()
    window_src = open(os.path.join(REPO_ROOT, "ui", "main_window.py"),
                      encoding="utf-8").read()
    for name in ("gripper_recalibration_requested",
                 "gripper_binding_requested",
                 "gripper_diagnostics_requested"):
        check(f"{name} = pyqtSignal(" in panel_src,
              f"面板声明了 {name}")
        check(f"self._device_panel.{name}.connect(" in window_src,
              f"主窗口 connect 了 {name}")

    # 线程体签名与 _start_gripper_worker 的调用口径一致（错一个参数＝开了线程就崩）
    import ast
    tree = ast.parse(window_src)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_gripper_recalibration_worker", "_gripper_binding_probe_worker",
                "_gripper_binding_write_worker", "_gripper_diagnostics_worker"):
            found[node.name] = len([a for a in node.args.args
                                    if a.arg != "self"])
    check(found == {"_gripper_recalibration_worker": 2,
                    "_gripper_binding_probe_worker": 2,
                    "_gripper_binding_write_worker": 3,
                    "_gripper_diagnostics_worker": 2},
          f"四个线程体都是 (dev, label[, 目标序列号])：{found}")

    # 三个 done 信号都用 object 载荷：PyQt5 的 str 信号不认 dict
    for sig in ("_gripper_binding_probe_done", "_gripper_binding_write_done",
                "_gripper_diagnostics_done"):
        check(f"{sig} = pyqtSignal(str, str, str, object)" in window_src,
              f"{sig} 用 object 声明 dict 载荷（否则跨线程静默丢参）")


def main():
    test_read_lines()
    test_open_readonly()
    test_esp_binding_status()
    test_online_fays_serials()
    test_write_bound_serial()
    test_esp_console_report()
    test_esp_console_report_write_blocked()
    test_window_offline_bookkeeping()
    test_window_binding_flow()
    test_window_diagnostics()
    test_signal_names_are_wired()
    test_serial_stuck_hint()
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
