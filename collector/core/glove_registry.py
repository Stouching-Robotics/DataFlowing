"""手套注册表（`glove_devices.json`）的**唯一读入口** —— 权威文件在项目根。

为什么要有这一层，而不是各处直接调 SDK：

1. **权威路径只有一个，且必须显式传**。SDK 的 `DeviceManager()` 默认指向它
   自己那份 `tools/glove_sdk/config/glove_devices.json` —— 那份两侧 `usb_serials`
   都是**空数组**，等于没绑任何手套。少传一次路径就是「手套都在，但左右手
   认不出来」这种静默失绑。本模块把「显式指向项目根」变成默认。
2. **SDK 的注册表加载会抛裸 `ValueError`**（不是 `GloveDeviceError`）：
   `load_device_registry` 对缺失的 `channel_to_hand` 拿默认 `[]` 去校验
   ⇒ 长度 0 ≠ 16 ⇒ `common/usb_cdc.py:656` 抛 `ValueError`；而
   `glove_io/devices.py:30` 只 `except GloveDeviceError` ⇒ 它**穿透**
   `DeviceManager.list_devices()`。设备面板一次手改配置就整块炸（实测）。
   本模块一律 `except Exception`，并在 SDK 那条路失败时退回自己枚举。
3. **绝不在权威文件上调 SDK 的写操作**（`bind` / `swap_bindings` /
   `clear_glove_bindings`）：`_serialize_registry` 只输出 `schema_version` +
   `left`/`right` 两个键，`_atomic_write` 又是 `os.replace` 整体覆盖 ⇒
   任何一次绑定写入都会把文件里的**其它键抹掉**。绑定变更继续走我方写入路径
   （`ui/settings_dialog.py` 那条）。

文件格式（v2）里 `usb_serials` 是**复数数组**，第一位是主、其余为备
（换机 / 固件改号后旧号仍认得出）。v1 的单数 `usb_serial` 与更早的
`extra_usb_serials` 仍然能读 —— 见 `side_by_serial()` 的三档回退。
"""

from __future__ import annotations

import json
import os
from typing import Dict, List

#: 权威注册表 = **项目根**那份（本文件在 `core/` 下，故上跳两层）。
REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "glove_devices.json")

#: 侧别 → 传感器列名（口径同 `_GLOVE_SIDE_NAMES`，全程序一致）
SIDE_ROLES = (("left", "left_glove"), ("right", "right_glove"))

#: 上次枚举/读取失败的原因（诊断与测试用；正常时为空串）
_last_error = ""


def last_error() -> str:
    """最近一次 `list_devices()` 里失败的原因（**不抛异常**的代价就是得能问）。

    只由 `list_devices()` 清空：它是唯一会把「SDK 那条路失败了」这条信息
    消化掉的地方。`read_raw` / `side_by_serial` 单独调用时**只置不清** ——
    否则退路里那次成功的读盘会把前面真正的失败原因抹掉，问出来是空串
    （实测踩过：SDK 明明抛了 ValueError，`last_error()` 却报 `""`）。
    """
    return _last_error


def _set_error(detail: str) -> None:
    global _last_error
    _last_error = detail or ""


def read_raw() -> dict:
    """读注册表原文；缺失 / 损坏返回 `{}`。

    **不抛** —— 调用方是 2s 轮询的设备面板，这里抛一次面板就整块空掉。
    失败原因留在 `last_error()` 里。
    """
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        _set_error(f"读取 {REGISTRY_PATH} 失败: {exc}")
        return {}
    if not isinstance(data, dict):
        _set_error(f"{REGISTRY_PATH} 顶层不是对象")
        return {}
    return data


