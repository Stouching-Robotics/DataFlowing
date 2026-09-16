"""Read a Fays S80M product serial through the official VI Kit SDK.

The FT602 USB bridge has duplicate USB descriptor data, so the only reliable
product identifier is ``ViKitDeviceInfo.serial_number``.  This module wraps
the existing calibration-probe executable and returns that serial without
opening a video stream or touching USB state.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from core.gripper.runtime.device_access import device_access_guard, FAYS_SDK_INITIALIZATION_LOCK, fays_device_guard
from typing import Mapping, Optional

from core.gripper.fays_runtime import (
    FAYS_DEVICE_CONFIG,
    GRIPPER_DIR,
    PROJECT_ROOT,
    discover_fays_device_groups,
    read_fays_config_ports,
)


DEFAULT_PROBE_BINARY = os.path.abspath(os.environ.get(
    "KSQ_FAYS_CALIBRATION_PROBE",
    os.path.join(
        PROJECT_ROOT, "dist", "fays_aikit", "bin",
        "fays_vikit_calibration_probe",
    ),
))
DEFAULT_TEMPLATE = FAYS_DEVICE_CONFIG
_SERIAL_LINE = re.compile(r"^device\.serial=(.+)$", re.MULTILINE)

# Temporary probes share the SDK initialization guard with the native launcher.
# A separate per-device lease prevents probes of an already streaming device.


def render_probe_config(
    ports: Mapping[str, str],
    destination: str,
    *,
    template_path: Optional[str] = None,
):
    """Render one candidate SDK YAML from the canonical template."""
    template = os.path.abspath(template_path or DEFAULT_TEMPLATE)
    try:
        with open(template, encoding="utf-8") as stream:
            content = stream.read()
    except OSError as exc:
        raise RuntimeError(f"无法读取 Fays 配置模板: {template}: {exc}") from exc
    for key in ("stereo_dev_port", "imu_dev_port"):
        value = str(ports.get(key, "")).strip()
        if not value:
            raise RuntimeError(f"缺少 Fays 端口字段: {key}")
        content, count = re.subn(
            rf"^(\s*{re.escape(key)}\s*:)\s*[^\s#]+",
            rf"\1 {value}",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise RuntimeError(f"Fays 配置模板无法定位字段: {key}")
    temporary = f"{destination}.tmp.{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise RuntimeError(f"写入探测配置失败: {destination}: {exc}") from exc
    return destination


def probe_product_serial(
    ports: Mapping[str, str],
    *,
    template_path: Optional[str] = None,
    probe_binary: Optional[str] = None,
    timeout: float = 60.0,
    environment: Optional[Mapping[str, str]] = None,
    serial_only_fast: bool = False,
) -> str:
    """Run the official calibration probe and return ``device.serial``.

    ``serial_only_fast`` 只允许用于身份扫描：探针读一次 SDK 设备信息、停掉
    自己持有的 IMU 流后直接退出进程，跳过厂商 SDK 较慢的 handle 析构。标定
    与运行路径必须走完整生命周期，不得使用该参数。

    timeout 同时是 SDK 全局初始化锁的等待上限与子进程运行上限：双夹爪
    并发打开时，第二台的探针要等第一台 SLAM 初始化（最长 45s）释放锁，
    20s 会在等待期间误报超时，故放宽到 60s。
    """
    executable = os.path.abspath(probe_binary or DEFAULT_PROBE_BINARY)
    if not os.path.isfile(executable):
        raise RuntimeError(f"Fays 标定探测程序不存在: {executable}")
    if not os.access(executable, os.X_OK):
        raise RuntimeError(f"Fays 标定探测程序不可执行: {executable}")
    with tempfile.TemporaryDirectory(prefix="ksq-fays-probe-") as temp_dir:
        config_path = render_probe_config(
            ports,
            os.path.join(temp_dir, "fays_vikit_probe.yaml"),
            template_path=template_path,
        )
        try:
            # The thread lock covers two slots in this process; flock also
            # protects against a second application instance probing through
            # the same vendor SDK at the same time.  This is synchronization
            # only: identity still comes exclusively from the live SDK result
            # and the device_setup manifest below.
            with device_access_guard(FAYS_SDK_INITIALIZATION_LOCK, timeout=timeout), \
                    fays_device_guard(ports["stereo_dev_port"]):
                command = [executable]
                if serial_only_fast:
                    command.append("--serial-only-fast")
                command.append(config_path)
                completed = subprocess.run(
                    command, capture_output=True, text=True,
                    timeout=timeout,
                    env=dict(os.environ if environment is None else environment),
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired 在 text=True 下也可能带着 bytes。超时发生在
            # SDK 释放阶段时，前半段（身份/序列号）往往已经成功，把子进程
            # 最后 20 行留下来，别让一句「超时」把底层证据丢掉。
            def _text(value):
                return (
                    value.decode("utf-8", errors="replace")
                    if isinstance(value, bytes) else (value or "")
                )
            diagnostic = "\n".join(
                (_text(exc.stdout) + "\n" + _text(exc.stderr)).splitlines()[-20:]
            )
            raise RuntimeError(
                f"Fays 标定探测超时（含 SDK 资源释放）: {ports} "
                f"({exc.timeout}s)\n{diagnostic}"
            ) from exc
        output = (
            (completed.stdout or "") + "\n" + (completed.stderr or "")
        )
        if completed.returncode != 0:
            tail = "\n".join(
                line for line in output.splitlines()[-20:]
            )
            raise RuntimeError(
                "Fays 标定探测失败: "
                f"returncode={completed.returncode} ports={ports}\n{tail}"
            )
        match = _SERIAL_LINE.search(output)
        if match is None:
            raise RuntimeError(
                f"Fays 标定探测未返回 device.serial: ports={ports}"
            )
        serial = match.group(1).strip()
        if not serial:
            raise RuntimeError(
                f"Fays 标定探测返回空序列号: ports={ports}"
            )
        return serial


def discover_fays_manifest_groups(
    *,
    target_unit=None,
    probe_binary: Optional[str] = None,
    timeout: float = 60.0,
    environment: Optional[Mapping[str, str]] = None,
    logger=None,
):
    """Resolve native Fays groups strictly through the device_setup manifest.

    Setup may probe every visible group. Connect supplies target_unit, limits
    access to its configured nodes, then verifies the live SDK product serial.
    Other rigs are never probed as a fallback for a missing selected device.
    Probe failure, an unknown serial, or missing manifest runtime artifacts is
    fatal.  YAML ports, old pair files, cached assignments and unique-group
    fallbacks are deliberately not accepted as identity sources.
    """
    from device_manifest_writer import load_device_manifest

    groups = tuple(discover_fays_device_groups())
    manifest = load_device_manifest()
    units = tuple(manifest["units"])
    units_by_serial = {
        str(unit["fays"]["product_serial"]).strip(): unit
        for unit in units
    }
    if target_unit is not None:
        # Physical position only limits access; identity still MUST come from
        # this live SDK probe and match the manifest serial below.
        expected = str(target_unit["fays"]["product_serial"])
        ports = read_fays_config_ports(target_unit["fays"]["sdk_yaml"])
        groups = tuple(g for g in groups if g["ports"] == ports)
        if len(groups) != 1:
            raise RuntimeError("所选 Fays 节点已变化，请重建设备清单；禁止探测其他夹爪")
        if expected not in units_by_serial:
            raise RuntimeError("所选 Fays 不在当前设备清单中")
    resolved = []
    probed_serials = set()
    for group in groups:
        try:
            serial = probe_product_serial(
                group["ports"],
                template_path=None,
                probe_binary=probe_binary,
                timeout=timeout,
                environment=environment,
            )
        except Exception as exc:
            raise RuntimeError(
                "Fays SDK 产品序列号探测失败，拒绝使用设备清单外的回退: "
                f"physical={group['physical_usb_path']} error={exc}"
            ) from exc
        if target_unit is not None and serial != expected:
            raise RuntimeError(f"所选 Fays SDK 序列号不匹配: expected={expected} actual={serial}")
        if serial in probed_serials:
            raise RuntimeError(
                "Fays SDK 返回重复产品序列号，拒绝猜测归属: "
                f"serial={serial}"
            )
        probed_serials.add(serial)
        unit = units_by_serial.get(serial)
        if unit is None:
            raise RuntimeError(
                "当前 Fays 不在 device_setup 设备清单中，拒绝启动: "
                f"serial={serial} physical={group['physical_usb_path']}"
            )
        fays = unit["fays"]
        required = ("sdk_yaml", "orb_yaml", "orb_binary")
        missing = [
            key for key in required
            if not fays.get(key) or not os.path.isfile(fays[key])
        ]
        if missing:
            raise RuntimeError(
                "device_setup 设备清单缺少 Fays 运行资源: "
                f"unit={unit['unit_id']} missing={','.join(missing)}"
            )
        annotated = dict(group)
        annotated.update({
            "unit_id": unit["unit_id"],
            "product_serial": serial,
            "calibration_serial": serial,
            "sdk_yaml": fays["sdk_yaml"],
            "orb_yaml": fays["orb_yaml"],
            "orb_binary": fays["orb_binary"],
            "mapping_source": "device_setup_manifest_sdk_serial",
            "manifest_esp32": dict(unit["esp32"]),
            "manifest_unit": unit["unit_id"],
        })
        resolved.append(annotated)

    if len(resolved) != len(groups):
        raise RuntimeError(
            "Fays 设备清单与当前硬件数量不一致，拒绝继续: "
            f"manifest={len(units)} visible={len(groups)} matched={len(resolved)}"
        )
    return tuple(resolved)
