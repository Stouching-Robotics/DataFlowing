"""Sightac left/right 发现、Flash 缓存和 TouchSensor 构造。"""

from __future__ import annotations

import ctypes
import glob
import importlib
import json
import math
import os
import re
import sys
import threading
from types import MappingProxyType
from typing import Callable, Optional

import cv2

from core.gripper.fays_runtime import GRIPPER_DIR


_ERROR_MARKERS = (
    "error",
    "failed",
    "exception",
    "traceback",
    "cannot",
    "unable",
    "denied",
    "timeout",
    "timed out",
    "device busy",
    "resource busy",
    "错误",
    "失败",
)
_SONIX_MODALIAS_PREFIX = "usb:v0c45p636f"
_SIGHTAC_API_PACKAGES = frozenset({
    "api",
    "api_legacy_2_4_5",
    "api_new",
    "api_v3_2_2_ksq",
})


class TactileDiscovery:
    """发现两路 Sonix Sightac，并在开流前缓存完整 Flash 参数。

    Linux 探测是 fail-closed 的：只有 ``/sys/class/video4linux/videoN`` 的
    ``name``、``index`` 和节点自身 ``device/modalias`` 都能只读确认、
    ``index == 0`` 且 VID/PID 为 ``0c45:636f`` 时，节点才会交给 Sonix
    SDK。旧模板身份只读解析 video class symlink 对应的 USB interface 及其
    唯一物理父设备，不遍历 USB 总线，不修改 sysfs，也不执行 USB reset。
    """

    def __init__(
        self,
        *,
        sysfs_root: str = "/sys/class/video4linux",
        dev_root: str = "/dev",
        platform: Optional[str] = None,
        sightac_root: Optional[str] = None,
        cv2_module=cv2,
        path_exists: Callable[[str], bool] = os.path.exists,
        logger: Callable[..., None] = print,
        full_logs: Optional[bool] = None,
        old_sensor_template_file: Optional[str] = None,
        api_package: Optional[str] = None,
    ):
        self.sysfs_root = os.path.abspath(sysfs_root)
        self.dev_root = os.path.abspath(dev_root)
        self.platform = platform or sys.platform
        self.sightac_root = sightac_root
        requested_api = (
            api_package
            or os.environ.get("KSQ_SIGHTAC_API_PACKAGE")
            or "api_new"
        ).strip()
        if requested_api not in _SIGHTAC_API_PACKAGES:
            raise ValueError(
                "unsupported Sightac API package: "
                f"{requested_api!r}; expected one of "
                f"{sorted(_SIGHTAC_API_PACKAGES)}"
            )
        self.api_package = requested_api
        self._cv2 = cv2_module
        self._path_exists = path_exists
        self._logger = logger
        if full_logs is None:
            full_logs = os.environ.get(
                "KSQ_FULL_LOG", "").strip().lower() in {
                    "1", "true", "yes", "on",
                }
        self.full_logs = bool(full_logs)
        self.flash_param_cache = {}
        self.flash_full_params_cache = {}
        self._camera_names = None
        self._sonix_dshow_map = None
        self._scan_lock = threading.RLock()
        self._reported_rejections = set()
        self._reported_touch_sensor_runtime = False
        self._old_sensor_template_file = (
            None
            if old_sensor_template_file is None
            else os.path.abspath(old_sensor_template_file)
        )
        self._old_sensor_template = self._load_old_sensor_template(
            self._old_sensor_template_file
        )

    @staticmethod
    def _validated_template_vector(value, name: str, length: int):
        if (
            not isinstance(value, list)
            or len(value) != int(length)
        ):
            raise ValueError(
                f"{name} must contain exactly {int(length)} values"
            )
        result = []
        for item in value:
            if isinstance(item, bool):
                raise ValueError(f"{name} must contain finite numbers")
            number = float(item)
            if not math.isfinite(number):
                raise ValueError(f"{name} must contain finite numbers")
            result.append(number)
        return result

    def _load_old_sensor_template(self, path: Optional[str]):
        """加载明确禁用力输出的旧传感器临时模板。"""
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as stream:
                encoded = json.load(stream)
            if not isinstance(encoded, dict):
                raise ValueError("template root must be an object")
            if encoded.get("enabled") is not True:
                return None
            if encoded.get("mode") != "old-sensor-template-no-force":
                raise ValueError("unsupported template mode")
            if encoded.get("force_output_enabled") is not False:
                raise ValueError(
                    "old-sensor template must keep force output disabled"
                )
            usb_identities = encoded.get("usb_identities")
            templates = encoded.get("templates")
            if (
                not isinstance(usb_identities, dict)
                or not isinstance(templates, dict)
            ):
                raise ValueError(
                    "usb_identities and templates must be objects"
                )

            validated_identities = {}
            validated_templates = {}
            for side in ("left", "right"):
                identity = usb_identities.get(side)
                if not isinstance(identity, dict):
                    raise ValueError(
                        f"missing USB identity for {side!r}"
                    )
                vendor_id = str(
                    identity.get("vendor_id", "")).strip().lower()
                product_id = str(
                    identity.get("product_id", "")).strip().lower()
                serial = str(identity.get("serial", "")).strip()
                interface_number = str(
                    identity.get("interface_number", "")).strip().lower()
                stream_index = identity.get("stream_index")
                if (
                    not re.fullmatch(r"[0-9a-f]{4}", vendor_id)
                    or not re.fullmatch(r"[0-9a-f]{4}", product_id)
                    or not serial
                    or not re.fullmatch(r"[A-Za-z0-9._-]+", serial)
                    or not re.fullmatch(
                        r"[0-9a-f]{2}", interface_number)
                    or isinstance(stream_index, bool)
                    or not isinstance(stream_index, int)
                    or stream_index != 0
                ):
                    raise ValueError(
                        f"invalid USB identity for {side!r}"
                    )
                if (
                    vendor_id != "0c45"
                    or product_id != "636f"
                    or interface_number != "00"
                ):
                    raise ValueError(
                        f"non-Sightac USB identity for {side!r}"
                    )
                validated_identities[side] = {
                    "vendor_id": vendor_id,
                    "product_id": product_id,
                    "serial": serial,
                    "interface_number": interface_number,
                    "stream_index": stream_index,
                }

                source = templates.get(side)
                if not isinstance(source, dict):
                    raise ValueError(
                        f"missing template for {side!r}"
                    )
                device_type = str(
                    source.get("device_type", "")).strip().lower()
                if device_type not in {"planar", "curved", "bevel"}:
                    raise ValueError(
                        f"invalid device_type for {side!r}"
                    )
                params = {
                    "device_type": device_type,
                    "fx_params": self._validated_template_vector(
                        source.get("fx_params"),
                        f"{side}.fx_params",
                        5,
                    ),
                    "fy_params": self._validated_template_vector(
                        source.get("fy_params"),
                        f"{side}.fy_params",
                        5,
                    ),
                    "fz_params": self._validated_template_vector(
                        source.get("fz_params"),
                        f"{side}.fz_params",
                        5,
                    ),
                    "fw_params": self._validated_template_vector(
                        source.get("fw_params"),
                        f"{side}.fw_params",
                        9,
                    ),
                    "flash_state": "old-sensor-template",
                    "calibration_source":
                        "old-sensor-template-unverified",
                }
                if device_type == "planar":
                    fw = params["fw_params"]
                    x1, x2, y1, y2 = (
                        int(fw[1]),
                        int(fw[2]),
                        int(fw[3]),
                        int(fw[4]),
                    )
                    if not (
                        0 <= x1 < x2 <= 640
                        and 0 <= y1 < y2 <= 480
                    ):
                        raise ValueError(
                            f"invalid planar ROI for {side!r}"
                        )
                validated_templates[side] = params

            if (
                validated_identities["left"]
                == validated_identities["right"]
                or validated_identities["left"]["serial"]
                == validated_identities["right"]["serial"]
            ):
                raise ValueError(
                    "left and right cannot use the same USB identity"
                )
            self._logger(
                "[Tactile] old-sensor template armed: "
                f"left={validated_identities['left']['serial']} "
                f"right={validated_identities['right']['serial']} "
                "(force output disabled)"
            )
            return {
                "usb_identities": validated_identities,
                "templates": validated_templates,
            }
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self._logger(
                f"[Tactile] old-sensor template disabled: {exc}"
            )
            return None

    @property
    def old_sensor_template_enabled(self) -> bool:
        return self._old_sensor_template is not None

    def physical_usb_port(self, video_index: int) -> Optional[str]:
        """从 video4linux class symlink 提取仅用于诊断的 USB 端口。"""
        if not self.platform.startswith("linux"):
            return None
        device_path = os.path.realpath(os.path.join(
            self._candidate_class_dir(int(video_index)),
            "device",
        ))
        interface_name = os.path.basename(device_path)
        port, separator, _interface = interface_name.partition(":")
        if not separator or not port:
            return None
        return port

    def usb_video_identity(self, video_index: int):
        """只读返回 Sightac 节点的 VID/PID、serial、interface 和 stream。"""
        try:
            index = int(video_index)
        except (TypeError, ValueError):
            return None
        if not self.platform.startswith("linux"):
            return None
        if self.probe_guard_reason(index) is not None:
            return None
        class_dir = self._candidate_class_dir(index)
        try:
            with open(
                os.path.join(class_dir, "device", "modalias"),
                encoding="utf-8",
            ) as stream:
                modalias = stream.read().strip().lower()
            match = re.match(
                r"usb:v([0-9a-f]{4})p([0-9a-f]{4})",
                modalias,
            )
            if match is None:
                return None
            with open(
                os.path.join(
                    class_dir, "device", "bInterfaceNumber"),
                encoding="utf-8",
            ) as stream:
                interface_number = stream.read().strip().lower()
            with open(
                os.path.join(class_dir, "index"),
                encoding="utf-8",
            ) as stream:
                stream_index = int(stream.read().strip())

            interface_path = os.path.realpath(
                os.path.join(class_dir, "device"))
            physical_path = (
                os.path.dirname(interface_path)
                if ":" in os.path.basename(interface_path)
                else interface_path
            )
            with open(
                os.path.join(physical_path, "serial"),
                encoding="utf-8",
            ) as stream:
                serial = stream.read().strip()
        except (OSError, ValueError):
            return None
        if (
            not serial
            or not re.fullmatch(r"[A-Za-z0-9._-]+", serial)
            or not re.fullmatch(
                r"[0-9a-f]{2}", interface_number)
        ):
            return None
        return {
            "vendor_id": match.group(1),
            "product_id": match.group(2),
            "serial": serial,
            "interface_number": interface_number,
            "stream_index": stream_index,
        }

    @staticmethod
    def _usb_identity_matches(actual, expected) -> bool:
        if not isinstance(actual, dict):
            return False
        return all(
            actual.get(key) == expected.get(key)
            for key in (
                "vendor_id",
                "product_id",
                "serial",
                "interface_number",
                "stream_index",
            )
        )

    @staticmethod
    def _usb_topology_identity_matches(actual, expected) -> bool:
        """Match a topology-pinned Sightac without trusting duplicate serials."""
        if not isinstance(actual, dict):
            return False
        return all(
            actual.get(key) == expected.get(key)
            for key in (
                "vendor_id",
                "product_id",
                "interface_number",
                "stream_index",
            )
        )

    def resolve_old_sensor_template_camera(
        self, side: str, excluded=(),
    ) -> Optional[int]:
        """按旧相机严格 USB 身份给无 Flash 相机分配 left/right。"""
        normalized = str(side).strip().lower()
        template = self._old_sensor_template
        if template is None or normalized not in {"left", "right"}:
            return None
        expected = template["usb_identities"][normalized]
        excluded_indices = {
            int(index) for index in excluded if index is not None
        }
        matches = [
            index
            for index in self.safe_sonix_indices(max_devices=64)
            if index not in excluded_indices
            and self._usb_identity_matches(
                self.usb_video_identity(index), expected)
        ]
        if len(matches) != 1:
            self._logger(
                "[Tactile] old-sensor template identity unresolved: "
                f"{normalized} expects USB serial "
                f"{expected['serial']}, "
                f"matches={matches}"
            )
            return None
        return int(matches[0])

    def apply_old_sensor_template(
        self,
        side: str,
        video_index: int,
        *,
        topology_verified: bool = False,
    ):
        """仅在硬件完整 Flash 缺失时注入旧模板；真实 Flash 永远优先。"""
        normalized = str(side).strip().lower()
        index = int(video_index)
        template = self._old_sensor_template
        if template is None or normalized not in {"left", "right"}:
            return None
        with self._scan_lock:
            cached = self.flash_full_params_cache.get(index)
            if cached is not None:
                return cached
        expected = template["usb_identities"][normalized]
        actual = self.usb_video_identity(index)
        exact_identity = self._usb_identity_matches(actual, expected)
        physical_port = self.physical_usb_port(index)
        expected_port_suffix = (
            ".2" if normalized == "left" else ".1"
        )
        topology_identity = (
            bool(topology_verified)
            and isinstance(physical_port, str)
            and physical_port.endswith(expected_port_suffix)
            and self._usb_topology_identity_matches(actual, expected)
        )
        if not exact_identity and not topology_identity:
            actual_serial = (
                actual.get("serial")
                if isinstance(actual, dict)
                else None
            )
            self._logger(
                "[Tactile] old-sensor template rejected: "
                f"{normalized}=video{index} has USB serial "
                f"{actual_serial!r}, expected {expected['serial']!r}"
            )
            return None
        params = {
            key: (
                list(value)
                if isinstance(value, list)
                else value
            )
            for key, value in template["templates"][normalized].items()
        }
        if topology_identity and not exact_identity:
            params["flash_state"] = "old-sensor-template-topology"
            params["calibration_source"] = (
                "old-sensor-template-topology-unverified"
            )
        with self._scan_lock:
            existing = self.flash_full_params_cache.get(index)
            if existing is not None:
                return existing
            self.flash_full_params_cache[index] = params
            self.flash_param_cache[normalized] = index
        fw = params["fw_params"]
        actual_serial = (
            actual.get("serial")
            if isinstance(actual, dict)
            else None
        )
        self._logger(
            "[Tactile] old-sensor template applied: "
            f"{normalized}=video{index} serial={actual_serial} "
            f"port={physical_port} "
            f"device={params['device_type']} "
            f"ROI=({int(fw[1])},{int(fw[3])})"
            f"-({int(fw[2])},{int(fw[4])}); "
            "Fx/Fy/Fz publication disabled"
        )
        return params

    def clear_cache(self) -> None:
        with self._scan_lock:
            self.flash_param_cache.clear()
            self.flash_full_params_cache.clear()
            self._camera_names = None
            self._sonix_dshow_map = None
            self._reported_rejections.clear()

    def cache_external_flash_params(self, video_index: int, params):
        """Store one live libusb Flash read for the next sensor instance.

        This is deliberately an in-memory handoff only.  The caller must
        provide parameters read during the current UVC discovery pass; no
        template, persistent cache, or default calibration is accepted here.
        """
        index = int(video_index)
        if not isinstance(params, dict):
            raise ValueError("external Flash parameters must be an object")
        device_type = str(params.get("device_type") or "").strip().lower()
        if device_type not in {"planar", "curved", "bevel"}:
            raise ValueError(
                f"external Flash device_type is invalid: {device_type!r}")
        expected_lengths = {
            "fx_params": 5,
            "fy_params": 5,
            "fz_params": 21,
            "ft_params": 20,
            "fw_params": 9,
        }
        copied = {"device_type": device_type}
        for key, length in expected_lengths.items():
            values = params.get(key)
            if not isinstance(values, (list, tuple)) or len(values) != length:
                raise ValueError(
                    f"external Flash {key} must contain {length} values")
            normalized = []
            for value_index, value in enumerate(values):
                if (
                    key == "fw_params"
                    and device_type == "bevel"
                    and value_index >= 5
                    and (
                        value is None
                        or (
                            isinstance(value, float)
                            and math.isnan(value)
                        )
                    )
                ):
                    # Reserved bevel-table tail; it is not consumed by the
                    # current ROI/line algorithm.  The libuvc JSON path uses
                    # null, while older in-process handoffs may use NaN.
                    normalized.append(float("nan"))
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"external Flash {key}[{value_index}] is not numeric: "
                        f"{value!r}"
                    ) from exc
                if not math.isfinite(number):
                    raise ValueError(
                        f"external Flash {key}[{value_index}] contains a "
                        f"non-finite value: {value!r}")
                normalized.append(number)
            copied[key] = normalized
        copied["flash_state"] = str(
            params.get("flash_state") or "programmed")
        copied["calibration_source"] = "hardware-live-flash"
        with self._scan_lock:
            self.flash_full_params_cache[index] = copied
        self._logger(
            f"[FlashCache] live libusb read cached for video{index}: "
            f"device={device_type} source=hardware-live-flash")
        return copied

    def cache_snapshot(self):
        """返回不会暴露内部列表的完整缓存副本。"""
        with self._scan_lock:
            labels = MappingProxyType(dict(self.flash_param_cache))
            full = {}
            for index, params in self.flash_full_params_cache.items():
                full[int(index)] = MappingProxyType({
                    "device_type": str(params["device_type"]),
                    "fx_params": tuple(params["fx_params"]),
                    "fy_params": tuple(params["fy_params"]),
                    "fz_params": tuple(params["fz_params"]),
                    "fw_params": tuple(params["fw_params"]),
                    "ft_params": tuple(params.get("ft_params", ())),
                    "flash_state": str(
                        params.get("flash_state", "unknown")),
                    "calibration_source": str(
                        params.get("calibration_source", "hardware")),
                })
            return labels, MappingProxyType(full)

    def _candidate_class_dir(self, video_index: int) -> str:
        return os.path.join(self.sysfs_root, f"video{int(video_index)}")

    def _candidate_device_path(self, video_index: int) -> str:
        return os.path.join(self.dev_root, f"video{int(video_index)}")

    def probe_guard_reason(self, video_index: int):
        """返回 ``None`` 表示节点可探测，否则返回 fail-closed 原因。"""
        try:
            index = int(video_index)
        except (TypeError, ValueError):
            return "invalid video index"
        if index < 0:
            return "invalid video index"
        if not self.platform.startswith("linux"):
            return None
        if not self._path_exists(self._candidate_device_path(index)):
            return "video node does not exist"
        class_dir = self._candidate_class_dir(index)
        try:
            with open(
                os.path.join(class_dir, "index"),
                encoding="utf-8",
            ) as stream:
                capture_index = int(stream.read().strip())
            with open(
                os.path.join(class_dir, "name"),
                encoding="utf-8",
            ) as stream:
                device_name = stream.read().strip().lower()
            with open(
                os.path.join(class_dir, "device", "modalias"),
                encoding="utf-8",
            ) as stream:
                modalias = stream.read().strip().lower()
        except (OSError, ValueError):
            return "video identity is unreadable"
        if capture_index != 0:
            return f"non-capture V4L2 index={capture_index}"
        if not device_name:
            return "empty video node name"
        if "ftdi" in device_name or "superspeed video bridge" in device_name:
            return "Fays FTDI bridge"
        if not modalias.startswith(_SONIX_MODALIAS_PREFIX):
            return "non-Sonix VID/PID"
        return None

    def is_safe_sonix_probe(self, video_index: int) -> bool:
        return self.probe_guard_reason(video_index) is None

    def _linux_video_indices(self):
        """按数字顺序返回 sysfs 中实际存在的 ``videoN`` 节点。"""
        indices = set()
        pattern = os.path.join(self.sysfs_root, "video*")
        for class_dir in glob.glob(pattern):
            suffix = os.path.basename(class_dir)[5:]
            if suffix.isdigit():
                indices.add(int(suffix))
        return tuple(sorted(indices))

    def _linux_sonix_indices(
        self, max_devices: int, *, report_rejections: bool,
    ):
        """发现实际 Sonix 节点；``max_devices`` 限制候选数而非节点号。"""
        limit = max(0, int(max_devices))
        if limit == 0:
            return ()
        candidates = []
        for index in self._linux_video_indices():
            reason = self.probe_guard_reason(index)
            if reason is not None:
                if report_rejections:
                    self._log_probe_rejection(index, reason)
                continue
            candidates.append(index)
            if len(candidates) >= limit:
                break
        return tuple(candidates)

    def safe_sonix_indices(self, max_devices: int = 10):
        """返回实际存在且 VID/PID 精确为 ``0c45:636f`` 的采集节点。

        ``max_devices`` 是最多返回的安全候选数量，不是 ``/dev/videoN`` 的
        N 上限；因此相机号漂移到 video10 以上仍能被发现。
        """
        if not self.platform.startswith("linux"):
            return ()
        return self._linux_sonix_indices(
            max_devices, report_rejections=False,
        )

    def _log_probe_rejection(self, video_index: int, reason: str) -> None:
        key = (int(video_index), str(reason))
        if key in self._reported_rejections:
            return
        self._reported_rejections.add(key)
        self._logger(
            f"[Cameras] Sonix probe skipped: video{video_index} ({reason})")

    def concise_sdk_print(self, *args, **kwargs) -> None:
        """第三方 Sightac 日志的唯一父进程过滤器。"""
        if self.full_logs:
            self._logger(*args, **kwargs)
            return
        message = " ".join(str(arg) for arg in args).strip()
        lowered = message.lower()
        if any(marker in lowered for marker in _ERROR_MARKERS):
            self._logger(*args, **kwargs)

    def _ensure_sightac_import_path(self) -> None:
        candidates = []
        if self.sightac_root:
            candidates.append(self.sightac_root)
        local = os.path.join(GRIPPER_DIR, "sightac_sdk-main")
        candidates.append(str(local))
        explicit = os.environ.get("KSQ_SIGHTAC_ROOT")
        if explicit:
            candidates.append(explicit)
        for candidate in candidates:
            normalized = os.path.abspath(os.path.normpath(candidate))
            if os.path.isdir(normalized):
                if normalized not in sys.path:
                    sys.path.insert(0, normalized)
                return
        raise RuntimeError("local Sightac SDK path is unavailable")

    def install_xu_probe_guard(self, xu_module):
        """给 SDK init 入口安装本适配器的 fail-closed guard。"""
        xu_module.print = self.concise_sdk_print
        original = getattr(
            xu_module, "_ksq_original_init_sonix_sdk", None)
        if original is None:
            original = xu_module.init_sonix_sdk
            xu_module._ksq_original_init_sonix_sdk = original

            def guarded_init(*args, **kwargs):
                video_index = (
                    args[0] if args
                    else kwargs.get("video_index")
                )
                guard = getattr(xu_module, "_ksq_active_probe_guard", None)
                if (video_index is not None and callable(guard)
                        and not guard(int(video_index))):
                    reporter = getattr(
                        xu_module, "_ksq_probe_rejection_reporter", None)
                    reason_getter = getattr(
                        xu_module, "_ksq_probe_guard_reason", None)
                    reason = (
                        reason_getter(int(video_index))
                        if callable(reason_getter)
                        else "rejected by tactile adapter"
                    )
                    if callable(reporter):
                        reporter(int(video_index), reason)
                    return False
                return original(*args, **kwargs)

            xu_module.init_sonix_sdk = guarded_init
        # wrapper 动态读取当前 adapter，避免重复包裹 SDK 全局函数。
        xu_module._ksq_active_probe_guard = self.is_safe_sonix_probe
        xu_module._ksq_probe_guard_reason = self.probe_guard_reason
        xu_module._ksq_probe_rejection_reporter = (
            self._log_probe_rejection)
        return xu_module

    def _load_xu_module(self):
        self._ensure_sightac_import_path()
        xu_camera = importlib.import_module(
            f"{self.api_package}.xu_camera")
        return self.install_xu_probe_guard(xu_camera)

    def _load_touch_sensor_class(self):
        self._ensure_sightac_import_path()
        api_module = importlib.import_module(self.api_package)
        TouchSensor = api_module.TouchSensor
        module = sys.modules.get(TouchSensor.__module__)
        with self._scan_lock:
            if not self._reported_touch_sensor_runtime:
                version = str(
                    getattr(api_module, "VERSION", "unknown")
                ).strip() or "unknown"
                vendor_base = str(
                    getattr(api_module, "VENDOR_BASE_VERSION", "unknown")
                ).strip() or "unknown"
                compatibility_base = str(
                    getattr(
                        api_module,
                        "COMPATIBILITY_BASE_VERSION",
                        "unknown",
                    )
                ).strip() or "unknown"
                source_file = os.path.abspath(
                    str(getattr(module, "__file__", "unknown"))
                ) if module is not None else "unknown"
                self._logger(
                    "[Tactile] SDK touch_sensor.py "
                    f"version={version} package={self.api_package} "
                    f"vendor_base={vendor_base} "
                    f"compatibility_base={compatibility_base} "
                    f"file={source_file}"
                )
                self._reported_touch_sensor_runtime = True
        if module is not None:
            module.print = self.concise_sdk_print
        # TouchSensor 会间接使用同一个 xu_camera；在构造前确保 guard 已安装。
        self._load_xu_module()
        return TouchSensor

    def get_camera_names(self):
        """只读枚举名称；Linux 仅访问 video4linux class 目录。"""
        with self._scan_lock:
            if self._camera_names is not None:
                return list(self._camera_names)
            if self.platform.startswith("linux"):
                names = {}
                pattern = os.path.join(self.sysfs_root, "video*")
                for class_dir in sorted(glob.glob(pattern)):
                    suffix = os.path.basename(class_dir)[5:]
                    if not suffix.isdigit():
                        continue
                    index = int(suffix)
                    try:
                        with open(
                            os.path.join(class_dir, "name"),
                            encoding="utf-8",
                        ) as stream:
                            name = stream.read().strip()
                    except OSError:
                        # 名称不可读的节点不能作为名称回退候选。
                        name = "?"
                    names[index] = name
                count = max(names) + 1 if names else 0
                self._camera_names = tuple(
                    names.get(index, "?") for index in range(count))
                return list(self._camera_names)
            try:
                from pygrabber.dshow_graph import FilterGraph
                self._camera_names = tuple(
                    FilterGraph().get_input_devices())
            except Exception:
                self._camera_names = ()
            return list(self._camera_names)

    def clear_camera_name_cache(self) -> None:
        with self._scan_lock:
            self._camera_names = None

    def find_camera_by_name(
        self, keyword: str, occurrence: int = 0,
    ) -> int:
        keyword = str(keyword).lower().strip()
        found = 0
        for index, name in enumerate(self.get_camera_names()):
            if keyword in str(name).lower():
                if found == int(occurrence):
                    return index
                found += 1
        return -1

    def get_touch_xu_ids(
        self, max_devices: int = 5, expected_count: Optional[int] = None,
    ):
        """调试枚举，只保留 SDK 明确识别出的 Sonix 触觉候选。"""
        valid = []
        try:
            xu_camera = self._load_xu_module()
        except Exception as exc:
            self._logger(f"[Heatmap] XU enum unavailable: {exc}")
            return valid
        if self.platform.startswith("linux"):
            indices = self._linux_sonix_indices(
                max_devices, report_rejections=True,
            )
        else:
            indices = range(max(0, int(max_devices)))
        for index in indices:
            try:
                camera = xu_camera.XuCamera(index)
                if not camera.is_available():
                    continue
                info = camera.get_device_info() or {}
                serial_number = str(
                    info.get("serial_number", "")).strip()
                vid_pid = str(info.get("vid_pid", "")).strip().lower()
                self._logger(
                    f"[Heatmap] XU candidate {index}: "
                    f"SN={serial_number or '?'} VIDPID={vid_pid or '?'}"
                )
                if serial_number.startswith("SN") or vid_pid == "0c45:636f":
                    valid.append(index)
                    if (
                        expected_count is not None
                        and len(valid) >= int(expected_count)
                    ):
                        break
            except Exception as exc:
                self._logger(
                    f"[Heatmap] XU candidate {index} failed: {exc}")
        self._logger(f"[Heatmap] touch XU ids: {valid}")
        return valid

    @staticmethod
    def _decode_ascii(raw: bytes) -> str:
        if all(byte == 0xFF for byte in raw):
            return "unknown"
        return raw.split(b"\x00", 1)[0].decode(
            "ascii", errors="replace")

    def read_flash_side(self, video_index: int) -> Optional[str]:
        """Read Flash ``device_params[1]`` (``left``/``right``) for one node.

        The node is opened through the XU control channel only and is
        released before returning; no ``VideoCapture`` or image stream is
        created here.
        """
        try:
            index = int(video_index)
        except (TypeError, ValueError):
            return None
        reason = self.probe_guard_reason(index)
        if reason is not None:
            self._log_probe_rejection(index, reason)
            return None
        try:
            xc = self._load_xu_module()
        except Exception as exc:
            self._logger(f"[FlashSide] XU unavailable: {exc}")
            return None
        try:
            if getattr(xc, "_sdk_initialized", False):
                library = getattr(xc, "_sonix_lib", None)
                if library is not None:
                    try:
                        library.SonixCam_UnInit()
                    except Exception:
                        pass
                xc._sdk_initialized = False
            if not xc.init_sonix_sdk(video_index=index):
                return None
            library = getattr(xc, "_sonix_lib", None)
            if library is None:
                return None
            buf = (ctypes.c_ubyte * 8)()
            if not library.SonixCam_SerialFlashRead(
                xc.DEVICE_BASE_ADDR + 1 * 8, buf, 8
            ):
                return None
            value = self._decode_ascii(bytes(buf)).lower().strip()
            return value if value in {"left", "right"} else None
        except Exception as exc:
            self._logger(f"[FlashSide] video{index} read failed: {exc}")
            return None
        finally:
            library = getattr(xc, "_sonix_lib", None)
            if (
                getattr(xc, "_sdk_initialized", False)
                and library is not None
            ):
                try:
                    library.SonixCam_UnInit()
                except Exception:
                    pass
            xc._sdk_initialized = False

    def read_sonix_product_serial(self, video_index: int) -> Optional[str]:
        """Read the Sonix XU product serial for one fail-closed node."""
        try:
            index = int(video_index)
        except (TypeError, ValueError):
            return None
        reason = self.probe_guard_reason(index)
        if reason is not None:
            self._log_probe_rejection(index, reason)
            return None
        try:
            xc = self._load_xu_module()
            if getattr(xc, "_sdk_initialized", False):
                library = getattr(xc, "_sonix_lib", None)
                if library is not None:
                    try:
                        library.SonixCam_UnInit()
                    except Exception:
                        pass
                xc._sdk_initialized = False
            if not xc.init_sonix_sdk(video_index=index):
                return None
            camera = xc.XuCamera(usb_id=index)
            info = camera.get_device_info() or {}
            serial = str(info.get("serial_number", "")).strip()
            return serial or None
        except Exception as exc:
            self._logger(
                f"[SonixSerial] video{index} read failed: {exc}"
            )
            return None
        finally:
            library = getattr(xc, "_sonix_lib", None)
            if (
                getattr(xc, "_sdk_initialized", False)
                and library is not None
            ):
                try:
                    library.SonixCam_UnInit()
                except Exception:
                    pass
            xc._sdk_initialized = False

    @staticmethod
    def _decode_double(raw: bytes) -> float:
        if all(byte == 0xFF for byte in raw):
            return 0.0
        value = ctypes.c_double()
        ctypes.memmove(ctypes.addressof(value), raw, 8)
        return float(value.value)

    def _read_full_params(self, xc, read_at):
        reads = {"ok": 0, "all_ff": 0, "payload": 0, "failed": 0}

        def read_raw(address, buf):
            if not read_at(address, buf):
                reads["failed"] += 1
                return False
            reads["ok"] += 1
            if all(byte == 0xFF for byte in bytes(buf)):
                reads["all_ff"] += 1
            else:
                reads["payload"] += 1
            return True

        device_values = []
        for offset in range(5):
            buf = (ctypes.c_ubyte * 8)()
            if read_raw(xc.DEVICE_BASE_ADDR + offset * 8, buf):
                device_values.append(self._decode_ascii(bytes(buf)))
            else:
                device_values.append("unknown")

        def read_doubles(base_address, count):
            values = []
            for offset in range(count):
                buf = (ctypes.c_ubyte * 8)()
                if read_raw(base_address + offset * 8, buf):
                    values.append(self._decode_double(bytes(buf)))
                else:
                    values.append(0.0)
            return values

        result = {
            "device_type": (
                device_values[0] if device_values else "unknown"),
            "fx_params": read_doubles(xc.FX_BASE_ADDR, 5),
            "fy_params": read_doubles(xc.FY_BASE_ADDR, 5),
            # 0902 FZ21 包含模型选择及两种布局；FT20 包含四组区域。
            # 必须完整读取，不可固定把第7/8项当成所有模型的R/B阈值。
            "fz_params": read_doubles(xc.FZ_BASE_ADDR, 21),
            "ft_params": read_doubles(0x47000, 20),
            "fw_params": read_doubles(xc.FW_BASE_ADDR, 9),
        }
        if reads["payload"]:
            result["flash_state"] = (
                "programmed" if not reads["failed"] else "partial")
        else:
            result["flash_state"] = "unreadable"
        result["calibration_source"] = "hardware"
        return result

    def read_and_cache_flash_params(self, xc, video_index: int):
        """从已绑定节点读取 device/fx/fy/fz/fw 全量参数。"""
        video_index = int(video_index)
        with self._scan_lock:
            cached = self.flash_full_params_cache.get(video_index)
            if cached is not None:
                return cached
        library = getattr(xc, "_sonix_lib", None)
        if library is None:
            return None
        try:
            cached = None
            for attempt in range(1, 3):
                candidate = self._read_full_params(
                    xc,
                    lambda address, buf: library.SonixCam_SerialFlashRead(
                        address, buf, 8),
                )
                if (
                    candidate["flash_state"] == "programmed"
                    and candidate["device_type"]
                    in {"planar", "curved", "bevel"}
                ):
                    cached = candidate
                    break
                if attempt == 1:
                    self._logger(
                        f"[FlashCache] video{video_index}: direct read "
                        f"returned {candidate['flash_state']} "
                        f"(device={candidate['device_type']!r}); retrying"
                    )
            if cached is None:
                self._logger(
                    f"[FlashCache] video{video_index} read failed: "
                    "no usable hardware parameters after 2 direct reads; "
                    "not caching or injecting defaults"
                )
                return None
            with self._scan_lock:
                self.flash_full_params_cache[video_index] = cached
            self._logger(
                f"[FlashCache] video{video_index}: "
                f"device={cached['device_type']}, "
                f"state={cached['flash_state']}, "
                f"source={cached['calibration_source']}, "
                f"fx={cached['fx_params'][0]:.4f}, "
                f"fy={cached['fy_params'][0]:.4f}, "
                f"fz={cached['fz_params'][0]:.4f}"
            )
            return cached
        except Exception as exc:
            self._logger(
                f"[FlashCache] video{video_index} read failed: {exc}")
            return None

    _read_and_cache_flash_params = read_and_cache_flash_params

    def _cache_windows_flash_params(
        self, xc, library, device_pointer, video_index: int,
    ):
        try:
            cached = self._read_full_params(
                xc,
                lambda address, buf: library.SonixCam_ReadFromSF(
                    device_pointer, address, buf, 8),
            )
            self.flash_full_params_cache[int(video_index)] = cached
            return cached
        except Exception as exc:
            self._logger(
                f"[FlashCache] video{video_index} read failed: {exc}")
            return None

    def scan_camera_by_flash_param(
        self,
        target: str,
        param_index: int = 1,
        max_devices: int = 10,
        *,
        candidate_indices=None,
    ):
        """按 Flash ``device_params[param_index]`` 匹配 left/right。"""
        normalized = str(target).lower().strip()
        with self._scan_lock:
            if normalized in self.flash_param_cache:
                cached = self.flash_param_cache[normalized]
                self._logger(
                    f"[Cameras] '{normalized}' → idx={cached} (cached)")
                return cached

            try:
                xc = self._load_xu_module()
            except Exception as exc:
                self._logger(f"[Cameras] Flash scan unavailable: {exc}")
                return None

            result = None
            flash_address = (
                xc.DEVICE_BASE_ADDR + int(param_index) * 8)
            if self.platform.startswith("win"):
                if xc._sonix_lib is None:
                    xc.init_sonix_sdk()
                library = xc._sonix_lib
                if library is None:
                    return None
                devices = (xc.ScDevice * int(max_devices))()
                count = ctypes.c_uint(0)
                if not library.SonixCam_EnumCameras(
                    ctypes.byref(count), devices, int(max_devices)
                ):
                    return None
                for sonix_index in range(
                    min(count.value, int(max_devices))
                ):
                    pointer = ctypes.pointer(devices[sonix_index])
                    buf = (ctypes.c_ubyte * 8)()
                    if not library.SonixCam_ReadFromSF(
                        pointer, flash_address, buf, 8
                    ):
                        continue
                    self._build_sonix_dshow_map()
                    video_index = next((
                        dshow_index
                        for dshow_index, mapped in
                        self._sonix_dshow_map.items()
                        if mapped == sonix_index
                    ), sonix_index)
                    if video_index not in self.flash_full_params_cache:
                        self._cache_windows_flash_params(
                            xc, library, pointer, video_index)
                    value = self._decode_ascii(bytes(buf)).lower().strip()
                    if value == normalized:
                        result = video_index
                        break
            else:
                try:
                    if candidate_indices is None:
                        linux_indices = self._linux_sonix_indices(
                            max_devices, report_rejections=True,
                        )
                    else:
                        linux_indices = []
                        for value in candidate_indices:
                            try:
                                video_index = int(value)
                            except (TypeError, ValueError):
                                continue
                            reason = self.probe_guard_reason(video_index)
                            if reason is not None:
                                self._log_probe_rejection(
                                    video_index, reason)
                                continue
                            if video_index not in linux_indices:
                                linux_indices.append(video_index)
                        linux_indices = tuple(
                            linux_indices[:max(0, int(max_devices))]
                        )
                    for video_index in linux_indices:
                        if getattr(xc, "_sdk_initialized", False):
                            library = getattr(xc, "_sonix_lib", None)
                            if library is not None:
                                try:
                                    library.SonixCam_UnInit()
                                except Exception:
                                    pass
                            xc._sdk_initialized = False
                        if not xc.init_sonix_sdk(
                            video_index=video_index
                        ):
                            continue
                        library = getattr(xc, "_sonix_lib", None)
                        if library is None:
                            continue
                        self.read_and_cache_flash_params(
                            xc, video_index)
                        buf = (ctypes.c_ubyte * 8)()
                        if library.SonixCam_SerialFlashRead(
                            flash_address, buf, 8
                        ):
                            value = self._decode_ascii(
                                bytes(buf)).lower().strip()
                            if value == normalized:
                                result = video_index
                                break
                finally:
                    library = getattr(xc, "_sonix_lib", None)
                    if (
                        getattr(xc, "_sdk_initialized", False)
                        and library is not None
                    ):
                        try:
                            library.SonixCam_UnInit()
                        except Exception:
                            pass
                    xc._sdk_initialized = False

            if result is not None:
                self.flash_param_cache[normalized] = int(result)
                self._logger(
                    f"[Cameras] Flash scan: '{normalized}' → "
                    f"video index {result}")
            else:
                self._logger(
                    f"[Cameras] Flash scan: '{normalized}' "
                    "not found in any camera")
            return result

    _scan_camera_by_flash_param = scan_camera_by_flash_param

    def prepare_topology_camera(
        self, side: str, video_index: int,
    ):
        """Cache one Hub-pinned camera before any member of its set opens."""
        normalized = str(side).strip().lower()
        if normalized not in {"left", "right"}:
            raise ValueError(f"unknown tactile side: {side!r}")
        index = int(video_index)
        with self._scan_lock:
            self.flash_param_cache.pop(normalized, None)
        self.scan_camera_by_flash_param(
            normalized,
            max_devices=1,
            candidate_indices=(index,),
        )
        cached = self.apply_old_sensor_template(
            normalized,
            index,
            topology_verified=True,
        )
        if cached is None:
            with self._scan_lock:
                cached = self.flash_full_params_cache.get(index)
        if cached is None:
            raise RuntimeError(
                "Sightac calibration unavailable for topology-pinned "
                f"{normalized}=video{index}"
            )
        with self._scan_lock:
            self.flash_param_cache[normalized] = index
        return cached

    def resolve_camera_id(self, value):
        """解析 left/right、数字、名称和 ``名称#N`` 回退格式。"""
        text = str(value).strip()
        if not text:
            return None
        lowered = text.lower()
        if lowered in {"left", "right"}:
            result = self.scan_camera_by_flash_param(lowered)
            if result is not None:
                self._logger(
                    f"[Cameras] '{text}' → USB index {result} "
                    "(matched by Flash param[1])")
                return result
            self._logger(
                f"[Cameras] '{text}' not found via Flash scan")
            return None
        if text.isdigit():
            index = int(text)
            if (
                self.platform.startswith("linux")
                and not self.is_safe_sonix_probe(index)
            ):
                self._log_probe_rejection(
                    index, self.probe_guard_reason(index))
                return None
            return index
        occurrence = 0
        keyword = text
        if "#" in text:
            keyword, suffix = text.rsplit("#", 1)
            try:
                occurrence = int(suffix)
            except ValueError:
                occurrence = 0
        index = self.find_camera_by_name(keyword, occurrence)
        if index >= 0:
            if (
                self.platform.startswith("linux")
                and not self.is_safe_sonix_probe(index)
            ):
                self._log_probe_rejection(
                    index, self.probe_guard_reason(index))
                return None
            return index
        try:
            index = int(text)
        except ValueError:
            return None
        return (
            index
            if not self.platform.startswith("linux")
            or self.is_safe_sonix_probe(index)
            else None
        )

    _resolve_camera_id = resolve_camera_id

    def _build_sonix_dshow_map(self) -> None:
        if self._sonix_dshow_map is not None:
            return
        self._sonix_dshow_map = {}
        if not self.platform.startswith("win"):
            return
        sonix_paths = {}
        try:
            xc = self._load_xu_module()
            if xc._sonix_lib is None:
                xc.init_sonix_sdk()
            library = xc._sonix_lib
            if library is None:
                return
            devices = (xc.ScDevice * 10)()
            count = ctypes.c_uint(0)
            if library.SonixCam_EnumCameras(
                ctypes.byref(count), devices, 10
            ):
                for index in range(count.value):
                    sonix_paths[index] = str(devices[index].devPath)
        except Exception as exc:
            self._logger(
                f"[Cameras] Sonix enumeration failed: {exc}")
            return

        dshow_paths = {}
        try:
            from comtypes.client import CreateObject
            from comtypes.persist import IPropertyBag
            from pygrabber.dshow_core import ICreateDevEnum
            from pygrabber.dshow_graph import GUID
            from pygrabber.dshow_ids import DeviceCategories, clsids

            enumerator = CreateObject(
                clsids.CLSID_SystemDeviceEnum,
                interface=ICreateDevEnum,
            )
            collection = enumerator.CreateClassEnumerator(
                GUID(DeviceCategories.VideoInputDevice), dwFlags=0)
            moniker, count = collection.Next(1)
            index = 0
            while count > 0:
                try:
                    bag = moniker.BindToStorage(
                        0, 0, IPropertyBag._iid_,
                    ).QueryInterface(IPropertyBag)
                    dshow_paths[index] = str(
                        bag.Read("DevicePath", pErrorLog=None) or "")
                except Exception:
                    pass
                moniker, count = collection.Next(1)
                index += 1
        except Exception as exc:
            self._logger(
                f"[Cameras] DirectShow enumeration failed: {exc}")
            return
        for dshow_index, dshow_path in dshow_paths.items():
            for sonix_index, sonix_path in sonix_paths.items():
                if dshow_path == sonix_path:
                    self._sonix_dshow_map[dshow_index] = sonix_index
                    break
        self._logger(
            f"[Cameras] DShow->Sonix map: {self._sonix_dshow_map}")

    @staticmethod
    def _decode_fourcc(value) -> str:
        try:
            encoded = int(value)
        except (TypeError, ValueError, OverflowError):
            return ""
        return "".join(
            chr((encoded >> shift) & 0xFF)
            for shift in range(0, 32, 8)
        ).rstrip("\x00 ")

    def _require_capture_mjpg(
        self, cap, video_index: int,
    ) -> str:
        """Request and independently verify MJPG before the first read."""
        try:
            requested = self._cv2.VideoWriter_fourcc(*"MJPG")
            cap.set(self._cv2.CAP_PROP_FOURCC, requested)
            actual_value = cap.get(self._cv2.CAP_PROP_FOURCC)
            actual_code = self._decode_fourcc(actual_value)
        except Exception as exc:
            try:
                if cap is not None and hasattr(cap, "release"):
                    cap.release()
            except Exception:
                pass
            raise RuntimeError(
                f"Sightac video{video_index} requires MJPG before "
                f"the first frame read; FOURCC verification failed: {exc}"
            ) from exc
        if actual_code != "MJPG":
            try:
                if cap is not None and hasattr(cap, "release"):
                    cap.release()
            except Exception:
                pass
            raise RuntimeError(
                f"Sightac video{video_index} requires MJPG before "
                "the first frame read; actual FOURCC is "
                f"{actual_code or '<unknown>'}"
            )
        return actual_code

    def _set_sensor_mjpeg(self, sensor, video_index: int) -> str:
        return self._require_capture_mjpg(
            sensor.cap, video_index)

    @staticmethod
    def _release_sensor(sensor) -> None:
        if sensor is None:
            return
        try:
            cap = getattr(sensor, "cap", None)
            if cap is not None and hasattr(cap, "release"):
                cap.release()
        except Exception:
            pass

    def init_sensor(self, video_index: int, reserved_camera=None):
        """构造 TouchSensor；Linux 命中缓存时完全绕过 Sonix SDK。"""
        video_index = int(video_index)
        reserved_is_ipc = (
            getattr(reserved_camera, "transport", None) == "libuvc-ipc"
        )
        if (
            self.platform.startswith("linux")
            and not reserved_is_ipc
            and not self.is_safe_sonix_probe(video_index)
        ):
            reason = self.probe_guard_reason(video_index)
            self._log_probe_rejection(video_index, reason)
            raise RuntimeError(
                f"unsafe Sightac video node {video_index}: {reason}")

        with self._scan_lock:
            cached = self.flash_full_params_cache.get(video_index)
        if reserved_is_ipc and cached is None:
            external = getattr(reserved_camera, "flash_params", None)
            if external is not None:
                cached = self.cache_external_flash_params(
                    video_index, external)
        if self.platform.startswith("linux") and cached is None:
            # Linux 上不能在 VideoCapture 已开流后再让 Sonix SDK 绑定同一
            # 节点。扫描阶段已做两次完整 Flash 读取；仍无有效缓存说明设备
            # 类型/标定不可用。此时必须在开流前停止，既不能注入伪造系数，
            # 也不能回退到会造成 select timeout 的 SDK + V4L2 双重打开。
            raise RuntimeError(
                "Sightac full Flash calibration is unavailable for "
                f"video{video_index}; refusing to open an uncalibrated "
                "or SDK-conflicting stream"
            )

        BaseTouchSensor = self._load_touch_sensor_class()
        if reserved_is_ipc:
            xu_index = video_index
        else:
            self._build_sonix_dshow_map()
            xu_index = self._sonix_dshow_map.get(
                video_index, video_index)
        cv = self._cv2
        platform = self.platform
        logger = self._logger
        require_mjpg = self._require_capture_mjpg
        api_package = self.api_package

        if platform.startswith("linux") and cached is not None:
            class CachedTouchSensor(BaseTouchSensor):
                """从预读缓存恢复算法参数，不再次调用 XU SDK。"""

                def __init__(self, cached_params, camera_index):
                    self._cached_params = cached_params
                    try:
                        super().__init__(
                            usb_id=camera_index,
                            defer_baseline=True,
                        )
                    except Exception:
                        cap = getattr(self, "cap", None)
                        if cap is not None:
                            try:
                                cap.release()
                            except Exception:
                                pass
                        raise

                def camera_init(self, camera_index, fix_config=False):
                    self.platform = "linux"
                    self.xu_camera = None
                    if reserved_camera is None:
                        self.cap = cv.VideoCapture(
                            camera_index, cv.CAP_V4L2)
                        prefetched_frame = None
                        actual_format = None
                    else:
                        (
                            self.cap,
                            prefetched_frame,
                            actual_format,
                        ) = reserved_camera.claim()
                    if not self.cap or not self.cap.isOpened():
                        raise RuntimeError(
                            "[CachedTouchSensor] cannot open "
                            f"video_id={camera_index}")
                    if actual_format is None:
                        self._ksq_fourcc_actual = require_mjpg(
                            self.cap, camera_index
                        )
                    else:
                        width, height, fps, actual_fourcc = actual_format
                        if (
                            int(width) != 640
                            or int(height) != 480
                            or abs(float(fps) - 30.0) > 0.5
                            or actual_fourcc != "MJPG"
                        ):
                            self.cap.release()
                            raise RuntimeError(
                                "[CachedTouchSensor] reserved format "
                                f"invalid for video{camera_index}: "
                                f"{width}x{height} "
                                f"{actual_fourcc or '<unknown>'}@{fps:g}"
                            )
                        if (
                            prefetched_frame is None
                            or getattr(prefetched_frame, "size", 0) <= 0
                        ):
                            self.cap.release()
                            raise RuntimeError(
                                "[CachedTouchSensor] reserved first frame "
                                f"is unavailable for video{camera_index}"
                            )
                        self._ksq_fourcc_actual = actual_fourcc
                        self._ksq_prefetched_frame = prefetched_frame
                    self._apply_cached_params()
                    check_config = getattr(self, "check_config", None)
                    if callable(check_config) and not check_config():
                        logger("WARNING:")
                    logger(
                        f"[CachedTouchSensor] video={camera_index} "
                        "init OK (cached, zero SDK calls)")

                def read_flash(self):
                    return None

                def _apply_cached_params(self):
                    params = self._cached_params
                    self.device_type = params["device_type"]
                    self.apply_flash_params(params)
                    logger(
                        f"[CachedTouchSensor] params injected: device={self.device_type} "
                        f"model={self.bevel_calibrate_model} source={params.get('calibration_source')} "
                        f"ROI=({self.roi_x1},{self.roi_y1})-({self.roi_x2},{self.roi_y2}) "
                        f"RB=({self.mask_r_threshold},{self.mask_b_threshold}) "
                        f"regions={self.rb_filter_regions} hue={self.hue_threshold}"
                    )

            sensor = CachedTouchSensor(cached, video_index)
            sensor._ksq_api_package = api_package
            sensor._ksq_deferred_baseline = True
            sensor._ksq_calibration_source = cached.get(
                "calibration_source", "hardware")
            self._logger(
                f"[Heatmap] CachedTouchSensor video={video_index} "
                f"FOURCC: {sensor._ksq_fourcc_actual} OK "
                "(cached, zero SDK calls, source="
                f"{sensor._ksq_calibration_source})")
            return sensor

        if xu_index == video_index:
            sensor = BaseTouchSensor(
                usb_id=video_index, defer_baseline=True)
            sensor._ksq_api_package = api_package
            sensor._ksq_deferred_baseline = True
            sensor._ksq_fourcc_actual = self._set_sensor_mjpeg(
                sensor, video_index)
            self._logger(
                f"[Heatmap] TouchSensor usb_id={video_index} "
                f"FOURCC: {sensor._ksq_fourcc_actual} OK")
            return sensor

        class RoutedTouchSensor(BaseTouchSensor):
            def __init__(self, routed_xu_index, routed_video_index):
                self._routed_video_index = routed_video_index
                super().__init__(
                    usb_id=routed_xu_index, defer_baseline=True)

            def camera_init(self, camera_index, fix_config=False):
                touch_sensor_module = importlib.import_module(
                    f"{api_package}.touch_sensor")
                if platform.startswith("win"):
                    self.platform = "win"
                    camera = touch_sensor_module.xu_camera.XuCamera(
                        camera_index)
                    if camera.is_available():
                        self.xu_camera = camera
                    self.cap = cv.VideoCapture(
                        self._routed_video_index, cv.CAP_DSHOW)
                elif platform.startswith("darwin"):
                    self.platform = "apple"
                    self.cap = cv.VideoCapture(
                        self._routed_video_index,
                        cv.CAP_AVFOUNDATION,
                    )
                else:
                    self.platform = "unknown"
                    self.cap = cv.VideoCapture(
                        self._routed_video_index)
                if not self.cap or not self.cap.isOpened():
                    raise RuntimeError(
                        "cannot open video_id="
                        f"{self._routed_video_index}")
                self._ksq_fourcc_actual = require_mjpg(
                    self.cap, self._routed_video_index
                )
                if getattr(self, "xu_camera", None):
                    self.device_type = (
                        self.xu_camera.read_device_params()[0])
                self.read_flash()
                check_config = getattr(self, "check_config", None)
                if callable(check_config) and not check_config():
                    logger("WARNING:")
                logger(
                    f"[Heatmap] routed: "
                    f"video={self._routed_video_index} "
                    f"xu={camera_index}")

        sensor = RoutedTouchSensor(xu_index, video_index)
        sensor._ksq_api_package = api_package
        sensor._ksq_deferred_baseline = True
        self._logger(
            f"[Heatmap] TouchSensor video={video_index} "
            f"xu={xu_index} FOURCC: "
            f"{sensor._ksq_fourcc_actual} OK")
        return sensor

    _init_tactile_sensor = init_sensor
