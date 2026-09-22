#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手套注册表唯一读入口（`core/glove_registry.py`）契约测试:

    venv/bin/python tools/tests/test_glove_registry.py

注册表这一层存在的全部理由是**兜住两种静默失败**，所以断言也围绕它们：

  1. SDK 的 `load_device_registry` 对缺失的 `channel_to_hand` 抛**裸
     ValueError**（不是 `GloveDeviceError`），而 `DeviceManagerEngine.list_devices`
     只 `except GloveDeviceError` ⇒ 一次手改配置就把设备面板整块炸掉。
  2. `_serialize_registry` 写盘只输出 `schema_version` + `left`/`right`，
     `os.replace` 整体覆盖 ⇒ **SDK 的任何写操作都会抹掉文件里的其它键**。
     所以权威文件上禁止调 SDK 写操作 —— 这条用静态扫描钉死。

第 2 条里的"陷阱确实存在"是**前提断言**：厂商哪天改了写盘口径，那条禁令
就该重新评估，而这里会红。不需要真机（有手套时顺带多验几条）。
退出码 0 = 全部通过。
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.glove_registry as reg                       # noqa: E402

FAILS = []
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


class fake_registry:
    """临时把权威路径换成一份内存里的注册表文件。"""

    def __init__(self, doc):
        self.doc = doc
        self.path = ""

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.doc, fh)
        self._real = reg.REGISTRY_PATH
        reg.REGISTRY_PATH = self.path
        return self.path

    def __exit__(self, *exc):
        reg.REGISTRY_PATH = self._real
        try:
            os.unlink(self.path)
        except OSError:
            pass


