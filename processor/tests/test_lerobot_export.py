import unittest

import numpy as np

from app.lerobot_export import _ensure_slam_pose, _ensure_observation_state
from app.umi_slam_action import poses_from_trajectory


def _sample(t: float, x: float, z: float) -> list[float]:
    """一组 slam_trajectory 样本:[t, x, y, z, qx, qy, qz, qw]。"""
    return [float(t), float(x), 0.0, float(z), 0.0, 0.0, 0.0, 1.0]


def _rows(episode: int, count: int, *, t0: float,
          slam_offset: float = 0.0) -> list[dict]:
    """造一集 rows:每帧两个 slam 样本,位姿随帧号线性推进。"""
    rows = []
    for index in range(count):
        stamp = t0 + index / 30.0
        rows.append({
            "episode_index": episode,
            "timestamp": stamp,
            "observation.slam_trajectory": [
                _sample(stamp - slam_offset, index * 0.01, index * 0.002),
                _sample(stamp - slam_offset + 0.01,
                        index * 0.01 + 0.001, index * 0.002),
            ],
        })
    return rows


class EnsureSlamPoseTests(unittest.TestCase):
    def test_derives_column_and_matches_direct_reconstruction(self):
        """整列缺失时补出 slam_pose,且值与单独调用重建函数逐值一致。"""
        rows = _rows(0, 40, t0=100.0)
        self.assertEqual(_ensure_slam_pose(rows), "observation.slam_pose")

        self.assertTrue(all(len(row["observation.slam_pose"]) == 7
                            for row in rows))
        expected = poses_from_trajectory(
            [row["observation.slam_trajectory"] for row in rows],
            np.array([row["timestamp"] for row in rows]), len(rows))
        actual = np.array([row["observation.slam_pose"] for row in rows])
        self.assertTrue(np.allclose(actual, expected, atol=1e-12))

    def test_episodes_are_fitted_independently(self):
        """时间基映射必须每集单独拟合,否则跨集求差会把原点差异当运动。

        两集的 slam 时钟相差一个很大的偏移量;若混在一起拟合,时间轴被拉
        成两段,重建值会偏离该集单独重建的结果。
        """
        first = _rows(0, 30, t0=100.0, slam_offset=0.0)
        second = _rows(1, 30, t0=500.0, slam_offset=400.0)
        rows = first + second
        self.assertEqual(_ensure_slam_pose(rows), "observation.slam_pose")

        for group, offset in ((first, 0.0), (second, 400.0)):
            expected = poses_from_trajectory(
                [row["observation.slam_trajectory"] for row in group],
                np.array([row["timestamp"] for row in group]), len(group),
            )
            actual = np.array([row["observation.slam_pose"] for row in group])
            self.assertTrue(np.allclose(actual, expected, atol=1e-12))

    def test_existing_column_is_left_untouched(self):
        """旧批两列都有 —— 重导时不得覆盖采集端原始 slam_pose。"""
        rows = _rows(0, 10, t0=100.0)
        original = [9.0] * 7
        for row in rows:
            row["observation.slam_pose"] = list(original)
        self.assertIsNone(_ensure_slam_pose(rows))
        self.assertTrue(all(row["observation.slam_pose"] == original
                            for row in rows))

    def test_skips_batches_without_trajectory(self):
        """手套 / D435 批次没有 slam_trajectory —— 不得凭空写入该列。"""
        rows = [{"episode_index": 0, "timestamp": 0.0,
                 "observation.tactile.left": [0.0] * 256}]
        self.assertIsNone(_ensure_slam_pose(rows))
        self.assertNotIn("observation.slam_pose", rows[0])

    def test_missing_timestamp_does_not_break_reconstruction(self):
        """时间戳缺帧(None)不能把 np.asarray 推断成 object 而抛异常。"""
        rows = _rows(0, 20, t0=200.0)
        rows[5]["timestamp"] = None
        self.assertEqual(_ensure_slam_pose(rows), "observation.slam_pose")
        self.assertTrue(all(np.all(np.isfinite(row["observation.slam_pose"]))
                            for row in rows))

    def test_derived_pose_feeds_observation_state(self):
        """串联顺序:补出的 slam_pose 要能继续供 state 派生使用。

        新批里 ep72/ep73 两列都缺,只补 slam_pose 而不让 state 跟上,导出
        产物仍然缺 observation.state,ACT 依旧 KeyError。
        """
        rows = _rows(0, 25, t0=300.0)
        _ensure_slam_pose(rows)
        self.assertEqual(_ensure_observation_state(rows), "observation.state")
        self.assertTrue(all(len(row["observation.state"]) == 4 for row in rows))
        # 位置分量是本帧相对本集第一帧的偏移 —— 首帧必须为原点
        self.assertTrue(np.allclose(rows[0]["observation.state"][:3], 0.0))


if __name__ == "__main__":
    unittest.main()
