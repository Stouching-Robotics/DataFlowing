"""Shared camera-node types for the dynamic libusb/libuvc transport.

The production selector lives in ``uvc_camera_service``.  This module keeps
only the common node/topology data structures, format constants, and the
diagnostic ESP32 tty root-path helper.  It does not scan or open camera nodes.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Callable, Optional

SIGHTAC_WIDTH = 640
SIGHTAC_HEIGHT = 480
SIGHTAC_FPS = 30.0
SIGHTAC_FOURCC = "MJPG"

DECXIN_WIDTH = 1280
DECXIN_HEIGHT = 960
DECXIN_FPS = 30.0
DECXIN_FOURCC = "MJPG"

class UsbCameraSetError(RuntimeError):
    """A required camera stream cannot be reserved."""


@dataclass(frozen=True)
class UsbCameraNode:
    role: str
    device_path: str
    video_index: int
    physical_usb_path: str
    physical_sysfs_path: str
    direct_parent_hub: str
    root_hub: str
    vendor_id: str
    product_id: str
    serial: str


@dataclass(frozen=True)
class UsbCameraSetTopology:
    """One manifest-selected DECXIN plus left/right Sightac pair."""

    root_hub: str
    tactile_hub: str
    decxin: UsbCameraNode
    left: UsbCameraNode
    right: UsbCameraNode

    def node(self, role: str) -> UsbCameraNode:
        if role not in {"decxin", "left", "right"}:
            raise KeyError(role)
        return getattr(self, role)


def _root_hub_from_physical(physical_usb_path: str) -> Optional[str]:
    match = re.fullmatch(r"(\d+-\d+)(?:\.\d+)*", physical_usb_path)
    return match.group(1) if match is not None else None


def usb_root_hub_for_tty(
    device_path: str,
    *,
    realpath: Callable[[str], str] = os.path.realpath,
):
    """Return the diagnostic root hub for a ttyACM path."""
    basename = os.path.basename(str(device_path).strip())
    if not re.fullmatch(r"ttyACM\d+", basename):
        return None
    interface_path = realpath(
        os.path.join("/sys/class/tty", basename, "device")
    )
    for component in reversed(interface_path.split(os.sep)):
        physical = component.split(":", 1)[0]
        root_hub = _root_hub_from_physical(physical)
        if root_hub is not None:
            return root_hub
    return None
