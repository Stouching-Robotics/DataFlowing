"""Runtime libusb/libuvc transport for Sightac and DECXIN.

Fays is deliberately absent from this module.  The Fays instance manager has
already identified and leased the S80M through the official SDK; this module
uses that live assignment only to associate the separate USB2 camera set.

The discovery executable is run for every selection.  Its Flash calibration
payload is therefore a per-run read from the camera, not a saved fallback.
The service then exposes the negotiated MJPEG frames through Unix sockets.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import sys

import cv2

from core.gripper.fays_runtime import PROJECT_ROOT
from core.gripper.paths import CAMERA_MODE_STATE_DIR
from core.gripper.runtime.device_access import device_access_guard

# camera_service/python is a checkout-local transport module, not an ambient
# dependency.  Resolve it before importing the IPC client.
IPC_PYTHON_ROOT = os.path.join(PROJECT_ROOT, "camera_service", "python")
if IPC_PYTHON_ROOT not in sys.path:
    sys.path.insert(0, IPC_PYTHON_ROOT)
from core.gripper.devices.usb_camera_ipc import Camera as IpcCamera

from .usb_camera_sets import (
    DECXIN_FOURCC,
    DECXIN_FPS,
    DECXIN_HEIGHT,
    DECXIN_WIDTH,
    SIGHTAC_FOURCC,
    SIGHTAC_FPS,
    SIGHTAC_HEIGHT,
    SIGHTAC_WIDTH,
    UsbCameraSetError,
    UsbCameraSetTopology,
)


DISCOVERY_BINARY = Path(os.environ.get(
    "KSQ_UVC_DISCOVERY_BINARY",
    os.path.join(PROJECT_ROOT, "camera_service", "build", "discover-uvc-config"),
)).resolve()
SERVICE_BINARY = Path(os.environ.get(
    "KSQ_UVC_SERVICE_BINARY",
    os.path.join(PROJECT_ROOT, "camera_service", "build", "ksq-camera-service"),
)).resolve()
DISCOVERY_TIMEOUT_S = 45.0
SERVICE_START_TIMEOUT_S = 10.0
FIRST_FRAME_ATTEMPTS = 20
FIRST_FRAME_RETRY_S = 0.05

# 原生二进制依赖交付目录内的 libuvc（原上位机由 start_upper_computer.sh
# 全局注入 LD_LIBRARY_PATH；主程序改为仅对这两个子进程注入，避免污染
# 主进程及 Fays 桥接的自建库路径）。
_LIBUVC_DIR = Path(
    PROJECT_ROOT, "camera_service", "build", "third_party", "libuvc")


def _native_env(binary=None):
    """只给这个子进程用交付目录里的 libuvc，绝不动主进程的环境。

    ``binary`` 自带 ``third_party/libuvc`` 时优先用它（KSQ_UVC_*_BINARY
    被指向别处的构建树时仍然自洽），再兜底交付目录内的副本。
    """
    env = dict(os.environ)
    directories = []
    if binary is not None:
        directories.append(
            str(Path(binary).resolve().parent / "third_party" / "libuvc"))
    directories.append(str(_LIBUVC_DIR))
    directories.extend(
        part for part in env.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if part
    )
    env["LD_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(directories))
    return env


class UvcCameraServiceError(RuntimeError):
    """The live libusb/libuvc camera set cannot be used."""


# 每个机型允许的档位对（altsetting, payload），按从好到差排列，必须与服务端
# camera_service.c 的档位表逐项一致。服务认不出的档位对会直接 rc=2 退出，
# 把整个夹爪一起带走，所以这里必须先挡一道，把「配置写错」变成一条看得懂的
# 报错，而不是一个哑掉的服务进程。
_MODE_LADDERS = {
    (0x0C45, 0x636F): ((3, 800),),
    (0x1BCF, 0x2D4F): ((7, 1280), (6, 944)),
}

# 起始档位一律取档位表里最保守的那一档（＝表的最后一档），不认控制器。
#
# 曾经这里有一张「已知扛不住的控制器 → 起始档位」的表（0000:74:00.4 → alt6）。
# 2026-09-15 实测把它推翻了：alt7 停摆不是某块控制器的毛病，0000:0a:00.0 上
# 整机三路一起出流时一样在 78 秒内停摆（两次独立运行、共 3 次停摆，全落在
# 68~116 秒）。当初判它「总线 1 没事」是拿一次根本没真的跑在 alt7 上的运行
# 当基准——那时 ini 里的 forced_* 还是死的，服务只是把 ini 的值原样打进了
# 日志。控制器不是变量，就没必要按机器写死。


def _usb_controller_for_bus(bus):
    """这台相机所在 USB 控制器的 PCI 路径，例如 ``0000:0a:00.0``。

    ``/sys/bus/usb/devices/usbN`` 是指向根 Hub 的符号链接，解开的上一级就是
    控制器；用 PCI 路径而不是总线号，因为总线号会随插口变。

    它只进落盘键，不参与「选哪一档」的判断——**控制器是不是变量并没有定论**
    （2026-09-15 实测 alt7 在 74:00.4 与 0a:00.0 上都停摆，但机制没查清），所
    以不拿它做任何决策。键里带上它只是取保守：学到的东西严格绑在「学它的那条
    物理路径」上，换机器/换口自然是新键。代价为零——新键没有记忆值，就退回档
    位表里最保守的那一档，那本来就是我们要的默认。取不到返回空串，调用方退化
    成「不区分控制器」，功能不受影响。
    """
    link = Path(f"/sys/bus/usb/devices/usb{int(bus)}")
    try:
        return link.resolve(strict=True).parent.name
    except (OSError, ValueError):
        return ""


def _mode_state_key(vid, pid, controller, serial):
    """「这台相机 × 这条物理路径」的落盘键，规则必须与 C 侧 ``mode_state_key``
    逐字一致：只保留 ``[A-Za-z0-9-]``，其余一律换成 ``_``。"""
    raw = (
        f"{int(vid):04x}_{int(pid):04x}_"
        f"{controller or 'unknown'}_{serial or 'noserial'}"
    )
    return "".join(
        char if (char.isascii() and (char.isalnum() or char == "-")) else "_"
        for char in raw
    )


def _pair_is_known(vid, pid, pair):
    return pair in _MODE_LADDERS.get((int(vid), int(pid)), ())


def _read_learned_mode(state_dir, vid, pid, controller, serial):
    """读服务上一轮学到的档位。文件坏了 / 越界了一律当没学到。"""
    if not state_dir:
        return None
    path = Path(state_dir) / (
        _mode_state_key(vid, pid, controller, serial) + ".mode"
    )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            values[key.strip()] = int(value.strip())
        except ValueError:
            return None
    pair = (values.get("altsetting"), values.get("payload"))
    if not _pair_is_known(vid, pid, pair):
        return None
    return pair


def _path_contains(bus, outer_path, child_path):
    outer = str(outer_path or "").strip()
    child = str(child_path or "").strip()
    if not outer or not child:
        return False
    if str(bus) != child.split("-", 1)[0]:
        return False
    return child == f"{bus}-{outer}" or child.startswith(
        f"{bus}-{outer}."
    )


def _require_finite_vector(params, key, length):
    values = params.get(key)
    if not isinstance(values, list) or len(values) != length:
        raise UvcCameraServiceError(
            f"当前 Flash 参数 {key} 长度无效: expected={length}"
        )
    result = []
    for index, value in enumerate(values):
        if key == "fw_params" and params.get("device_type") == "bevel" and index >= 5 and (
            value is None or (
                isinstance(value, float) and math.isnan(value)
            )
        ):
            # Reserved bevel-table tail: firmware may store NaN here and
            # the current bevel ROI algorithm consumes only fw[0..4]. Keep
            # either JSON null or a Python NaN sentinel through the external
            # handoff; the tactile cache converts it to its internal NaN
            # representation.
            result.append(None)
            continue
        if isinstance(value, bool):
            raise UvcCameraServiceError(
                f"当前 Flash 参数 {key} 包含非数值"
            )
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise UvcCameraServiceError(
                f"当前 Flash 参数 {key}[{index}] 包含非数值: {value!r}"
            ) from exc
        if not math.isfinite(number):
            raise UvcCameraServiceError(
                f"当前 Flash 参数 {key}[{index}] 包含非有限值: {value!r}"
            )
        result.append(number)
    return result


def _flash_params(candidate):
    if not candidate.get("flash_ok") or not candidate.get(
        "flash_params_ok"
    ):
        raise UvcCameraServiceError(
            "Sightac 本次 Flash 身份或完整标定参数读取失败: "
            f"side={candidate.get('side') or '<none>'} "
            f"error={candidate.get('error') or '<unknown>'}"
        )
    device_type = str(
        candidate.get("device_type") or ""
    ).strip().lower()
    if device_type not in {"planar", "curved", "bevel"}:
        raise UvcCameraServiceError(
            f"Flash device_type 无效: {device_type!r}"
        )
    encoded = candidate.get("flash_params")
    if not isinstance(encoded, dict):
        raise UvcCameraServiceError(
            "libusb 探测没有返回本次完整 Flash 参数"
        )
    encoded_type = str(encoded.get("device_type") or "").strip().lower()
    if encoded_type != device_type:
        raise UvcCameraServiceError(
            "Flash device_type 身份与参数表不一致"
        )
    return {
        "device_type": device_type,
        "fx_params": _require_finite_vector(encoded, "fx_params", 5),
        "fy_params": _require_finite_vector(encoded, "fy_params", 5),
        "fz_params": _require_finite_vector(encoded, "fz_params", 21),
        "fw_params": _require_finite_vector(encoded, "fw_params", 9),
        "ft_params": _require_finite_vector(encoded, "ft_params", 20),
        "flash_state": str(
            encoded.get("flash_state") or "programmed"
        ),
        "calibration_source": "hardware-live-flash",
    }


class IpcCameraNode:
    """The subset of UsbCameraNode consumed by the existing lifecycle."""

    def __init__(self, *, role, socket_path, video_index, candidate,
                 outer_path, bus, flash_params=None):
        self.role = str(role)
        self.device_path = str(socket_path)
        self.video_index = int(video_index)
        self.physical_usb_path = f"{int(bus)}-{candidate['port']}"
        self.physical_sysfs_path = self.physical_usb_path
        self.direct_parent_hub = f"{int(bus)}-{outer_path}"
        self.root_hub = f"{int(bus)}-{str(outer_path).split('.', 1)[0]}"
        self.vendor_id = f"{int(candidate['vid']):04x}"
        self.product_id = f"{int(candidate['pid']):04x}"
        self.serial = str(
            candidate.get("flash_serial")
            or candidate.get("usb_serial")
            or ""
        )
        self.transport = "libuvc-ipc"
        self.flash_params = flash_params
        self.capture_format = (
            DECXIN_WIDTH,
            DECXIN_HEIGHT,
            DECXIN_FPS,
            DECXIN_FOURCC,
        ) if self.role == "decxin" else (
            SIGHTAC_WIDTH,
            SIGHTAC_HEIGHT,
            SIGHTAC_FPS,
            SIGHTAC_FOURCC,
        )


class IpcReservedCamera:
    """Lazy IPC client; opening it claims exactly one service socket."""

    def __init__(self, node):
        self.node = node
        self.actual_format = tuple(node.capture_format)
        self.transport = "libuvc-ipc"
        self.flash_params = node.flash_params
        self._camera = None
        self._first_frame = None
        self._claimed = False
        self._lock = threading.Lock()

    @property
    def claimed(self):
        with self._lock:
            return self._claimed

    def claim(self):
        with self._lock:
            if self._claimed:
                raise UsbCameraSetError(
                    f"{self.node.role} IPC reservation was already claimed"
                )
            camera = IpcCamera(self.node.device_path, timeout=0.5)
            try:
                camera.open()
                first_frame = None
                failed_reads = 0
                last_decode_packets = ()
                for attempt in range(FIRST_FRAME_ATTEMPTS):
                    ok, frame = camera.read()
                    if ok and frame is not None and getattr(
                        frame, "size", 0
                    ) > 0:
                        first_frame = frame
                        break
                    failed_reads += 1
                    last_decode_packets = (
                        camera.last_decode_diagnostics or ()
                    )
                    if attempt + 1 < FIRST_FRAME_ATTEMPTS:
                        time.sleep(FIRST_FRAME_RETRY_S)
                if first_frame is None:
                    details = "".join(
                        f" seq={item.get('sequence')} bytes={item.get('bytes')}"
                        f" head={item.get('head')} tail={item.get('tail')}"
                        for item in last_decode_packets[-3:]
                    )
                    raise UsbCameraSetError(
                        f"{self.node.role} libuvc IPC has no first frame: "
                        f"{self.node.device_path} "
                        f"failed_reads={failed_reads}{details}"
                    )
                self._claimed = True
                self._camera = camera
                self._first_frame = first_frame
                return camera, first_frame, self.actual_format
            except Exception:
                camera.release()
                raise

    def release(self):
        with self._lock:
            camera = self._camera
            self._camera = None
            self._first_frame = None
        if camera is not None:
            camera.release()


class IpcCameraSetReservation:
    def __init__(self, topology, cameras, on_release=None):
        self.topology = topology
        self._cameras = dict(cameras)
        self._on_release = on_release
        self._released = False

    def camera(self, role):
        return self._cameras[role]

    def release_unclaimed(self):
        for camera in self._cameras.values():
            camera.release()
        if not self._released:
            self._released = True
            if self._on_release is not None:
                self._on_release()


class UvcCameraServiceManager:
    """Own dynamic camera-service processes for the active UI slots.

    One service process is used per selected physical camera set.  This is
    intentional: the current socket protocol accepts one client per output,
    and two UI slots must not force a running first set to be reconfigured
    underneath its clients.
    """

    def __init__(self, *, logger=print, discovery_binary=DISCOVERY_BINARY,
                 service_binary=SERVICE_BINARY,
                 service_cpus: tuple[int, ...] | None = None):
        self._logger = logger
        self._discovery_binary = Path(discovery_binary).resolve()
        self._service_binary = Path(service_binary).resolve()
        self._service_cpus = tuple(
            int(cpu) for cpu in (service_cpus or ())
        )
        self._lock = threading.RLock()
        self._services = {}

    def _run_discovery(self, *, usb_bus=None, port_prefix=None,
                       skip_flash=False, identity_only=False,
                       topology_only=False):
        """Run the live libusb/libuvc discovery pass.

        Three depths, mutually exclusive: ``topology_only`` (enumeration
        only), ``identity_only`` (also opens UVC control, verifies mode and
        reads Flash side/serial, skipping the five calibration tables) and
        the default full read.  ``skip_flash`` is the diagnostic alias for
        the first two.  Only the full mode can emit a usable config.
        """
        if not self._discovery_binary.is_file():
            raise UvcCameraServiceError(
                f"libusb/libuvc 发现程序不存在: {self._discovery_binary}"
            )
        if not os.access(self._discovery_binary, os.X_OK):
            raise UvcCameraServiceError(
                f"libusb/libuvc 发现程序不可执行: {self._discovery_binary}"
            )
        if sum(bool(flag) for flag in (
                skip_flash, identity_only, topology_only)) > 1:
            raise UvcCameraServiceError(
                "discover-uvc-config 的扫描深度参数互斥，只能选一个"
            )
        command = [str(self._discovery_binary), "--json"]
        if usb_bus is not None and port_prefix is not None:
            command += ["--bus", str(usb_bus), "--port-prefix", str(port_prefix)]
        if topology_only:
            command.append("--topology-only")
        elif identity_only:
            command.append("--identity-only")
        elif skip_flash:
            command.append("--skip-flash")
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=DISCOVERY_TIMEOUT_S,
            check=False,
            env=_native_env(self._discovery_binary),
        )
        if completed.returncode != 0:
            details = "\n".join(
                (completed.stderr or "").splitlines()[-20:]
            )
            raise UvcCameraServiceError(
                "libusb/libuvc 动态发现失败 "
                f"rc={completed.returncode}: {details or '<no detail>'}"
            )
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise UvcCameraServiceError(
                f"libusb/libuvc 发现 JSON 无效: {exc}"
            ) from exc
        groups = report.get("groups") if isinstance(report, dict) else None
        if not isinstance(groups, list) or not groups:
            raise UvcCameraServiceError(
                "libusb/libuvc 没有发现完整 Sightac + DECXIN 组"
            )
        return groups

    @staticmethod
    def _assignment_identity(assignment):
        if not isinstance(assignment, dict):
            raise UvcCameraServiceError(
                "Fays 尚未完成身份分配"
            )
        esp = assignment.get("esp32") or {}
        fays = assignment.get("fays") or {}
        esp_serial = str(esp.get("serial") or "").strip()
        fays_serial = str(fays.get("product_serial") or "").strip()
        if not esp_serial or not fays_serial:
            raise UvcCameraServiceError(
                "Fays 当前分配缺少 ESP32 serial 或 Fays serial"
            )
        return esp_serial, fays_serial

    def _associate_group(self, group, assignment, live_esp):
        """UVC 组只按 ESP32 serial + bus/outer_path 关联。

        Fays 归属由 ESP32 NVS 里的绑定序列号决定（见 SingleFaysLease），
        这里不再比较 Fays 的 USB 根端口：Fays 是 USB3/FT602，插在 USB3 口
        时走 5000M 伴生总线，与触觉/RGB 的 480M 总线 bus 号不同；多控制器
        机器上两套 rig 还常落在同号根端口，按端口关联只会互相误配。
        """
        esp_serial, fays_serial = self._assignment_identity(assignment)
        bus = int(group.get("bus", -1))
        outer_path = str(group.get("outer_path") or "").strip()
        matching_esp = [
            item for item in live_esp
            if str(item.get("serial") or "").strip() == esp_serial
            and _path_contains(bus, outer_path,
                               item.get("physical_usb_path"))
        ]
        if len(matching_esp) != 1:
            raise UvcCameraServiceError(
                "当前 UVC 组无法按 ESP32 serial 唯一关联: "
                f"serial={esp_serial} matches={len(matching_esp)} "
                f"group={bus}-{outer_path}"
            )
        normalized = {
            "bus": bus,
            "outer_path": outer_path,
            "esp32_serial": esp_serial,
            "fays_serial": fays_serial,
            "sightac": {},
            "decxin": group.get("decxin"),
        }
        sightac = group.get("sightac")
        if not isinstance(sightac, list) or len(sightac) != 2:
            raise UvcCameraServiceError(
                "UVC 组 Sightac 数量不是 2"
            )
        for candidate in sightac:
            side = str(candidate.get("side") or "").strip().lower()
            if side not in {"left", "right"} or side in normalized[
                "sightac"
            ]:
                raise UvcCameraServiceError(
                    f"UVC 组 left/right Flash 身份不唯一: side={side!r}"
                )
            normalized["sightac"][side] = candidate
        if set(normalized["sightac"]) != {"left", "right"}:
            raise UvcCameraServiceError(
                "UVC 组缺少动态 Flash left/right 身份"
            )
        if not group.get("valid"):
            raise UvcCameraServiceError(
                "UVC 组未通过动态模式或 Flash 完整参数校验"
            )
        for side in ("left", "right"):
            normalized["sightac"][side]["flash_params_local"] = (
                _flash_params(normalized["sightac"][side])
            )
        decxin = normalized["decxin"]
        if not isinstance(decxin, dict) or not decxin.get("mode_ok"):
            raise UvcCameraServiceError(
                "UVC 组 DECXIN 未通过动态模式校验"
            )
        return normalized

    def _discovery_scope(self, assignment, live_esp=None):
        from core.gripper.fays_runtime import discover_esp32_devices
        serial, _ = self._assignment_identity(assignment)
        devices = tuple(discover_esp32_devices()) if live_esp is None else live_esp
        matches = [d for d in devices if str(d.get("serial", "")).strip() == serial]
        if len(matches) != 1:
            raise UvcCameraServiceError("ESP32 身份无法唯一定位，拒绝启动全设备扫描")
        physical = str(matches[0].get("physical_usb_path", ""))
        bus, separator, ports = physical.partition("-")
        root = ports.split(".")[0]
        if not separator or not bus.isdigit() or not root.isdigit():
            raise UvcCameraServiceError("ESP32 USB 路径无效，请重建设备清单")
        return int(bus), root

    def _resolve_current_group(self, assignment):
        from core.gripper.fays_runtime import discover_esp32_devices

        live_esp = tuple(discover_esp32_devices())
        bus, port = self._discovery_scope(assignment, live_esp)
        groups = self._run_discovery(usb_bus=bus, port_prefix=port)
        matches = []
        for group in groups:
            try:
                matches.append(self._associate_group(
                    group, assignment, live_esp))
            except UvcCameraServiceError:
                continue
        if len(matches) != 1:
            esp_serial, fays_serial = self._assignment_identity(assignment)
            raise UvcCameraServiceError(
                "当前设备清单身份与动态 UVC 拓扑无法唯一配对: "
                f"esp32={esp_serial} fays={fays_serial} "
                f"matches={len(matches)} groups={len(groups)}"
            )
        return matches[0]

    @staticmethod
    def _signature_for(records):
        return tuple(sorted(
            (
                int(record["bus"]),
                record["outer_path"],
                # ini 里那一行 `usb_controller=` 就是它，而它参与落盘键，所以
                # 「这份配置长什么样」含控制器：换控制器必须重写 ini，不能复用
                # 上一个进程。
                _usb_controller_for_bus(record["bus"]),
                tuple(sorted(
                    (side, str(candidate["port"]))
                    for side, candidate in record["sightac"].items()
                )),
                str(record["decxin"]["port"]),
            )
            for record in records
        ))

    @staticmethod
    def _starting_mode(candidate, controller):
        """这台相机从哪一档开始。

        优先级：上一轮学到的 > 档位表里最保守的那一档 > 扫描程序报的首选档。

        为什么默认不是首选档：实测 DECXIN 的 alt7（10.24 MB/s）在整机三路一起
        出流时每 68~116 秒必停摆一次，alt6（7.552 MB/s）同样三路下 30.00 fps
        长跑不停，而两档出帧率一模一样——首选档多出来的那点每帧余量（327680
        对 241664 字节）不值一次停摆，用户要的「稳定帧数」也不允许开机就故意
        赔一次。要那点余量就把 ini 里 forced_altsetting 改回 alt7：真撑不住
        时服务停两次后会自己退回 alt6 并把结论落盘，下次开机直接用（见
        camera_service.c 的 maybe_downgrade_altsetting）。学到的值就在这里读
        回来。"""
        vid = int(candidate["vid"])
        pid = int(candidate["pid"])
        preferred = (int(candidate["altsetting"]),
                     int(candidate["descriptor_payload"]))
        if not _pair_is_known(vid, pid, preferred):
            # 服务对认不出的档位对是 rc=2 直接退出，整个夹爪一起挂。先查
            # 一遍扫描程序的输出：扫描程序与服务端档位表脱节（比如只装了新
            # 扫描程序、没重编服务）必须报出来，不能被下面的记忆值盖过去。
            raise UvcCameraServiceError(
                f"扫描程序报出服务端不认识的档位 {vid:04x}:{pid:04x} "
                f"alt={preferred[0]} payload={preferred[1]}；"
                "扫描程序与服务端档位表已经脱节，需要一起重编"
            )
        serial = str(candidate.get("usb_serial") or "").strip()
        learned = _read_learned_mode(
            CAMERA_MODE_STATE_DIR, vid, pid, controller, serial
        )
        if learned is not None:
            return learned, "learned"
        ladder = _MODE_LADDERS[(vid, pid)]
        if ladder[-1] != preferred:
            return ladder[-1], "conservative"
        return preferred, "preferred"

    def _write_config(self, records):
        runtime_dir = Path(tempfile.mkdtemp(prefix="ksq-camera-service-"))
        lines = [
            "# Generated at runtime from the current libusb/libuvc scan.",
            "# Fays is not handled by this service.",
            "",
        ]
        sockets = {}
        for index, record in enumerate(records, 1):
            controller = _usb_controller_for_bus(record["bus"])
            for role in ("left", "right", "decxin"):
                candidate = (
                    record["sightac"][role]
                    if role in {"left", "right"}
                    else record["decxin"]
                )
                mode, mode_source = self._starting_mode(candidate, controller)
                name = f"unit_{index}_{role}"
                socket_path = runtime_dir / f"{name}.sock"
                sockets[(index, role)] = str(socket_path)
                lines.extend([
                    f"[{name}]",
                    f"vid=0x{int(candidate['vid']):04x}",
                    f"pid=0x{int(candidate['pid']):04x}",
                    f"usb_bus={int(record['bus'])}",
                    f"usb_port_path={candidate['port']}",
                    f"streaming_interface={int(candidate['streaming_interface'])}",
                    # altsetting 和 payload 是一个整体（alt7 的端点就是
                    # 1280 B/包），必须成对来自同一处：要么都是扫描程序实测的
                    # 端点描述符，要么都是服务上一轮学到的结论。拆开配会得到
                    # 一个自相矛盾的档位——2026-09-15 主程序连不上夹爪就是这
                    # 么来的（alt 取自扫描程序、payload 却是硬编码）。
                    #
                    # 这两项现在是**生效的**：服务打开设备后会把它们交给
                    # libuvc 的 uvc_set_altsetting_override()。在此之前它们
                    # 只是校验和日志，真正决定档位的是 libuvc 的编译期表，
                    # 所以「改 ini」曾经完全不改变行为。
                    f"forced_altsetting={mode[0]}",
                    f"forced_payload={mode[1]}",
                    f"# 起始档位来源: {mode_source}",
                    f"usb_serial={str(candidate.get('usb_serial') or '').strip()}",
                    f"usb_controller={controller}",
                    f"state_dir={CAMERA_MODE_STATE_DIR}",
                    "width=640" if role in {"left", "right"}
                    else "width=1280",
                    "height=480" if role in {"left", "right"}
                    else "height=960",
                    "fps=30",
                    "format=MJPEG",
                    f"socket_path={socket_path}",
                    "",
                ])
        config_path = runtime_dir / "current.ini"
        config_path.write_text("\n".join(lines), encoding="utf-8")
        return runtime_dir, config_path, sockets

    def _ensure_service(self, records):
        signature = self._signature_for(records)
        with self._lock:
            entry = self._services.get(signature)
            if entry is not None and entry["process"].poll() is None:
                if entry["leases"]:
                    raise UvcCameraServiceError(
                        "当前物理相机组已有活动 libuvc 租约；"
                        "不能让两个界面共用同一组相机"
                    )
                return signature, entry["runtime_dir"], entry["sockets"]
            if entry is not None:
                self._stop_entry_locked(entry)
                self._services.pop(signature, None)
            if not self._service_binary.is_file():
                raise UvcCameraServiceError(
                    f"libusb/libuvc 服务程序不存在: {self._service_binary}"
                )
            runtime_dir, config_path, sockets = self._write_config(records)
            service_log_path = runtime_dir / "camera-service.log"
            service_log = service_log_path.open("a", encoding="utf-8")
            try:
                process = subprocess.Popen(
                    [
                        str(self._service_binary),
                        "--config", str(config_path),
                        "--parent-pid", str(os.getpid()),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=service_log,
                    start_new_session=True,
                    env=_native_env(self._service_binary),
                )
            finally:
                service_log.close()
            def startup_details():
                with service_log_path.open("rb") as stream:
                    stream.seek(max(0, service_log_path.stat().st_size - 8192))
                    tail = stream.read().decode("utf-8", errors="replace")
                return "\n".join(tail.splitlines()[-12:])
            if self._service_cpus:
                try:
                    os.sched_setaffinity(
                        process.pid, set(self._service_cpus))
                    actual_cpus = tuple(sorted(
                        os.sched_getaffinity(process.pid)))
                    if actual_cpus != self._service_cpus:
                        self._logger(
                            "[UVC] camera-service affinity mismatch: "
                            f"pid={process.pid} "
                            f"expected={self._service_cpus} "
                            f"actual={actual_cpus}"
                        )
                except (OSError, PermissionError) as exc:
                    self._logger(
                        "[UVC] camera-service affinity bind FAILED: "
                        f"pid={process.pid} "
                        f"cpus={self._service_cpus}: {exc}"
                    )
            deadline = time.monotonic() + SERVICE_START_TIMEOUT_S
            required = tuple(sockets.values())
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise UvcCameraServiceError(
                        f"camera-service 提前退出 rc={process.returncode}; "
                        f"日志={service_log_path}\n{startup_details()}"
                    )
                if all(os.path.exists(path) for path in required):
                    self._services[signature] = {
                        "process": process,
                        "runtime_dir": runtime_dir,
                        "sockets": sockets,
                        "leases": 0,
                    }
                    self._logger(
                        "[UVC] libusb/libuvc camera-service started: "
                        f"group={signature[0][1]} pid={process.pid} "
                        f"cpus={self._service_cpus or '<inherit>'} "
                        f"log={service_log_path}"
                    )
                    return signature, runtime_dir, sockets
                time.sleep(0.05)
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            raise UvcCameraServiceError(
                "camera-service 未在限定时间取得全部相机的有效 MJPG 帧；"
                f"日志={service_log_path}\n{startup_details()}"
            )

    def _release_lease(self, signature):
        with self._lock:
            entry = self._services.get(signature)
            if entry is not None:
                entry["leases"] = max(0, entry["leases"] - 1)
            if (
                entry is not None
                and int(entry["leases"]) <= 0
            ):
                # 租约归零说明该物理相机组已无人使用。必须立刻停掉并释放
                # libusb，否则重连时 discover 二进制无法读取已被占用的
                # Flash 身份，_resolve_current_group 会得到 matches=0
                # （“设备清单身份与动态 UVC 拓扑无法唯一配对”）。
                self._stop_entry_locked(entry)
                self._services.pop(signature, None)

    def select(self, manifest_provider):
        """Claim the physical group before any discovery opens its cameras."""
        assignment = manifest_provider()
        bus, port = self._discovery_scope(assignment)
        guard = device_access_guard(f"/tmp/ksq-uvc-group-{bus}-{port}.lock", timeout=0)
        guard.__enter__()
        try:
            with self._lock:
                reservation = self._select_assignment(assignment)
            release = reservation._on_release
            def release_guarded():
                try:
                    release()
                finally:
                    guard.__exit__(None, None, None)
            reservation._on_release = release_guarded
            return reservation
        except BaseException:
            try:
                with self._lock:
                    for signature, entry in tuple(self._services.items()):
                        if (not entry["leases"] and any(
                                item[0] == bus and item[1].split(".")[0] == port
                                for item in signature)):
                            self._stop_entry_locked(entry)
                            self._services.pop(signature, None)
            finally:
                guard.__exit__(None, None, None)
            raise

    def _select_assignment(self, assignment):
        selected = self._resolve_current_group(assignment)
        # Use one service per physical set so selecting a second device does
        # not restart or invalidate the first set's Unix-socket clients.
        signature, _, sockets = self._ensure_service((selected,))
        index = 1
        left_candidate = selected["sightac"]["left"]
        right_candidate = selected["sightac"]["right"]
        decxin_candidate = selected["decxin"]
        left_params = left_candidate["flash_params_local"]
        right_params = right_candidate["flash_params_local"]
        left_node = IpcCameraNode(
            role="left", socket_path=sockets[(index, "left")],
            video_index=1000000 + index * 10 + 1,
            candidate=left_candidate, outer_path=selected["outer_path"],
            bus=selected["bus"], flash_params=left_params,
        )
        right_node = IpcCameraNode(
            role="right", socket_path=sockets[(index, "right")],
            video_index=1000000 + index * 10 + 2,
            candidate=right_candidate, outer_path=selected["outer_path"],
            bus=selected["bus"], flash_params=right_params,
        )
        decxin_node = IpcCameraNode(
            role="decxin", socket_path=sockets[(index, "decxin")],
            video_index=1000000 + index * 10 + 3,
            candidate=decxin_candidate, outer_path=selected["outer_path"],
            bus=selected["bus"],
        )
        topology = UsbCameraSetTopology(
            root_hub=f"{selected['bus']}-{selected['outer_path'].split('.', 1)[0]}",
            tactile_hub=f"{selected['bus']}-{selected['outer_path']}",
            decxin=decxin_node,
            left=left_node,
            right=right_node,
        )
        cameras = {
            "left": IpcReservedCamera(left_node),
            "right": IpcReservedCamera(right_node),
            "decxin": IpcReservedCamera(decxin_node),
        }
        with self._lock:
            self._services[signature]["leases"] += 1
        return IpcCameraSetReservation(
            topology,
            cameras,
            on_release=lambda sig=signature: self._release_lease(sig),
        )

    @staticmethod
    def _stop_entry_locked(entry):
        process = entry["process"]
        runtime_dir = entry["runtime_dir"]
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        shutil.rmtree(runtime_dir, ignore_errors=True)

    def _stop_locked(self):
        for entry in tuple(self._services.values()):
            self._stop_entry_locked(entry)
        self._services.clear()

    def stop(self):
        with self._lock:
            self._stop_locked()


def build_uvc_camera_set_selector(discovery, manifest_provider,
                                  service_manager, *, logger=print):
    """Build the production selector backed by dynamic libusb/libuvc."""

    class Selector:
        def select(self):
            reservation = service_manager.select(manifest_provider)
            try:
                for side in ("left", "right"):
                    camera = reservation.camera(side)
                    cache = getattr(
                        discovery, "cache_external_flash_params")
                    cache(camera.node.video_index, camera.flash_params)
            except Exception:
                reservation.release_unclaimed()
                raise
            logger(
                "[CameraSet] libusb/libuvc selected dynamic group: "
                f"left={reservation.topology.left.device_path} "
                f"right={reservation.topology.right.device_path} "
                f"decxin={reservation.topology.decxin.device_path}"
            )
            return reservation

    return Selector()
