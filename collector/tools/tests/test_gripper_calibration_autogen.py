#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新夹爪接入即自动生成标定 —— 离线自检（不碰真设备/真实 native 目录）。

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_gripper_calibration_autogen.py

背景：接入没见过的夹爪（序列号没被覆盖）时主程序直接拒绝启动，报
「当前 Fays 缺少运行标定文件…请先在夹爪上位机中运行 device_setup」——
要人工去上位机跑一遍 GUI 再把两个 YAML 搬过来。现在改成：`acquire()` 发现
缺文件就地调厂商导出程序从**这只夹爪自身**读出厂标定，写完直接连上。

出厂标定是逐设备的（`Camera1.fx/fy/cx/cy`、`Stereo.b`、`IMU.T_b_c1` 实测
098/099 三组全不同），所以本测试用**合成**的 dump 与模板，不用真机数据。

覆盖:
  1. _find_dump：唯一命中 / 多设备取对号 / 无命中 / 序列号不符 四种判定
  2. write_fays_sdk_yaml 只换两个端口字段，其余逐字节不动；非法输入拒绝
  3. dump_fays_calibration：假厂商二进制（sh 脚本）→ 产物落盘且逐字节一致
  4. **两个子进程都套了 SDK 初始化锁 + 本设备锁**（参数逐项核对）
  5. **两个子进程拿到的都是 build_fays_probe_env() 的环境**（OpenCV 4.2 必需）
  6. 假二进制的**非零退出码 / 超时**都要变成可读的 RuntimeError
  7. _parse_factory_dump 逐字段解析 + 双目分辨率不一致要报错
  8. _parse_imu_probe 五个字段映射；缺字段 / 非正值要报错
  9. generate_orb_yaml：Stereo.b = |T_cn_cnm1 平移|（独立算的期望值）、
     IMU 噪声五项等于探针读数、T_b_c1 结构合法、尺寸字段不被改写
 10. ensure_fays_calibration 快路径：两个文件都在时不生成、不碰设备
 11. ensure_fays_calibration 端到端：缺失 → 生成 → per_device_fays_yamls 通过
 12. fays_single._ensure_calibration：已覆盖走快路径；缺文件触发生成；
     生成失败抛 GripperFaysError 且**同时**给出底层原因与手工兜底办法
 13. 真机产物回放（native/ 在时）：真实 dump + 真实 ORB YAML 逐字节复现
 14. 设备面板右键菜单：只对夹爪行出现、录制中禁用、非夹爪行不发信号
退出码 0 = 全部通过。
"""

from __future__ import annotations

import math
import os
import re
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock                                       # noqa: E402

from core.gripper import calibration, fays_serial_probe, paths   # noqa: E402
from core.gripper.fays_single import (                           # noqa: E402
    GripperFaysError, SingleFaysLease,
)
from core.gripper.runtime.device_access import (                 # noqa: E402
    FAYS_SDK_INITIALIZATION_LOCK,
)

FAILS = []
SERIAL = "3500000999990001"
OTHER_SERIAL = "3500000999990002"

# ── 合成的出厂 dump（结构照真机，数值是编的但自洽：鱼眼双目 + 8cm 基线）──
DUMP_NAME = f"FS-VI80-S80M_{SERIAL}_dump_calib.yaml"
LEFT_FX = 235.5
_T_CAM_IMU = """  T_cam_imu:
  - [-0.9998, -0.0177, -0.0053, -0.0046]
  - [0.0176, -0.9996, 0.0238, 0.0088]
  - [-0.0058, 0.0237, 0.9997, -0.0088]
  - [0.0, 0.0, 0.0, 1.0]
"""
_T_CN_CNM1 = """  T_cn_cnm1:
  - [0.9999, -0.0073, 0.0093, -0.07985]
  - [0.0075, 0.9998, -0.0211, -0.00021]
  - [-0.0091, 0.0211, 0.9997, 0.00011]
  - [0.0, 0.0, 0.0, 1.0]
"""
DUMP_BODY = (
    "cam0:\n" + _T_CAM_IMU
    + "  camera_model: pinhole\n"
    + "  distortion_coeffs: [0.031, 0.062, -0.048, 0.013]\n"
    + "  distortion_model: equidistant\n"
    + f"  intrinsics: [{LEFT_FX}, 235.2, 318.7, 205.4]\n"
    + "  resolution: [640, 400]\n"
    + "  timeshift_cam_imu: 0.001\n"
    + "cam1:\n" + _T_CAM_IMU + _T_CN_CNM1
    + "  camera_model: pinhole\n"
    + "  distortion_coeffs: [0.033, 0.060, -0.047, 0.0129]\n"
    + "  distortion_model: equidistant\n"
    + "  intrinsics: [235.1, 235.0, 326.3, 203.9]\n"
    + "  resolution: [640, 400]\n"
    + "  timeshift_cam_imu: 0.0011\n"
)
# 基线应等于 T_cn_cnm1 平移的模（测试里独立算，不看实现）
EXPECTED_BASELINE = math.sqrt(0.07985 ** 2 + 0.00021 ** 2 + 0.00011 ** 2)

# ── 合成的 SDK 模板（两个端口字段 + 若干无关字段）────────────────────────
SDK_TEMPLATE_BODY = """%YAML:1.0
# FS-VI-S80M test template
stereo_dev_port: /dev/video0
stereo_single_cam_width: 640
stereo_single_cam_height: 400
stereo_fps: 50
imu_dev_port: /dev/video2
gravity: 9.7946
"""

# ── 合成的 ORB 模板（含所有会被改写的键）───────────────────────────────
ORB_TEMPLATE_BODY = """%YAML:1.0
# FS-VI-S80M → ORB-SLAM3 test template

