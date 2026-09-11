"""内容指纹:识别"换个包名重传同一份数据"。

实测背景:采集端把已上传的录制重新推一遍时包名会变(本地去重标记丢失
或包名规则变化),``session.py`` 的 incoming_name 判断不出来,同一份数据
被追加成新 episode(UMIGripper_AI 出现 4 组字节级相同的批次)。这里的
指纹必须与包名无关,且对"同一次录制的两份拷贝"稳定命中。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from app import batch_fingerprint


def _write_episode(root: Path, episode_index: int, video_bytes: bytes,
                   frames: int = 4) -> None:
    """写一个最小 canonical 布局:一个视频 + 一个 data parquet。"""
    videos = root / "videos" / "observation.images.gripper_rgb" / "chunk-000"
    videos.mkdir(parents=True, exist_ok=True)
    (videos / f"episode_{episode_index:06d}.mp4").write_bytes(video_bytes)

    data = root / "data" / "chunk-000"
    data.mkdir(parents=True, exist_ok=True)
    table = pa.table({
        "episode_index": pa.array([episode_index] * frames, pa.int64()),
        "frame_index": pa.array(list(range(frames)), pa.int64()),
        "observation.value": pa.array(
            [[float(i), float(i) + 1.0] for i in range(frames)],
            pa.list_(pa.float32()),
        ),
    })
    pq.write_table(table, data / f"episode_{episode_index:06d}.parquet")


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # 索引落到临时目录,别碰真实 state/
        self._index_patch = mock.patch.object(
            batch_fingerprint, "INDEX_PATH", self.root / "fingerprints.json")
        self._index_patch.start()

    def tearDown(self):
        self._index_patch.stop()
        self._tmp.cleanup()

    def test_same_recording_different_episode_index_matches(self):
        """同一次录制的两份拷贝,只有 episode_index 不同 → 指纹相同。"""
        a = self.root / "a"
        b = self.root / "b"
        _write_episode(a, 3, b"IDENTICAL-VIDEO-BYTES")
        _write_episode(b, 4, b"IDENTICAL-VIDEO-BYTES")

        self.assertIsNotNone(batch_fingerprint.compute_fingerprint(a))
        self.assertEqual(
            batch_fingerprint.compute_fingerprint(a),
            batch_fingerprint.compute_fingerprint(b),
        )

    def test_different_video_differs(self):
        a = self.root / "a"
        b = self.root / "b"
        _write_episode(a, 0, b"VIDEO-ONE")
        _write_episode(b, 0, b"VIDEO-TWO")
        self.assertNotEqual(
            batch_fingerprint.compute_fingerprint(a),
            batch_fingerprint.compute_fingerprint(b),
        )

    def test_different_frame_data_differs(self):
        """视频相同但帧数据不同 → 不同指纹(不能只比视频)。"""
        a = self.root / "a"
        b = self.root / "b"
        _write_episode(a, 0, b"SAME-VIDEO")
        _write_episode(b, 0, b"SAME-VIDEO")
        # 只改 b 的一帧数据
        path = b / "data" / "chunk-000" / "episode_000000.parquet"
        table = pq.read_table(path).to_pydict()
        table["observation.value"] = [[9.0, 9.0] for _ in table["frame_index"]]
        pq.write_table(pa.table(table), path)
        self.assertNotEqual(
            batch_fingerprint.compute_fingerprint(a),
            batch_fingerprint.compute_fingerprint(b),
        )

    def test_missing_data_returns_none(self):
        """没有 parquet 就算不出指纹 → None(调用方按新批次放行)。"""
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIsNone(batch_fingerprint.compute_fingerprint(empty))

    def test_record_and_lookup_roundtrip(self):
        fingerprint = "v:test=abc;d:def"
        batch_fingerprint.record_fingerprint("UMIGripper_AI", fingerprint, "ep_000003")
        self.assertEqual(
            batch_fingerprint.lookup_duplicate(
                "UMIGripper_AI", fingerprint, {"ep_000003"}),
            "ep_000003",
        )

    def test_lookup_ignores_deleted_episode(self):
        """索引指向的 episode 已删除 → 不判重(允许重新上传)。"""
        fingerprint = "v:test=abc;d:def"
        batch_fingerprint.record_fingerprint("UMIGripper_AI", fingerprint, "ep_000003")
        self.assertIsNone(
            batch_fingerprint.lookup_duplicate(
                "UMIGripper_AI", fingerprint, {"ep_000004"}),
        )

    def test_lookup_none_fingerprint_is_safe(self):
        self.assertIsNone(
            batch_fingerprint.lookup_duplicate("p", None, {"a"}))

    def test_record_drops_stale_entry_for_same_episode(self):
        """同名重传换了内容:旧指纹不该继续指向这个 episode。"""
        batch_fingerprint.record_fingerprint("p", "old-content", "ep_1")
        batch_fingerprint.record_fingerprint("p", "new-content", "ep_1")
        self.assertIsNone(
            batch_fingerprint.lookup_duplicate("p", "old-content", {"ep_1"}))
        self.assertEqual(
            batch_fingerprint.lookup_duplicate("p", "new-content", {"ep_1"}),
            "ep_1",
        )

    def test_forget_fingerprint(self):
        batch_fingerprint.record_fingerprint("p", "abc", "ep_1")
        batch_fingerprint.forget_fingerprint("ep_1")
        self.assertIsNone(
            batch_fingerprint.lookup_duplicate("p", "abc", {"ep_1"}))

    def test_corrupt_index_is_tolerated(self):
        batch_fingerprint.INDEX_PATH.write_text("{not json", encoding="utf-8")
        self.assertIsNone(
            batch_fingerprint.lookup_duplicate("p", "abc", {"ep_1"}))
        batch_fingerprint.record_fingerprint("p", "abc", "ep_1")
        self.assertEqual(
            batch_fingerprint.lookup_duplicate("p", "abc", {"ep_1"}), "ep_1")

    def test_index_is_valid_json_with_episode_map(self):
        batch_fingerprint.record_fingerprint("proj", "fp1", "ep_1")
        data = json.loads(batch_fingerprint.INDEX_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["proj"]["fp1"], "ep_1")


class ParquetHashTests(unittest.TestCase):
    def test_episode_index_column_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            table = pa.table({
                "episode_index": pa.array([7], pa.int64()),
                "frame_index": pa.array([0], pa.int64()),
                "v": pa.array([[1.0, 2.0]], pa.list_(pa.float32())),
            })
            one = root / "one.parquet"
            pq.write_table(table, one)
            other = root / "other.parquet"
            pq.write_table(
                table.set_column(0, "episode_index",
                                 pa.array([99], pa.int64())), other)
            self.assertEqual(
                batch_fingerprint._parquet_content_hash(one),
                batch_fingerprint._parquet_content_hash(other),
            )

    def test_unreadable_parquet_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.parquet"
            bad.write_bytes(b"not a parquet file")
            self.assertIsNone(batch_fingerprint._parquet_content_hash(bad))


if __name__ == "__main__":
    unittest.main()
