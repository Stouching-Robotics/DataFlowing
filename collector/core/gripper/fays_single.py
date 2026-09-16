"""单夹爪场景的 Fays S80M 租约（无 device_setup 清单版本）。

主程序一次只服务一套夹爪 rig，设备身份全部来自运行时探测：
    1. ESP32 按 USB serial 唯一匹配（不看 /dev/ttyACMN 编号）；
    2. 从该 ESP32 的 NVS 读它绑定的 Fays 产品序列号（串口命令 QF）；
    3. 枚举在线完整 S80M，先逐台做 USB SuperSpeed 预检，再用官方 SDK 的
       serial-only-fast 读出真实产品序列号（FT602 描述符和枚举顺序都不算
       身份）；
    4. 绑定的序列号必须唯一命中一台在线 Fays —— **不按根端口猜归属**；
       被其他会话占用的 Fays 不参与匹配，但要写进错误文案；
    5. 对选中的那台再走一次完整 SDK 生命周期，复核序列号与 ESP32 绑定值
       一致（Connect 路径的完整校验）；
    6. 按序列号定位 fays_config/fays_vikit_{serial}.yaml 与
       dist/fays_opencv48/s80m_{serial}_stereo_inertial.yaml；缺任一个就**现场
       用厂商导出程序从这只夹爪读出厂标定补齐**（见 calibration.py），不必再
       人工跑上位机 device_setup + 导入；
    7. flock 租约 /tmp/ksq-gripper-fays-locks/{serial}.lock 防多开；
    8. materialize 当前端口的运行时 SDK yaml，并在 /dev/shm 建 IPC 目录。

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

from config import settings
from core.gripper import calibration, paths
from core.gripper.devices.gripper_serial import GripperSerial
from core.gripper.fays_runtime import (
    FAYS_MIN_USB_SPEED_MBPS,
    build_fays_probe_env,
    build_fays_runtime_env,
    discover_esp32_devices,
    discover_fays_device_groups,
    materialize_fays_device_config,
    validate_fays_sdk_access,
    validate_fays_superspeed,
)
from core.gripper.fays_serial_probe import probe_product_serial
from core.gripper.runtime.device_access import fays_device_guard

_INSTANCE_IPC_FILES = (
    "orb_pose.json.tmp", "orb_pose.json", "orb_meta.json",
    "orb_current_frame.jpg", "orb_control.json.tmp", "orb_control.json",
    "orb_raw_stream.sock", "camera_calibration.yaml", "imu.yaml",
)

# 原生 SLAM 日志留档。runtime_dir 在 release() 里被 rmtree，而 ORB_STAGE /
# FAYS-AFFINITY 这类逐秒诊断**只**写这个文件（GUI 日志刻意不转发它们），
# 所以一次干净退出的会话，其原生日志原本不可恢复 —— 2026-09-11 排查 SLAM
# 崩溃时就撞在这上面：唯一的证据随目录一起被删了。留档放 logs/ 下的独立
# 子目录，免得和 main.log 混放。
_NATIVE_LOG_NAME = "slam_stdout.log"
_NATIVE_LOG_ARCHIVE_KEEP = 20


def _native_log_archive_dir():
    return os.path.join(settings.LOGS_DIR, "slam_native")


def _prune_native_log_archive(logger=None):
    """只保留最近 _NATIVE_LOG_ARCHIVE_KEEP 份留档（长期运行不撑爆磁盘）。"""
    directory = _native_log_archive_dir()
    try:
        names = sorted(
            name for name in os.listdir(directory)
            if name.endswith(_NATIVE_LOG_NAME)
        )
    except OSError:
        return
    # 文件名前缀是 %Y%m%d_%H%M%S，字典序即时间序，不必逐个 stat
    for name in names[:-_NATIVE_LOG_ARCHIVE_KEEP]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass


def _archive_native_log(runtime_dir, serial, logger=None):
    """把即将被 rmtree 的 slam_stdout.log 拷进 logs/slam_native/。

    纯取证手段：任何失败只记一行日志，绝不打断 release() 的清理流程
    （留档丢了是少一份证据，清理半途而废会留下脏的租约/IPC 目录）。
    """
    source = os.path.join(runtime_dir, _NATIVE_LOG_NAME)
    if not os.path.isfile(source):
        return None
    try:
        directory = _native_log_archive_dir()
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        lease_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(serial or "unknown"))
        target = os.path.join(
            directory, f"{stamp}_{lease_key}_{_NATIVE_LOG_NAME}",
        )
        shutil.copy2(source, target)
    except OSError as exc:
        if logger is not None:
            logger(f"[Gripper-Fays] 原生日志留档失败: {exc}")
        return None
    _prune_native_log_archive(logger)
    return target


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
        """按 USB serial 唯一匹配 ESP32 控制板，返回其设备信息。"""
        wanted = str(esp_serial or "").strip()
        if not wanted:
            raise GripperFaysError(
                "未提供 ESP32 USB serial，拒绝按端口号或枚举顺序猜测控制板")
        devices = discover_esp32_devices()
        matches = [
            info for info in devices
            if str(info.get("serial") or "").strip() == wanted
        ]
        if len(matches) != 1:
            available = ", ".join(
                str(info.get("serial") or "<missing>") for info in devices
            )
            raise GripperFaysError(
                "ESP32 控制板未唯一匹配: 期望 serial={} 当前=[{}]".format(
                    wanted, available)
            )
        return matches[0]

    def _query_esp_bound_fays_serial(self, esp):
        """读 ESP32 NVS 里绑定的 Fays 产品序列号（串口命令 ``QF``）。

        串口是独占资源：查询完立刻归还，随后运行时的桥接还要独占打开。
        未绑定、空值、ERR 一律失败关闭 —— 不允许回退到拓扑猜测。
        """
        device = str(esp.get("device") or "").strip()
        esp_serial = str(esp.get("serial") or "<unknown>").strip()
        if not device:
            raise GripperFaysError(
                "ESP32 {} 缺少串口节点，无法查询绑定的 Fays 序列号".format(
                    esp_serial))
        bridge = GripperSerial(logger=self._logger)
        if not bridge.connect(device):
            raise GripperFaysError(
                "ESP32 {} 串口连接失败，无法查询绑定的 Fays 序列号: {}".format(
                    esp_serial, bridge.last_error or "unknown error"))
        try:
            response = ""
            for attempt in range(3):
                response = bridge.send("QF")
                if response.startswith(("FAYS_SERIAL:", "ERR FAYS_SERIAL")):
                    break
                if response.startswith("STATE") and attempt < 2:
                    # 固件周期性状态行插在命令响应前面：重试，别把串口
                    # 收发竞态误判成「没有绑定」。
                    time.sleep(0.05)
                    continue
                break
        finally:
            bridge.disconnect()
        prefix = "FAYS_SERIAL:"
        if response.startswith(prefix):
            serial = response[len(prefix):].strip()
            if serial:
                return serial
        if response.startswith("ERR FAYS_SERIAL_NOT_SET"):
            raise GripperFaysError(
                "ESP32 {} 尚未写入 Fays 序列号，拒绝按端口猜测配对；"
                "请先使用 WF:<serial> 写入后再扫描".format(esp_serial))
        raise GripperFaysError(
            "ESP32 {} 查询 Fays 序列号失败: {}".format(
                esp_serial, response or "<empty>"))

    @staticmethod
    def _device_in_use(group):
        """这台 Fays 是否已被运行中的会话独占（另一套 rig 的 SLAM）。

        双夹爪时另一套 rig 的 SLAM 会把本设备锁一直持有到会话结束，
        它按定义不可能是本次要打开的夹爪，但探测不了就得如实记下来。
        """
        port = str((group.get("ports") or {}).get("stereo_dev_port") or "")
        if re.fullmatch(r"/dev/video[0-9]+", port) is None:
            return False        # 节点无效留给后面的 SDK 预检去报错
        try:
            with fays_device_guard(port, timeout=0.0):
                return False
        except RuntimeError:
            return True

    def _probe_fays_groups_by_serial(self, groups):
        """SuperSpeed 预检 + ``serial-only-fast`` 读出每台 Fays 的产品序列号。

        预检在启动 SDK **之前**逐台做（只读 sysfs）：降级到 USB2 的设备
        根本不该进 SDK。返回 ``(by_serial, skipped)``：skipped 是被其他
        会话占用的设备，它们不参与匹配，但要写进错误文案。
        """
        for group in groups:
            try:
                validate_fays_superspeed(group)
            except Exception as exc:
                raise GripperFaysError(
                    "Fays USB 链路预检失败，拒绝启动 SDK: "
                    "physical={} error={}".format(
                        group.get("physical_usb_path"), exc)) from exc
        by_serial = {}
        skipped = []
        for group in groups:
            if self._device_in_use(group):
                skipped.append(group)
                continue
            ports = dict(group.get("ports") or {})
            if not ports:
                raise GripperFaysError(
                    "Fays 设备组缺少 SDK 端口: {!r}".format(group))
            try:
                serial = str(probe_product_serial(
                    ports,
                    environment=build_fays_probe_env(),
                    serial_only_fast=True,
                ) or "").strip()
            except Exception as exc:
                raise GripperFaysError(
                    "Fays SDK 产品序列号探测失败，拒绝回退到拓扑猜测: "
                    "physical={} error={}".format(
                        group.get("physical_usb_path"), exc)) from exc
            if not serial:
                raise GripperFaysError(
                    "Fays SDK 返回空产品序列号，拒绝继续: physical={}".format(
                        group.get("physical_usb_path")))
            if serial in by_serial:
                raise GripperFaysError(
                    "Fays SDK 返回重复产品序列号，拒绝猜测归属: "
                    "serial={}".format(serial))
            by_serial[serial] = group
        return by_serial, skipped

    def _resolve_fays_group(self, esp):
        """唯一 ESP32 → ``QF`` 绑定序列号 → 按 SDK serial 唯一匹配 Fays。

        返回 ``(group, bound_serial)``。**不再按 USB 根端口关联**：Fays 是
        USB3/FT602，插在 USB3 口时走 5000M 伴生总线，bus 号与 ESP/触觉
        相机的 480M 总线不同；多控制器机器上两套 rig 又经常落在同号根
        端口，按端口关联只会互相误配。
        """
        groups = tuple(discover_fays_device_groups())
        if not groups:
            raise GripperFaysError(
                "未发现完整的 Fays S80M（stereo + IMU 两个节点必须同时在线）")
        bound = self._query_esp_bound_fays_serial(esp)
        by_serial, skipped = self._probe_fays_groups_by_serial(groups)
        group = by_serial.get(bound)
        if group is None:
            online = ", ".join(sorted(by_serial)) or "<无>"
            detail = ""
            if skipped:
                detail = "；另有正在被占用的 Fays 未能探测: [{}]".format(
                    ", ".join(
                        "{}({})".format(
                            item.get("physical_usb_path"),
                            (item.get("ports") or {}).get("stereo_dev_port"))
                        for item in skipped))
            raise GripperFaysError(
                "ESP32 {} 绑定的 Fays 序列号 {} 不在当前在线的 Fays 中，"
                "拒绝按端口或枚举顺序猜测配对：在线 serial=[{}]{}".format(
                    esp.get("serial"), bound, online, detail))
        return group, bound

    def _probe_serial(self, group, expected_serial=None):
        """官方 SDK 完整生命周期读序列号；USB 速度不足时给出换口提示。

        ``expected_serial`` 是 ESP32 NVS 里的绑定值：Connect 路径必须复核
        两者一致，不允许一台「碰巧在线」的 Fays 顶替绑定设备。
        """
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
        if expected_serial is not None and \
                str(serial).strip() != str(expected_serial).strip():
            raise GripperFaysError(
                "Fays SDK 序列号与 ESP32 绑定值不一致，拒绝连接: "
                "physical={} bound={} actual={}".format(
                    group.get("physical_usb_path"),
                    expected_serial, serial))
        return serial, speed

    def _ensure_calibration(self, serial, group):
        """返回 (sdk_yaml, orb_yaml)；缺文件就现场读这只夹爪的出厂标定。

        快路径只 stat 两个文件，已覆盖的夹爪启动零额外开销、不碰设备。
        """
        try:
            return paths.per_device_fays_yamls(serial)
        except RuntimeError:
            pass                      # 缺标定，走下面现场生成
        self._logger(
            f"[Gripper-Fays] {serial} 尚无运行标定文件，"
            f"正在从这只夹爪读取厂商出厂标定…"
        )
        try:
            calibration.generate_fays_calibration(
                serial, group["ports"], logger=self._logger,
            )
            # 复核也用 collector 自己的解析函数：生成"成功"却没落在
            # paths 认得的路径上时，这里必须报出来，不能让下面那句裸
            # RuntimeError 把「请先跑 device_setup」当成唯一解释。
            return paths.per_device_fays_yamls(serial)
        except Exception as exc:
            raise GripperFaysError(
                f"Fays {serial} 缺少运行标定文件，且现场读取出厂标定未成功：{exc}。"
                f"可改用夹爪上位机运行 device_setup 后，执行 "
                f"tools/import_gripper_calibration.py {serial} "
                f"--source <上位机根目录> 手工导入。"
            ) from exc

    def refresh_calibration(self, esp_serial):
        """强制重新读取这只夹爪的出厂标定（覆盖已有文件）。

        与 acquire() 无关：不建租约、不占 IPC 目录，只是重新探一次设备身份
        再跑一遍厂商导出。调用前应确保该夹爪未在录制（否则 SDK 正被占用）。
        """
        with self._lock:
            esp = self._match_esp(esp_serial)
            group, bound = self._resolve_fays_group(esp)
            serial, _speed = self._probe_serial(
                group, expected_serial=bound)
            self._logger(
                f"[Gripper-Fays] 正在重新读取 {serial} 的厂商出厂标定…"
            )
            result = calibration.generate_fays_calibration(
                serial, group["ports"], logger=self._logger,
            )
            self._logger(
                f"[Gripper-Fays] {serial} 出厂标定已重新生成"
            )
            return result

    def acquire(self, esp_serial):
        """锁定并返回 selected 字典；重复调用返回当前租约快照。"""
        with self._lock:
            if self.is_acquired():
                return dict(self._selected)
            self.release()
            esp = self._match_esp(esp_serial)
            group, bound = self._resolve_fays_group(esp)
            serial, speed = self._probe_serial(group, expected_serial=bound)
            sdk_yaml, orb_yaml = self._ensure_calibration(serial, group)

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
                # 先留档再删：slam_stdout.log 只活在这个目录里，删掉就没了
                _archive_native_log(
                    self._runtime_dir,
                    (selected or {}).get("product_serial"),
                    self._logger,
                )
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
