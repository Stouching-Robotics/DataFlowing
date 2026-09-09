"""ESP32-anchored discovery for one physical gripper set.

The only accepted topology is:

    ESP32 (VID/PID 303a:1001)
      -> H1: one DECXIN (VID/PID 1bcf:2d4f)
      -> H0 = parent(H1): two Sightac (VID/PID 0c45:636f)

Fays is identified independently as one FT602 physical device with
interface 00 and interface 02. It is assigned to the ESP32 unit only when
their USB root-port numbers match. No independent camera unit, enumeration-order
pairing, or fallback topology is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
import glob
import os
import re
from typing import Callable, Iterable, Optional


FAYS_VENDOR_ID = "0403"
FAYS_PRODUCT_ID = "602e"
SIGHTAC_VENDOR_ID = "0c45"
SIGHTAC_PRODUCT_ID = "636f"
DECXIN_VENDOR_ID = "1bcf"
DECXIN_PRODUCT_ID = "2d4f"
ESP32_VENDOR_ID = "303a"
ESP32_PRODUCT_ID = "1001"
DECXIN_NAME = "DECXIN CAMERA: DECXIN CAMERA"
OUTER_HUB_MIN_SPEED_MBPS = 5000.0
INNER_HUB_MAX_SPEED_MBPS = 480.0


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as stream:
        return stream.read().strip()


def _natural_key(value: str):
    return tuple(
        int(part) if part.isdigit() else part
        for part in re.split(r"([0-9]+)", str(value))
        if part
    )


def _root_port_key(physical_usb_path: str) -> Optional[str]:
    """Return the bus-independent root-port number from a USB path."""
    match = re.fullmatch(r"\d+-(\d+)(?:\.\d+)*", str(physical_usb_path))
    return match.group(1) if match is not None else None


@dataclass(frozen=True)
class RelativeDeviceNode:
    role: str
    device_path: str
    physical_usb_path: str
    physical_sysfs_path: str
    parent_hub_sysfs_path: str
    vendor_id: str
    product_id: str
    serial: str
    name: str
    interface_number: str
    stream_index: int

    @property
    def video_index(self) -> Optional[int]:
        basename = os.path.basename(self.device_path)
        if basename.startswith("video") and basename[5:].isdigit():
            return int(basename[5:])
        return None


@dataclass(frozen=True)
class RelativeUnitTopology:
    fays_stereo: Optional[RelativeDeviceNode]
    fays_imu: Optional[RelativeDeviceNode]
    sightac_nodes: tuple
    decxin: Optional[RelativeDeviceNode]
    esp32: Optional[RelativeDeviceNode]
    outer_hub_sysfs_path: str
    inner_hub_sysfs_path: str
    fays_physical_sysfs_path: str

    @property
    def ports(self):
        if self.fays_stereo is None or self.fays_imu is None:
            return {}
        return {
            "stereo_dev_port": self.fays_stereo.device_path,
            "imu_dev_port": self.fays_imu.device_path,
        }

    @property
    def outer_hub_path(self) -> str:
        """Diagnostic only; never used as a matching key."""
        return os.path.basename(self.outer_hub_sysfs_path)

    @property
    def inner_hub_path(self) -> str:
        """Diagnostic only; never used as a matching key."""
        return os.path.basename(self.inner_hub_sysfs_path)


def _parent_hub(path: str) -> str:
    return os.path.dirname(path)


def _hub_speed_mbps(
    hub_sysfs_path: str,
    *,
    read_text: Callable[[str], str],
) -> Optional[float]:
    try:
        return float(read_text(os.path.join(hub_sysfs_path, "speed")))
    except (OSError, ValueError):
        return None


def _hub_device_class(
    hub_sysfs_path: str,
    *,
    read_text: Callable[[str], str],
) -> str:
    try:
        return read_text(os.path.join(hub_sysfs_path, "bDeviceClass")).lower()
    except OSError:
        return ""


def is_hub_path(
    hub_sysfs_path: str,
    *,
    read_text: Callable[[str], str] = _read_text,
    isdir: Callable[[str], bool] = os.path.isdir,
) -> bool:
    """Return True when the directory describes a USB hub (class 0x09)."""
    try:
        return (
            isdir(hub_sysfs_path)
            and _hub_device_class(hub_sysfs_path, read_text=read_text) == "09"
        )
    except OSError:
        return False


def _physical_device_from_interface(
    interface_sysfs_path: str,
    *,
    realpath: Callable[[str], str],
) -> tuple:
    physical_path = _parent_hub(interface_sysfs_path)
    interface_basename = os.path.basename(interface_sysfs_path)
    physical_usb_path = interface_basename.split(":", 1)[0]
    if (
        ":" not in interface_basename
        or not re.fullmatch(r"\d+-\d+(?:\.\d+)*", physical_usb_path)
    ):
        raise ValueError(f"not a USB interface path: {interface_sysfs_path}")
    return physical_path, physical_usb_path


def inspect_video_node(
    class_path: str,
    *,
    read_text: Callable[[str], str] = _read_text,
    realpath: Callable[[str], str] = os.path.realpath,
    device_exists: Callable[[str], bool] = os.path.exists,
    isdir: Callable[[str], bool] = os.path.isdir,
):
    """Classify one video4linux node using only its sysfs identity."""
    del isdir
    basename = os.path.basename(class_path)
    if not re.fullmatch(r"video\d+", basename):
        return None
    try:
        stream_index = int(read_text(os.path.join(class_path, "index")))
        if stream_index != 0:
            return None
        name = read_text(os.path.join(class_path, "name"))
        interface_number = read_text(
            os.path.join(class_path, "device", "bInterfaceNumber")
        ).lower()
        modalias = read_text(
            os.path.join(class_path, "device", "modalias")
        ).lower()
        interface_sysfs_path = realpath(os.path.join(class_path, "device"))
        physical_sysfs_path, physical_usb_path = _physical_device_from_interface(
            interface_sysfs_path, realpath=realpath
        )
        vendor_id = read_text(
            os.path.join(physical_sysfs_path, "idVendor")
        ).lower()
        product_id = read_text(
            os.path.join(physical_sysfs_path, "idProduct")
        ).lower()
        try:
            serial = read_text(os.path.join(physical_sysfs_path, "serial"))
        except OSError:
            serial = ""
        parent_hub = _parent_hub(physical_sysfs_path)
    except (OSError, ValueError):
        return None

    if not device_exists(f"/dev/{basename}"):
        return None

    lowered_name = name.lower()
    role = None
    if (
        vendor_id == FAYS_VENDOR_ID
        and product_id == FAYS_PRODUCT_ID
        and "ftdi" in lowered_name
        and "superspeed video bridge" in lowered_name
    ):
        if interface_number == "00":
            role = "fays_stereo"
        elif interface_number == "02":
            role = "fays_imu"
    elif vendor_id == SIGHTAC_VENDOR_ID and product_id == SIGHTAC_PRODUCT_ID:
        role = "sightac"
    elif (
        vendor_id == DECXIN_VENDOR_ID
        and product_id == DECXIN_PRODUCT_ID
        and name == DECXIN_NAME
    ):
        role = "decxin"
    if role is None:
        return None
    return RelativeDeviceNode(
        role=role,
        device_path=f"/dev/{basename}",
        physical_usb_path=physical_usb_path,
        physical_sysfs_path=physical_sysfs_path,
        parent_hub_sysfs_path=parent_hub,
        vendor_id=vendor_id,
        product_id=product_id,
        serial=serial,
        name=name,
        interface_number=interface_number,
        stream_index=stream_index,
    )


def inspect_tty_node(
    class_path: str,
    *,
    read_text: Callable[[str], str] = _read_text,
    realpath: Callable[[str], str] = os.path.realpath,
    device_exists: Callable[[str], bool] = os.path.exists,
    isdir: Callable[[str], bool] = os.path.isdir,
):
    """Classify one ttyACM node as an ESP32 using only sysfs identity."""
    del isdir
    basename = os.path.basename(class_path)
    if not re.fullmatch(r"ttyACM\d+", basename):
        return None
    try:
        interface_sysfs_path = realpath(os.path.join(class_path, "device"))
        physical_sysfs_path, physical_usb_path = _physical_device_from_interface(
            interface_sysfs_path, realpath=realpath
        )
        vendor_id = read_text(
            os.path.join(physical_sysfs_path, "idVendor")
        ).lower()
        product_id = read_text(
            os.path.join(physical_sysfs_path, "idProduct")
        ).lower()
        if vendor_id != ESP32_VENDOR_ID or product_id != ESP32_PRODUCT_ID:
            return None
        serial = read_text(os.path.join(physical_sysfs_path, "serial"))
        parent_hub = _parent_hub(physical_sysfs_path)
    except (OSError, ValueError):
        return None

    if not device_exists(f"/dev/{basename}"):
        return None
    return RelativeDeviceNode(
        role="esp32",
        device_path=f"/dev/{basename}",
        physical_usb_path=physical_usb_path,
        physical_sysfs_path=physical_sysfs_path,
        parent_hub_sysfs_path=parent_hub,
        vendor_id=vendor_id,
        product_id=product_id,
        serial=serial,
        name=basename,
        interface_number="",
        stream_index=0,
    )


def discover_relative_units(
    *,
    video_sysfs_glob: Callable[[], Iterable[str]] = lambda: glob.glob(
        "/sys/class/video4linux/video*"
    ),
    tty_sysfs_glob: Callable[[], Iterable[str]] = lambda: glob.glob(
        "/sys/class/tty/ttyACM*"
    ),
    read_text: Callable[[str], str] = _read_text,
    realpath: Callable[[str], str] = os.path.realpath,
    device_exists: Callable[[str], bool] = os.path.exists,
    isdir: Callable[[str], bool] = os.path.isdir,
):
    """Discover complete units using only ESP32 -> DECXIN -> H0 -> Sightac."""
    video_nodes = []
    for class_path in sorted(video_sysfs_glob(), key=_natural_key):
        node = inspect_video_node(
            class_path,
            read_text=read_text,
            realpath=realpath,
            device_exists=device_exists,
            isdir=isdir,
        )
        if node is not None:
            video_nodes.append(node)

    tty_nodes = []
    for class_path in sorted(tty_sysfs_glob(), key=_natural_key):
        node = inspect_tty_node(
            class_path,
            read_text=read_text,
            realpath=realpath,
            device_exists=device_exists,
            isdir=isdir,
        )
        if node is not None:
            tty_nodes.append(node)

    fays_by_physical = {}
    sightac_by_parent = {}
    decxin_by_parent = {}
    esp32_by_parent = {}
    for node in video_nodes:
        if node.role in {"fays_stereo", "fays_imu"}:
            fays_by_physical.setdefault(
                node.physical_sysfs_path, {}
            )[node.role] = node
        elif node.role == "sightac":
            sightac_by_parent.setdefault(
                node.parent_hub_sysfs_path, []
            ).append(node)
        elif node.role == "decxin":
            decxin_by_parent.setdefault(
                node.parent_hub_sysfs_path, []
            ).append(node)
    for node in tty_nodes:
        esp32_by_parent.setdefault(node.parent_hub_sysfs_path, []).append(node)

    fays_groups = []
    for physical_path, interfaces in sorted(
        fays_by_physical.items(), key=lambda item: _natural_key(item[0])
    ):
        stereo = interfaces.get("fays_stereo")
        imu = interfaces.get("fays_imu")
        if stereo is None or imu is None:
            continue
        fays_parent = stereo.parent_hub_sysfs_path
        if fays_parent != imu.parent_hub_sysfs_path:
            continue
        if not is_hub_path(fays_parent, read_text=read_text, isdir=isdir):
            continue
        speed = _hub_speed_mbps(fays_parent, read_text=read_text)
        if speed is None or speed < OUTER_HUB_MIN_SPEED_MBPS:
            continue
        fays_groups.append((physical_path, stereo, imu))

    candidates = []
    for esp_parent, esp32_nodes in sorted(
        esp32_by_parent.items(), key=lambda item: _natural_key(item[0])
    ):
        if len(esp32_nodes) != 1:
            continue
        esp32 = esp32_nodes[0]
        if not is_hub_path(esp_parent, read_text=read_text, isdir=isdir):
            continue
        speed = _hub_speed_mbps(esp_parent, read_text=read_text)
        if speed is None or speed > INNER_HUB_MAX_SPEED_MBPS:
            continue

        decxin_nodes = tuple(decxin_by_parent.get(esp_parent, ()))
        if len(decxin_nodes) != 1:
            continue

        outer_hub = os.path.dirname(esp_parent)
        if not is_hub_path(outer_hub, read_text=read_text, isdir=isdir):
            continue
        speed = _hub_speed_mbps(outer_hub, read_text=read_text)
        if speed is None or speed > INNER_HUB_MAX_SPEED_MBPS:
            continue
        sightac_nodes = tuple(sorted(
            sightac_by_parent.get(outer_hub, ()),
            key=lambda node: _natural_key(node.physical_usb_path),
        ))
        if len(sightac_nodes) != 2:
            continue

        esp_root_port = _root_port_key(esp32.physical_usb_path)
        if esp_root_port is None:
            continue
        fays_matches = [
            group for group in fays_groups
            if _root_port_key(group[1].physical_usb_path) == esp_root_port
        ]
        if len(fays_matches) != 1:
            continue
        candidates.append((
            fays_matches[0], esp32, decxin_nodes[0], sightac_nodes,
            outer_hub, esp_parent,
        ))

    fays_use_count = {}
    for fays_group, *_ in candidates:
        fays_use_count[fays_group[0]] = fays_use_count.get(
            fays_group[0], 0
        ) + 1

    units = []
    for (
        fays_group, esp32, decxin, sightac_nodes, outer_hub, inner_hub
    ) in candidates:
        fays_physical_path, stereo, imu = fays_group
        if fays_use_count[fays_physical_path] != 1:
            continue
        units.append(RelativeUnitTopology(
            fays_stereo=stereo,
            fays_imu=imu,
            sightac_nodes=sightac_nodes,
            decxin=decxin,
            esp32=esp32,
            outer_hub_sysfs_path=outer_hub,
            inner_hub_sysfs_path=inner_hub,
            fays_physical_sysfs_path=fays_physical_path,
        ))

    return tuple(units)


def unit_for_esp_serial(
    units: Iterable[RelativeUnitTopology],
    serial: str,
) -> Optional[RelativeUnitTopology]:
    """Return the relative unit whose ESP32 matches ``serial`` exactly."""
    wanted = str(serial or "").strip()
    if not wanted:
        return None
    for unit in units:
        if unit.esp32 is not None and unit.esp32.serial == wanted:
            return unit
    return None
