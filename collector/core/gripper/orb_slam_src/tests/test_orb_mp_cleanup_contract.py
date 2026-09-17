#!/usr/bin/env python3
"""Static safety contract for threshold/periodic inactive-MP cleanup."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OrbMapPointCleanupContractTest(unittest.TestCase):
    def test_cleanup_keeps_map_and_raw_pointer_shells(self):
        mp_source = (ROOT / "ORB-SLAM/src/MapPoint.cc").read_text()
        kf_source = (ROOT / "ORB-SLAM/src/KeyFrame.cc").read_text()
        cleanup = (ROOT / "ORB-SLAM/include/MapPointCleanup.h").read_text()

        self.assertIn("CompactBadMapPointPayloads", cleanup)
        self.assertIn("mDescriptor.release();", mp_source)
        self.assertIn("if(!mbBad)", mp_source)
        self.assertNotIn("CompactBadKeyFramePayloads", cleanup)
        self.assertNotIn("QueueBadKeyFramePayload", kf_source)
        self.assertNotIn("KeyFrame::CompactBadPayload", kf_source)
        self.assertNotIn("swap(mvpMapPoints)", kf_source)
        self.assertNotIn("delete candidates[i]", mp_source)
        self.assertNotIn("delete candidates[i]", kf_source)
        self.assertNotIn("ResetActiveMap", mp_source)
        self.assertNotIn("ResetActiveMap", kf_source)

    def test_bridge_uses_1000_high_water_with_periodic_fallback(self):
        source = (
            ROOT / "ORB-SLAM/Examples/fays/fayssense_orb_slam.cc"
        ).read_text()

        self.assertIn("kInactiveCleanupHighWater = 1000", source)
        self.assertIn("kInactiveCleanupBatch = 1000", source)
        self.assertIn("GetQueuedBadMapPointPayloadCount()", source)
        self.assertIn("queued_bad_mp >= kInactiveCleanupHighWater", source)
        self.assertNotIn("GetQueuedBadKeyFramePayloadCount()", source)
        self.assertNotIn("CompactBadKeyFramePayloads", source)
        self.assertIn("disabled_raw_pointer_safety", source)
        self.assertIn("kMpCleanupFirstTriggerS = 240.0", source)
        self.assertIn("kMpCleanupIntervalS = 240.0", source)
        self.assertIn("kMpCleanupGraceS = 30.0", source)
        self.assertIn("next_mp_cleanup_s += kMpCleanupIntervalS", source)
        self.assertIn("mp_cleanup_running.exchange", source)
        self.assertIn('" reason=" << cleanup_reason', source)
        self.assertIn("[MP_CLEANUP]", source)

    def test_native_exception_diagnostics_identify_orb_phase(self):
        frame = (ROOT / "ORB-SLAM/src/Frame.cc").read_text()
        bridge = (
            ROOT / "ORB-SLAM/Examples/fays/fayssense_orb_slam.cc"
        ).read_text()

        self.assertIn("RunFaysOrbWorkers", frame)
        self.assertIn("[ORB_WORKER_ERROR]", frame)
        self.assertIn('"thread_start"', frame)
        self.assertIn('"extract"', frame)
        self.assertIn('"thread_join"', frame)
        self.assertIn("std::set_terminate(terminateWithDiagnostics)", bridge)
        self.assertIn("[TRACK_ERROR]", bridge)
        self.assertIn("[NATIVE_EXCEPTION]", bridge)
        self.assertIn("[NATIVE_BACKTRACE]", bridge)

    def test_runtime_map_diagnostic_is_read_only(self):
        bridge = (
            ROOT / "ORB-SLAM/Examples/fays/fayssense_orb_slam.cc"
        ).read_text()
        system = (ROOT / "ORB-SLAM/src/System.cc").read_text()
        header = (
            ROOT / "ORB-SLAM/include/RuntimeMapDiagnostics.h"
        ).read_text()

        self.assertIn("[ORB_MAP_DIAG]", bridge)
        self.assertIn("activeKf = atlas->KeyFramesInMap()", system)
        self.assertIn(
            "localMp = gRuntimeLocalMp.load", system)
        self.assertIn("GetCreatedMapPointCount()", system)
        self.assertIn("do not own", header)
        self.assertNotIn("ResetActiveMap", header)
        self.assertNotIn("delete candidates", header)


if __name__ == "__main__":
    unittest.main()
