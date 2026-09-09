"""单夹爪场景的 Fays S80M 租约（无 device_setup 清单版本）。

主程序一次只服务一套夹爪 rig，设备身份全部来自运行时探测：
    1. ESP32 串口（VID/PID + USB serial）唯一匹配；
    2. discover_fays_device_groups() 发现且仅发现一台完整 S80M；
    3. USB 链路速度 >= FAYS_MIN_USB_SPEED_MBPS，否则拒绝（提示换 USB3 口）；
    4. probe_product_serial() 通过官方 SDK 探针读取真实产品序列号；
    5. 按序列号定位 fays_config/fays_vikit_{serial}.yaml 与
       dist/fays_opencv48/s80m_{serial}_stereo_inertial.yaml；
    6. flock 租约 /tmp/ksq-gripper-fays-locks/{serial}.lock 防多开；
    7. materialize 当前端口的运行时 SDK yaml，并在 /dev/shm 建 IPC 目录。

返回的 selected 字典携带 SlamProcessController 与相机服务组合所需字段
（ports/runtime_config/ipc_dir/trajectory/settings/executable/...）。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
import threading
import time

from core.gripper import paths
from core.gripper.fays_runtime import (
    FAYS_MIN_USB_SPEED_MBPS,
    build_fays_probe_env,
    build_fays_runtime_env,
    discover_esp32_devices,
    discover_fays_device_groups,
    materialize_fays_device_config,
    validate_fays_sdk_access,
)
from core.gripper.fays_serial_probe import probe_product_serial

_INSTANCE_IPC_FILES = (
    "orb_pose.json.tmp", "orb_pose.json", "orb_meta.json",
    "orb_current_frame.jpg", "orb_control.json.tmp", "orb_control.json",
    "orb_raw_stream.sock", "camera_calibration.yaml", "imu.yaml",
)


class GripperFaysError(RuntimeError):
    """夹爪 Fays 租约失败（设备缺失、速度不足、标定缺失、被占用）。"""


class SingleFaysLease:
    """一次打开夹爪的 Fays 租约；release() 幂等。"""

    def __init__(self, *, logger=print):
        self._logger = logger
        self._lock = threading.RLock()
        self._stream = None
        self._selected = None
        self._runtime_dir = None
        self._ipc_dir = None
        self._lock_path = None

    @property
    def ipc_dir(self):
        return self._ipc_dir

    @property
    def runtime_config_path(self):
        if self._selected is None:
            raise GripperFaysError("Fays 租约尚未持有")
        return self._selected["runtime_config"]

    def is_acquired(self):
        with self._lock:
            return bool(
                self._selected is not None
                and self._stream is not None
                and not self._stream.closed
            )

    def _match_esp(self, esp_serial):
        """按 USB serial 唯一匹配 ESP32 控制板，返回其 tty 设备路径。"""
        devices = discover_esp32_devices()
        matches = [
            info for info in devices
            if str(info.get("serial") or "").strip() == str(esp_serial).strip()
        ]
        if len(matches) != 1:
            available = ", ".join(
                str(info.get("serial") or "<missing>") for info in devices
            )
            raise GripperFaysError(
                "ESP32 控制板未唯一匹配: 期望 serial={} 当前=[{}]".format(
                    esp_serial, available)
            )
        return matches[0]

    def _discover_unique_fays_group(self, esp):
        """按 ESP 的根端口关联 rig 内的 S80M，要求唯一命中。

        Fays 是 USB3/FT602：插在 USB3 口时走 5000M 伴生总线，与 ESP/
        触觉相机的 480M 总线 bus 号不同（如 ESP=7-2、Fays=8-2 是同一
        物理口的双总线伴生），因此原程序的拓扑规则只按根端口关联
        （见 uvc_camera_service._associate_group 的同名注释）。
        同一台机器可能还插着主程序自己的独立 S80M（不同根端口），
        必须按夹爪 rig 的拓扑范围区分，不能全机枚举；多台命中即报错
        不猜归属。rig 内仍只允许 1 台完整 S80M。
        多控制器机器（双夹爪）还必须带控制器身份比较：两套 rig 可能
        都插在各自控制器的同号根端口上（实测都是根端口 2，控制器
        0000:0a:00.0 与 0000:74:00.4），只比端口号会互相误配。
        """
        esp_controller, esp_root = self._bus_root(
            str(esp.get("physical_usb_path") or ""))
        if not esp_controller or not esp_root:
            raise GripperFaysError(
                "ESP32 物理 USB 路径无效: {!r}".format(
                    esp.get("physical_usb_path")))
        groups = tuple(discover_fays_device_groups())
        same_rig = [
            group for group in groups
            if self._bus_root(group["physical_usb_path"])
            == (esp_controller, esp_root)
        ]
        if len(same_rig) != 1:
            found = ", ".join(
                str(g["physical_usb_path"]) for g in same_rig)
            raise GripperFaysError(
                "夹爪 rig（控制器 {} 根端口 {}）内发现 {} 台 Fays S80M，"
                "只允许 1 台: [{}]".format(
                    esp_controller, esp_root, len(same_rig), found))
        return same_rig[0]

    @staticmethod
    def _bus_root(physical):
        """"1-3.4.1" → (controller, "3")。

        controller 取 /sys/bus/usb/devices/usb{bus} 的 realpath 父路径
        （usbN 节点之上的 PCI devpath，如 .../0000:0a:00.0）——多控制器
        机器上不同控制器的同号根端口必须区分；USB2/USB3 伴生双总线
        （同一物理口 ESP=7-2、Fays=8-2）仍只按控制器+根端口关联，
        不比较总线号，伴生容忍不变。
        """
        bus, separator, ports = str(physical or "").partition("-")
        root = ports.split(".")[0] if separator else ""
        if not separator:
            return "", ""
        try:
            real = os.path.realpath(
                os.path.join("/sys", "bus", "usb", "devices", f"usb{bus}"))
        except OSError:
            return f"bus-{bus}", root   # sysfs 不可读时退回总线区分
        return os.path.dirname(real), root

    def _probe_serial(self, group):
        """官方 SDK 探针读取产品序列号；USB 速度不足时给出换口提示。"""
        try:
            speed = validate_fays_sdk_access(group)
        except Exception as exc:
            raise GripperFaysError(
                f"Fays USB 链路校验失败（疑似插在 USB2 口，"
                f"SuperSpeed 需 >= {FAYS_MIN_USB_SPEED_MBPS:g}M）: {exc}"
            ) from exc
        if speed < FAYS_MIN_USB_SPEED_MBPS:
            raise GripperFaysError(
                "Fays USB 链路速度 {:.0f}M，低于 {:.0f}M SuperSpeed 要求；"
                "请把夹爪 USB3 接头插到电脑的 USB3 口后重试".format(
                    speed, FAYS_MIN_USB_SPEED_MBPS)
            )
        try:
            serial = probe_product_serial(
                group["ports"],
                environment=build_fays_probe_env(),
            )
        except Exception as exc:
            raise GripperFaysError(
                f"Fays SDK 产品序列号探测失败: {exc}"
            ) from exc
        return serial, speed

    def acquire(self, esp_serial):
        """锁定并返回 selected 字典；重复调用返回当前租约快照。"""
        with self._lock:
            if self.is_acquired():
                return dict(self._selected)
            self.release()
            esp = self._match_esp(esp_serial)
            group = self._discover_unique_fays_group(esp)
            serial, speed = self._probe_serial(group)
            sdk_yaml, orb_yaml = paths.per_device_fays_yamls(serial)

            os.makedirs(paths.FAYS_LOCK_DIR, mode=0o700, exist_ok=True)
            lease_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", serial)
            lock_path = os.path.join(
                paths.FAYS_LOCK_DIR, f"{lease_key}.lock",
            )
            stream = open(lock_path, "a+", encoding="utf-8")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                stream.seek(0)
                owner = stream.read().strip() or "unknown owner"
                stream.close()
                raise GripperFaysError(
                    f"Fays {serial} 已被另一进程占用: {owner}"
                ) from exc

            runtime_dir = tempfile.mkdtemp(prefix="ksq-gripper-fays-")
            # 双夹爪同进程各持一份租约：IPC 目录必须含租约身份（序列号），
            # 否则第二台撞 orb_raw_stream.sock/orb_pose.json 导致 SLAM
            # 初始化失败，且一方 release() 会连带删掉另一方的 IPC 文件。
            instance_id = f"{lease_key}-{os.getpid()}"
            ipc_dir = os.path.join("/dev/shm", f"ksq-gripper-{instance_id}")
            os.makedirs(ipc_dir, mode=0o700, exist_ok=True)
            try:
                owner = {
                    "pid": os.getpid(),
                    "run_id": "collector-main",
                    "instance_id": instance_id,
                    "esp_serial": str(esp["serial"]),
                    "fays_serial": serial,
                    "physical_usb_path": group["physical_usb_path"],
                }
                stream.seek(0)
                stream.truncate()
                json.dump(owner, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
                runtime_config = materialize_fays_device_config(
                    group["ports"],
                    os.path.join(runtime_dir, "fays_vikit_runtime.yaml"),
                    sdk_yaml,
                )
                selected = dict(group)
                selected.update({
                    "unit_id": f"gripper_{serial[-4:]}",
                    "esp_serial": str(esp["serial"]),
                    "product_serial": serial,
                    "calibration_serial": serial,
                    "mapping_source": "runtime_probe_sdk_serial",
                    "sdk_yaml": sdk_yaml,
                    "orb_yaml": orb_yaml,
                    "orb_binary": paths.FAYS_MARK_ONLY_BINARY,
                    "runtime_config": runtime_config,
                    "ipc_dir": ipc_dir,
                    "trajectory": os.path.join(
                        runtime_dir, "orb_trajectory.txt",
                    ),
                    "work_dir": runtime_dir,
                    "lock_path": lock_path,
                    "usb_speed_mbps": speed,
                    "esp_tty": str(esp["device"]),
                })
                self._stream = stream
                self._selected = selected
                self._runtime_dir = runtime_dir
                self._ipc_dir = ipc_dir
                self._lock_path = lock_path
                self._logger(
                    "[Gripper-Fays] 租约已锁定: serial={} "
                    "stereo={} imu={} speed={:g}M ipc={}".format(
                        serial,
                        group["ports"]["stereo_dev_port"],
                        group["ports"]["imu_dev_port"],
                        speed, ipc_dir,
                    )
                )
                return dict(selected)
            except Exception:
                try:
                    shutil.rmtree(runtime_dir, ignore_errors=True)
                except Exception:
                    pass
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
                raise

    def resolve_assignment(self, esp_serial):
        """相机服务所需的身份 assignment（不探测、不锁租约、不校验速度）。

        UvcCameraServiceManager 只把 fays identity 用作同根端口关联与
        错误文案，从不打开 Fays 相机；此处用实时拓扑推导的物理路径即可，
        S80M 的官方 SDK 串行探测留给 acquire()（P3 全链，含 USB3 校验）。
        """
        esp = self._match_esp(esp_serial)
        group = self._discover_unique_fays_group(esp)
        return {
            "esp32": {"serial": str(esp["serial"])},
            "fays": {
                "product_serial": "runtime-unprobed",
                "physical_usb_path": group["physical_usb_path"],
            },
        }

    def snapshot(self):
        with self._lock:
            return (
                json.loads(json.dumps(self._selected))
                if self._selected is not None else None
            )

    def release(self):
        with self._lock:
            stream, selected = self._stream, self._selected
            self._stream = None
            self._selected = None
            if stream is not None:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
            if self._ipc_dir:
                for leaf in _INSTANCE_IPC_FILES:
                    try:
                        os.unlink(os.path.join(self._ipc_dir, leaf))
                    except FileNotFoundError:
                        pass
                try:
                    os.rmdir(self._ipc_dir)
                except OSError:
                    pass
            self._ipc_dir = None
            if self._runtime_dir:
                shutil.rmtree(self._runtime_dir, ignore_errors=True)
                self._runtime_dir = None
            if selected is not None:
                self._logger(
                    "[Gripper-Fays] 租约已释放: serial={}".format(
                        selected.get("product_serial")
                    )
                )
            self._lock_path = None
            return True
