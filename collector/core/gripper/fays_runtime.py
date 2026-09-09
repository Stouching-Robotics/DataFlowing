#!/usr/bin/env python3
"""FaysSense S80M 的单一运行时配置来源。

这个模块只使用 Python 标准库，因此上位机、启动器和检查脚本都可以安全导入。
相机设备节点来自 Fays SDK 配置，禁止在业务代码中另写一套默认值。
"""

import fcntl
import glob
import hashlib
import json
import os
import re
import threading
import time
import sys

if "__compiled__" in globals():
    GRIPPER_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))  # 二进制所在目录
else:
    GRIPPER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(GRIPPER_DIR)
SUPPORTED_CAMERA = "FaysSense S80M stereo fisheye + IMU"
FAYS_MIN_USB_SPEED_MBPS = 5000.0
FAYS_STEREO_FPS = 30.0

FAYS_SDK_ROOT = os.path.abspath(os.environ.get(
    "FAYS_SDK_ROOT",
    os.path.join(PROJECT_ROOT, "FaysSense_VI_Kit_Release"),
))
ORB_ROOT = os.path.abspath(os.environ.get(
    "KSQ_ORB_ROOT",
    os.path.join(PROJECT_ROOT, "ORB-SLAM"),
))

# There is one production ORB bridge. ``device_setup`` records this executable
# together with the per-device SDK/ORB YAML paths in the canonical manifest.
FAYS_MARK_ONLY_BINARY = os.path.join(
    PROJECT_ROOT,
    "dist",
    "fays_opencv48",
    "bin",
    "fayssense_orb_slam_sn219_opencv48_mark_only",
)
# Do not expose an executable override: the production bridge is fixed.
FAYS_ORB_BINARY = FAYS_MARK_ONLY_BINARY
FAYS_DEVICE_CONFIG = os.path.join(
    FAYS_SDK_ROOT,
    "config",
    "fays_vikit_s80m.yaml",
)
DEVICE_MANIFEST = os.path.join(
    GRIPPER_DIR,
    "device_manifest.json",
)
_ORB_VOCABULARY_CONFIG = os.environ.get("KSQ_ORB_VOCABULARY", "ORBvoc.txt")
ORB_VOCABULARY = os.path.abspath(
    _ORB_VOCABULARY_CONFIG
    if os.path.isabs(_ORB_VOCABULARY_CONFIG)
    else os.path.join(PROJECT_ROOT, _ORB_VOCABULARY_CONFIG)
)
ORB_LIBRARY = os.path.abspath(os.environ.get(
    "KSQ_ORB_LIBRARY",
    os.path.join(
        PROJECT_ROOT,
        "dist",
        "orb_mark_only",
        "lib",
        "libORB_SLAM3.so",
    ),
))
ORB_WORK_DIR = os.path.join(PROJECT_ROOT, "ORB-SLAM", "Examples", "fays")
ORB_TRAJECTORY = os.path.join(PROJECT_ROOT, "ORB-SLAM", "traj.txt")

# AIKit is a peer localization backend, not an ORB-SLAM calibration mode.  The
# official service keeps its runtime tree in a bind-mounted host directory;
# before each launch we replace only the current device's factory calibration
# and ephemeral video nodes in that tree.
AIKIT_BRIDGE_BINARY = os.path.abspath(os.environ.get(
    "KSQ_FAYS_AIKIT_BRIDGE",
    os.path.join(
        PROJECT_ROOT, "dist", "fays_aikit", "bin",
        "fays_aikit_slam_bridge",
    ),
))
AIKIT_CLIENT_LIBRARY_DIR = os.path.abspath(os.environ.get(
    "KSQ_FAYS_AIKIT_LIBRARY_DIR",
    os.path.join(PROJECT_ROOT, "dist", "fays_aikit", "lib"),
))
AIKIT_CLIENT_CONFIG = os.path.abspath(os.environ.get(
    "KSQ_FAYS_AIKIT_CLIENT_CONFIG",
    os.path.join(
        PROJECT_ROOT, "dist", "fays_aikit", "config",
        "active_tracker_client.yaml",
    ),
))
AIKIT_SERVICE_CONTAINER = os.environ.get(
    "KSQ_FAYS_AIKIT_CONTAINER", "ksq-fays-aikit-sn198"
).strip()
AIKIT_SERVICE_HOST = os.environ.get(
    "KSQ_FAYS_AIKIT_HOST", "127.0.0.1"
).strip()
AIKIT_SERVICE_PORT = int(os.environ.get("KSQ_FAYS_AIKIT_PORT", "5413"))
AIKIT_SERVICE_RUNTIME_ROOT = os.path.abspath(os.environ.get(
    "KSQ_FAYS_AIKIT_RUNTIME_ROOT",
    os.path.expanduser("~/Downloads/FaysSense_AIKit/runtime_sn198"),
))
AIKIT_SERVICE_DEVICE_CONFIG = os.path.join(
    AIKIT_SERVICE_RUNTIME_ROOT, "driver", "fays_vikit.yaml"
)
AIKIT_SERVICE_CALIBRATION = os.path.join(
    AIKIT_SERVICE_RUNTIME_ROOT, "calib", "calib.yaml"
)
AIKIT_FACTORY_CALIBRATION_DIR = os.path.join(
    PROJECT_ROOT, "config", "calibration"
)

FAYS_VENDOR_ID = "0403"
FAYS_PRODUCT_ID = "602e"
ESP32_VENDOR_ID = "303a"
ESP32_PRODUCT_ID = "1001"
_USB_PHYSICAL_PATTERN = re.compile(r"\d+-\d+(?:\.\d+)*")
_INSTANCE_IPC_FILES = (
    "orb_current_frame_tmp.jpg",
    "orb_current_frame.jpg",
    "orb_meta_tmp.json",
    "orb_meta.json",
    "orb_pose.json.tmp",
    "orb_pose.json",
    "orb_control.json.tmp",
    "orb_control.json",
    "orb_raw_stream.sock",
    "camera_calibration.yaml",
    "imu.yaml",
)


class FaysRuntimeError(RuntimeError):
    """Fays 运行时文件或配置不完整。"""


def fays_slam_profile(calibration_serial):
    """Return the setup-time artifact paths for one discovered Fays serial.

    This helper is used only while ``device_setup`` creates the manifest.  It
    does not select among historical devices or provide a runtime fallback.
    """

    serial = str(calibration_serial or "").strip()
    if not serial:
        raise FaysRuntimeError(
            "device_setup 无法为缺少 product serial 的 Fays 生成运行资源"
        )
    settings = os.path.join(
        PROJECT_ROOT,
        "dist",
        "fays_opencv48",
        f"s80m_{serial}_stereo_inertial.yaml",
    )
    if not os.path.isfile(FAYS_MARK_ONLY_BINARY):
        raise FaysRuntimeError(
            f"稳定版 ORB-SLAM 程序不存在: {FAYS_MARK_ONLY_BINARY}"
        )
    if not os.path.isfile(settings):
        raise FaysRuntimeError(
            "device_setup 尚未生成当前 Fays 的 ORB YAML: "
            f"serial={serial} path={settings}"
        )
    return {
        "backend": "orb",
        "calibration_serial": serial,
        "executable": os.path.abspath(FAYS_MARK_ONLY_BINARY),
        "settings": os.path.abspath(settings),
    }


