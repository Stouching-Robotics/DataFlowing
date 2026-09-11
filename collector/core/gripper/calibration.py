"""现场读取一只 Fays 的出厂标定，生成它的 per-serial 运行文件。

厂商 ``dump_calib_opencv48`` 是唯一可信来源：``Camera1.fx/fy/cx/cy``、
``Stereo.b``、``IMU.T_b_c1`` 全是**逐设备**出厂标定，实测 098 与 099 三组
数值全不同，抄别人的会让 SLAM 初始化失败或位姿系统性跑偏。本模块从
``online/gripper_version1/fays_calibration.py`` 分叉而来（那份只在上位机
跑，且 ``online/`` 不入库），路径常量改指 ``core/gripper/native/``，两个
子进程补上 collector 的跨进程设备锁与 OpenCV 4.2 探针环境。

生成的三个产物（与原 device_setup 一致）：

    native/config/calibration/FS-VI80-<model>_<serial>_dump_calib.yaml
    native/gripper_version1/fays_config/fays_vikit_<serial>.yaml
    native/dist/fays_opencv48/s80m_<serial>_stereo_inertial.yaml

后两个正是 ``paths.per_device_fays_yamls()`` 要的文件，写齐即可开夹爪。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Mapping, Optional

from core.gripper import paths
from core.gripper.fays_runtime import FAYS_DEVICE_CONFIG, build_fays_probe_env
from core.gripper.fays_serial_probe import render_probe_config
from core.gripper.runtime.device_access import (
    FAYS_SDK_INITIALIZATION_LOCK,
    device_access_guard,
    fays_device_guard,
)


DUMP_BINARY = os.path.abspath(os.environ.get(
    "KSQ_FAYS_CALIBRATION_DUMP",
    os.path.join(
        paths.DIST_ROOT, "fays_opencv48", "bin", "dump_calib_opencv48",
    ),
))
# 与序列号探针是同一个二进制（既打 device.serial= 也打 imu.*=），
# 因此直接复用 paths 里那份已随包交付的路径。
IMU_PROBE_BINARY = os.path.abspath(os.environ.get(
    "KSQ_FAYS_IMU_CALIBRATION_PROBE", paths.FAYS_CALIBRATION_PROBE,
))
ORB_TEMPLATE = os.path.abspath(os.environ.get(
    "KSQ_FAYS_ORB_TEMPLATE",
    os.path.join(
        paths.ORB_ROOT, "Examples", "fays", "s80m_stereo_inertial.yaml",
    ),
))
SDK_TEMPLATE = FAYS_DEVICE_CONFIG
# 原始 dump 交给 AIKit 服务路径（fays_runtime._factory_calibration_for_serial
# 按 `FS-VI80-*_{serial}_dump_calib.yaml` 唯一命中）。
DUMP_OUTPUT_DIR = os.path.join(paths.NATIVE_ROOT, "config", "calibration")
SDK_CONFIG_DIR = paths.FAYS_CONFIG_DIR
ORB_OUTPUT_DIR = paths.ORB_DEVICE_CONFIG_DIR

_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
_PORT_PATTERN = re.compile(r"/dev/video\d+")
_DUMP_NAME_PATTERN = re.compile(r"^FS-VI80-.+_dump_calib\.yaml$")


def _log(logger, text):
    if logger is not None:
        try:
            logger(text)
        except Exception:          # 日志永远不能拖垮标定流程
            pass


def _spawn(executable, config_path, ports, temp_dir, timeout):
    """在 SDK 初始化锁 + 本设备锁下跑一次厂商二进制。

    两把锁缺一不可：初始化锁挡住与序列号探针/桥接并发初始化 SDK，设备锁挡住
    与另一只夹爪同时开流。
    """
    if not os.path.isfile(executable):
        raise RuntimeError(f"厂商标定程序不存在: {executable}")
    if not os.access(executable, os.X_OK):
        raise RuntimeError(f"厂商标定程序不可执行: {executable}")
    environment = build_fays_probe_env()
    environment.setdefault("ASAN_OPTIONS", "detect_leaks=0")
    with device_access_guard(FAYS_SDK_INITIALIZATION_LOCK, timeout=timeout), \
            fays_device_guard(ports["stereo_dev_port"], timeout=timeout):
        return subprocess.run(
            [executable, config_path],
            cwd=temp_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
        )


def _find_dump(output_root: str, product_serial: str) -> str:
    candidates = []
    for directory, _subdirectories, filenames in os.walk(output_root):
        for filename in filenames:
            if _DUMP_NAME_PATTERN.fullmatch(filename):
                candidates.append(os.path.join(directory, filename))
    matching = [
        path for path in candidates
        if product_serial in os.path.basename(path)
    ]
    if len(matching) != 1:
        if not matching and len(candidates) == 1:
            raise RuntimeError(
                "官方工具生成的标定文件序列号不匹配: "
                f"expected={product_serial}, file={os.path.basename(candidates[0])}"
            )
        raise RuntimeError(
            "官方工具未生成唯一的当前设备标定 YAML: "
            f"expected={product_serial}, files="
            f"{', '.join(os.path.basename(path) for path in candidates) or '<none>'}"
        )
    return matching[0]


def _copy_atomically(source: str, destination: str) -> str:
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = f"{destination}.tmp.{os.getpid()}"
    try:
        with open(source, "rb") as source_stream, open(temporary, "wb") as target:
            shutil.copyfileobj(source_stream, target)
            target.flush()
            os.fsync(target.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise RuntimeError(
            f"保存 Fays 标定 YAML 失败: {destination}: {exc}"
        ) from exc
    return destination


def _write_text_atomically(destination: str, content: str) -> str:
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = f"{destination}.tmp.{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise RuntimeError(
            f"保存 Fays ORB YAML 失败: {destination}: {exc}"
        ) from exc
    return destination


def write_fays_sdk_yaml(
    product_serial: str,
    ports: Mapping[str, str],
    *,
    template_path: Optional[str] = None,
    config_dir: Optional[str] = None,
) -> str:
    """按 per-serial 模板写 ``fays_vikit_<serial>.yaml``（端口逐台不同）。"""
    serial = str(product_serial or "").strip()
    if not _SERIAL_PATTERN.fullmatch(serial):
        raise ValueError(f"Fays 产品序列号无效: {product_serial!r}")
    stereo = str(ports.get("stereo_dev_port", "")).strip()
    imu = str(ports.get("imu_dev_port", "")).strip()
    if not _PORT_PATTERN.fullmatch(stereo):
        raise ValueError(f"stereo_dev_port 无效: {stereo!r}")
    if not _PORT_PATTERN.fullmatch(imu):
        raise ValueError(f"imu_dev_port 无效: {imu!r}")
    template = os.path.abspath(template_path or SDK_TEMPLATE)
    try:
        with open(template, encoding="utf-8") as stream:
            content = stream.read()
    except OSError as exc:
        raise RuntimeError(f"无法读取 Fays 模板: {template}: {exc}") from exc
    for key, value in (("stereo_dev_port", stereo), ("imu_dev_port", imu)):
        content, count = re.subn(
            rf"^(\s*{re.escape(key)}\s*:)\s*[^\s#]+",
            rf"\1 {value}",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise RuntimeError(f"Fays 模板无法定位字段: {key}")
    destination = os.path.join(
        os.path.abspath(config_dir or SDK_CONFIG_DIR),
        f"fays_vikit_{serial}.yaml",
    )
    return _write_text_atomically(destination, content)


def run_imu_probe(
    ports: Mapping[str, str],
    *,
    probe_binary: Optional[str] = None,
    template_path: Optional[str] = None,
    timeout: float = 60.0,
) -> str:
    """跑一次官方探针，返回它的原始输出（IMU 噪声模型从中解析）。"""
    executable = os.path.abspath(probe_binary or IMU_PROBE_BINARY)
    with tempfile.TemporaryDirectory(prefix="ksq-fays-imu-probe-") as temp_dir:
        config_path = render_probe_config(
            ports,
            os.path.join(temp_dir, "fays_vikit_imu_probe.yaml"),
            template_path=template_path,
        )
        try:
            completed = _spawn(executable, config_path, ports, temp_dir, timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Fays IMU 标定读取超时: timeout={exc.timeout}s"
            ) from exc
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        if completed.returncode != 0:
            tail = "\n".join(output.splitlines()[-20:])
            raise RuntimeError(
                "Fays IMU 标定读取失败: "
                f"returncode={completed.returncode}\n{tail}"
            )
        return output


def dump_fays_calibration(
    product_serial: str,
    ports: Mapping[str, str],
    *,
    output_dir: Optional[str] = None,
    dump_binary: Optional[str] = None,
    template_path: Optional[str] = None,
    timeout: float = 60.0,
) -> str:
    """跑官方导出程序，把这只设备的出厂标定存成 ``FS-VI80-*_dump_calib.yaml``。"""
    serial = str(product_serial or "").strip()
    if not _SERIAL_PATTERN.fullmatch(serial):
        raise ValueError(f"Fays 产品序列号无效: {product_serial!r}")
    executable = os.path.abspath(dump_binary or DUMP_BINARY)
    with tempfile.TemporaryDirectory(prefix="ksq-fays-calibration-") as temp_dir:
        config_path = render_probe_config(
            ports,
            os.path.join(temp_dir, "fays_vikit_calibration.yaml"),
            template_path=template_path,
        )
        try:
            completed = _spawn(executable, config_path, ports, temp_dir, timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Fays 标定导出超时: serial={serial}, timeout={exc.timeout}s"
            ) from exc
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        if completed.returncode != 0:
            tail = "\n".join(output.splitlines()[-20:])
            raise RuntimeError(
                "Fays 官方标定导出失败: "
                f"serial={serial}, returncode={completed.returncode}\n{tail}"
            )
        source = _find_dump(temp_dir, serial)
        destination = os.path.join(
            os.path.abspath(output_dir or DUMP_OUTPUT_DIR),
            os.path.basename(source),
        )
        return _copy_atomically(source, destination)


def _numbers(value: str):
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def _parse_factory_dump(path: str):
    """Parse the small, fixed schema emitted by FAYS_VIK_DumpCalib."""
    try:
        with open(path, encoding="utf-8") as stream:
            content = stream.read()
    except OSError as exc:
        raise RuntimeError(f"无法读取 Fays 工厂标定: {path}: {exc}") from exc

    cameras = []
    for index in range(2):
        section_match = re.search(rf"(?m)^cam{index}:\s*$", content)
        if section_match is None:
            raise RuntimeError(f"Fays 标定缺少 cam{index}: {path}")
        next_match = re.search(
            rf"(?m)^cam{index + 1}:\s*$",
            content[section_match.end():],
        )
        section_end = (
            section_match.end() + next_match.start()
            if next_match is not None else len(content)
        )
        section = content[section_match.end():section_end]

        def vector(key, expected):
            match = re.search(
                rf"(?m)^\s*{re.escape(key)}:\s*\[([^\]]+)\]",
                section,
            )
            if match is None:
                raise RuntimeError(f"Fays cam{index} 缺少 {key}")
            values = _numbers(match.group(1))
            if len(values) != expected:
                raise RuntimeError(f"Fays cam{index} 的 {key} 长度错误")
            return values

        def matrix(key):
            match = re.search(
                rf"(?ms)^\s*{re.escape(key)}:\s*\n"
                rf"((?:^\s*-\s*\[[^\n]+\]\s*\n?){{4}})",
                section,
            )
            if match is None:
                raise RuntimeError(f"Fays cam{index} 缺少 {key}")
            rows = re.findall(
                r"^\s*-\s*\[([^\]]+)\]",
                match.group(1),
                re.MULTILINE,
            )
            values = [_numbers(row) for row in rows]
            if len(values) != 4 or any(len(row) != 4 for row in values):
                raise RuntimeError(f"Fays cam{index} 的 {key} 不是 4x4")
            return values

        cameras.append({
            "intrinsics": vector("intrinsics", 4),
            "distortion": vector("distortion_coeffs", 4),
            "resolution": vector("resolution", 2),
            "T_cam_imu": matrix("T_cam_imu"),
            "T_cn_cnm1": (
                matrix("T_cn_cnm1") if index == 1 else None
            ),
            "timeshift": float(re.search(
                rf"(?m)^\s*timeshift_cam_imu:\s*([^\s#]+)",
                section,
            ).group(1)),
        })
    if cameras[0]["resolution"] != cameras[1]["resolution"]:
        raise RuntimeError("Fays 双目分辨率不一致")
    return cameras


def _parse_imu_probe(output: str):
    fields = {
        "noise_acc": "accelerometer_noise_density",
        "walk_acc": "accelerometer_random_walk",
        "noise_gyro": "gyroscope_noise_density",
        "walk_gyro": "gyroscope_random_walk",
        "imu_hz": "update_rate",
    }
    values = {}
    for target, source in fields.items():
        match = re.search(
            rf"(?m)^imu\.{re.escape(source)}=([^\s#]+)\s*$",
            output,
        )
        if match is None:
            raise RuntimeError(f"Fays IMU 标定输出缺少 imu.{source}")
        value = float(match.group(1))
        if value <= 0:
            raise RuntimeError(f"Fays IMU 标定值无效: imu.{source}={value}")
        values[target] = value
    return values


def _replace_yaml_scalar(content: str, key: str, value) -> str:
    rendered = str(value)
    if isinstance(value, float) and "." not in rendered:
        rendered += ".0"
    updated, count = re.subn(
        rf"(?m)^{re.escape(key)}\s*:\s*[^\n]*$",
        f"{key}: {rendered}",
        content,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"ORB YAML 模板缺少字段: {key}")
    return updated


def generate_orb_yaml(
    product_serial: str,
    dump_path: str,
    imu_output: str,
    *,
    template_path: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> str:
    """把设备 dump 换算成 ORB 的双目矫正内参 + IMU 外参 YAML。"""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("生成 ORB YAML 需要 OpenCV/Numpy") from exc

    cameras = _parse_factory_dump(dump_path)
    imu = _parse_imu_probe(imu_output)
    left, right = cameras
    left_intrinsics = left["intrinsics"]
    right_intrinsics = right["intrinsics"]
    K1 = np.array([
        [left_intrinsics[0], 0.0, left_intrinsics[2]],
        [0.0, left_intrinsics[1], left_intrinsics[3]],
        [0.0, 0.0, 1.0],
    ], dtype=float)
    K2 = np.array([
        [right_intrinsics[0], 0.0, right_intrinsics[2]],
        [0.0, right_intrinsics[1], right_intrinsics[3]],
        [0.0, 0.0, 1.0],
    ], dtype=float)
    D1 = np.array(left["distortion"], dtype=float).reshape(1, 4)
    D2 = np.array(right["distortion"], dtype=float).reshape(1, 4)
    T_lr = np.array(right["T_cn_cnm1"], dtype=float)
    T_cam_imu = np.array(left["T_cam_imu"], dtype=float)
    resolution = tuple(int(value) for value in left["resolution"])
    try:
        R1, _R2, P1, _P2, _Q = cv2.fisheye.stereoRectify(
            K1,
            D1,
            K2,
            D2,
            resolution,
            T_lr[:3, :3],
            T_lr[:3, 3],
            flags=cv2.CALIB_ZERO_DISPARITY,
        )
        T_rectified_to_unrectified = np.eye(4, dtype=float)
        T_rectified_to_unrectified[:3, :3] = R1.T
        T_b_c1 = np.linalg.inv(T_cam_imu) @ T_rectified_to_unrectified
    except Exception as exc:
        raise RuntimeError(f"Fays 双目鱼眼校正失败: {exc}") from exc

    try:
        with open(
            os.path.abspath(template_path or ORB_TEMPLATE),
            encoding="utf-8",
        ) as stream:
            content = stream.read()
    except OSError as exc:
        raise RuntimeError(
            f"无法读取 ORB YAML 模板: {template_path or ORB_TEMPLATE}"
        ) from exc

    model = os.path.basename(dump_path).split(f"_{product_serial}_", 1)[0]
    # 模板历史上同时用过 ``FS-VI-S80M`` 与 ``FS-VI80-S80M`` 两种型号注释，
    # 都只是标记，不能因为连字符差异挡住本次生成。
    content, count = re.subn(
        r"(?m)^# FS-VI(?:80-|-)?S80M.*$",
        f"# {model} serial {product_serial}",
        content,
        count=1,
    )
    if count != 1:
        raise RuntimeError("ORB YAML 模板缺少 Fays 序列号注释")
    # 模板第 3 行是厂商模板自带的出处注释，写死指向 3500000261870088。照抄
    # 下去，这台夹爪的 IMU 外参就被记成"取自另一台设备"——逐设备出厂标定最
    # 怕这种看起来像抄来的出处，真出位姿问题时会把排查方向带偏。改写成这只
    # 夹爪自己的序列号。模板里没这行也不报错：本改动只可能去掉假出处。
    content, _ = re.subn(
        r"(?m)^# IMU 外参来源:.*$",
        f"# IMU 外参来源: 工厂标定 {model}_{product_serial}"
        f" (dump_calib_opencv48 现场读取)",
        content,
        count=1,
    )
    content = _replace_yaml_scalar(content, "Camera1.fx", float(P1[0, 0]))
    content = _replace_yaml_scalar(content, "Camera1.fy", float(P1[1, 1]))
    content = _replace_yaml_scalar(content, "Camera1.cx", float(P1[0, 2]))
    content = _replace_yaml_scalar(content, "Camera1.cy", float(P1[1, 2]))
    content = _replace_yaml_scalar(
        content,
        "Stereo.b",
        float(np.linalg.norm(T_lr[:3, 3])),
    )
    content = _replace_yaml_scalar(content, "IMU.NoiseGyro", imu["noise_gyro"])
    content = _replace_yaml_scalar(content, "IMU.NoiseAcc", imu["noise_acc"])
    content = _replace_yaml_scalar(content, "IMU.GyroWalk", imu["walk_gyro"])
    content = _replace_yaml_scalar(content, "IMU.AccWalk", imu["walk_acc"])
    content = _replace_yaml_scalar(content, "IMU.Frequency", imu["imu_hz"])
    content = re.sub(
        r"(?ms)^IMU\.T_b_c1:\s*!!opencv-matrix\s*\n"
        r"\s*rows:\s*4\s*\n\s*cols:\s*4\s*\n"
        r"\s*dt:\s*f\s*\n\s*data:\s*\[[^\]]*\]\s*$",
        "IMU.T_b_c1: !!opencv-matrix\n"
        "  rows: 4\n  cols: 4\n  dt: f\n  data: [ "
        + ", ".join(
            f"{float(T_b_c1[row, col]):.15f}"
            for row in range(4) for col in range(4)
        )
        + " ]",
        content,
        count=1,
    )
    if "IMU.T_b_c1: !!opencv-matrix" not in content:
        raise RuntimeError("ORB YAML 模板缺少 IMU.T_b_c1")

    destination = os.path.join(
        os.path.abspath(output_dir or ORB_OUTPUT_DIR),
        f"s80m_{product_serial}_stereo_inertial.yaml",
    )
    return _write_text_atomically(destination, content)


def generate_fays_calibration(
    product_serial: str,
    ports: Mapping[str, str],
    *,
    logger=None,
    timeout: float = 60.0,
) -> dict:
    """读取这只夹爪的出厂标定并写齐三份产物（会打开设备）。"""
    serial = str(product_serial or "").strip()
    if not _SERIAL_PATTERN.fullmatch(serial):
        raise ValueError(f"Fays 产品序列号无效: {product_serial!r}")
    _log(logger, f"[Gripper-Fays] 新夹爪 {serial}：正在读取厂商出厂标定…")
    dump_path = dump_fays_calibration(serial, ports, timeout=timeout)
    sdk_yaml = write_fays_sdk_yaml(serial, ports)
    _log(logger, f"[Gripper-Fays] {serial} SDK 配置已就位，正在读取 IMU 出厂参数…")
    imu_output = run_imu_probe(ports, timeout=timeout)
    orb_yaml = generate_orb_yaml(serial, dump_path, imu_output)
    # 用 collector 自己的解析函数复核：这一步过了，插上就能开。
    resolved_sdk, resolved_orb = paths.per_device_fays_yamls(serial)
    _log(logger, f"[Gripper-Fays] {serial} 出厂标定已就位")
    return {
        "calibration_yaml": dump_path,
        "sdk_yaml": resolved_sdk or sdk_yaml,
        "orb_yaml": resolved_orb or orb_yaml,
    }


def ensure_fays_calibration(
    product_serial: str,
    ports: Mapping[str, str],
    *,
    logger=None,
    force: bool = False,
    timeout: float = 60.0,
) -> tuple:
    """返回 ``(sdk_yaml, orb_yaml)``；缺失或 force 时先现场生成。

    快路径只是一次 ``isfile``，已覆盖的夹爪启动时零额外开销、不碰设备。
    """
    if not force:
        try:
            return paths.per_device_fays_yamls(product_serial)
        except RuntimeError:
            pass
    generate_fays_calibration(
        product_serial, ports, logger=logger, timeout=timeout)
    return paths.per_device_fays_yamls(product_serial)