File.version: "1.0"
Camera.type: "Rectified"

Camera1.fx: 182.542
Camera1.fy: 182.542
Camera1.cx: 329.164
Camera1.cy: 185.844

Stereo.b: 0.0801
Stereo.ThDepth: 40.0

IMU.T_b_c1: !!opencv-matrix
  rows: 4
  cols: 4
  dt: f
  data: [ 1.0, 0.0, 0.0, 0.0,
          0.0, 1.0, 0.0, 0.0,
          0.0, 0.0, 1.0, 0.0,
          0.0, 0.0, 0.0, 1.0 ]
IMU.NoiseGyro: 0.00017
IMU.NoiseAcc:  0.002
IMU.GyroWalk:  0.000022
IMU.AccWalk:   0.00086
IMU.Frequency: 1000.0
IMU.InsertKFsWhenLost: 1

Camera.width: 640
Camera.height: 400
Camera.fps: 25
"""
IMU_VALUES = {
    "gyroscope_noise_density": "0.0008001",
    "accelerometer_noise_density": "0.0071002",
    "gyroscope_random_walk": "0.0000082003",
    "accelerometer_random_walk": "0.000071004",
    "update_rate": "1035.5",
}


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def _make_script(path, body):
    """假厂商二进制：sh 脚本，0700。"""
    return _write(path, "#!/bin/sh\n" + body + "\n", mode=0o700)


class GuardRecorder:
    """替身锁：记录调用参数，不做真实 flock（测试不该碰 /tmp 的真实锁文件）。"""

    def __init__(self, label, calls):
        self._label = label
        self._calls = calls

    def __call__(self, target, **kwargs):
        self._calls.append((self._label, target, kwargs.get("timeout")))
        return mock.MagicMock()


class Sandbox:
    """把三个产物目录 + 两个模板 + 两把设备锁全重定向到临时目录。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ksq-calib-test-")
        self.sdk_dir = os.path.join(self.root, "fays_config")
        self.orb_dir = os.path.join(self.root, "orb_cfg")
        self.dump_dir = os.path.join(self.root, "dumps")
        self.sdk_template = _write(
            os.path.join(self.root, "sdk_template.yaml"), SDK_TEMPLATE_BODY)
        self.orb_template = _write(
            os.path.join(self.root, "orb_template.yaml"), ORB_TEMPLATE_BODY)
        self.guard_calls = []
        self.env_calls = []
        self.probe_env = dict(os.environ, KSQ_TEST_PROBE_ENV="1")

        def _env():
            self.env_calls.append(True)
            return dict(self.probe_env)

        self._patches = [
            mock.patch.object(paths, "FAYS_CONFIG_DIR", self.sdk_dir),
            mock.patch.object(paths, "ORB_DEVICE_CONFIG_DIR", self.orb_dir),
            mock.patch.object(calibration, "DUMP_OUTPUT_DIR", self.dump_dir),
            mock.patch.object(calibration, "SDK_CONFIG_DIR", self.sdk_dir),
            mock.patch.object(calibration, "ORB_OUTPUT_DIR", self.orb_dir),
            mock.patch.object(calibration, "SDK_TEMPLATE", self.sdk_template),
            mock.patch.object(calibration, "ORB_TEMPLATE", self.orb_template),
            mock.patch.object(fays_serial_probe, "DEFAULT_TEMPLATE",
                              self.sdk_template),
            mock.patch.object(calibration, "build_fays_probe_env", _env),
            mock.patch.object(calibration, "device_access_guard",
                              GuardRecorder("init", self.guard_calls)),
            mock.patch.object(calibration, "fays_device_guard",
                              GuardRecorder("device", self.guard_calls)),
        ]
        for patch in self._patches:
            patch.start()

    def ports(self, stereo="/dev/video9", imu="/dev/video11"):
        return {"stereo_dev_port": stereo, "imu_dev_port": imu}

    def dump_exporter(self, *, body=None, serial=SERIAL, name=None):
        """假 dump 二进制：把 dump 内容写到 cwd（= 厂商程序的工作目录）。"""
        name = name or f"FS-VI80-S80M_{serial}_dump_calib.yaml"
        if body is None:
            body = f"cat > {name} <<'KQS_EOF'\n{DUMP_BODY}KQS_EOF"
        return _make_script(os.path.join(self.root, "dump_calib_opencv48"), body)

    def imu_probe(self, *, values=None, extra=""):
        """假 IMU 探针：打出 imu.* 行。"""
        values = IMU_VALUES if values is None else values
        lines = "".join(
            f"printf 'imu.{key}=%s\\n' '{value}'\n"
            for key, value in values.items())
        return _make_script(
            os.path.join(self.root, "fays_vikit_calibration_probe"),
            "printf 'device.serial=%s\\n' " + f"'{SERIAL}'\n" + lines + extra)

    def close(self):
        for patch in reversed(self._patches):
            patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)


def section(title):
    print(f"\n[{title}]")


# ═══════════════════════════════════════════════════════════════════
# 1. _find_dump
# ═══════════════════════════════════════════════════════════════════