def main():
    # ── 1. 权威路径：必须是**项目根**那份 ──
    check(os.path.dirname(reg.REGISTRY_PATH) == REPO_ROOT,
          f"REGISTRY_PATH 落在项目根: {reg.REGISTRY_PATH}")
    # 路径问 boot 要（不写死）：SDK 挪地方时这条跟着走，只盯"是不是 SDK 那份"
    import core.glove_sdk_boot as _boot
    sdk_default = os.path.join(_boot.sdk_relpath(), "config", "glove_devices.json")
    check(not reg.REGISTRY_PATH.endswith(sdk_default),
          f"不指向 SDK 自带那份（{sdk_default}，那份两侧 usb_serials 是空的"
          " ⇒ 静默失绑）")

    # ── 2. 真文件能被 SDK 加载（S4 的前提：v1 文件在这里抛裸 ValueError）──
    err = reg.read_raw() and ""
    try:
        import core.glove_sdk_boot as boot
        boot.ensure_sdk()
        from glove_io.device_registry import load_device_registry
        loaded = load_device_registry(reg.REGISTRY_PATH)
        check(True, "权威注册表能被 SDK 的 load_device_registry 加载")
        # 逐序列号比对**文件原文 ⇄ SDK 读回**（而不是写死一份名单：换手套
        # 往数组里加序列号是常规操作，写死会让每次都在这里撞红，逼出"顺手
        # 改成期望值"的习惯 —— 那样这行就再也拦不住真正的折叠丢号）。
        raw = reg.read_raw()
        check(loaded["left"]["usb_serials"] == raw["left"]["usb_serials"]
              and loaded["right"]["usb_serials"] == raw["right"]["usb_serials"]
              and bool(raw["left"]["usb_serials"])
              and bool(raw["right"]["usb_serials"]),
              f"两侧复数序列号折叠正确（文件⇄SDK 读回逐号相同，且非空）: "
              f"left={loaded['left']['usb_serials']} "
              f"right={loaded['right']['usb_serials']}")
        # 2026-09-21 实机那对：右手被判成左手就是因为这两个号不在文件里
        for serial, side in (("364133593535", "right"), ("364933593535", "left")):
            check(serial in raw[side]["usb_serials"],
                  f"{serial} 在注册表 {side} 侧（用户报告的那只右手套）")
        check(loaded["left"]["usb_serial"] == "2067376F3032"
              and loaded["right"]["usb_serial"] == "2095376E3032",
              "SDK 把复数首位派生回单数 usb_serial（legacy 读取方不受影响）")
    except Exception as exc:
        check(False, f"权威注册表加载失败: {type(exc).__name__}: {exc}")

    # ── 3. 三档回退，顺序即优先级 ──
    with fake_registry({"schema_version": 1,
                        "left": {"usb_serial": "AAA"},
                        "right": {"usb_serial": "BBB"}}):
        got = reg.side_by_serial()
        check(got == {"aaa": "left_glove", "bbb": "right_glove"},
              f"v1 单数 usb_serial 能读: {got}")

    with fake_registry({"schema_version": 1,
                        "left": {"usb_serial": "AAA"},
                        "right": {"usb_serial": "BBB"},
                        "extra_usb_serials": {"CCC": "left_glove",
                                              "DDD": "right_glove"}}):
        got = reg.side_by_serial()
        check(got.get("ccc") == "left_glove" and got.get("ddd") == "right_glove",
              f"extra_usb_serials 仍能读（更老的文件不用改）: {sorted(got)}")

    with fake_registry({"schema_version": 2,
                        "left": {"usb_serials": ["AAA", "CCC"]},
                        "right": {"usb_serials": ["BBB"]},
                        "extra_usb_serials": {"CCC": "right_glove"}}):
        got = reg.side_by_serial()
        check(got.get("ccc") == "left_glove",
              f"v2 复数压过 extra_usb_serials（顺序即优先级）: {got}")

    with fake_registry({"left": {"usb_serials": ["AAA"], "usb_serial": "ZZZ"},
                        "right": {}}):
        got = reg.side_by_serial()
        check("zzz" not in got and got.get("aaa") == "left_glove",
              f"复数非空时不回退单数: {got}")

    # 坏文件不得抛（面板 2s 轮询一次，抛一次就整块空）
    with fake_registry({}):
        with open(reg.REGISTRY_PATH, "w", encoding="utf-8") as fh:
            fh.write("{ 不是 JSON")
        check(reg.side_by_serial() == {} and reg.read_raw() == {},
              "损坏的 JSON 返回空 dict 而不是抛异常")

    # ── 4. 裸 ValueError 必须被兜住（本层存在的头号理由）──
    broken = {"schema_version": 1, "left": {"usb_serial": "AAA"},
              "right": {"usb_serial": "BBB"}}          # 缺 channel_to_hand
    with fake_registry(broken):
        # 反向验证：不兜的话它确实会抛，而且是**裸 ValueError**
        raised = ""
        try:
            reg._from_sdk()
        except Exception as exc:
            raised = type(exc).__name__
        check(raised == "ValueError",
              f"反证：绕过兜底直调 SDK 确实抛裸 ValueError（实际 {raised!r}）"
              " ⇒ 下面那条断言不是恒真")

        devs = reg.list_devices()                      # 不得抛
        check(isinstance(devs, list),
              f"缺 channel_to_hand 时 list_devices 不抛，返回 {len(devs)} 个")
        check("channel_to_hand" in reg.last_error(),
              f"原因留在 last_error()（不抛的代价就是得能问）: "
              f"{reg.last_error()[:70]}")

        # 退路必须真的退到了端口枚举，而不是返回空
        from glove_io.device_registry import list_matching_ports
        expected = len([p for p in list_matching_ports()])
        check(len(devs) == expected,
              f"退路枚举到全部端口（{len(devs)} 个，端口总数 {expected}）")

    # 好注册表下 last_error 必须清空（否则"上次失败"永远挂着，等于没信号）
    n = len(reg.list_devices())
    check(reg.last_error() == "",
          f"注册表正常时 last_error() 清空（枚举到 {n} 个）")

    # ── 5. link_kind 收窄：蓝牙 dongle 不进面板 ──
    import glove_io.device_registry as sdk_reg
    from common.usb_cdc import STM32_LINK_IDS

    kinds = {kind for _vid, _pid, kind in STM32_LINK_IDS}
    check("bluetooth" in kinds,
          f"前提成立：SDK 会枚举蓝牙 dongle {sorted(kinds)}（所以收窄不是空操作）")

    class FakePort:
        def __init__(self, device, serial, vid, pid):
            self.device, self.serial_number = device, serial
            self.vid, self.pid, self.location = vid, pid, "1-1"

    # 两个模块各有一份名字：`_from_sdk` 走 sdk_devices 的（它是
    # `from ... import list_matching_ports` 进来的**独立绑定**），
    # `_from_ports` 走 device_registry 的。只 patch 一处会测出"过滤没生效"
    # 这种假失败（实测：只改了 device_registry，SDK 那路照样返回真机端口）。
    import glove_io.devices as sdk_devices
    fake = lambda *a, **k: [                                # noqa: E731
        FakePort("/dev/ttyUSB0", "WIRED", 0x0483, 0x5740),
        FakePort("/dev/ttyUSB1", "DONGLE", 0x0483, 0x2013),
    ]
    real_ports = sdk_reg.list_matching_ports
    real_devices_ports = sdk_devices.list_matching_ports
    sdk_reg.list_matching_ports = fake
    sdk_devices.list_matching_ports = fake
    try:
        wired = reg.list_devices()
        every = reg.list_devices(kinds=None)
        check(len(wired) == 1 and wired[0]["serial"] == "WIRED",
              f"默认只要有线: {[d['serial'] for d in wired]}")
        check(len(every) == 2,
              f"kinds=None 时两种都在（证明过滤真的在起作用）: "
              f"{[d['serial'] for d in every]}")
    finally:
        sdk_reg.list_matching_ports = real_ports
        sdk_devices.list_matching_ports = real_devices_ports

    # ── 6. key 前缀契约（settings / device_names.json 按它索引）──
    check(reg.device_key({"serial": "ABC"}) == "usbglove:ABC",
          "device_key 无序列号时带 serial")
    check(reg.device_key({"serial": "", "device": "/dev/ttyACM0"})
          == "usbglove:/dev/ttyACM0",
          "device_key 无序列号时退回端口路径")
    import config.settings as settings
    src = open(os.path.join(REPO_ROOT, "config", "settings.py"),
               encoding="utf-8").read()
    check("usbglove:" in src,
          "settings.py 确实按 usbglove: 前缀索引（key 口径不能改）")

    # ── 7. 前提：SDK 写操作确实会抹掉未知键 ⇒ 权威文件上禁用 ──
    try:
        from glove_io.device_registry import _serialize_registry
        out = _serialize_registry({
            "schema_version": 2,
            "left": {"usb_serials": ["A"], "note": "手写的注释"},
            "right": {"usb_serials": ["B"]},
            "extra_usb_serials": {"X": "left_glove"},
        })
        # 陷阱**只在顶层**：侧别条目内部是整份 dict 拷过去的，未知键反而留着
        # （第一版把 extra_usb_serials 嵌进 left 去测，断言就反了 —— 边界值得
        # 写死在测试里，将来厂商改了写盘口径这里会红）。
        check("extra_usb_serials" not in out,
              "前提成立：SDK 写盘丢掉**顶层**未知键（extra_usb_serials 就是这么"
              "没的）⇒ 权威文件上禁止调 SDK 写操作")
        check("note" in (out.get("left") or {}),
              "边界：侧别条目**内部**的未知键会保留（陷阱只在顶层）")
    except Exception as exc:
        check(False, f"_serialize_registry 前提断言失败: {exc}")

    # 静态扫描：我们自己的代码里不得出现 SDK 的三个写操作
    banned = ("set_glove_serial", "swap_glove_serials", "clear_glove_bindings")
    hits = []
    for base, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in
                   ("venv", "venv_lite", "glove_sdk", ".git", "node_modules",
                    "__pycache__", "lite_package")]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for fn in banned:
                if re.search(rf"\b{fn}\s*\(", text):
                    hits.append(f"{os.path.relpath(path, REPO_ROOT)}:{fn}")
    check(not hits, f"我方代码不调用 SDK 的注册表写操作: {hits[:3]}")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: 手套注册表读入口契约全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