def side_by_serial() -> Dict[str, str]:
    """→ `{序列号小写: "left_glove"/"right_glove"}`；读不到返回 `{}`。

    三档并存，**顺序即优先级**（老文件不用改就能继续用）：

      1. `usb_serials`（v2 复数数组）—— 权威
      2. `usb_serial`（v1 单数）—— 仅在复数缺失或为空时回退
      3. `extra_usb_serials`（`{序列号: 角色}`）—— 更早的格式，优先级最低
         （用 `setdefault`，压不过上面两档）
    """
    result: Dict[str, str] = {}
    data = read_raw()
    for side, role in SIDE_ROLES:
        entry = data.get(side)
        if not isinstance(entry, dict):
            continue
        serials = entry.get("usb_serials")
        if not isinstance(serials, list) or not serials:
            single = entry.get("usb_serial")
            serials = [single] if single else []
        for serial in serials:
            key = str(serial or "").strip().lower()
            if key:
                result[key] = role
    extras = data.get("extra_usb_serials")
    if isinstance(extras, dict):
        for serial, role in extras.items():
            key = str(serial or "").strip().lower()
            if key and role in ("left_glove", "right_glove"):
                result.setdefault(key, role)
    return result


def _from_sdk() -> List[dict]:
    """走 SDK 的 `DeviceManager` 枚举（权威路径显式传）。失败见 `last_error()`。"""
    from core.glove_sdk_boot import ensure_sdk
    err = ensure_sdk()
    if err:
        raise RuntimeError(err)
    from glove_io.devices import DeviceManagerEngine
    manager = DeviceManagerEngine(REGISTRY_PATH)
    return [
        {
            "device": str(dev.device),
            "serial": str(dev.serial_number or "").strip(),
            "vid": int(dev.vid or 0),
            "pid": int(dev.pid or 0),
            "link_kind": str(dev.link_kind or "usb"),
            "side": str(dev.bound_side or ""),
        }
        for dev in manager.list_devices()
    ]


def _from_ports() -> List[dict]:
    """`DeviceManager` 失败时的退路：只枚举端口，侧别用我方容错读法配。

    少的是 SDK 那层绑定解析，多的是「注册表有任何毛病也照样列出手套」——
    面板宁可少个「·左手」后缀，也不能整块空掉。
    """
    from core.glove_sdk_boot import ensure_sdk
    err = ensure_sdk()
    if err:
        raise RuntimeError(err)
    from glove_io.device_registry import link_kind_of, list_matching_ports
    sides = side_by_serial()
    infos: List[dict] = []
    for port in list_matching_ports():
        serial = str(port.serial_number or "").strip()
        infos.append({
            "device": str(port.device),
            "serial": serial,
            "vid": int(port.vid or 0),
            "pid": int(port.pid or 0),
            "link_kind": link_kind_of(port) or "usb",
            "side": sides.get(serial.lower(), ""),
        })
    return infos


def list_devices(kinds=("usb",)) -> List[dict]:
    """枚举手套链路 → `[{device, serial, vid, pid, link_kind, side}]`。

    `kinds` 默认只要**有线**（`"usb"`）：SDK 的 `list_matching_ports()` 把蓝牙
    dongle（`0483:2013`）也一起枚举，比迁移前的 VID/PID 硬过滤**面更宽**；
    蓝牙那条路本次不动，所以在这里按 `link_kind` 收窄 —— 与迁移前行为一致。

    任何异常都吞掉并退回端口枚举，**绝不向上抛**（理由见模块 docstring 第 2 条）。
    """
    _set_error("")          # 唯一清空点：这次枚举里发生的失败才是"当前"失败
    try:
        infos = _from_sdk()
    except Exception as exc:
        _set_error(f"SDK 枚举失败，退回端口枚举: {type(exc).__name__}: {exc}")
        try:
            infos = _from_ports()
        except Exception as exc2:
            _set_error(f"手套枚举不可用: {type(exc2).__name__}: {exc2}")
            return []
    if kinds:
        infos = [d for d in infos if d["link_kind"] in kinds]
    return infos


def device_key(dev: dict) -> str:
    """`DeviceInfo.key` 的构造口径（全程序按 `usbglove:` 前缀索引）。

    没序列号时退回端口路径 —— `data/device_names.json` 与
    `config/settings.py` 的 `key.startswith("usbglove:")` 都按这个前缀匹配。
    """
    serial = (dev.get("serial") or "").strip()
    return f"usbglove:{serial}" if serial else f"usbglove:{dev.get('device', '')}"
