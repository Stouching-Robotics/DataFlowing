import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.lerobot_export import (_ensure_slam_pose, _ensure_observation_state,
                                _force_matrix_semantics, _write_stats)
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


class ForceMatrixSemanticsTests(unittest.TestCase):
    """源 info.json → 导出 features 的力矩阵语义元数据搬运。

    这些字段(scale/encoding)决定数值怎么还原:力矩阵存的是「放大 scale 倍
    再取行内差分」的整数,少任何一个,产物里的力矩阵都无法解读。它们只写在
    info.json 里,parquet 本身不带,所以导出必须显式搬运。
    """

    def _root(self, features: dict) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "meta").mkdir(parents=True)
        (root / "meta" / "info.json").write_text(
            json.dumps({"features": features}), encoding="utf-8")
        return root

    def test_carries_scale_and_encoding(self):
        root = self._root({
            "observation.gripper_left_force_matrix": {
                "dtype": "int16", "shape": [250, 250, 3],
                "encoding": "row_diff_quantized", "scale": 100,
                "names": ["fx", "fy", "fz"], "units": ["mN", "mN", "mN"],
            },
        })
        self.assertEqual(_force_matrix_semantics(root), {
            "observation.gripper_left_force_matrix": {
                "encoding": "row_diff_quantized", "scale": 100,
                "names": ["fx", "fy", "fz"], "units": ["mN", "mN", "mN"],
            },
        })

    def test_does_not_carry_shape_or_dtype(self):
        """shape/dtype 由导出自己按实际写入值声明,不能被源覆盖。

        源声明 shape [250,250,3] 是逻辑形状,而 parquet 里存的是展平的
        187500 长列表;照搬声明会让官方加载器按 3 维 reshape 而失败。
        """
        root = self._root({
            "observation.gripper_left_force_matrix": {
                "dtype": "int16", "shape": [250, 250, 3], "scale": 100,
            },
        })
        carried = _force_matrix_semantics(root)["observation.gripper_left_force_matrix"]
        self.assertNotIn("shape", carried)
        self.assertNotIn("dtype", carried)

    def test_ns_columns_keep_their_encoding(self):
        """_ns 采集时刻列同样靠 encoding 声明时基,int64 裸列无法解读。"""
        root = self._root({
            "observation.gripper_left_force_matrix_ns": {
                "dtype": "int64", "encoding": "host_monotonic_ns",
            },
        })
        self.assertEqual(
            _force_matrix_semantics(root)["observation.gripper_left_force_matrix_ns"],
            {"encoding": "host_monotonic_ns"})

    def test_ignores_unrelated_features(self):
        root = self._root({
            "observation.gripper_state": {"dtype": "float32", "scale": 100},
            "action": {"dtype": "float32"},
        })
        self.assertEqual(_force_matrix_semantics(root), {})

    def test_missing_or_broken_source_degrades_to_empty(self):
        """源元数据读不到时返回空字典 —— 导出照常进行,不因缺元数据中断。"""
        self.assertEqual(_force_matrix_semantics(Path("/nonexistent/nope")), {})
        broken = Path(tempfile.mkdtemp())
        (broken / "meta").mkdir()
        (broken / "meta" / "info.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(_force_matrix_semantics(broken), {})


class WriteStatsDataSourceTests(unittest.TestCase):
    """stats.json 的两种取数路径必须产出同一份统计量。

    ``_write_stats`` 原先一律把整个 data/ 目录从磁盘重读一遍;调用方手里
    其实就有刚写完的 arrow table,传进去可省掉这次全量往返。但两条路径的
    DataFrame 必须逐列同 dtype、同值 —— 否则归一化统计会悄悄漂移,而训练
    侧不会报错,只会训出偏差。
    """

    def _build(self, root: Path):
        """造一份含标量列/定长向量列/字符串列的 data/,返回写入的 table。"""
        import pyarrow as pa
        import pyarrow.parquet as pq

        rows = [
            {"frame_index": i,
             "timestamp": i / 30.0,                    # 标量浮点
             "observation.state": float(i % 3),        # 标量浮点
             "action": [float(i), float(i) * 2, -1.0],  # 定长向量(走逐维 stats)
             "gesture": "open" if i % 2 else ""}       # 字符串(应被跳过)
            for i in range(20)
        ]
        table = pa.Table.from_pylist(rows)
        data_dir = root / "data" / "chunk-000"
        data_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, data_dir / "file-000.parquet")
        return table

    def test_table_path_matches_disk_path(self):
        disk_root = Path(tempfile.mkdtemp())
        table_root = Path(tempfile.mkdtemp())
        table = self._build(disk_root)
        self._build(table_root)

        _write_stats(disk_root)                    # 旧路径:从磁盘重读
        _write_stats(table_root, None, table)      # 新路径:直接用写入的表

        from_disk = json.loads((disk_root / "meta" / "stats.json").read_text())
        from_table = json.loads((table_root / "meta" / "stats.json").read_text())
        self.assertEqual(from_disk, from_table)
        # 别退化成"两边都空"的假通过
        self.assertIn("observation.state", from_disk)
        self.assertIn("action", from_disk)

    def test_string_columns_are_not_given_numeric_stats(self):
        """官方加载器会把 stats 每一项转张量,字符串列必须跳过。"""
        root = Path(tempfile.mkdtemp())
        table = self._build(root)
        _write_stats(root, None, table)
        stats = json.loads((root / "meta" / "stats.json").read_text())
        self.assertNotIn("gesture", stats)

    def test_missing_data_dir_degrades_quietly(self):
        """没有 data/ 时直接返回,不抛异常(旧路径原有的容错)。"""
        root = Path(tempfile.mkdtemp())
        _write_stats(root)
        self.assertFalse((root / "meta" / "stats.json").exists())


if __name__ == "__main__":
    unittest.main()