def test_find_dump(box):
    section("1. _find_dump 四种判定")
    found = {}
    for serial in (SERIAL, OTHER_SERIAL):
        _write(os.path.join(box.dump_dir,
                            f"FS-VI80-S80M_{serial}_dump_calib.yaml"), DUMP_BODY)
    got = calibration._find_dump(box.dump_dir, SERIAL)
    check(os.path.basename(got) == DUMP_NAME,
          f"三份 dump 里按序列号取对：{os.path.basename(got)}")

    empty = os.path.join(box.root, "empty")
    os.makedirs(empty, exist_ok=True)
    try:
        calibration._find_dump(empty, SERIAL)
        check(False, "空目录应报错")
    except RuntimeError as exc:
        check("未生成唯一的当前设备标定 YAML" in str(exc),
              f"空目录报可读错误：{exc}")

    mismatch = os.path.join(box.root, "mismatch")
    _write(os.path.join(mismatch,
                        f"FS-VI80-S80M_{OTHER_SERIAL}_dump_calib.yaml"), DUMP_BODY)
    try:
        calibration._find_dump(mismatch, SERIAL)
        check(False, "序列号不符应报错")
    except RuntimeError as exc:
        check("序列号不匹配" in str(exc),
              f"序列号不符单独报错（不误报成『没有』）：{exc}")

    # 文件名只含模糊子串不算命中（防 001 命中 0001）
    partial = os.path.join(box.root, "partial")
    _write(os.path.join(partial,
                        f"FS-VI80-S80M_1{SERIAL}_dump_calib.yaml"), DUMP_BODY)
    _write(os.path.join(partial,
                        f"FS-VI80-S80M_{SERIAL}_dump_calib.yaml"), DUMP_BODY)
    try:
        calibration._find_dump(partial, SERIAL)
        check(False, "两份同时含该序列号片段应报『不唯一』")
    except RuntimeError as exc:
        check("未生成唯一的当前设备标定 YAML" in str(exc),
              "含序列号片段的两份 dump → 判为不唯一，不瞎挑")
    del found


# ═══════════════════════════════════════════════════════════════════
# 2/3/4/5/6. 两个子进程
# ═══════════════════════════════════════════════════════════════════

def test_sdk_yaml(box):
    section("2. write_fays_sdk_yaml")
    target = calibration.write_fays_sdk_yaml(
        SERIAL, box.ports("/dev/video7", "/dev/video13"))
    text = open(target, encoding="utf-8").read()
    check(target == os.path.join(box.sdk_dir, f"fays_vikit_{SERIAL}.yaml"),
          f"落点符合 paths.per_device_fays_yamls 的约定：{target}")
    check("stereo_dev_port: /dev/video7" in text, "stereo 端口已写入")
    check("imu_dev_port: /dev/video13" in text, "imu 端口已写入")
    check("stereo_fps: 50" in text and "gravity: 9.7946" in text,
          "无关字段逐字节不动")
    check(oct(os.stat(target).st_mode & 0o777) == "0o600", "权限 0600")
    check(not [n for n in os.listdir(box.sdk_dir) if ".tmp." in n],
          "无残留 .tmp 文件")

    for label, bad in [("非法序列号", "../../etc/passwd"),
                       ("空序列号", "")]:
        try:
            calibration.write_fays_sdk_yaml(bad, box.ports())
            check(False, f"{label} 应被拒绝")
        except ValueError:
            check(True, f"{label} 被拒绝（不写文件）")
    for label, port in [("非法 stereo 端口", "video7"),
                        ("非法 imu 端口", "/dev/ttyUSB0")]:
        try:
            calibration.write_fays_sdk_yaml(
                SERIAL, {"stereo_dev_port": port, "imu_dev_port": "/dev/video2"})
            check(False, f"{label} 应被拒绝")
        except ValueError:
            check(True, f"{label} 被拒绝")


def test_dump_subprocess(box):
    section("3/4/5. dump_fays_calibration 子进程")
    box.guard_calls.clear()
    box.env_calls.clear()
    exporter = box.dump_exporter()
    result = calibration.dump_fays_calibration(
        SERIAL, box.ports(), dump_binary=exporter, timeout=30)
    check(result == os.path.join(box.dump_dir, DUMP_NAME),
          f"dump 已原子落到 config/calibration：{os.path.basename(result)}")
    check(open(result, encoding="utf-8").read() == DUMP_BODY,
          "dump 内容逐字节一致")
    check(oct(os.stat(result).st_mode & 0o777) == "0o600", "dump 权限 0600")

    check(box.env_calls != [], "子进程用的是 build_fays_probe_env()（不是裸 os.environ）")
    marks = {label for label, _t, _to in box.guard_calls}
    check(marks == {"init", "device"},
          f"SDK 初始化锁 + 本设备锁都取了：{sorted(marks)}")
    init = [c for c in box.guard_calls if c[0] == "init"]
    device = [c for c in box.guard_calls if c[0] == "device"]
    check(init and init[0][1] == FAYS_SDK_INITIALIZATION_LOCK,
          f"初始化锁路径正确：{init[0][1] if init else '-'}")
    check(device and device[0][1] == "/dev/video9",
          f"设备锁按本次的 stereo 节点取：{device[0][1] if device else '-'}")

    # 假二进制把探到的探针环境记号也写出来 → 证明确实传进去了
    marker = box.root + "/marker.txt"
    exporter2 = box.dump_exporter(body=(
        f"printf '%s' \"$KSQ_TEST_PROBE_ENV\" > {marker}\n"
        f"cat > {DUMP_NAME} <<'KQS_EOF'\n{DUMP_BODY}KQS_EOF"))
    calibration.dump_fays_calibration(
        SERIAL, box.ports(), dump_binary=exporter2, timeout=30)
    check(open(marker, encoding="utf-8").read() == "1",
          "子进程真实拿到了探针环境变量（OpenCV 4.2 依赖这条链）")


