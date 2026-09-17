#!/usr/bin/env python3
"""Contracts for the OpenCV 4.8 port of the historical Fays ORB behavior."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERIALS = ("3500000261870088", "3500000261980088")


class FaysHistoricalOrbContractTests(unittest.TestCase):
    def test_both_serial_profiles_use_historical_feature_settings(self):
        for serial in SERIALS:
            with self.subTest(serial=serial):
                config = (
                    ROOT
                    / "dist/fays_opencv48"
                    / f"s80m_{serial}_stereo_inertial.yaml"
                ).read_text(encoding="utf-8")
                for required in (
                    "ORBextractor.nFeatures: 1500",
                    "ORBextractor.scaleFactor: 1.1",
                    "ORBextractor.nLevels: 8",
                    "ORBextractor.iniThFAST: 20",
                    "ORBextractor.minThFAST: 7",
                ):
                    self.assertIn(required, config)

    def test_local_mapping_matches_historical_inertial_policy(self):
        source = (ROOT / "ORB-SLAM/src/LocalMapping.cc").read_text(
            encoding="utf-8"
        )
        for required in (
            "t_now - mLastPeriodicBA > 240.0",
            "redundant_th = 0.3",
            "int nObs = 2",
            "mpCurrentKeyFrame->GetMap()->SetIniertialBA1();",
            "mpCurrentKeyFrame->GetMap()->SetIniertialBA2();",
            "[VIBA-OFF] matched historical Fays recorder baseline",
        ):
            self.assertIn(required, source)

    def test_tracking_restores_historical_imu_fill_guard(self):
        source = (ROOT / "ORB-SLAM/src/Tracking.cc").read_text(
            encoding="utf-8"
        )
        for required in (
            "const Sophus::SE3f T_before = mCurrentFrame.GetPose();",
            "trans_dist > 0.05f",
            "rot_angle > 1.19f",
            "mCurrentFrame.SetPose(T_before);",
            "[IMU-FILL] visual discarded",
        ):
            self.assertIn(required, source)


if __name__ == "__main__":
    unittest.main()