def _atomic_write(path, payload, *, binary=False):
    destination = os.path.abspath(path)
    temporary = (
        f"{destination}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8"}
    try:
        with open(temporary, mode, **kwargs) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise FaysRuntimeError(
            f"更新 AIKit 运行配置失败: {destination}: {exc}"
        ) from exc


def _factory_calibration_for_serial(calibration_serial):
    serial = str(calibration_serial or "").strip()
    candidates = sorted(glob.glob(os.path.join(
        AIKIT_FACTORY_CALIBRATION_DIR,
        f"FS-VI80-*_{serial}_dump_calib.yaml",
    )))
    if len(candidates) != 1:
        raise FaysRuntimeError(
            "device_setup 未生成当前 Fays 的唯一工厂标定 YAML: "
            f"serial={serial or '<missing>'} files="
            + (", ".join(candidates) if candidates else "<none>")
        )
    return os.path.abspath(candidates[0])


def prepare_aikit_service_runtime(calibration_serial, selected_ports):
    """Atomically select the current device for the mounted AIKit service."""

    serial = str(calibration_serial or "").strip()
    if not serial:
        raise FaysRuntimeError("当前 Fays 缺少 product serial，不能启动 AIKit")
    missing_ports = [
        name for name in ("stereo_dev_port", "imu_dev_port")
        if not str(selected_ports.get(name, "")).strip()
    ]
    if missing_ports:
        raise FaysRuntimeError(
            "device_setup 设备清单缺少 AIKit 所需节点: "
            + ", ".join(missing_ports)
        )
    ports = {
        name: f"/dev/video{video_index_from_device(selected_ports[name])}"
        for name in ("stereo_dev_port", "imu_dev_port")
    }
    calibration_source = _factory_calibration_for_serial(serial)
    try:
        with open(AIKIT_SERVICE_DEVICE_CONFIG, encoding="utf-8") as stream:
            driver_config = stream.read()
        with open(calibration_source, "rb") as stream:
            calibration_payload = stream.read()
    except OSError as exc:
        raise FaysRuntimeError(f"无法读取 AIKit 运行配置: {exc}") from exc

    for key, value in ports.items():
        driver_config, count = re.subn(
            rf"^(\s*{re.escape(key)}\s*:)\s*[^\s#]+",
            rf"\1 {value}",
            driver_config,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise FaysRuntimeError(
                f"AIKit driver 配置无法定位字段: {key}"
            )

    lock_path = os.path.join(
        AIKIT_SERVICE_RUNTIME_ROOT, ".ksq_runtime_config.lock"
    )
    try:
        with open(lock_path, "a+", encoding="utf-8") as lock_stream:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            _atomic_write(
                AIKIT_SERVICE_CALIBRATION, calibration_payload, binary=True
            )
            _atomic_write(AIKIT_SERVICE_DEVICE_CONFIG, driver_config)
    except OSError as exc:
        raise FaysRuntimeError(f"无法锁定 AIKit 运行配置: {exc}") from exc
    return {
        "calibration_serial": serial,
        "calibration_source": calibration_source,
        "calibration_target": AIKIT_SERVICE_CALIBRATION,
        "device_config": AIKIT_SERVICE_DEVICE_CONFIG,
        "ports": ports,
    }


def fays_aikit_profile(calibration_serial, selected_ports):
    """Return an AIKit profile for any device configured by device_setup."""

    required = (
        AIKIT_BRIDGE_BINARY,
        AIKIT_CLIENT_CONFIG,
        os.path.join(
            AIKIT_CLIENT_LIBRARY_DIR, "libfayssense_aikit_client.so"
        ),
        AIKIT_SERVICE_DEVICE_CONFIG,
    )
    missing = [path for path in required if not os.path.isfile(path)]
    if missing:
        raise FaysRuntimeError(
            "AIKit 运行文件不存在: " + ", ".join(missing)
        )
    if not os.access(AIKIT_BRIDGE_BINARY, os.X_OK):
        raise FaysRuntimeError(
            f"AIKit 文件不可执行: {AIKIT_BRIDGE_BINARY}"
        )
    if not AIKIT_SERVICE_CONTAINER:
        raise FaysRuntimeError("KSQ_FAYS_AIKIT_CONTAINER 不能为空")

    prepare_aikit_service_runtime(calibration_serial, selected_ports)
    return {
        "backend": "aikit",
        "calibration_serial": str(calibration_serial).strip(),
        "executable": AIKIT_BRIDGE_BINARY,
        "settings": AIKIT_CLIENT_CONFIG,
        "arguments": (AIKIT_CLIENT_CONFIG,),
        "service_container": AIKIT_SERVICE_CONTAINER,
        "service_host": AIKIT_SERVICE_HOST,
        "service_port": AIKIT_SERVICE_PORT,
    }


def video_index_from_device(device_path):
    """把严格的 ``/dev/videoN`` 路径转换为 OpenCV 索引 N。"""
    match = re.fullmatch(r"/dev/video(\d+)", str(device_path).strip())
    if not match:
        raise FaysRuntimeError(f"不是有效的 V4L2 视频节点: {device_path!r}")
    return int(match.group(1))


def inspect_video_node(device_path):
    """只读检查 V4L2 节点、USB interface 编号和 FT602 身份。"""
    index = video_index_from_device(device_path)
    class_dir = f"/sys/class/video4linux/video{index}"
    info = {
        "device": device_path,
        "exists": os.path.exists(device_path),
        "name": "?",
        "stream_index": None,
        "interface_number": None,
        "modalias": "",
        "vendor_id": "",
        "product_id": "",
        "is_fays_ft602": False,
        "physical_usb_path": None,
        "usb_device_sysfs_path": None,
    }

    fields = {
        "name": os.path.join(class_dir, "name"),
        "stream_index": os.path.join(class_dir, "index"),
        "interface_number": os.path.join(class_dir, "device", "bInterfaceNumber"),
        "modalias": os.path.join(class_dir, "device", "modalias"),
    }
    for key, path in fields.items():
        try:
            with open(path, encoding="utf-8") as stream:
                value = stream.read().strip()
            info[key] = int(value) if key == "stream_index" else value
        except (OSError, ValueError):
            pass

    name = str(info["name"]).lower()
    modalias = str(info["modalias"]).lower()
    info["is_fays_ft602"] = (
        "ftdi" in name
        and "superspeed video bridge" in name
        and "v0403p602e" in modalias
    )
    try:
        interface_path = os.path.realpath(os.path.join(class_dir, "device"))
        physical_path = os.path.dirname(interface_path)
        with open(
            os.path.join(physical_path, "idVendor"), encoding="utf-8"
        ) as stream:
            info["vendor_id"] = stream.read().strip().lower()
        with open(
            os.path.join(physical_path, "idProduct"), encoding="utf-8"
        ) as stream:
            info["product_id"] = stream.read().strip().lower()
        info["physical_usb_path"] = os.path.basename(
            interface_path
        ).split(":", 1)[0]
        info["usb_device_sysfs_path"] = physical_path
    except OSError:
        pass
    info["is_fays_ft602"] = (
        info["vendor_id"] == FAYS_VENDOR_ID
        and info["product_id"] == FAYS_PRODUCT_ID
        and str(info["interface_number"]) in {"00", "02"}
    )
    return info


def inspect_tty_device(device_path):
    """Resolve one selected ESP32 tty to stable read-only USB identity."""
    basename = os.path.basename(str(device_path).strip())
    if not re.fullmatch(r"ttyACM\d+", basename):
        raise FaysRuntimeError(f"不是有效的 ESP32 串口节点: {device_path!r}")
    interface_path = os.path.realpath(
        os.path.join("/sys/class/tty", basename, "device")
    )
    interface_name = os.path.basename(interface_path)
    if (
        not interface_path.startswith("/sys/devices/")
        or ":" not in interface_name
    ):
        raise FaysRuntimeError(
            f"无法解析 ESP32 串口 USB 身份: {device_path}"
        )
    physical_usb_path = interface_name.split(":", 1)[0]
    if not _USB_PHYSICAL_PATTERN.fullmatch(physical_usb_path):
        raise FaysRuntimeError(
            f"ESP32 串口物理路径无效: {physical_usb_path!r}"
        )
    physical_path = os.path.dirname(interface_path)

    def read_attribute(name):
        try:
            with open(
                os.path.join(physical_path, name), encoding="utf-8"
            ) as stream:
                return stream.read().strip()
        except OSError as exc:
            raise FaysRuntimeError(
                f"无法读取 ESP32 {name}: {device_path}: {exc}"
            ) from exc

    root_match = re.match(r"(\d+-\d+)", physical_usb_path)
    return {
        "device": f"/dev/{basename}",
        "physical_usb_path": physical_usb_path,
        "root_hub": root_match.group(1),
        "vendor_id": read_attribute("idVendor").lower(),
        "product_id": read_attribute("idProduct").lower(),
        "serial": read_attribute("serial"),
    }


def discover_esp32_devices():
    """只读枚举当前在线的 ESP32 控制板节点。

    ``/dev/ttyACM*`` 是本次枚举得到的路径，VID/PID 和 USB serial 才是
    设备身份。这里沿用 ``inspect_tty_device`` 的 sysfs 读取路径，不打开串口，
    不访问 USB interface 目录，也不修改硬件状态。
    """
    devices = []
    for class_path in sorted(
        glob.glob("/sys/class/tty/ttyACM*"),
        key=lambda path: tuple(
            int(part) if part.isdigit() else part
            for part in re.split(r"([0-9]+)", str(path))
            if part
        ),
    ):
        basename = os.path.basename(class_path)
        device_path = f"/dev/{basename}"
        try:
            info = inspect_tty_device(device_path)
        except FaysRuntimeError:
            continue
        if (
            info["vendor_id"] == ESP32_VENDOR_ID
            and info["product_id"] == ESP32_PRODUCT_ID
            and info.get("serial")
        ):
            devices.append(info)
    return tuple(devices)


def read_fays_usb_speed_mbps(
    usb_device_sysfs_path,
    physical_usb_path,
):
    """从 USB 设备目录只读获取 Fays 当前协商链路速度。"""
    physical = str(physical_usb_path or "").strip()
    if not re.fullmatch(r"\d+-\d+(?:\.\d+)*", physical):
        raise FaysRuntimeError(
            f"Fays 物理 USB 路径无效: {physical_usb_path!r}"
        )
    device_path = os.path.realpath(
        str(usb_device_sysfs_path or "").strip()
    )
    if (
        not device_path.startswith("/sys/devices/")
        or os.path.basename(device_path) != physical
        or ":" in os.path.basename(device_path)
    ):
        raise FaysRuntimeError(
            "Fays USB 设备 sysfs 路径无效: "
            f"{usb_device_sysfs_path!r}"
        )
    # ``inspect_video_node`` 已将 video class symlink 解析到无冒号的
    # 物理 device 父目录。这里只读该目录的 speed，不访问 ``*:*``
    # interface 属性，也不写 sysfs、不复位或重绑驱动。
    speed_path = os.path.join(device_path, "speed")
    try:
        with open(speed_path, encoding="utf-8") as stream:
            speed = float(stream.read().strip())
    except (OSError, ValueError) as exc:
        raise FaysRuntimeError(
            f"无法读取 Fays USB 链路速度: {speed_path}: {exc}"
        ) from exc
    if speed <= 0:
        raise FaysRuntimeError(
            f"Fays USB 链路速度无效: {speed:g}M ({physical})"
        )
    return speed


def validate_fays_superspeed(devices):
    """拒绝 FT602 降级到 USB 2.0，返回当前链路速度（Mb/s）。"""
    physical_paths = [
        devices.get(role, {}).get("physical_usb_path")
        for role in ("stereo", "imu")
    ]
    device_paths = [
        devices.get(role, {}).get("usb_device_sysfs_path")
        for role in ("stereo", "imu")
    ]
    if (
        any(not path for path in physical_paths)
        or len(set(physical_paths)) != 1
        or any(not path for path in device_paths)
        or len(set(map(os.path.realpath, device_paths))) != 1
    ):
        raise FaysRuntimeError(
            "无法确认 Fays stereo/IMU 的唯一物理 USB 设备"
        )
    physical = physical_paths[0]
    speed = read_fays_usb_speed_mbps(device_paths[0], physical)
    if speed < FAYS_MIN_USB_SPEED_MBPS:
        raise FaysRuntimeError(
            "Fays USB 链路不是 SuperSpeed: "
            f"physical={physical} speed={speed:g}M，"
            f"要求至少 {FAYS_MIN_USB_SPEED_MBPS:g}M；"
            "请将 Fays 直连 USB 3.x 端口"
        )
    return speed


def fays_usb_control_node(devices):
    """Resolve the FT602 USB device node used by the D3XX control path."""
    physical_paths = [
        devices.get(role, {}).get("physical_usb_path")
        for role in ("stereo", "imu")
    ]
    device_paths = [
        devices.get(role, {}).get("usb_device_sysfs_path")
        for role in ("stereo", "imu")
    ]
    if (
        any(not path for path in physical_paths)
        or len(set(physical_paths)) != 1
        or any(not path for path in device_paths)
        or len(set(map(os.path.realpath, device_paths))) != 1
    ):
        raise FaysRuntimeError(
            "无法确认 Fays stereo/IMU 的唯一物理 USB 设备"
        )

    physical = str(physical_paths[0])
    device_path = os.path.realpath(str(device_paths[0]))
    if (
        not _USB_PHYSICAL_PATTERN.fullmatch(physical)
        or not device_path.startswith("/sys/devices/")
        or os.path.basename(device_path) != physical
        or ":" in os.path.basename(device_path)
    ):
        raise FaysRuntimeError(
            f"Fays USB 设备 sysfs 路径无效: {device_paths[0]!r}"
        )

    values = {}
    for attribute in ("busnum", "devnum"):
        path = os.path.join(device_path, attribute)
        try:
            with open(path, encoding="utf-8") as stream:
                values[attribute] = int(stream.read().strip())
        except (OSError, ValueError) as exc:
            raise FaysRuntimeError(
                f"无法读取 Fays USB {attribute}: {path}: {exc}"
            ) from exc
        if not 0 < values[attribute] <= 999:
            raise FaysRuntimeError(
                f"Fays USB {attribute} 无效: {values[attribute]!r}"
            )
    return (
        f"/dev/bus/usb/{values['busnum']:03d}/"
        f"{values['devnum']:03d}"
    )


def validate_fays_control_access(devices):
    """Require read/write access to the FT602 D3XX register-control node."""
    node = fays_usb_control_node(devices)
    if os.path.exists(node) and os.access(node, os.R_OK | os.W_OK):
        return node

    details = "missing"
    try:
        metadata = os.stat(node)
        details = (
            f"mode={metadata.st_mode & 0o777:04o} "
            f"uid={metadata.st_uid} gid={metadata.st_gid}"
        )
    except OSError:
        pass
    installer = os.path.join(
        PROJECT_ROOT, "env_init", "install_fays_permissions.sh"
    )
    raise FaysRuntimeError(
        "Fays FT602 D3XX 控制节点不可读写: "
        f"{node} ({details})；双目/IMU 可能仍能取流，但曝光、增益和旋转"
        "写入会报 FT_Create failed。请先执行 "
        f"sudo {installer}，然后重新 Connect"
    )


def validate_fays_sdk_access(devices):
    """Validate both the SuperSpeed stream and writable D3XX control path."""
    speed = validate_fays_superspeed(devices)
    validate_fays_control_access(devices)
    return speed


def discover_fays_device_groups():
    """只读发现全部完整 S80M，并按物理 USB 路径分组。"""
    groups = {}
    for class_path in sorted(glob.glob("/sys/class/video4linux/video*")):
        basename = os.path.basename(class_path)
        if not re.fullmatch(r"video\d+", basename):
            continue
        info = inspect_video_node(f"/dev/{basename}")
        if not info["is_fays_ft602"] or info["stream_index"] != 0:
            continue
        physical = info.get("physical_usb_path")
        interface = info.get("interface_number")
        if physical and interface in {"00", "02"}:
            groups.setdefault(physical, {}).setdefault(interface, []).append(info)

    complete = []
    for physical, interfaces in groups.items():
        if len(interfaces.get("00", [])) == 1 and len(interfaces.get("02", [])) == 1:
            stereo, imu = interfaces["00"][0], interfaces["02"][0]
            complete.append({
                "physical_usb_path": physical,
                "ports": {
                    "stereo_dev_port": stereo["device"],
                    "imu_dev_port": imu["device"],
                },
                "stereo": stereo,
                "imu": imu,
                "config_updated": False,
            })
    return tuple(sorted(
        complete,
        key=lambda item: tuple(
            int(part) if part.isdigit() else part
            for part in re.split(
                r"([0-9]+)", item["physical_usb_path"]
            )
            if part
        ),
    ))


def read_fays_config_ports(config_path):
    """Read configured access nodes; never infer a product identity from them."""
    with open(config_path, encoding="utf-8") as stream:
        text = stream.read()
    ports = {}
    for key in ("stereo_dev_port", "imu_dev_port"):
        match = re.search(r"^\s*" + key + r"\s*:\s*['\"]?(/dev/video[0-9]+)(?=['\"\s#]|$)",
                          text, re.MULTILINE)
        if match is None:
            raise FaysRuntimeError(f"Fays SDK 配置缺少有效 {key}: {config_path}")
        ports[key] = match.group(1)
    return ports


def materialize_fays_device_config(
    ports,
    destination,
    template_path,
):
    """从只读模板原子生成一个实例专用 SDK 配置。"""
    try:
        with open(template_path, encoding="utf-8") as stream:
            content = stream.read()
    except OSError as exc:
        raise FaysRuntimeError(
            f"无法读取 Fays 配置模板: {template_path}: {exc}"
        ) from exc
    replacements = {
        "stereo_dev_port": (
            f"/dev/video{video_index_from_device(ports['stereo_dev_port'])}"
        ),
        "imu_dev_port": (
            f"/dev/video{video_index_from_device(ports['imu_dev_port'])}"
        ),
    }
    for key, value in replacements.items():
        content, count = re.subn(
            rf"^(\s*{re.escape(key)}\s*:)\s*[^\s#]+",
            rf"\1 {value}",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise FaysRuntimeError(f"Fays 配置模板无法定位字段: {key}")
    destination = os.path.abspath(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = f"{destination}.tmp.{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise FaysRuntimeError(
            f"生成 Fays 实例配置失败: {destination}: {exc}"
        ) from exc
    return destination


def discover_fays_manifest_device_groups():
    """Discover Fays groups only after device_setup manifest resolution."""
    from fays_serial_probe import discover_fays_manifest_groups

    return discover_fays_manifest_groups()


class FaysInstanceManager:
    """Own one manifest-selected Fays lease and all per-GUI runtime paths."""

    def __init__(
        self,
        runtime_dir,
        *,
        run_id=None,
        lock_dir="/tmp/ksq-gripper-fays-locks",
        ipc_root="/dev/shm",
        discover_devices=None,
        discover_selected_devices=None,
        inspect_tty=inspect_tty_device,
        validate_link=validate_fays_sdk_access,
        logger=print,
        pid=None,
    ):
        self.runtime_dir = os.path.abspath(runtime_dir)
        self.run_id = str(
            run_id or os.path.basename(self.runtime_dir)
        )
        self.pid = int(os.getpid() if pid is None else pid)
        rendered = re.sub(
            r"[^A-Za-z0-9_.-]+", "-", self.run_id
        ).strip("-") or "run"
        self.instance_id = f"{rendered}-{self.pid}"
        self.manifest_path = os.path.abspath(DEVICE_MANIFEST)
        self.lock_dir = os.path.abspath(lock_dir)
        self.ipc_dir = os.path.join(
            os.path.abspath(ipc_root),
            f"ksq-gripper-{self.instance_id}",
        )
        self.device_config_path = os.path.join(
            self.runtime_dir, "fays_vikit_runtime.yaml"
        )
        self.trajectory_path = os.path.join(
            self.runtime_dir, "orb_trajectory.txt"
        )
        self.assignment_path = os.path.join(
            self.runtime_dir, "hardware_assignment.json"
        )
        self.event_log_path = os.path.join(
            self.runtime_dir, "fays_instance.log"
        )
        self.native_log_path = os.path.join(
            self.runtime_dir, "fays_native.log"
        )
        self._discover_selected_devices = discover_selected_devices
        self._discover_devices = (
            discover_devices or discover_fays_manifest_device_groups
        )
        self._inspect_tty = inspect_tty
        self._validate_link = validate_link
        self._logger = logger
        self._lock = threading.RLock()
        self._lock_stream = None
        self._selected = None
        self._assignment = None
        self._tty = None
        os.makedirs(self.runtime_dir, exist_ok=True)
        os.makedirs(self.lock_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.ipc_dir, mode=0o700, exist_ok=True)
        self._clear_ipc_files()
        self._event("instance_created")

    def ipc_path(self, leaf):
        if str(leaf) not in _INSTANCE_IPC_FILES:
            raise ValueError(f"unknown Fays IPC leaf: {leaf!r}")
        return os.path.join(self.ipc_dir, str(leaf))

    @property
    def pose_paths(self):
        return (
            self.ipc_path("orb_pose.json.tmp"),
            self.ipc_path("orb_pose.json"),
        )

    @property
    def frame_reader_paths(self):
        return {
            "meta_path": self.ipc_path("orb_meta.json"),
            "current_frame_path": self.ipc_path(
                "orb_current_frame.jpg"
            ),
        }

    @property
    def control_paths(self):
        return (
            self.ipc_path("orb_control.json.tmp"),
            self.ipc_path("orb_control.json"),
        )

    @property
    def raw_stream_socket_path(self):
        return self.ipc_path("orb_raw_stream.sock")

    @property
    def calibration_yaml_paths(self):
        return (
            self.ipc_path("camera_calibration.yaml"),
            self.ipc_path("imu.yaml"),
        )

    def runtime_environment(self, base=None):
        environment = dict(base or {})
        environment.update({
            "KSQ_FAYS_IPC_DIR": self.ipc_dir,
            "KSQ_FAYS_INSTANCE_ID": self.instance_id,
        })
        assignment = self.snapshot()
        if assignment:
            calibration_serial = assignment.get("fays", {}).get(
                "calibration_serial"
            )
            if calibration_serial:
                environment["KSQ_FAYS_CALIBRATION_SERIAL"] = str(
                    calibration_serial
                )
        return environment

    def record_camera_set(self, topology):
        """Append the selected USB2 camera topology to this instance log."""
        if topology is None:
            return False
        roles = {}
        try:
            for role in ("decxin", "left", "right"):
                node = topology.node(role)
                roles[role] = {
                    "device_path": node.device_path,
                    "physical_usb_path": node.physical_usb_path,
                    "vendor_id": node.vendor_id,
                    "product_id": node.product_id,
                    "serial": node.serial,
                }
            camera_set = {
                "root_hub": topology.root_hub,
                "tactile_hub": topology.tactile_hub,
                "roles": roles,
            }
        except (AttributeError, KeyError) as exc:
            raise FaysRuntimeError(
                f"无法记录 USB2 相机套件拓扑: {exc}"
            ) from exc
        with self._lock:
            if (
                self._assignment is None
                or self._assignment.get("status") != "acquired"
                or self._selected is None
                or self._lock_stream is None
                or self._lock_stream.closed
            ):
                return False
            expected_root = (
                self._tty.get("root_hub") if self._tty else None
            )
            # The schema-v2 selector already resolved the camera set from the
            # ESP32 serial and the relative H0/H1 topology.  ``root_hub`` is
            # diagnostic only for this path, because a nested USB3 hub can
            # legitimately put the cameras one level below the ESP root hub
            # (e.g. ESP root 1-5 vs camera H0 1-5.2).
            actual_root = str(camera_set["root_hub"] or "")
            same_usb_branch = bool(
                actual_root
                and expected_root
                and (
                    actual_root == expected_root
                    or actual_root.startswith(f"{expected_root}.")
                )
            )
            if expected_root and not same_usb_branch:
                raise FaysRuntimeError(
                    "USB2 相机套件不属于当前 ESP32: "
                    f"expected={expected_root} "
                    f"actual={actual_root}"
                )
            self._assignment["usb2_camera_set"] = camera_set
            self._write_assignment()
            self._event("camera_set_selected", camera_set=camera_set)
        return True

    def set_preferred_tty(self, device_path):
        with self._lock:
            rendered = str(device_path or "").strip()
            candidate = self._inspect_tty(rendered) if rendered else None
            if self.is_acquired():
                previous = self._tty
                self._tty = candidate
                try:
                    group, _unit, _manifest = self._resolve_assignment()
                except Exception:
                    self._tty = previous
                    raise
                if (
                    group["physical_usb_path"]
                    != self._selected["physical_usb_path"]
                ):
                    self._tty = previous
                    raise FaysRuntimeError(
                        "当前 Fays 正在使用，不能切换到另一套 ESP32；"
                        "请先 Disconnect"
                    )
            else:
                self._tty = candidate
            self._event(
                "control_port_selected",
                tty=self._tty,
            )
            return dict(self._tty) if self._tty else None

    def verify_preferred_tty(self, device_path):
        """Recheck that a tty node still names the preflight ESP32."""
        with self._lock:
            if self._tty is None:
                raise FaysRuntimeError("尚未选择 ESP32 控制串口")
            current = self._inspect_tty(device_path)
            fields = (
                "vendor_id",
                "product_id",
                "serial",
                "physical_usb_path",
                "root_hub",
            )
            differences = [
                name for name in fields
                if current.get(name) != self._tty.get(name)
            ]
            if differences:
                self._event(
                    "control_port_identity_changed",
                    expected=self._tty,
                    actual=current,
                    differences=differences,
                )
                raise FaysRuntimeError(
                    "串口在 Connect 交接期间指向了另一设备: "
                    + ", ".join(differences)
                )
            return dict(current)

    def is_acquired(self):
        with self._lock:
            return bool(
                self._selected is not None
                and self._lock_stream is not None
                and not self._lock_stream.closed
                and self._assignment is not None
                and self._assignment.get("status") == "acquired"
            )

    def selected_ports(self):
        with self._lock:
            if self._selected is None:
                raise FaysRuntimeError("当前界面尚未分配 Fays 设备")
            return dict(self._selected["ports"])

    def slam_profile(self, backend="orb"):
        """Return the selected localization backend's runtime profile."""

        with self._lock:
            if self._selected is None:
                raise FaysRuntimeError("当前界面尚未分配 Fays 设备")
            selected = dict(self._selected)
            calibration_serial = selected.get("calibration_serial")
            normalized_backend = str(backend or "orb").strip().lower()
            if normalized_backend == "orb":
                serial = str(calibration_serial or "").strip()
                if not serial:
                    raise FaysRuntimeError(
                        "设备清单缺少当前 Fays 标定序列号，拒绝启动 ORB-SLAM；"
                        "请先运行 device_setup"
                    )
                sdk_yaml = os.path.abspath(
                    os.fspath(selected.get("sdk_yaml") or "")
                ) if selected.get("sdk_yaml") else ""
                manifest_orb_yaml = os.path.abspath(
                    os.fspath(selected.get("orb_yaml") or "")
                ) if selected.get("orb_yaml") else ""
                missing_manifest = [
                    label for label, path in (
                        ("SDK YAML", sdk_yaml),
                        ("ORB YAML", manifest_orb_yaml),
                    )
                    if not path or not os.path.isfile(path)
                ]
                if missing_manifest:
                    raise FaysRuntimeError(
                        "设备清单缺少当前 Fays 的运行文件，拒绝启动 ORB-SLAM: "
                        f"serial={serial} 缺少="
                        + ", ".join(missing_manifest)
                        + "；请先运行 device_setup"
                    )
                for label, path in (
                    ("SDK YAML", sdk_yaml),
                    ("ORB YAML", manifest_orb_yaml),
                ):
                    if serial not in os.path.basename(path):
                        raise FaysRuntimeError(
                            "设备清单运行文件与当前 Fays 序列号不匹配，"
                            "拒绝启动 ORB-SLAM: "
                            f"serial={serial} {label}={path}"
                        )
                executable = os.path.abspath(
                    os.fspath(selected.get("orb_binary") or "")
                ) if selected.get("orb_binary") else ""
                if not executable:
                    raise FaysRuntimeError(
                        "device_setup 设备清单缺少当前 Fays 的 ORB 程序，"
                        "请先运行 device_setup"
                    )
                profile = {
                    "backend": "orb",
                    "calibration_serial": serial,
                    "executable": executable,
                    "settings": manifest_orb_yaml,
                }
            elif normalized_backend == "aikit":
                profile = fays_aikit_profile(
                    calibration_serial,
                    selected.get("ports") or {},
                )
            else:
                raise FaysRuntimeError(
                    f"未知定位后端: {backend!r}"
                )
        missing = [
            path for path in (
                profile["executable"], profile["settings"]
            )
            if not os.path.isfile(path)
        ]
        if missing:
            raise FaysRuntimeError(
                "Fays 标定运行时文件不存在: " + ", ".join(missing)
            )
        if not os.access(profile["executable"], os.X_OK):
            raise FaysRuntimeError(
                f"Fays 定位文件不可执行: {profile['executable']}"
            )
        return profile

    def snapshot(self):
        with self._lock:
            return (
                json.loads(json.dumps(self._assignment))
                if self._assignment is not None else None
            )

    def verify_active_link(self):
        """Revalidate the selected Fays immediately before SDK startup."""
        with self._lock:
            if not self.is_acquired():
                raise FaysRuntimeError("当前实例尚未持有 Fays 租约")
            try:
                group, _unit, _manifest = self._resolve_assignment()
                speed = self._validate_link(group)
                selected = self._selected
                if selected["ports"] != group["ports"]:
                    selected = self.acquire()
                else:
                    selected = dict(selected)
                selected["usb_speed_mbps"] = speed
                self._event(
                    "runtime_link_verified",
                    physical_usb_path=group["physical_usb_path"],
                    ports=group["ports"],
                    usb_speed_mbps=speed,
                )
                return selected
            except Exception as exc:
                try:
                    self._event(
                        "runtime_link_failed",
                        error=str(exc),
                        selected=self._selected,
                    )
                except Exception:
                    pass
                if isinstance(exc, FaysRuntimeError):
                    raise
                raise FaysRuntimeError(
                    f"Fays 运行时链路校验失败: {exc}"
                ) from exc

    def inspect(self):
        """Resolve the manifest assignment without locking or opening Fays."""
        with self._lock:
            group, unit, manifest = self._resolve_assignment()
            result = dict(group)
            result.update({
                "unit_id": unit["unit_id"],
                "calibration_serial": unit.get("fays", {}).get(
                    "calibration_serial"
                ) or result.get("product_serial"),
                "mapping_source": result.get(
                    "mapping_source",
                    "device_setup_manifest_sdk_serial",
                ),
                "manifest_sha256": manifest["sha256"],
            })
            return result

    def acquire(self):
        with self._lock:
            group, unit, manifest = self._resolve_assignment()
            physical = group["physical_usb_path"]
            lease_key = str(
                group.get("product_serial")
                or group.get("calibration_serial")
                or physical
            )
            sdk_template = str(group.get("sdk_yaml") or "").strip()
            if not sdk_template:
                raise FaysRuntimeError(
                    "device_setup 设备清单缺少当前 Fays 的 SDK YAML，"
                    "请先运行 device_setup"
                )
            if self.is_acquired():
                if self._selected["physical_usb_path"] != physical:
                    raise FaysRuntimeError(
                        "当前实例仍持有另一台 Fays；请先停止资源并释放租约"
                    )
                if self._selected["ports"] != group["ports"]:
                    materialize_fays_device_config(
                        group["ports"],
                        self.device_config_path,
                        sdk_template,
                    )
                    refreshed = dict(group)
                    refreshed.update({
                        key: self._selected[key]
                        for key in (
                            "unit_id",
                            "calibration_serial",
                            "mapping_source",
                            "runtime_config",
                            "ipc_dir",
                            "trajectory",
                            "lock_path",
                            "usb_speed_mbps",
                        )
                    })
                    assignment = self._build_assignment(
                        refreshed, unit, manifest, "acquired"
                    )
                    assignment["created_at"] = self._assignment[
                        "created_at"
                    ]
                    if "usb2_camera_set" in self._assignment:
                        assignment["usb2_camera_set"] = self._assignment[
                            "usb2_camera_set"
                        ]
                    self._write_assignment(assignment)
                    self._selected = refreshed
                    self._assignment = assignment
                    self._event(
                        "video_nodes_refreshed",
                        ports=group["ports"],
                        physical_usb_path=physical,
                    )
                return dict(self._selected)
            self.release()
            speed = self._validate_link(group)
            lock_path = os.path.join(
                self.lock_dir,
                f"{re.sub(r'[^A-Za-z0-9_.-]+', '-', lease_key)}.lock",
            )
            stream = open(lock_path, "a+", encoding="utf-8")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                stream.seek(0)
                owner = stream.read().strip() or "unknown owner"
                stream.close()
                self._event(
                    "lease_busy",
                    unit_id=unit["unit_id"],
                    physical_usb_path=physical,
                    owner=owner,
                )
                raise FaysRuntimeError(
                    f"Fays {physical} 已被另一界面占用: {owner}"
                ) from exc
            assignment = None
            try:
                owner = {
                    "pid": self.pid,
                    "run_id": self.run_id,
                    "instance_id": self.instance_id,
                    "unit_id": unit["unit_id"],
                    "physical_usb_path": physical,
                }
                stream.seek(0)
                stream.truncate()
                json.dump(owner, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
                materialize_fays_device_config(
                    group["ports"],
                    self.device_config_path,
                    sdk_template,
                )
                self._clear_ipc_files()
                selected = dict(group)
                selected.update({
                    "unit_id": unit["unit_id"],
                    "calibration_serial": unit.get("fays", {}).get(
                        "calibration_serial"
                    ) or selected.get("product_serial"),
                    "product_serial": selected.get("product_serial"),
                    "sdk_yaml": selected.get("sdk_yaml"),
                    "orb_yaml": selected.get("orb_yaml"),
                    "orb_binary": selected.get("orb_binary"),
                    "decxin": unit.get("decxin"),
                    "sightac_left": unit.get("sightac_left"),
                    "sightac_right": unit.get("sightac_right"),
                    "mapping_source": selected.get(
                        "mapping_source",
                        "device_setup_manifest_sdk_serial",
                    ),
                    "runtime_config": self.device_config_path,
                    "ipc_dir": self.ipc_dir,
                    "trajectory": self.trajectory_path,
                    "lock_path": lock_path,
                    "usb_speed_mbps": speed,
                })
                assignment = self._build_assignment(
                    selected, unit, manifest, "acquired"
                )
                self._write_assignment(assignment)
                self._event("lease_acquired", assignment=assignment)
                try:
                    self._logger(
                        "[Fays-Instance] selected: "
                        f"unit={unit['unit_id']} "
                        f"esp={(self._tty or unit.get('esp32', {})).get('serial', '-')} "
                        f"fays={physical} "
                        f"stereo={group['ports']['stereo_dev_port']} "
                        f"imu={group['ports']['imu_dev_port']} "
                        f"ipc={self.ipc_dir}"
                    )
                except Exception:
                    pass
                self._lock_stream = stream
                self._selected = selected
                self._assignment = assignment
                return dict(selected)
            except Exception as exc:
                self._lock_stream = None
                self._selected = None
                self._assignment = None
                try:
                    if assignment is not None:
                        assignment["status"] = "acquire_failed"
                        assignment["error"] = str(exc)
                        self._write_assignment(assignment)
                    self._event(
                        "lease_acquire_failed",
                        unit_id=unit["unit_id"],
                        physical_usb_path=physical,
                        error=str(exc),
                    )
                except Exception:
                    pass
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
                raise

    def release(self):
        with self._lock:
            stream = self._lock_stream
            selected = self._selected
            self._lock_stream = None
            self._selected = None
            if stream is not None:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
            self._clear_ipc_files()
            if self._assignment is not None:
                self._assignment["status"] = "released"
                self._assignment["released_at"] = time.time()
                self._write_assignment()
            if selected is not None:
                self._event(
                    "lease_released",
                    unit_id=selected.get("unit_id"),
                    physical_usb_path=selected.get(
                        "physical_usb_path"
                    ),
                )
            return True

    def _resolve_assignment(self):
        """Resolve one selected ESP32 strictly through device_setup."""
        if self._tty is None:
            raise FaysRuntimeError(
                "尚未选择 ESP32 控制串口；设备身份只能由 device_setup "
                "设备清单中的 ESP32 serial 确定"
            )
        manifest = self._load_manifest()
        matches = [
            unit for unit in manifest["units"]
            if unit["esp32"]["vendor_id"] == self._tty["vendor_id"]
            and unit["esp32"]["product_id"] == self._tty["product_id"]
            and unit["esp32"]["serial"] == self._tty["serial"]
        ]
        if len(matches) != 1:
            raise FaysRuntimeError(
                "当前 ESP32 不在 device_setup 设备清单中，拒绝猜测 Fays 归属: "
                f"serial={self._tty.get('serial', '<missing>')} "
                f"matches={len(matches)} manifest={self.manifest_path}"
            )
        unit = matches[0]
        groups = tuple(self._discover_selected_devices(unit)
                       if self._discover_selected_devices is not None
                       else self._discover_devices())
        if not groups:
            raise FaysRuntimeError("未发现所选 FaysSense S80M FT602")
        product_serial = unit["fays"]["product_serial"]
        group_matches = [
            group for group in groups
            if str(group.get("manifest_unit") or "").strip()
            == unit["unit_id"]
            and str(group.get("product_serial") or "").strip()
            == product_serial
        ]
        if len(group_matches) != 1:
            available = ", ".join(
                str(group.get("product_serial") or "<missing>")
                for group in groups
            )
            raise FaysRuntimeError(
                "device_setup 设备清单与当前 Fays SDK 序列号无法唯一匹配: "
                f"unit={unit['unit_id']} expected={product_serial} "
                f"available={available}"
            )
        selected = dict(group_matches[0])
        selected.update({
            "unit_id": unit["unit_id"],
            "product_serial": product_serial,
            "calibration_serial": product_serial,
            "sdk_yaml": unit["fays"]["sdk_yaml"],
            "orb_yaml": unit["fays"]["orb_yaml"],
            "orb_binary": unit["fays"]["orb_binary"],
            "mapping_source": "device_setup_manifest_sdk_serial",
        })
        return selected, unit, manifest

    def _load_manifest(self):
        from device_manifest_writer import load_device_manifest

        try:
            manifest = load_device_manifest()
            with open(self.manifest_path, "rb") as stream:
                raw = stream.read()
        except (OSError, RuntimeError) as exc:
            raise FaysRuntimeError(
                f"无法读取 device_setup 设备清单: {self.manifest_path}: {exc}"
            ) from exc
        manifest["sha256"] = hashlib.sha256(raw).hexdigest()
        return manifest

    def _build_assignment(self, selected, unit, manifest, status):
        return {
            "schema_version": 1,
            "status": status,
            "created_at": time.time(),
            "pid": self.pid,
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "unit_id": unit["unit_id"],
            "pairing_status": unit.get(
                "pairing_status", "unspecified"
            ),
            "mapping_source": selected["mapping_source"],
            "manifest": {
                "path": self.manifest_path,
                "sha256": manifest["sha256"],
            },
            "esp32": dict(self._tty or unit.get("esp32", {})),
            "decxin": selected.get("decxin"),
            "sightac_left": selected.get("sightac_left"),
            "sightac_right": selected.get("sightac_right"),
            "fays": {
                "calibration_serial": selected.get(
                    "calibration_serial"
                ),
                "product_serial": selected.get("product_serial"),
                "sdk_yaml": selected.get("sdk_yaml"),
                "orb_yaml": selected.get("orb_yaml"),
                "orb_binary": selected.get("orb_binary"),
                "physical_usb_path": selected["physical_usb_path"],
                "stereo_dev_port": selected["ports"][
                    "stereo_dev_port"
                ],
                "imu_dev_port": selected["ports"]["imu_dev_port"],
                "stereo_interface": selected["stereo"].get(
                    "interface_number"
                ),
                "imu_interface": selected["imu"].get("interface_number"),
                "usb_speed_mbps": selected["usb_speed_mbps"],
            },
            "runtime": {
                "device_config": self.device_config_path,
                "ipc_dir": self.ipc_dir,
                "trajectory": self.trajectory_path,
                "work_dir": self.runtime_dir,
                "lock_path": selected["lock_path"],
            },
        }

    def _write_assignment(self, assignment=None):
        assignment = self._assignment if assignment is None else assignment
        if assignment is None:
            return
        temporary = f"{self.assignment_path}.tmp.{self.pid}"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(
                assignment,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.assignment_path)

    def _event(self, event, **payload):
        record = {
            "time": time.time(),
            "event": str(event),
            "pid": self.pid,
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            **payload,
        }
        with open(self.event_log_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(
                record, ensure_ascii=False, sort_keys=True
            ))
            stream.write("\n")

    def _clear_ipc_files(self):
        for leaf in _INSTANCE_IPC_FILES:
            try:
                os.unlink(os.path.join(self.ipc_dir, leaf))
            except FileNotFoundError:
                pass


def build_fays_runtime_env():
    """构造生产 Fays/OpenCV 4.8/ORB-SLAM3 子进程环境。"""
    env = os.environ.copy()
    arch = os.uname().machine if hasattr(os, "uname") else "x86_64"
    priority_dirs = [
        "/usr/local/lib",
        os.path.join(FAYS_SDK_ROOT, f"lib/fays_atrak/{arch}/Release"),
        os.path.join(FAYS_SDK_ROOT, f"thirdparty/ft602-linux-{arch}"),
        os.path.dirname(ORB_LIBRARY),
        os.path.join(ORB_ROOT, "lib"),
        os.path.join(ORB_ROOT, "Thirdparty", "DBoW2", "lib"),
        os.path.join(ORB_ROOT, "Thirdparty", "g2o", "lib"),
    ]

    # 生产桥接程序只允许 OpenCV 4.8。继承环境中的 SDK 4.2 和旧 dist/lib
    # 会让同一进程混装两个 OpenCV ABI，因此必须过滤。
    legacy_dist = os.path.abspath(os.path.join(PROJECT_ROOT, "dist", "lib"))
    inherited_dirs = []
    for path in env.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        if not path:
            continue
        absolute = os.path.abspath(path)
        if "opencv-4.2.0" in absolute or absolute == legacy_dist:
            continue
        inherited_dirs.append(absolute)

    ordered = []
    for path in priority_dirs + inherited_dirs:
        if path not in ordered:
            ordered.append(path)
    env["LD_LIBRARY_PATH"] = os.pathsep.join(ordered)

    # 当前 ORB 核心直接链接系统 Boost 1.74。清除继承环境中人为预加载的
    # Boost serialization，避免另一 ABI 在系统 1.74 之前发生符号抢占；其余
    # 与 ORB 无关的预加载项保持不变。
    inherited_preloads = [
        path for path in re.split(
            r"[\s:]+", env.get("LD_PRELOAD", "").strip()
        )
        if path and "libboost_serialization.so" not in os.path.basename(path)
    ]
    if inherited_preloads:
        env["LD_PRELOAD"] = " ".join(inherited_preloads)
    else:
        env.pop("LD_PRELOAD", None)
    env["FAYS_SDK_ROOT"] = FAYS_SDK_ROOT
    env["KSQ_ORB_ROOT"] = ORB_ROOT
    return env


def build_fays_probe_env():
    """构造官方标定探针子进程环境（OpenCV 4.2 + fays_vikit + ft602）。

    探针二进制依赖 opencv-4.2（libopencv_imgproc/core.so.4.2），与桥接
    进程的 OpenCV 4.8 环境必须隔离；探针环境不过滤 opencv-4.2，也不含
    orb48_env/ORB-SLAM 目录。
    """
    env = os.environ.copy()
    arch = os.uname().machine if hasattr(os, "uname") else "x86_64"
    priority_dirs = [
        "/usr/local/lib",
        os.path.join(FAYS_SDK_ROOT, f"lib/fays_atrak/{arch}/Release"),
        os.path.join(FAYS_SDK_ROOT, f"thirdparty/ft602-linux-{arch}"),
        os.path.join(FAYS_SDK_ROOT,
                     f"thirdparty/opencv-4.2.0-linux-{arch}/lib"),
        # opencv-4.2 的传递依赖（libtbb.so.2 等）只存在于 orb48_env；
        # 排在 opencv-4.2 之后，4.2 同名 soname 优先命中
        os.path.join(FAYS_SDK_ROOT, "thirdparty", "orb48_env", "lib"),
    ]
    inherited_dirs = [
        os.path.abspath(path)
        for path in env.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if path
    ]
    ordered = []
    for path in priority_dirs + inherited_dirs:
        if path not in ordered:
            ordered.append(path)
    env["LD_LIBRARY_PATH"] = os.pathsep.join(ordered)
    env["FAYS_SDK_ROOT"] = FAYS_SDK_ROOT
    return env


def validate_fays_runtime():
    """校验启动所需静态文件；不要求相机当前已插入。"""
    from device_manifest_writer import load_device_manifest

    required_files = {
        "Fays ORB 可执行文件": FAYS_ORB_BINARY,
        "设备清单": DEVICE_MANIFEST,
        "ORB 词典": ORB_VOCABULARY,
        "ORB-SLAM3 核心库": ORB_LIBRARY,
    }
    try:
        manifest = load_device_manifest()
    except RuntimeError as exc:
        raise FaysRuntimeError(str(exc)) from exc
    for unit in manifest["units"]:
        fays = unit["fays"]
        prefix = f"{unit['unit_id']} ({fays['product_serial']})"
        required_files[f"{prefix} SDK YAML"] = fays["sdk_yaml"]
        required_files[f"{prefix} ORB YAML"] = fays["orb_yaml"]
        required_files[f"{prefix} ORB 程序"] = fays["orb_binary"]
    errors = [
        f"{label}不存在: {path}"
        for label, path in required_files.items()
        if not os.path.isfile(path)
    ]
    if os.path.isfile(FAYS_ORB_BINARY) and not os.access(FAYS_ORB_BINARY, os.X_OK):
        errors.append(f"Fays ORB 文件不可执行: {FAYS_ORB_BINARY}")
    if errors:
        raise FaysRuntimeError("Fays 运行时检查失败:\n- " + "\n- ".join(errors))
    return required_files


def main():
    """执行不打开相机、不复位 USB 的 Fays 运行时与设备预检。"""
    try:
        required = validate_fays_runtime()
        groups = discover_fays_manifest_device_groups()
        verified = []
        for group in groups:
            speed = validate_fays_sdk_access(group)
            verified.append((group, speed, fays_usb_control_node(group)))
    except FaysRuntimeError as exc:
        print(f"[FAYS-PREFLIGHT] FAILED\n{exc}")
        return 1

    print(f"[FAYS-PREFLIGHT] OK: {SUPPORTED_CAMERA}")
    for label, path in required.items():
        print(f"  {label}: {path}")
    for group, usb_speed, control_node in verified:
        print(
            f"  unit={group['unit_id']} serial={group['product_serial']} "
            f"physical={group['physical_usb_path']}"
        )
        for role in ("stereo", "imu"):
            info = group[role]
            print(
                f"    {role}: {info['device']} "
                f"interface={info['interface_number']} "
                f"index={info['stream_index']}"
            )
        print(f"    Fays USB link: {usb_speed:g}M SuperSpeed")
        print(f"    Fays D3XX control: {control_node} read/write OK")
    print("  Hardware access: sysfs read-only; no VideoCapture; no USB reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
