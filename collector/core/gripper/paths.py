"""core/gripper 包的原生资源路径与运行时常量补丁。

夹爪 Python 代码与原生资源全部随本包交付（有意从 online/gripper_version1
分叉）：原生二进制、官方 SDK、标定文件与 orb48_env 都镜像在
core/gripper/native/ 下（布局与 online/ 一致，保持 $ORIGIN 相对 RUNPATH
可解析）；触觉 Sightac SDK 单独放在 core/gripper/sightac_sdk/（pyarmor
加密后随包入库）。主程序不再依赖 online/ 树。本模块是唯一路径解析点。

主程序导入 core.gripper 时由 __init__ 执行 patch_fays_runtime_constants()，
把 fays_runtime 的 PROJECT_ROOT/GRIPPER_DIR 等常量改指到 native/ 下，并向
build_fays_runtime_env 注入 orb48_env（与 1234 启动脚本的顺序一致）。
"""

from __future__ import annotations

import os

# 采集器仓库根（core/gripper/paths.py 的上三级）。
COLLECTOR_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))

NATIVE_ROOT = os.path.abspath(os.environ.get(
    "KSQ_GRIPPER_NATIVE_ROOT",
    os.path.join(PACKAGE_ROOT, "native"),
))
GRIPPER_DIR = os.path.join(NATIVE_ROOT, "gripper_version1")
FAYS_SDK_ROOT = os.path.join(NATIVE_ROOT, "FaysSense_VI_Kit_Release")
ORB_ROOT = os.path.join(NATIVE_ROOT, "ORB-SLAM")
DIST_ROOT = os.path.join(NATIVE_ROOT, "dist")
ORB48_ENV_DIR = os.path.join(FAYS_SDK_ROOT, "thirdparty", "orb48_env")

FAYS_MARK_ONLY_BINARY = os.path.join(
    DIST_ROOT, "fays_opencv48", "bin",
    "fayssense_orb_slam_sn219_opencv48_mark_only",
)
FAYS_CALIBRATION_PROBE = os.path.join(
    DIST_ROOT, "fays_aikit", "bin", "fays_vikit_calibration_probe",
)
ORB_LIBRARY = os.path.join(
    DIST_ROOT, "orb_mark_only", "lib", "libORB_SLAM3.so",
)
ORB_VOCABULARY = os.path.join(NATIVE_ROOT, "ORBvoc.txt")
CAMERA_SERVICE_DIR = os.path.join(NATIVE_ROOT, "camera_service")
CAMERA_SERVICE_BINARY = os.path.join(
    CAMERA_SERVICE_DIR, "build", "ksq-camera-service",
)
DISCOVER_UVC_CONFIG_BINARY = os.path.join(
    CAMERA_SERVICE_DIR, "build", "discover-uvc-config",
)
SIGHTAC_SDK_ROOT = os.path.abspath(os.environ.get(
    "KSQ_SIGHTAC_ROOT",
    os.path.join(PACKAGE_ROOT, "sightac_sdk"),
))
FAYS_CONFIG_DIR = os.path.join(GRIPPER_DIR, "fays_config")
ORB_DEVICE_CONFIG_DIR = os.path.join(DIST_ROOT, "fays_opencv48")
FAYS_LOCK_DIR = "/tmp/ksq-gripper-fays-locks"


def required_resources():
    """资源校验表：路径 -> 是否需要可执行位。全部就绪夹爪才对外可见。"""
    return {
        "Fays ORB 桥接": (FAYS_MARK_ONLY_BINARY, True),
        "Fays 标定探测": (FAYS_CALIBRATION_PROBE, True),
        "ORB-SLAM3 核心库": (ORB_LIBRARY, False),
        "ORB 词典": (ORB_VOCABULARY, False),
        "libuvc 相机服务": (CAMERA_SERVICE_BINARY, True),
        "Sightac SDK": (os.path.join(SIGHTAC_SDK_ROOT, "api_new"), False),
        "Fays SDK lib": (os.path.join(FAYS_SDK_ROOT, "lib"), False),
    }


def gripper_resources_available():
    """全部原生资源就绪才允许夹爪设备出现在主程序设备列表中。"""
    for path, executable in required_resources().values():
        if not os.path.exists(path):
            return False
        if executable and not os.access(path, os.X_OK):
            return False
    return True