def test_subprocess_failures(box):
    section("6. 子进程失败路径")
    failing = box.dump_exporter(body="echo 'boom: usb stalled' >&2\nexit 3")
    try:
        calibration.dump_fays_calibration(
            SERIAL, box.ports(), dump_binary=failing, timeout=30)
        check(False, "非零退出码应报错")
    except RuntimeError as exc:
        text = str(exc)
        check("returncode=3" in text and "boom: usb stalled" in text,
              "非零退出码报错并带上厂商输出尾部")

    sleeping = box.dump_exporter(body="sleep 5")
    try:
        calibration.dump_fays_calibration(
            SERIAL, box.ports(), dump_binary=sleeping, timeout=0.4)
        check(False, "超时应报错")
    except RuntimeError as exc:
        check("超时" in str(exc), f"超时报可读错误：{exc}")

    # 导出成功但没写出任何 dump → 走 _find_dump 的『未生成』分支
    silent = box.dump_exporter(body="exit 0")
    try:
        calibration.dump_fays_calibration(
            SERIAL, box.ports(), dump_binary=silent, timeout=30)
        check(False, "没产出文件应报错")
    except RuntimeError as exc:
        check("未生成唯一" in str(exc), "厂商程序没写文件 → 报『未生成』")

    missing = os.path.join(box.root, "not_there")
    try:
        calibration.dump_fays_calibration(
            SERIAL, box.ports(), dump_binary=missing, timeout=30)
        check(False, "二进制不存在应报错")
    except RuntimeError as exc:
        check("不存在" in str(exc), f"二进制缺失报可读错误：{exc}")


def test_imu_probe(box):
    section("7/8. IMU 探针与解析")
    box.guard_calls.clear()
    probe = box.imu_probe()
    output = calibration.run_imu_probe(
        box.ports("/dev/video7", "/dev/video13"), probe_binary=probe, timeout=30)
    check("imu.update_rate=1035.5" in output, "探针原始输出被完整带回")
    parsed = calibration._parse_imu_probe(output)
    check(parsed["imu_hz"] == 1035.5, "imu_hz → update_rate")
    check(parsed["noise_gyro"] == float(IMU_VALUES["gyroscope_noise_density"]),
          "noise_gyro → gyroscope_noise_density")
    check(parsed["walk_acc"] == float(IMU_VALUES["accelerometer_random_walk"]),
          "walk_acc → accelerometer_random_walk")
    device = [c for c in box.guard_calls if c[0] == "device"]
    check(device and device[0][1] == "/dev/video7",
          f"IMU 探针也按 stereo 节点取设备锁：{device[0][1] if device else '-'}")

    # run_imu_probe 只管跑并带回原始输出；字段解析在 _parse_imu_probe。
    # 两处的失败都要能拦住生成，所以两条路径都测。
    for label, bad in [
            ("缺字段", {k: v for k, v in IMU_VALUES.items()
                        if k != "update_rate"}),
            ("非正值", dict(IMU_VALUES, gyroscope_noise_density="0"))]:
        broken = box.imu_probe(values=bad)
        raw = calibration.run_imu_probe(box.ports(), probe_binary=broken,
                                        timeout=30)
        try:
            calibration._parse_imu_probe(raw)
            check(False, f"IMU 探针{label}应报错")
        except RuntimeError as exc:
            check(True, f"IMU 探针{label}被拒：{exc}")
        # 端到端：同一个坏探针喂给整条生成链，也必须失败
        with mock.patch.object(calibration, "IMU_PROBE_BINARY", broken), \
                mock.patch.object(calibration, "DUMP_BINARY",
                                  box.dump_exporter()):
            try:
                calibration.generate_fays_calibration(SERIAL, box.ports(),
                                                      timeout=30)
                check(False, f"坏 IMU 探针（{label}）不该生成出可用标定")
            except RuntimeError:
                check(True, f"坏 IMU 探针（{label}）在整链上被拒")
    failing = _make_script(os.path.join(box.root, "imu_fail"), "exit 4")
    try:
        calibration.run_imu_probe(box.ports(), probe_binary=failing, timeout=30)
        check(False, "IMU 探针非零退出应报错")
    except RuntimeError as exc:
        check("returncode=4" in str(exc), "IMU 探针非零退出码带上返回码")


def test_parse_dump(box):
    section("7b. _parse_factory_dump")
    path = _write(os.path.join(box.root, "p.yaml"), DUMP_BODY)
    cameras = calibration._parse_factory_dump(path)
    check(cameras[0]["intrinsics"] == [LEFT_FX, 235.2, 318.7, 205.4],
          f"cam0 内参：{cameras[0]['intrinsics']}")
    check(cameras[1]["intrinsics"] == [235.1, 235.0, 326.3, 203.9],
          f"cam1 内参：{cameras[1]['intrinsics']}")
    check(cameras[0]["distortion"] == [0.031, 0.062, -0.048, 0.013],
          "cam0 畸变系数")
    check(cameras[0]["resolution"] == [640.0, 400.0], "分辨率")
    check(cameras[0]["timeshift"] == 0.001, "cam0 时间偏移")
    check(cameras[0]["T_cn_cnm1"] is None, "cam0 无 T_cn_cnm1")
    check(cameras[1]["T_cn_cnm1"][0][3] == -0.07985, "cam1 的 T_cn_cnm1 解析")
    check(len(cameras[0]["T_cam_imu"]) == 4
          and len(cameras[0]["T_cam_imu"][0]) == 4, "T_cam_imu 是 4x4")

    # 只改 cam0 的分辨率（count=1），制造"双目不一致"
    bad = _write(os.path.join(box.root, "bad.yaml"),
                 DUMP_BODY.replace("resolution: [640, 400]\n  timeshift",
                                   "resolution: [1280, 800]\n  timeshift", 1))
    try:
        calibration._parse_factory_dump(bad)
        check(False, "双目分辨率不一致应报错")
    except RuntimeError as exc:
        check("分辨率不一致" in str(exc), "双目分辨率不一致被拒")
    try:
        calibration._parse_factory_dump(_write(
            os.path.join(box.root, "nocam.yaml"), "cam0:\n  intrinsics: [1,2,3,4]\n"))
        check(False, "缺字段应报错")
    except RuntimeError:
        check(True, "dump 缺字段被拒")
    try:
        calibration._parse_factory_dump(os.path.join(box.root, "nope.yaml"))
        check(False, "文件不存在应报错")
    except RuntimeError:
        check(True, "dump 文件缺失被拒")


