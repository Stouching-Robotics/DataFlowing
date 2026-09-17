"""DECXIN（1bcf:2d4f）RGB 曝光归一化 —— 让每台接上来的夹爪 RGB 都亮着。

画面暗的原因不在采集链，而在**相机自己**：DECXIN 把曝光状态存在机身里
（跨重插保持）。一旦被写成 `auto_exposure=1`（手动）+ `white_balance_automatic=0`，
它就永久停在出厂 156/10000 的积分上。所以「修好 001 那台」对 002 一点用都
没有 —— 那是两台各自存着自己的值；而主程序侧根本没有写入口（libuvc 服务只有
`uvc_set_altsetting_override`；`core.camera._apply_exposure_to` 那套只走 OpenCV
通用相机路径，夹爪 RGB 不经过；UVC 在 UI 上连曝光入口都没有），于是过去每接
一台新夹爪都要人工跑一次：

    v4l2-ctl -d /dev/videoN -c auto_exposure=3,white_balance_automatic=1

这里就是那次人工动作的自动化版本：连夹爪时把所有 1bcf:2d4f 的 DECXIN 读一遍，
不是「自动曝光 + 自动白平衡」就写回去。已是自动则一个字节都不写 —— 既省一次
USB 往返，也不会每连一次夹爪就把用户特意设的手动曝光抹掉。

**必须在 libuvc 服务启动之前调用**：服务一开就把设备从 libusb 拿走，内核
uvcvideo 被 `libusb_detach_kernel_driver()` 摘掉、/dev/videoN 随之注销，之后
所有 V4L2 ioctl 都会失败 —— 这正是「要停掉主程序才能 v4l2-ctl」的由来。
调用点在 `core/gripper/bridge.py` 的 `_open_run`：拿到 Fays 租约之后、
`UvcCameraServiceManager.select()` 之前。

只认 1bcf:2d4f：Sightac 触觉相机（0c45:636f）绝不触碰 —— 它的
AE=1/AWB=0/WBTmp=6500/ET=312 是原厂存储态。

单独使用（主程序已停）：

    venv/bin/python -m core.gripper.decxin_exposure             # 归一化所有 DECXIN
    venv/bin/python -m core.gripper.decxin_exposure --dry-run    # 只看当前值，不写
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import stat
import struct
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

DECXIN_VID = "1bcf"
DECXIN_PID = "2d4f"

# V4L2 常量：数值照 /usr/include/linux/v4l2-controls.h 抄（勿凭记忆改）
#   V4L2_CID_BASE            = V4L2_CTRL_CLASS_USER(0x00980000) | 0x900
#   V4L2_CID_CAMERA_CLASS_BASE = V4L2_CTRL_CLASS_CAMERA(0x009a0000) | 0x900
V4L2_CID_AUTO_WHITE_BALANCE = 0x0098090C        # V4L2_CID_BASE + 12
V4L2_CID_EXPOSURE_AUTO = 0x009A0901             # V4L2_CID_CAMERA_CLASS_BASE + 1

# enum v4l2_exposure_auto_type：只有 1 是手动，其余都是某种自动
V4L2_EXPOSURE_AUTO = 0
V4L2_EXPOSURE_MANUAL = 1
V4L2_EXPOSURE_SHUTTER_PRIORITY = 2
V4L2_EXPOSURE_APERTURE_PRIORITY = 3

# DECXIN 的 auto_exposure 菜单是「1=手动 / 3=光圈优先」，**没有 0**，写 0 会
# EINVAL。所以 3 打头；后面两个是给别家 UVC 菜单布局留的兜底，顺序与
# core.camera._apply_exposure_to 的候选表同源（那边是 0/3/2，因为通用相机
# 大多吃 0，而 DECXIN 这条链上实测可用值是 3）。任一个都不许写手动档。
_EXPOSURE_AUTO_CANDIDATES = (
    V4L2_EXPOSURE_APERTURE_PRIORITY,
    V4L2_EXPOSURE_AUTO,
    V4L2_EXPOSURE_SHUTTER_PRIORITY,
)

# ioctl 号：照 <asm-generic/ioctl.h> 的 _IOWR(type, nr, size) 宏算。
# 期望值 VIDIOC_G_CTRL=0xC008561B / VIDIOC_S_CTRL=0xC008561C /
# VIDIOC_QUERYCTRL=0xC0445624（tools/tests/test_decxin_exposure.py 里钉住了）。
_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS, _IOC_DIRBITS = 8, 8, 14, 2
_IOC_NRSHIFT, _IOC_TYPESHIFT, _IOC_SIZESHIFT, _IOC_DIRSHIFT = 0, 8, 16, 30
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _iowr(type_char: str, number: int, size: int) -> int:
    return (
        ((_IOC_READ | _IOC_WRITE) << _IOC_DIRSHIFT)
        | (size << _IOC_SIZESHIFT)
        | (ord(type_char) << _IOC_TYPESHIFT)
        | (number << _IOC_NRSHIFT)
    )


_VIDIOC_G_CTRL = _iowr("V", 27, 8)       # struct v4l2_control (u32 id, s32 value)
_VIDIOC_S_CTRL = _iowr("V", 28, 8)
# struct v4l2_queryctrl: u32 id, u32 type, u8 name[32], s32 min/max/step/def,
# u32 flags, u32 reserved[2] = 68 字节（全是 4 字节对齐，无填充）
_VIDIOC_QUERYCTRL = _iowr("V", 36, 68)
_QUERYCTRL_SIZE = 68
_V4L2_CTRL_TYPE_MENU = 3

_VIDEO_NODE_RE = re.compile(r"video(\d+)$")

Logger = Callable[[str], None]


class _VideoNode:
    """一个 /dev/videoN 的裸 fd：只做控制类 ioctl，不申请缓冲、不取流。"""

    def __init__(self, path: str):
        self.path = path
        # O_NONBLOCK：控制 ioctl 用不上阻塞语义，且避免设备正在收流时卡在 open
        self._fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def __enter__(self) -> "_VideoNode":
        return self

    def __exit__(self, *_exc) -> bool:
        self.close()
        return False

    def get(self, control_id: int) -> int:
        """VIDIOC_G_CTRL；控件不存在时 OSError(EINVAL)。"""
        buf = bytearray(struct.pack("<Ii", control_id, 0))
        fcntl.ioctl(self._fd, _VIDIOC_G_CTRL, buf, True)
        return struct.unpack("<Ii", bytes(buf))[1]

    def set(self, control_id: int, value: int) -> None:
        """VIDIOC_S_CTRL；值非法/控件不存在时 OSError(EINVAL)。"""
        buf = bytearray(struct.pack("<Ii", control_id, value))
        fcntl.ioctl(self._fd, _VIDIOC_S_CTRL, buf, True)

    def query(self, control_id: int) -> Optional[dict]:
        """VIDIOC_QUERYCTRL 的诊断信息（菜单范围/默认值）；不支持返回 None。

        纯诊断：老驱动可能不认这个 ioctl，失败绝不能影响写值那条路。
        """
        buf = bytearray(_QUERYCTRL_SIZE)
        struct.pack_into("<II", buf, 0, control_id, 0)
        try:
            fcntl.ioctl(self._fd, _VIDIOC_QUERYCTRL, buf, True)
        except OSError:
            return None
        ctype, = struct.unpack_from("<I", buf, 4)
        name = bytes(buf[8:40]).split(b"\x00", 1)[0].decode("ascii", "replace")
        minimum, maximum, step, default = struct.unpack_from("<iiii", buf, 40)
        return {
            "type": ctype,
            "name": name,
            "min": minimum,
            "max": maximum,
            "step": step,
            "default": default,
        }


@dataclass
class DecxinExposureResult:
    """一台 DECXIN 的处理结论（给日志与测试断言看）。"""

    node: str                                   # "/dev/video3"
    usb_path: str                               # USB 拓扑路径 "1-2.1.2"
    state: str      # ok/changed/partial/dry-run/skipped/unsupported
    before: Optional[Tuple[Optional[int], Optional[int]]] = None   # (AE, AWB)
    after: Optional[Tuple[Optional[int], Optional[int]]] = None
    detail: str = ""

    @property
    def healthy(self) -> bool:
        """亮度契约是否成立：skipped / unsupported 之外都意味着自动曝光在生效。

        partial（写了但相机没认，例如忽略 AWB）算成立 —— 用户要的是「画面不暗」，
        那条由 auto_exposure 决定，AWB 只影响色。
        """
        return self.state not in {"skipped", "unsupported"}


def _decxin_groups(max_index: int = 16) -> List[Tuple[str, List[int]]]:
    """[(usb_path, [video_index, ...]), ...]，只保留 1bcf:2d4f 的物理设备。

    分组键用 USB 拓扑路径：节点号随重插漂移、by-id 链接在同型号两台并存时
    会被顶掉并在重枚举时翻转（见 core.camera._ambiguous_by_id_prefixes），
    只有拓扑路径稳定。一台 DECXIN 通常有主/次两个 video 节点，两个都要留着
    —— 哪个带控制项随驱动/机型不同，靠下面 probe 出来。
    """
    from core.camera import _physical_usb_path, _usb_vid_pid

    groups: dict = {}
    try:
        entries = os.listdir("/sys/class/video4linux")
    except OSError:
        return []
    for entry in entries:
        match = _VIDEO_NODE_RE.fullmatch(entry)
        if not match:
            continue
        index = int(match.group(1))
        if index >= max_index:
            continue
        if _usb_vid_pid(index) != (DECXIN_VID, DECXIN_PID):
            continue
        key = _physical_usb_path(index) or entry
        groups.setdefault(key, []).append(index)
    return [(key, sorted(indices)) for key, indices in sorted(groups.items())]


def _node_holders(path: str) -> List[str]:
    """正打开着这个节点的进程（"pid:命令名"），没有则空表。

    挡的是「主程序正用 OpenCV 录着这台相机」：那种时候写曝光会直接改到录制
    画面里去。**本进程也要算**——夹爪桥就跑在主程序进程里，正是主程序自己
    可能用 OpenCV 开着同一台 DECXIN。被 libuvc 流式占用的相机不走这条路：
    它的内核驱动已被摘掉、/dev/videoN 压根不存在。所以调用方必须在
    **自己 open 之前**问，不然查到的占用者就是自己。
    """
    try:
        target = os.stat(path)
    except OSError:
        return []
    if not stat.S_ISCHR(target.st_mode):
        return []
    holders: List[str] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = os.path.join("/proc", entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                st = os.stat(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if st.st_rdev != target.st_rdev:
                continue
            try:
                with open(os.path.join("/proc", entry, "comm")) as fh:
                    comm = fh.read().strip()
            except OSError:
                comm = "?"
            holders.append(f"{entry}:{comm}")
            break
    return holders


def _open_control_node(
    indices: Sequence[int],
    *,
    opener: Callable[[str], _VideoNode] = _VideoNode,
) -> Tuple[Optional[str], Optional[_VideoNode], str]:
    """在候选节点里找出带 auto_exposure 的那一个（(路径, 句柄, 失败原因)）。

    次级节点没有控制项（G_CTRL 报 EINVAL），所以只能一个个试；试到就返回，
    调用方负责 close。
    """
    errors: List[str] = []
    for index in indices:
        path = f"/dev/video{index}"
        try:
            node = opener(path)
        except OSError as exc:
            errors.append(f"{path}: open 失败 {exc.strerror or exc}")
            continue
        try:
            node.get(V4L2_CID_EXPOSURE_AUTO)
        except OSError as exc:
            errors.append(f"{path}: 无 auto_exposure 控制项 {exc.strerror or exc}")
            node.close()
            continue
        return path, node, ""
    return None, None, "；".join(errors) if errors else "无候选节点"


def _normalize_one(
    usb_path: str,
    indices: Sequence[int],
    *,
    log: Logger,
    dry_run: bool,
    opener: Callable[[str], _VideoNode] = _VideoNode,
    holder_probe: Callable[[str], List[str]] = _node_holders,
) -> DecxinExposureResult:
    # 占用检查必须在**自己 open 之前**：开完再查，查到的占用者就是本进程。
    # 一个候选节点被占就整台跳过 —— 同一个相机的两个节点，谁拿着都说明它
    # 正被使用（也可能是主程序自己用 OpenCV 录着这台 DECXIN）。
    for index in indices:
        path = f"/dev/video{index}"
        holders = holder_probe(path)
        if holders:
            shown = ", ".join(holders[:3]) + (
                f" 等 {len(holders)} 个" if len(holders) > 3 else "")
            detail = f"{path} 被占用（{shown}），跳过以免改到正在录制的画面"
            log(f"[DECXIN] usb={usb_path} {detail}")
            return DecxinExposureResult(
                node=path, usb_path=usb_path, state="skipped", detail=detail,
            )

    node_path, node, failure = _open_control_node(indices, opener=opener)
    if node is None:
        log(f"[DECXIN] usb={usb_path} 找不到曝光控制节点：{failure}")
        return DecxinExposureResult(
            node=f"video{list(indices)}", usb_path=usb_path,
            state="unsupported", detail=failure,
        )
    try:
        auto_exposure = node.get(V4L2_CID_EXPOSURE_AUTO)
        try:
            auto_wb: Optional[int] = node.get(V4L2_CID_AUTO_WHITE_BALANCE)
        except OSError:
            auto_wb = None
        menu = node.query(V4L2_CID_EXPOSURE_AUTO)
        menu_note = ""
        if menu is not None:
            menu_note = f"（auto_exposure 菜单 {menu['min']}~{menu['max']}）"
        before = (auto_exposure, auto_wb)

        if auto_exposure != V4L2_EXPOSURE_MANUAL and auto_wb == 1:
            log(f"[DECXIN] usb={usb_path} {node_path} 已是自动曝光/自动白平衡"
                f"（AE={auto_exposure} AWB={auto_wb}），无需处理")
            return DecxinExposureResult(
                node=node_path, usb_path=usb_path, state="ok",
                before=before, after=before, detail="已是自动",
            )

        if dry_run:
            log(f"[DECXIN] usb={usb_path} {node_path} [dry-run] 当前 AE={auto_exposure} "
                f"AWB={auto_wb}{menu_note}，本应改写为自动曝光 + 自动白平衡")
            return DecxinExposureResult(
                node=node_path, usb_path=usb_path, state="dry-run",
                before=before, after=before, detail="仅检查未写入",
            )

        changed = False
        attempted = False
        detail_parts: List[str] = []
        if auto_exposure == V4L2_EXPOSURE_MANUAL:
            attempted = True
            for candidate in _EXPOSURE_AUTO_CANDIDATES:
                try:
                    node.set(V4L2_CID_EXPOSURE_AUTO, candidate)
                except OSError:
                    continue
                try:
                    readback = node.get(V4L2_CID_EXPOSURE_AUTO)
                except OSError:
                    readback = None
                if readback == candidate:
                    changed = True
                    detail_parts.append(f"AE {auto_exposure}→{readback}")
                    break
            else:
                detail_parts.append(
                    f"AE 仍是手动 {auto_exposure}：候选档 "
                    f"{list(_EXPOSURE_AUTO_CANDIDATES)} 全部写不进去{menu_note}")
        if auto_wb != 1:
            attempted = True
            try:
                node.set(V4L2_CID_AUTO_WHITE_BALANCE, 1)
                readback_wb = node.get(V4L2_CID_AUTO_WHITE_BALANCE)
            except OSError as exc:
                detail_parts.append(f"AWB 写入失败（{exc.strerror or exc}）")
            else:
                if readback_wb == 1:
                    changed = True
                    detail_parts.append(f"AWB {auto_wb}→1")
                else:
                    detail_parts.append(f"AWB 读回 {readback_wb}（写了 1 未生效）")

        try:
            after = (node.get(V4L2_CID_EXPOSURE_AUTO),
                     _quiet_get(node, V4L2_CID_AUTO_WHITE_BALANCE))
        except OSError:
            after = None
        detail = "；".join(detail_parts) or "无需处理"
        still_manual = after is not None and after[0] == V4L2_EXPOSURE_MANUAL
        if still_manual:
            state = "unsupported"
        elif changed:
            state = "changed"
        elif attempted:
            state = "partial"
        else:
            state = "ok"
        if still_manual:
            log(f"[DECXIN] usb={usb_path} {node_path} 曝光归一化**失败**：{detail}"
                f"{menu_note}")
        else:
            log(f"[DECXIN] usb={usb_path} {node_path} 曝光已归一化：{detail}{menu_note}")
        return DecxinExposureResult(
            node=node_path, usb_path=usb_path, state=state,
            before=before, after=after, detail=detail,
        )
    finally:
        node.close()


def _quiet_get(node: _VideoNode, control_id: int) -> Optional[int]:
    try:
        return node.get(control_id)
    except OSError:
        return None


def normalize_decxin_exposure(
    *,
    logger: Optional[Logger] = None,
    dry_run: bool = False,
    max_index: int = 16,
    opener: Callable[[str], _VideoNode] = _VideoNode,
) -> List[DecxinExposureResult]:
    """把所有接着的 DECXIN 写回「自动曝光 + 自动白平衡」。

    **绝不抛异常**：这是连夹爪路上的一步附加动作，写不动相机最多是画面暗，
    绝不能因此让夹爪连不上。逐台处理，单台失败只记日志、继续下一台。
    """
    log: Logger = logger or (lambda message: print(message))
    results: List[DecxinExposureResult] = []
    try:
        cameras = _decxin_groups(max_index)
    except Exception as exc:                     # noqa: BLE001 —— 见 docstring
        log(f"[DECXIN] 枚举失败，跳过曝光归一化：{exc}")
        return results
    if not cameras:
        log("[DECXIN] 未发现 DECXIN 相机（1bcf:2d4f），跳过曝光归一化")
        return results
    for usb_path, indices in cameras:
        try:
            results.append(_normalize_one(
                usb_path, indices, log=log, dry_run=dry_run, opener=opener))
        except Exception as exc:                 # noqa: BLE001
            log(f"[DECXIN] usb={usb_path} 处理失败，跳过：{exc}")
            results.append(DecxinExposureResult(
                node=f"video{list(indices)}", usb_path=usb_path,
                state="unsupported", detail=str(exc)))
    return results


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.gripper.decxin_exposure",
        description="把接着的 DECXIN 相机（1bcf:2d4f）写回自动曝光 + 自动白平衡。"
                    "必须在主程序/夹爪相机服务停止时运行 —— 它们一开就把设备从 "
                    "libusb 拿走，/dev/videoN 会注销、写不进去。",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="只读当前值并报告，不写相机")
    parser.add_argument("--max-index", type=int, default=16,
                        help="最多枚举到 /dev/videoN-1（默认 16）")
    args = parser.parse_args(argv)

    def log(message: str) -> None:
        print(message, flush=True)

    results = normalize_decxin_exposure(
        logger=log, dry_run=args.dry_run, max_index=args.max_index)
    if not results:
        log("[DECXIN] 没有可处理的相机")
        return 0
    log("")
    log("节点         usb          状态        AE(前→后)    AWB(前→后)   说明")
    for item in results:
        before = item.before or (None, None)
        after = item.after or (None, None)
        ae = f"{before[0]}→{after[0]}"
        awb = f"{before[1]}→{after[1]}"
        log(f"{item.node:<12} {item.usb_path:<13} {item.state:<11} "
            f"{ae:<11} {awb:<12} {item.detail}")
    failed = [item for item in results if not item.healthy]
    if failed:
        log("")
        log(f"[DECXIN] {len(failed)}/{len(results)} 台没能归一化（见上表）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