def per_device_fays_yamls(serial):
    """按 SDK 探测序列号定位这台 Fays 的 SDK 模板与 ORB 标定 yaml。"""
    serial = str(serial or "").strip()
    if not serial:
        raise RuntimeError("Fays 序列号为空，无法定位设备标定")
    sdk_yaml = os.path.join(FAYS_CONFIG_DIR, f"fays_vikit_{serial}.yaml")
    orb_yaml = os.path.join(
        ORB_DEVICE_CONFIG_DIR, f"s80m_{serial}_stereo_inertial.yaml",
    )
    missing = [
        label for label, path in (
            ("SDK YAML", sdk_yaml), ("ORB YAML", orb_yaml),
        )
        if not os.path.isfile(path)
    ]
    if missing:
        raise RuntimeError(
            "当前 Fays 缺少运行标定文件: serial={} 缺少={}；"
            "请先在夹爪上位机中运行 device_setup".format(
                serial, ",".join(missing))
        )
    return sdk_yaml, orb_yaml


def patch_fays_runtime_constants():
    """把 fays_runtime 模块常量改指到 native/ 树，并注入 orb48_env。

    必须在任何 `from core.gripper.fays_runtime import <常量>` 之前调用——
    core/gripper/__init__.py 在包导入时最先执行，顺序天然保证。
    """
    from core.gripper import fays_runtime as runtime

    runtime.GRIPPER_DIR = GRIPPER_DIR
    runtime.PROJECT_ROOT = NATIVE_ROOT
    runtime.FAYS_SDK_ROOT = FAYS_SDK_ROOT
    runtime.ORB_ROOT = ORB_ROOT
    runtime.FAYS_MARK_ONLY_BINARY = FAYS_MARK_ONLY_BINARY
    runtime.FAYS_ORB_BINARY = FAYS_MARK_ONLY_BINARY
    runtime.ORB_VOCABULARY = ORB_VOCABULARY
    runtime.ORB_LIBRARY = ORB_LIBRARY
    # 派生常量在 fays_runtime 导入时已按旧根计算，必须逐一改写。
    runtime.FAYS_DEVICE_CONFIG = os.path.join(
        FAYS_SDK_ROOT, "config", "fays_vikit_s80m.yaml")
    runtime.DEVICE_MANIFEST = os.path.join(
        GRIPPER_DIR, "device_manifest.json")
    runtime.ORB_WORK_DIR = os.path.join(
        NATIVE_ROOT, "ORB-SLAM", "Examples", "fays")
    runtime.ORB_TRAJECTORY = os.path.join(
        NATIVE_ROOT, "ORB-SLAM", "traj.txt")
    runtime.AIKIT_BRIDGE_BINARY = os.path.join(
        DIST_ROOT, "fays_aikit", "bin", "fays_aikit_slam_bridge")
    runtime.AIKIT_CLIENT_LIBRARY_DIR = os.path.join(
        DIST_ROOT, "fays_aikit", "lib")
    runtime.AIKIT_CLIENT_CONFIG = os.path.join(
        DIST_ROOT, "fays_aikit", "config", "active_tracker_client.yaml")
    runtime.AIKIT_FACTORY_CALIBRATION_DIR = os.path.join(
        NATIVE_ROOT, "config", "calibration")

    _original_build_env = runtime.build_fays_runtime_env

    def build_fays_runtime_env():
        environment = _original_build_env()
        orb48_lib = os.path.join(ORB48_ENV_DIR, "lib")
        if not os.path.isdir(orb48_lib):
            return environment
        # 原版优先目录缺少 orb48_env（opencv4.8/pangolin 等桥接依赖）。
        # 按 1234 启动脚本的固定顺序插在 ft602 之后、ORB 库之前。
        parts = [
            part for part in
            environment.get("LD_LIBRARY_PATH", "").split(os.pathsep)
            if part
        ]
        insert_at = len(parts)
        for index, part in enumerate(parts):
            if os.path.basename(part).startswith("ft602-linux"):
                insert_at = index + 1
                break
        parts.insert(insert_at, orb48_lib)
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(parts)
        return environment

    runtime.build_fays_runtime_env = build_fays_runtime_env