def test_orb_yaml(box):
    section("9. generate_orb_yaml")
    dump_path = _write(os.path.join(box.root, DUMP_NAME), DUMP_BODY)
    probe = box.imu_probe()
    output = calibration.run_imu_probe(
        box.ports(), probe_binary=probe, timeout=30)
    target = calibration.generate_orb_yaml(
        SERIAL, dump_path, output, output_dir=box.orb_dir)
    text = open(target, encoding="utf-8").read()
    check(target == os.path.join(
        box.orb_dir, f"s80m_{SERIAL}_stereo_inertial.yaml"),
        f"落点符合 paths.per_device_fays_yamls 的约定：{target}")

    def scalar(key):
        for line in text.splitlines():
            if line.startswith(key + ":"):
                return float(line.split(":", 1)[1].strip())
        return None

    baseline = scalar("Stereo.b")
    check(abs(baseline - EXPECTED_BASELINE) < 1e-12,
          f"Stereo.b = |T_cn_cnm1 平移| = {EXPECTED_BASELINE}（实得 {baseline}）")
    for key in ("Camera1.fx", "Camera1.fy", "Camera1.cx", "Camera1.cy"):
        value = scalar(key)
        check(value is not None and value > 0 and value == value,
              f"{key} 已改写成有效正数：{value}")
    check(scalar("IMU.NoiseGyro") == float(IMU_VALUES["gyroscope_noise_density"]),
          "IMU.NoiseGyro 等于探针读数")
    check(scalar("IMU.NoiseAcc") == float(IMU_VALUES["accelerometer_noise_density"]),
          "IMU.NoiseAcc 等于探针读数")
    check(scalar("IMU.GyroWalk") == float(IMU_VALUES["gyroscope_random_walk"]),
          "IMU.GyroWalk 等于探针读数")
    check(scalar("IMU.AccWalk") == float(IMU_VALUES["accelerometer_random_walk"]),
          "IMU.AccWalk 等于探针读数")
    check(scalar("IMU.Frequency") == float(IMU_VALUES["update_rate"]),
          "IMU.Frequency 等于探针读数")
    check("Camera.width: 640" in text and "Camera.height: 400" in text,
          "尺寸字段不被改写")
    check("Camera.fps: 25" in text, "模板里我们不碰的字段原样保留")
    check(f"# FS-VI80-S80M serial {SERIAL}" in text, "型号注释反映本设备的 dump")
    check("IMU.InsertKFsWhenLost: 1" in text, "T_b_c1 之后的字段没被吞掉")

    rows = [line for line in text.splitlines() if line.startswith("  data:")]
    check(len(rows) == 1, "T_b_c1 的 data 只有一块（count=1，不会重复改写）")
    values = rows[0].split("[", 1)[1].rsplit("]", 1)[0].split(",")
    numbers = [float(v) for v in values]
    check(len(numbers) == 16, f"T_b_c1 是 16 个值（实得 {len(numbers)}）")
    check(all(n == n and abs(n) != float("inf") for n in numbers), "T_b_c1 全为有限值")
    check(numbers[12:] == [0.0, 0.0, 0.0, 1.0], "T_b_c1 末行是 [0,0,0,1]")

    no_comment = _write(os.path.join(box.root, "orb_nc.yaml"),
                        ORB_TEMPLATE_BODY.replace("# FS-VI-S80M → ORB-SLAM3 test template\n", ""))
    try:
        calibration.generate_orb_yaml(SERIAL, dump_path, output,
                                      template_path=no_comment,
                                      output_dir=box.orb_dir)
        check(False, "模板缺型号注释应报错")
    except RuntimeError as exc:
        check("序列号注释" in str(exc), "模板缺 Fays 注释被拒")
    missing_key = _write(os.path.join(box.root, "orb_mk.yaml"),
                         ORB_TEMPLATE_BODY.replace("Stereo.b: 0.0801\n", ""))
    try:
        calibration.generate_orb_yaml(SERIAL, dump_path, output,
                                      template_path=missing_key,
                                      output_dir=box.orb_dir)
        check(False, "模板缺字段应报错")
    except RuntimeError as exc:
        check("缺少字段" in str(exc), "模板缺 Stereo.b 被拒")


# ═══════════════════════════════════════════════════════════════════
# 10/11. ensure_fays_calibration
# ═══════════════════════════════════════════════════════════════════

def _populate(box, serial):
    """就地把该序列号的两个产物文件造出来（走真实的生成链）。"""
    with mock.patch.object(calibration, "DUMP_BINARY",
                           box.dump_exporter(serial=serial)), \
            mock.patch.object(calibration, "IMU_PROBE_BINARY", box.imu_probe()):
        return calibration.ensure_fays_calibration(serial, box.ports())


