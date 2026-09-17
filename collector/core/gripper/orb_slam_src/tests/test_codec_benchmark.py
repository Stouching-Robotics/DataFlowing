"""Metric correctness only: no codecs, GPU, SLAM or hardware are started."""
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
ROOT = Path(__file__).resolve().parents[1]          # core/gripper/orb_slam_src/
sys.path.insert(0, str(ROOT / 'ORB-SLAM/Examples/fays/offline'))
# offline/codec_benchmark.py 里写死了 PROJECT=parents[4] 再 import 上位机的
# `runtime.cpu_policy`；那套上位机源码（gripper_version1/runtime/）只在 online/ 里、
# 不入库，新树里 parents[4] 落在 orb_slam_src/ 上，没有 gripper_version1。本测试
# 验的是**度量函数本身**，不碰上位机；所以 online 在就借它的 runtime，不在就跳过。
LEGACY = ROOT.parents[2] / "online"
if (LEGACY / "gripper_version1").is_dir():
    sys.path.insert(0, str(LEGACY / "gripper_version1"))
try:
    from codec_benchmark import alignment, compare_pixels, replay_metrics, W, H
except ModuleNotFoundError as exc:      # 纯 clone：没有上位机 runtime 包
    raise unittest.SkipTest(
        f"codec_benchmark.py 需要 online/ 的上位机 runtime 包（未入库）: {exc}")


class CodecMetricTests(unittest.TestCase):
    def test_sim3_scale_direction_and_translation(self):
        truth = np.array([[0, 0, 0], [1, 0, 0], [1, 2, 0], [0, 1, 3]], dtype=float)
        est = truth * 2 + np.array([5, -3, 2])
        result = alignment(est, truth, True)
        self.assertAlmostEqual(result['scale'], .5)
        self.assertLess(result['APE_Max'], 1e-12)
        self.assertGreater(alignment(est, truth, False)['APE_RMSE'], .5)

    def test_rotated_trajectory(self):
        truth = np.random.default_rng(7).normal(size=(30, 3))
        rot = np.array([[0., -1, 0], [1, 0, 0], [0, 0, 1]])
        self.assertLess(alignment(truth @ rot.T + 3, truth, True)['APE_RMSE'], 1e-12)

    def test_constant_trajectory_cannot_define_scale(self):
        with self.assertRaises(ValueError):
            alignment(np.zeros((3, 3)), np.zeros((3, 3)), True)

    def test_exact_and_known_pixel_difference(self):
        with tempfile.TemporaryDirectory(dir='/var/tmp') as tmp:
            a, b = Path(tmp) / 'a', Path(tmp) / 'b'
            a.write_bytes(bytes([100]) * (W * H))
            b.write_bytes(bytes([101]) * (W * H))
            result = compare_pixels(a, b, 1)
            self.assertEqual(result['unequal_frames'], 1)
            self.assertEqual(result['unequal_pixel_ratio'], 1.)
            self.assertEqual(result['max_abs_difference'], 1)
            self.assertEqual(result['mae'], 1)
            self.assertAlmostEqual(result['psnr_db'], 48.1308036086791)
            same = compare_pixels(a, a, 1)
            self.assertTrue(same['exact_equal'])
            self.assertTrue(same['psnr_infinite'])

    def test_full_replay_is_separate_from_shutdown_success(self):
        with tempfile.TemporaryDirectory(dir='/var/tmp') as tmp:
            root = Path(tmp)
            (root / 'native.log').write_text('REPLAY_FINISHED ok=2 total=2 injected_frames=0\nShutdown\n')
            (root / 'frames.csv').write_text('index,timestamp_ns,state,max_rss_kib\n0,100,2,1024\n1,200,2,2048\n')
            result = replay_metrics(root, 2)
            self.assertTrue(result['replay_complete'])
            self.assertEqual(result['tracking_ok_frames'], 2)
            self.assertNotIn('slam_success', result)
            self.assertFalse(replay_metrics(root, 3)['replay_complete'])
            (root / 'native.log').write_text('Shutdown\n')
            self.assertFalse(replay_metrics(root, 2)['replay_complete'])

    def test_frame_count_mismatch_rejected(self):
        with tempfile.TemporaryDirectory(dir='/var/tmp') as tmp:
            a = Path(tmp) / 'a'
            a.write_bytes(b'x')
            with self.assertRaises(ValueError):
                compare_pixels(a, a, 1)


if __name__ == '__main__':
    unittest.main()
