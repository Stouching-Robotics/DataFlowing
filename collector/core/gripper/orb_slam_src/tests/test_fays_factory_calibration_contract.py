#!/usr/bin/env python3
"""Production S80M factory-calibration wiring contracts."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]        # core/gripper/orb_slam_src/
# 部署位置在 native/（paths.py 的 ORB_LIBRARY）；本树只放源码与构建配方。
NATIVE = ROOT.parent / "native"
# 上位机那侧的源码（gripper_version1/、scripts/）只在 online/ 里、不入库。本树里
# 没有它们，所以纯 clone 上整模块跳过而不是报错 —— 迁过来的是**读 ORB 源码**那
# 部分契约，上位机部分只是同模块里的邻居。
LEGACY = ROOT.parents[2] / "online"
if not (LEGACY / "gripper_version1").is_dir():
    raise unittest.SkipTest("需要 online/ 的夹爪上位机源码（该目录不入库）")
sys.path.insert(0, str(LEGACY / "gripper_version1"))

import fays_runtime  # noqa: E402


SERIAL = "3500000261870088"
SN198_SERIAL = "3500000261980088"


class FaysFactoryCalibrationContractTests(unittest.TestCase):
    def test_upper_runtime_selects_the_sn187_artifacts(self):
        self.assertEqual(
            Path(fays_runtime.FAYS_ORB_BINARY),
            Path(fays_runtime.FAYS_MARK_ONLY_BINARY),
        )
        self.assertEqual(
            Path(fays_runtime.FAYS_ORB_YAML).name,
            f"s80m_{SERIAL}_stereo_inertial.yaml",
        )
        self.assertEqual(
            Path(fays_runtime.ORB_LIBRARY),
            NATIVE / "dist/orb_mark_only/lib/libORB_SLAM3.so",
        )

    def test_bridge_reads_and_guards_sdk_factory_calibration(self):
        source = (
            ROOT / "ORB-SLAM/Examples/fays/fayssense_orb_slam.cc"
        ).read_text(encoding="utf-8")
        for required in (
            "FAYS_VIK_GetDeviceInfo",
            "FAYS_VIK_GetCalibrationParam",
            "configureSdkFactoryCalibration(",
            "cv::fisheye::stereoRectify",
            "serial mismatch",
            "imu.timestamp * 1e-9 - g_cam_imu_timeshift_s",
        ):
            self.assertIn(required, source)

    def test_sn187_yaml_contains_factory_imu_model(self):
        config = (
            ROOT / f"dist/fays_opencv48/s80m_{SERIAL}_stereo_inertial.yaml"
        ).read_text(encoding="utf-8")
        for required in (
            "IMU.NoiseGyro: 0.000806373747996",
            "IMU.NoiseAcc: 0.007187583066766",
            "IMU.GyroWalk: 0.000008271973275",
            "IMU.AccWalk: 0.000071624945534",
            "IMU.Frequency: 1035.0",
        ):
            self.assertIn(required, config)

    def test_build_target_has_the_same_serial_guard(self):
        cmake = (ROOT / "dist/fays_opencv48/CMakeLists.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("fayssense_orb_slam_sn187_opencv48", cmake)
        self.assertIn(f'KSQ_FAYS_CALIBRATION_SERIAL=\\"{SERIAL}\\"', cmake)

    def test_every_startup_uses_a_fresh_official_sdk_camera_dump(self):
        source = (
            ROOT / "ORB-SLAM/Examples/fays/fayssense_orb_slam.cc"
        ).read_text(encoding="utf-8")
        cmake = (ROOT / "dist/fays_opencv48/CMakeLists.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("FAYS_VIK_DumpCalib", source)
        self.assertIn('camera_source = "sdk_factory_dump_live"', source)
        self.assertIn("parseDumpCalibration", source)
        self.assertIn("findLiveDump", source)
        self.assertIn("T_cam_imu.inv() * T_r1_u1.inv()", source)
        self.assertIn("ORB-SLAM3's IMU.T_b_c1 is Tbc", source)
        self.assertNotIn("configureSn198DumpCalibration", source)
        self.assertNotIn("KSQ_FAYS_SN198_DUMP_FALLBACK", cmake)
        for factory_value in (
            "231.012939453125",
            "-0.079930186271667",
            "-0.003753688884899",
        ):
            self.assertNotIn(factory_value, source)

    def test_orb_is_constructed_only_after_runtime_yaml_is_generated(self):
        source = (
            ROOT / "ORB-SLAM/Examples/fays/fayssense_orb_slam.cc"
        ).read_text(encoding="utf-8")
        calibration_read = source.rindex("configureSdkFactoryCalibration(")
        runtime_write = source.index("writeRuntimeOrbYaml(oc", calibration_read)
        system_construct = source.index("ORB_SLAM3::System SLAM", runtime_write)
        self.assertLess(calibration_read, runtime_write)
        self.assertLess(runtime_write, system_construct)
        self.assertIn("vp, runtime_orb_yaml", source)
        self.assertIn("orb_runtime_factory_", source)
        self.assertIn('text += ".0"', source)
        self.assertIn("replace_integer", source)
        for required in (
            "Camera1.fx",
            "Stereo.b",
            "IMU.T_b_c1",
            "IMU.NoiseGyro",
            "IMU.NoiseAcc",
            "IMU.GyroWalk",
            "IMU.AccWalk",
            "IMU.Frequency",
        ):
            self.assertIn(required, source)

    def test_sn198_yaml_contains_direct_sdk_factory_imu_model(self):
        config = (
            ROOT
            / "dist/fays_opencv48"
            / f"s80m_{SN198_SERIAL}_stereo_inertial.yaml"
        ).read_text(encoding="utf-8")
        for required in (
            "IMU.NoiseGyro: 0.000806373747996",
            "IMU.NoiseAcc: 0.007187583066766",
            "IMU.GyroWalk: 0.000008271973275",
            "IMU.AccWalk: 0.000071624945534",
            "IMU.Frequency: 1035.0",
        ):
            self.assertIn(required, config)

        source = (
            ROOT / "ORB-SLAM/Examples/fays/fayssense_orb_slam.cc"
        ).read_text(encoding="utf-8")
        for required in (
            "runtime->noise_acc = imu.accelerometer_noise_density",
            "runtime->walk_acc = imu.accelerometer_random_walk",
            "runtime->noise_gyro = imu.gyroscope_noise_density",
            "runtime->walk_gyro = imu.gyroscope_random_walk",
            "runtime->imu_hz = imu.update_rate",
        ):
            self.assertIn(required, source)

    def test_calibration_probe_reads_camera_and_imu_sections(self):
        source = (
            ROOT / "ORB-SLAM/Examples/fays/fays_vikit_calibration_probe.cpp"
        ).read_text(encoding="utf-8")
        for required in (
            "FAYS_VIK_GetDeviceInfo",
            "FAYS_VIK_GetCalibrationParam",
            "calibration.cameras.num_of_cams",
            "camera.available_mask",
            "intrinsics.distortion",
            "camera.T_cn_cnm1",
            "camera.T_cn_imu",
            "camera.timeshift_cam_imu",
            "calibration.imu",
        ):
            self.assertIn(required, source)


if __name__ == "__main__":
    unittest.main()