def test_ensure(box):
    section("10/11. ensure_fays_calibration 快路径与端到端")
    # 独占一个序列号：上面的用例已经往 sandbox 里写过 SERIAL 的文件了
    serial = "3500000999990005"
    try:
        paths.per_device_fays_yamls(serial)
        check(False, "先决条件：sandbox 里此刻不该已有该序列号的标定文件")
    except RuntimeError:
        check(True, "先决条件成立：sandbox 里还没有标定文件")

    sdk_yaml, orb_yaml = _populate(box, serial)
    check(os.path.isfile(sdk_yaml) and os.path.isfile(orb_yaml),
          "端到端生成后 per_device_fays_yamls 通过（两个文件都在）")
    check(sdk_yaml.startswith(box.sdk_dir) and orb_yaml.startswith(box.orb_dir),
          "产物落在 path 约定目录内")

    with mock.patch.object(calibration, "generate_fays_calibration",
                           side_effect=AssertionError("已覆盖不该再生成")), \
            mock.patch.object(calibration, "build_fays_probe_env",
                              side_effect=AssertionError("快路径不该碰设备环境")):
        again = calibration.ensure_fays_calibration(serial, box.ports())
    check(again == (sdk_yaml, orb_yaml),
          "已覆盖时走 isfile 快路径：不生成、不碰设备、零开销")

    with mock.patch.object(calibration, "generate_fays_calibration") as forced:
        forced.return_value = {"sdk_yaml": sdk_yaml, "orb_yaml": orb_yaml}
        calibration.ensure_fays_calibration(serial, box.ports(), force=True)
    check(forced.call_count == 1, "force=True 时不走快路径，强制重新读取")

    try:
        calibration.ensure_fays_calibration(OTHER_SERIAL, box.ports())
        check(False, "没有设备时生成应失败而不是假装成功")
    except RuntimeError:
        check(True, "缺设备/二进制时 ensure 抛错，不静默返回不存在的路径")


def test_fays_single_hook(box):
    section("12. SingleFaysLease._ensure_calibration")
    lease = SingleFaysLease(logger=lambda _text: None)
    group = {"ports": box.ports()}

    sdk_yaml, orb_yaml = lease._ensure_calibration(SERIAL, group)
    check(os.path.isfile(sdk_yaml) and os.path.isfile(orb_yaml),
          "已覆盖 → 直接返回两个路径（不生成）")

    def _fake_generate(serial, ports, **_kwargs):
        """假生成：真的把该序列号的两个文件写出来（否则复核必然失败）。"""
        sdk = calibration.write_fays_sdk_yaml(serial, ports)
        orb = _write(os.path.join(
            box.orb_dir, f"s80m_{serial}_stereo_inertial.yaml"), ORB_TEMPLATE_BODY)
        return {"sdk_yaml": sdk, "orb_yaml": orb}

    with mock.patch.object(calibration, "generate_fays_calibration",
                           side_effect=_fake_generate) as gen:
        got = lease._ensure_calibration(OTHER_SERIAL, group)
        check(gen.call_count == 1,
              "缺文件 → 触发一次现场生成（用户要的『插上就能用』）")
        check(got == paths.per_device_fays_yamls(OTHER_SERIAL),
              "生成后按 paths 复核并返回该序列号的真实路径")
        check(got[0].endswith(f"fays_vikit_{OTHER_SERIAL}.yaml"),
              "返回的是这台夹爪自己的标定，不是别人的")

    with mock.patch.object(calibration, "generate_fays_calibration",
                           side_effect=RuntimeError("厂商导出程序不存在")):
        try:
            lease._ensure_calibration("3500000999990003", group)
            check(False, "生成失败应抛 GripperFaysError")
        except GripperFaysError as exc:
            text = str(exc)
            check("厂商导出程序不存在" in text, "带上底层原因")
            check("device_setup" in text, "给出上位机手工兜底办法")
            check("import_gripper_calibration.py" in text, "给出导入脚本兜底办法")
            check("3500000999990003" in text, "带上序列号")

    # 生成"成功"但产物没落在 paths 认得的路径上 → 也必须带上生成上下文，
    # 不能让 paths 那句「请先跑 device_setup」成为唯一解释
    with mock.patch.object(calibration, "generate_fays_calibration",
                           side_effect=lambda *a, **k: {"sdk_yaml": "", "orb_yaml": ""}):
        try:
            lease._ensure_calibration("3500000999990004", group)
            check(False, "产物落错位置应报错")
        except GripperFaysError as exc:
            check("3500000999990004" in str(exc),
                  "产物落错位置时报的是 GripperFaysError 且带序列号")


# ═══════════════════════════════════════════════════════════════════
# 13. 真机产物回放
# ═══════════════════════════════════════════════════════════════════

