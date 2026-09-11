import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.routes.video import (
    _apply_tactile_scale,
    _compute_tactile_display_range,
    _decode_tactile_matrix,
    _tactile_scale_factor,
)


class TactileDecodeTests(unittest.TestCase):
    def test_interleaved_row_diff_is_decoded_before_hwc_reshape(self):
        matrix = np.zeros((250, 250, 3), dtype=np.int16)
        matrix[12, 34] = [7, -2, 19]
        matrix[12, 35] = [8, -1, 23]
        matrix[200, 249] = [-11, 3, 31]

        rows = matrix.reshape(250, 750)
        diff = np.empty_like(rows)
        diff[:, 0] = rows[:, 0]
        diff[:, 1:] = rows[:, 1:] - rows[:, :-1]

        decoded = _decode_tactile_matrix(diff.reshape(-1).tolist())

        self.assertEqual(decoded.shape, (3, 250, 250))
        np.testing.assert_array_equal(decoded.transpose(1, 2, 0), matrix)


class TactileScaleTests(unittest.TestCase):
    """量化整数 → mN 的比例因子必须取自元数据,不能硬编码。

    新采集端按 ``scale: 100`` 存储(units 仍是 mN);旧数据无该字段且
    本身就是 mN。硬编码 100 会把旧数据错误缩小 100 倍。
    """

    def _dataset(self, root: Path, scale) -> Path:
        (root / "meta").mkdir(parents=True)
        (root / "data" / "chunk-000").mkdir(parents=True)
        feature = {"dtype": "int16", "shape": [250, 250, 3],
                   "units": ["mN", "mN", "mN"]}
        if scale is not None:
            feature["scale"] = scale
        (root / "meta" / "info.json").write_text(json.dumps({
            "features": {"observation.gripper_left_force_matrix": feature},
        }), encoding="utf-8")
        return root / "data" / "chunk-000" / "episode_000000.parquet"

    def test_reads_declared_scale(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._dataset(Path(temp), 100)
            self.assertEqual(_tactile_scale_factor(path), 100.0)

    def test_defaults_to_one_when_declared_or_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._dataset(Path(temp), None)
            self.assertEqual(_tactile_scale_factor(path), 1.0)

        with tempfile.TemporaryDirectory() as temp:
            path = self._dataset(Path(temp), 0)
            self.assertEqual(_tactile_scale_factor(path), 1.0)

    def test_missing_metadata_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data" / "chunk-000" / "episode_000000.parquet"
            path.parent.mkdir(parents=True)
            self.assertEqual(_tactile_scale_factor(path), 1.0)

    def test_old_data_is_left_untouched(self):
        """scale=1 时不得改变数值 —— 旧数据必须保持原样。"""
        matrix = np.full((3, 250, 250), 5.0, dtype=np.float32)
        scaled = _apply_tactile_scale(matrix, 1.0)
        np.testing.assert_array_equal(scaled, matrix)

    def test_new_data_is_divided(self):
        matrix = np.full((3, 250, 250), 250.0, dtype=np.float32)
        scaled = _apply_tactile_scale(matrix, 100.0)
        np.testing.assert_allclose(scaled, 2.5, atol=1e-6)

    def test_none_matrix_is_passed_through(self):
        self.assertIsNone(_apply_tactile_scale(None, 100.0))


def _write_episode(path: Path, matrices) -> Path:
    """Write an episode whose row-diff column decodes back to ``matrices``."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    flattened = []
    for matrix in matrices:
        rows = np.asarray(matrix, dtype=np.int16).reshape(250, 750)
        diff = np.empty_like(rows)
        diff[:, 0] = rows[:, 0]
        diff[:, 1:] = rows[:, 1:] - rows[:, :-1]
        flattened.append(diff.reshape(-1))
    table = pa.table({
        "frame_index": pa.array(range(len(matrices)), type=pa.int64()),
        "observation.gripper_left_force_matrix":
            pa.array(flattened, type=pa.list_(pa.int16())),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(path))
    return path


def _episode_frames(quiet=9, contact=80):
    """A quiet episode (noise only) with one frame carrying real contact."""
    rng = np.random.default_rng(20260911)
    matrices = []
    for _ in range(quiet):
        frame = np.zeros((250, 250, 3), dtype=np.int16)
        frame[:, :, 2] = rng.integers(0, 3, (250, 250))
        matrices.append(frame)
    loaded = np.zeros((250, 250, 3), dtype=np.int16)
    loaded[100:130, 100:130, 2] = contact
    matrices.append(loaded)
    return matrices


class TactileDisplayRangeTests(unittest.TestCase):
    """色阶必须取自整集,不能逐帧自比。

    没有接触的帧里唯一超过噪声门限的就是噪声本身;逐帧归一化会把这份噪声
    当成满量程,每个噪点都被拉成"接触点"。整集取色阶后安静帧保持背景色。
    """

    def _episode(self, temp: str):
        path = _write_episode(
            Path(temp) / "data" / "chunk-000" / "episode_000000.parquet",
            _episode_frames())
        return path

    def test_range_is_driven_by_the_contact_not_the_noise(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._episode(temp)
            lo, hi = _compute_tactile_display_range(
                path, "observation.gripper_left_force_matrix")
            self.assertGreaterEqual(hi, 80.0)
            self.assertLess(lo, hi)

    def test_quiet_frame_stays_background(self):
        """整集色阶下,无接触的帧几乎全黑;旧逐帧逻辑在该帧给出 174 级亮度。"""
        from app.routes.video import _tactile_range_payload

        with tempfile.TemporaryDirectory() as temp:
            path = self._episode(temp)
            payload = _tactile_range_payload(path, 0, 10, "left")
            self.assertIsNotNone(payload)
            pixels = 250 * 250
            blocks = np.frombuffer(payload[:10 * pixels], dtype=np.uint8)
            quiet = blocks[:9 * pixels].reshape(9, pixels)
            loud = blocks[9 * pixels:].reshape(pixels)
            self.assertLess(float((quiet >= 128).mean()), 0.01)
            self.assertGreater(int(loud.max()), 150)

    def test_flat_episode_is_drawable(self):
        with tempfile.TemporaryDirectory() as temp:
            blank = np.zeros((250, 250, 3), dtype=np.int16)
            path = _write_episode(
                Path(temp) / "data" / "chunk-000" / "episode_000000.parquet",
                [blank, blank])
            lo, hi = _compute_tactile_display_range(
                path, "observation.gripper_left_force_matrix")
            self.assertLess(lo, hi)


if __name__ == "__main__":
    unittest.main()
