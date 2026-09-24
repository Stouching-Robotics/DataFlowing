"""帧对齐检查的回归测试(app/cleaning.py)。

背景:2026-09-23 之前 ``_check_frame_alignment_fs`` 直接用了 ``cv2`` 却没 import,
``NameError`` 被外层 ``except`` 吞掉 → 该检查在文件级路径下**永远静默通过**,
detail 写着 "Check error (skipped)" 而 passed=True,没人会注意。

本文件锁住三件事:
  1. 检查真的跑了(不再返回 Check error / Degraded pass)
  2. 没有视频时不误报 —— 纯传感器批次(如纯手套)是合法的,判"未适用"
  3. 帧数真的不匹配时能抓出来

    pytest tests/test_cleaning_frame_alignment.py
    python3 tests/test_cleaning_frame_alignment.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.cleaning import _check_frame_alignment_fs  # noqa: E402

CHECK = "frame_alignment"


def _write_parquet(batch: Path, frames: int) -> None:
    """写一个带 frame_index 列的 data parquet(检查按 rglob('data') 找)。"""
    data_dir = batch / "data" / "chunk-000"
    data_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"frame_index": range(frames)}).to_parquet(
        data_dir / "episode_000000.parquet")


def _write_video(batch: Path, frames: int, name: str = "cam.mp4") -> None:
    """写一个真 mp4,帧数可控。"""
    import cv2
    path = batch / name
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (64, 48))
    try:
        for index in range(frames):
            writer.write(np.full((48, 64, 3), index % 256, dtype=np.uint8))
    finally:
        writer.release()


# ── 不再静默通过 ────────────────────────────────────────────
def test_check_actually_runs_without_video(tmp_path):
    """没有视频时必须给出明确结论,而不是被异常吞掉的伪通过。"""
    _write_parquet(tmp_path, 100)
    result = _check_frame_alignment_fs(tmp_path)
    assert result["name"] == CHECK
    # 修复前的症状:detail 里写 "Check error (skipped)"
    assert "Check error" not in result["detail"]
    assert "Degraded pass" not in result["detail"]


def test_no_video_is_not_a_failure(tmp_path):
    """纯传感器批次(只有手套、没有相机)是合法的 —— 没有可比对的对象。"""
    _write_parquet(tmp_path, 100)
    result = _check_frame_alignment_fs(tmp_path)
    assert result["passed"] is True
    assert "No video" in result["detail"]


def test_no_parquet_is_skipped(tmp_path):
    result = _check_frame_alignment_fs(tmp_path)
    assert result["passed"] is True
    assert "No parquet" in result["detail"]


# ── 真的能抓出不匹配 ────────────────────────────────────────
def test_matching_counts_pass(tmp_path):
    _write_parquet(tmp_path, 50)
    _write_video(tmp_path, 50)
    result = _check_frame_alignment_fs(tmp_path)
    assert result["passed"] is True, result["detail"]


def test_short_video_fails(tmp_path):
    """视频只有 10 帧、数据有 100 帧 → 必须判失败(容差 10%)。"""
    _write_parquet(tmp_path, 100)
    _write_video(tmp_path, 10)
    result = _check_frame_alignment_fs(tmp_path)
    assert result["passed"] is False, result["detail"]


def test_within_tolerance_passes(tmp_path):
    """差 5% 在 ±10% 容差内 → 通过。"""
    _write_parquet(tmp_path, 100)
    _write_video(tmp_path, 95)
    result = _check_frame_alignment_fs(tmp_path)
    assert result["passed"] is True, result["detail"]


def test_result_shape_is_stable(tmp_path):
    """返回结构必须与本模块另外两个检查一致(前端按 name/passed/detail 读)。"""
    _write_parquet(tmp_path, 30)
    _write_video(tmp_path, 30)
    result = _check_frame_alignment_fs(tmp_path)
    assert set(result) == {"name", "passed", "detail"}
    assert isinstance(result["passed"], bool)


if __name__ == "__main__":
    import inspect
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as tmp:
                    fn(Path(tmp))
            else:
                fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