def test_real_artifacts_replay():
    section("13. 真实产物逐字节回放（native/ 缺失则跳过）")
    serial = "3500000262300098"
    dump = os.path.join(paths.NATIVE_ROOT, "config", "calibration",
                        f"FS-VI80-S80M_{serial}_dump_calib.yaml")
    orb = os.path.join(paths.ORB_DEVICE_CONFIG_DIR,
                       f"s80m_{serial}_stereo_inertial.yaml")
    sdk = os.path.join(paths.FAYS_CONFIG_DIR, f"fays_vikit_{serial}.yaml")
    if not (os.path.isfile(dump) and os.path.isfile(orb) and os.path.isfile(sdk)):
        print("  SKIP: 本机没有该序列号的真机产物")
        return

    expected = open(orb, encoding="utf-8").read()
    fake_probe = "\n".join(
        "imu.{src}={value}".format(src=src, value=_scalar_of(expected, dst))
        for src, dst in (("accelerometer_noise_density", "IMU.NoiseAcc"),
                         ("accelerometer_random_walk", "IMU.AccWalk"),
                         ("gyroscope_noise_density", "IMU.NoiseGyro"),
                         ("gyroscope_random_walk", "IMU.GyroWalk"),
                         ("update_rate", "IMU.Frequency"))) + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        got = calibration.generate_orb_yaml(
            serial, dump, fake_probe, output_dir=tmp)
        generated = open(got, encoding="utf-8").read()
    # 出处注释要单独校验：这台真机产物（098）带的是模板遗留的 1870088——
    # 它自己就是"照抄模板出处"的实例，而生成器现在必须改写成本机序列号。
    # 把这一行摘出去再比对，逐字节那条断言才仍然是"除 Camera.fps 外全等"，
    # 而不是给注释开一个能吞掉错值的口子。
    def _provenance(text):
        return [ln for ln in text.splitlines() if ln.startswith("# IMU 外参来源:")]

    def _strip_provenance(text):
        return [ln for ln in text.splitlines()
                if not ln.startswith("# IMU 外参来源:")]

    got_prov, exp_prov = _provenance(generated), _provenance(expected)
    check(len(got_prov) == 1 and len(exp_prov) == 1,
          f"两侧各有且仅有一条出处注释（生成 {got_prov} / 真机 {exp_prov}）")
    if got_prov and exp_prov:
        check(serial in got_prov[0], f"出处注释写明本机序列号：{got_prov[0]}")
        check("1870088" in exp_prov[0] and got_prov[0] != exp_prov[0],
              f"真机产物带的是模板遗留的 1870088（历史产物本次不动）：{exp_prov[0]}")
    # 顺手堵住"别的序列号漏进产物"：生成物里出现的 16 位序列号只许是这一台
    found = set(re.findall(r"\b3\d{15}\b", generated))
    check(found <= {serial}, f"生成物只提到本机序列号（出现 {sorted(found)}）")

    gen_lines, exp_lines = _strip_provenance(generated), _strip_provenance(expected)
    diffs = [
        (index + 1, left, right)
        for index, (left, right) in enumerate(zip(gen_lines, exp_lines))
        if left != right
    ]
    check(len(gen_lines) == len(exp_lines), "行数与真机产物一致")
    # 唯一允许的差异是模板漂移的 Camera.fps（模板 25 / 旧产物 30，产线两种
    # 值并存，与本次改动无关）
    unexpected = [d for d in diffs if not d[1].startswith("Camera.fps:")]
    check(not unexpected, f"逐字节复现真机 ORB YAML（差异仅 {diffs}）")
    check(all(d[1].startswith("Camera.fps:") for d in diffs),
          f"唯一差异是模板的 Camera.fps：{diffs}")

    with tempfile.TemporaryDirectory() as tmp:
        ports = _ports_of(sdk)
        got = calibration.write_fays_sdk_yaml(serial, ports, config_dir=tmp)
        check(open(got, encoding="utf-8").read() == open(sdk, encoding="utf-8").read(),
              "逐字节复现真机 SDK YAML（只换两个端口字段）")


def _scalar_of(text, key):
    for line in text.splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"真机产物里没有 {key}")


def _ports_of(sdk_yaml):
    import re
    text = open(sdk_yaml, encoding="utf-8").read()
    ports = {}
    for key in ("stereo_dev_port", "imu_dev_port"):
        ports[key] = re.search(
            rf"(?m)^\s*{key}\s*:\s*(/dev/video\d+)", text).group(1)
    return ports


# ═══════════════════════════════════════════════════════════════════
# 14. 设备面板右键菜单
# ═══════════════════════════════════════════════════════════════════

def test_device_panel_menu():
    section("14. 设备面板右键菜单")
    from PyQt5.QtWidgets import QApplication
    from config.i18n import tr
    from core.device_detector import DeviceInfo
    from ui.device_panel import DevicePanel

    app = QApplication.instance() or QApplication([])
    panel = DevicePanel()
    # group 是 kind 的派生属性（不是构造参数）
    gripper = DeviceInfo(key="gripper:3500000999990001", kind="gripper",
                         display_name="UMI", serial="3500000999990001")
    camera = DeviceInfo(key="uvc:cam0", kind="uvc",
                        display_name="CAM", serial="")
    check(gripper.group == "gripper" and camera.group == "camera",
          "先决条件：DeviceInfo.group 按 kind 派生正确")
    panel.set_devices([gripper, camera])

    for name in ("gripper_recalibration_requested",
                 "gripper_binding_requested",
                 "gripper_diagnostics_requested"):
        check(hasattr(panel, name), f"面板暴露 {name} 信号")
    # 与实现同一口径取自翻译表，语言切换下都成立（英文界面断言中文会误判）
    hint_with = tr("双击设备可重命名") + tr(
        "；右键夹爪可重读标定 / 配对序列号 / 串口诊断")
    hint_without = tr("双击设备可重命名")
    check(panel._refresh_hint.text() == hint_with,
          f"有夹爪时底部提示右键入口：{panel._refresh_hint.text()!r}")
    panel.set_devices([camera])
    check(panel._refresh_hint.text() == hint_without,
          "没有夹爪时不提右键入口")

    emitted = []
    panel.gripper_recalibration_requested.connect(
        lambda dev: emitted.append(("recalib", dev)))
    panel.gripper_binding_requested.connect(
        lambda dev: emitted.append(("binding", dev)))
    panel.gripper_diagnostics_requested.connect(
        lambda dev: emitted.append(("diagnostics", dev)))

    class _FakeAction:
        def __init__(self, text):
            self.text = text
            self.enabled = True

        def setEnabled(self, on):
            self.enabled = bool(on)

    class _FakeMenu:
        last = None
        accept = False       # True → exec_ 返回本次刚建的那一项（模拟用户点选）
        pick = 0             # 点第几项

        def __init__(self, _parent):
            _FakeMenu.last = self
            self.actions = []

        def addAction(self, text):
            action = _FakeAction(text)
            self.actions.append(action)
            return action

        def exec_(self, _pos):
            # 必须回传自己 addAction 造的那个对象（实现按 `chosen is action` 判等）
            if not _FakeMenu.accept or not self.actions:
                return None
            return self.actions[min(_FakeMenu.pick, len(self.actions) - 1)]

    # 菜单项顺序是契约的一部分：重读标定必须留在第一项
    menu_texts = [tr("重新读取出厂标定"), tr("读取/写入 Fays 绑定序列号"),
                  tr("ESP32 串口诊断")]
    menu_actions = ["recalib", "binding", "diagnostics"]

    panel.set_devices([gripper, camera])
    gpos = panel._tree.visualItemRect(panel._items[gripper.key]).center()
    cpos = panel._tree.visualItemRect(panel._items[camera.key]).center()

    with mock.patch("ui.device_panel.QMenu", _FakeMenu):
        _FakeMenu.accept = False
        _FakeMenu.last = None
        panel._on_context_menu(cpos)
        check(_FakeMenu.last is None, "相机行右键不弹夹爪菜单（无菜单对象）")
        check(emitted == [], "相机行不发夹爪信号")

        _FakeMenu.last = None
        panel._on_context_menu(gpos)
        menu = _FakeMenu.last
        check(menu is not None and len(menu.actions) == len(menu_texts),
              f"夹爪行右键弹出菜单，共 {len(menu_texts)} 项")
        check([a.text for a in menu.actions] == menu_texts,
              f"菜单文案与顺序：{[a.text for a in menu.actions]!r}")
        check(emitted == [], "只弹菜单不选 → 不发信号")

        # 每一项都要能选中，且发的是它自己那条信号
        for index, (want, text) in enumerate(zip(menu_actions, menu_texts)):
            emitted.clear()
            _FakeMenu.accept = True
            _FakeMenu.pick = index
            panel._on_context_menu(gpos)
            check(emitted and emitted[-1][0] == want
                  and emitted[-1][1].key == gripper.key,
                  f"选中「{text}」→ 发 {want} 信号带该夹爪：{emitted}")

        panel.set_locked(True)
        before = len(emitted)
        for index in range(len(menu_texts)):
            _FakeMenu.pick = index
            panel._on_context_menu(gpos)
        check(len(emitted) == before, "录制中三项都不触发（设备要独占）")
        panel.set_locked(False)

    panel.deleteLater()
    del app


def test_main_window_reopen_bookkeeping():
    section("15. 主窗口标定完成后的开回逻辑")
    import ui.main_window as main_window

    # MainWindow.__new__ 绕过 Qt 基类初始化，不能当 QMessageBox 的父窗口，
    # 所以弹窗整体换成替身（本用例只关心开回与日志，不关心弹窗长什么样）
    boxes = []

    class _FakeBox:
        @staticmethod
        def critical(_parent, title, text):
            boxes.append(("critical", title, text))

    window = main_window.MainWindow.__new__(main_window.MainWindow)
    logs = []
    window._log = logs.append
    window._gripper_recalib_reopen = {"g:1"}
    reopened = []
    window._on_device_toggled = lambda dev, on: reopened.append((dev.key, on))
    window._device_panel = mock.MagicMock()
    fake_dev = mock.MagicMock(key="g:1")
    window._device_panel.device_for_key.return_value = fake_dev

    with mock.patch.object(main_window, "QMessageBox", _FakeBox):
        window._on_gripper_recalibration_done("g:1", "UMI", "")
        check(reopened == [("g:1", True)], "成功且原先开着 → 自动开回来")
        check(not window._gripper_recalib_reopen, "开回后清掉待开回记录")
        check(any("已更新" in line for line in logs), f"记一行成功日志：{logs}")
        check(boxes == [], "成功不弹窗")

        reopened.clear()
        window._gripper_recalib_reopen = {"g:1"}
        window._on_gripper_recalibration_done("g:1", "UMI", "设备被占用")
        check(reopened == [], "失败不自动开回（避免二次弹错）")
        check(not window._gripper_recalib_reopen, "失败也清掉待开回记录")
        check(any("设备被占用" in line for line in logs), "失败写日志带上原因")
        check(len(boxes) == 1 and "设备被占用" in boxes[0][2],
              "失败弹窗带上底层原因")

        reopened.clear()
        window._gripper_recalib_reopen = set()
        window._on_gripper_recalibration_done("g:2", "UMI2", "")
        check(reopened == [], "本来就没开 → 不擅自打开")


def main():
    box = Sandbox()
    try:
        test_find_dump(box)
        test_sdk_yaml(box)
        test_dump_subprocess(box)
        test_subprocess_failures(box)
        test_imu_probe(box)
        test_parse_dump(box)
        test_orb_yaml(box)
        test_ensure(box)
        test_fays_single_hook(box)
    finally:
        box.close()
    test_real_artifacts_replay()
    test_device_panel_menu()
    test_main_window_reopen_bookkeeping()

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败:")
        for item in FAILS:
            print(f"   - {item}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
