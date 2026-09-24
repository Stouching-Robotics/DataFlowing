"""采集层的两条静默失效，各上一把锁。

它们都不报错、报告照常生成，只是内容是错的：

  1. **非首集读出空帧** —— canonical 是一集一文件，曾经取 ``parquets[0]`` 再按
     ``episode_index`` 列筛行，于是除第 0 集外全筛成空。报告退化成
     ``empty_report``（``episode.status = ERROR``），在"数据不对就拦回人工审核"
     的链路里表现为**每一集都被卡住**。
  2. **触觉阵列被截断** —— ``_CHANNEL_WIDTH`` 没有 ``tactile`` 项，落到 3 的
     兜底值，16×16=256 个感应点被静默截成 3 个，检查项只看得到 1%。

外加两套命名的去重（``observation.tactile.*`` 与 ``observation.*_glove``）。

造的是最小 fixture，不依赖真实数据、不需要 cv2。

    pytest tests/test_cleaning_collect.py
    python3 tests/test_cleaning_collect.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.processing.cleaning import collect  # noqa: E402


# ── fixture ─────────────────────────────────────────────────
def _canonical_batch(root: Path, episodes) -> Path:
    """canonical 布局：一集一文件。``episodes`` 是 {集号: 行数}。"""
    (root / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (root / "meta").mkdir(exist_ok=True)
    for index, rows in episodes.items():
        pd.DataFrame({
            "episode_index": np.full(rows, index, dtype="int64"),
            "frame_index": np.arange(rows, dtype="int64"),
            "timestamp": np.arange(rows, dtype=float) / 30.0,
        }).to_parquet(root / "data" / "chunk-000" / f"episode_{index:06d}.parquet")
    return root


def _tactile_batch(root: Path, *, columns, frames=8, width=256) -> Path:
    """带触觉阵列的批次。``columns`` 决定用哪套命名。"""
    (root / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (root / "meta").mkdir(exist_ok=True)
    data = {"timestamp": np.arange(frames, dtype=float) / 30.0}
    for name in columns:
        data[name] = [list(np.full(width, float(i))) for i in range(frames)]
    pd.DataFrame(data).to_parquet(root / "data" / "chunk-000" / "episode_000000.parquet")
    return root


# ── 定位这一集的数据文件 ─────────────────────────────────────
def test_picks_the_file_for_the_requested_episode(tmp_path):
    """★ 核心回归：非首集必须读到**自己**那个文件，而不是空帧。"""
    batch = _canonical_batch(tmp_path / "b", {0: 10, 1: 20, 7: 30})

    for index, rows in ((0, 10), (1, 20), (7, 30)):
        path, filter_rows = collect.resolve_episode_data(batch, index)
        assert path is not None and path.name == f"episode_{index:06d}.parquet"
        assert filter_rows is False, "canonical 一集一文件，不该再按列筛行"
        assert len(collect.load_rows(path)) == rows


def test_the_original_bug_is_actually_a_bug(tmp_path):
    """把当初的写法钉在这里：它**不报错**，只是静默返回空帧。

    当初是「取 ``parquets[0]`` 再按 ``episode_index`` 列筛行」。canonical 每一集
    一个文件，第 0 个文件里只有第 0 集的行 —— 拿它去筛第 3 集得到 0 行。
    """
    batch = _canonical_batch(tmp_path / "b", {0: 10, 3: 25})
    first = collect.resolve_episode_data(batch, 0)[0]

    assert len(collect.load_rows(first, 3)) == 0        # ← 当初的 bug
    assert len(collect.load_rows(first, 0)) == 10       # 只有筛自己那集才对

    # 现在的做法：选对文件，且**不再按列筛行**
    third, filter_rows = collect.resolve_episode_data(batch, 3)
    assert filter_rows is False
    assert len(collect.load_rows(third, None)) == 25


def test_gather_evidence_reads_the_requested_episode(tmp_path):
    """★ 端到端那把锁。

    上面两条直接测 ``resolve_episode_data``，**测不到接线**：把
    ``gather_evidence`` 改回"取第一个文件 + 按列筛行"，它们照样全绿。而当初
    出 bug 的正是接线那一处。这里断言的是采集**结果**。
    """
    batch = _canonical_batch(tmp_path / "b", {0: 10, 3: 25, 9: 40})

    for index, rows in ((0, 10), (3, 25), (9, 40)):
        evidence = collect.gather_evidence(batch, episode_index=index)
        assert evidence.rows == rows, (
            f"episode {index} 读到 {evidence.rows} 行、应为 {rows} 行 —— "
            f"多半是又退回了「取第一个文件再按列筛行」")
        assert evidence.notes.get("reason") is None, "退化成空报告了"


def test_missing_episode_returns_none(tmp_path):
    batch = _canonical_batch(tmp_path / "b", {0: 5})
    assert collect.resolve_episode_data(batch, 99)[0] is None


def test_multi_episode_layout_still_filters_rows(tmp_path):
    """导出产物是一文件多集，那条路必须继续按行筛。"""
    root = tmp_path / "b" / "data" / "chunk-000"
    root.mkdir(parents=True)
    pd.DataFrame({
        "episode_index": np.repeat([4, 5], 6),
        "timestamp": np.arange(12, dtype=float),
    }).to_parquet(root / "file-000.parquet")

    path, filter_rows = collect.resolve_episode_data(tmp_path / "b", 5)
    assert path is not None and filter_rows is True
    frame = collect.load_rows(path, 5)
    assert len(frame) == 6 and set(frame["episode_index"]) == {5}


# ── 触觉阵列 ────────────────────────────────────────────────
def test_tactile_array_is_not_truncated(tmp_path):
    """★ 256 个感应点必须原样保留 —— 截成 3 个检查项就废了。"""
    batch = _tactile_batch(tmp_path / "b", columns=["observation.left_glove"])
    evidence = collect.gather_evidence(batch)

    stream = next(s for s in evidence.streams if s.channel == "tactile")
    series = stream.series["tactile"]
    assert len(series[0]) == 256, f"触觉阵列被截断成 {len(series[0])}"


def test_unknown_channel_is_not_truncated():
    """兜底宽度不能是某个具体数字 —— 换设备时同样会静默丢数据。"""
    assert collect.vector_series([[1.0] * 40])[0] == tuple([1.0] * 40)
    assert len(collect.vector_series([[1.0] * 40], 7)[0]) == 7   # 显式声明才截断


def test_tactile_alias_is_recognized(tmp_path):
    """★ 只跑过导出的树上只有 observation.tactile.* —— 以前认不出，手套检查
    整条静默跳过。"""
    batch = _tactile_batch(tmp_path / "b", columns=["observation.tactile.left"])
    evidence = collect.gather_evidence(batch)

    assert "tactile" in evidence.channels
    keys = [s.key for s in evidence.streams if s.channel == "tactile"]
    assert keys == ["left_glove"], f"别名列没归一成协议流名: {keys}"


def test_both_naming_schemes_do_not_duplicate(tmp_path):
    """两套命名同时存在时是同一份数据，登记成两条流会让缺陷被报两次。"""
    batch = _tactile_batch(
        tmp_path / "b",
        columns=["observation.left_glove", "observation.tactile.left"])
    evidence = collect.gather_evidence(batch)

    keys = [s.key for s in evidence.streams if s.channel == "tactile"]
    assert keys == ["left_glove"], f"同一份数据被登记了多条流: {keys}"


def test_one_handed_episode_yields_one_stream(tmp_path):
    """同项目内 schema 不一致是真实存在的（D435 第 3 集只有右手）。

    采集层只要"有什么收什么"即可；检查项不能假设双手都在。
    """
    batch = _tactile_batch(tmp_path / "b", columns=["observation.right_glove"])
    evidence = collect.gather_evidence(batch)

    keys = [s.key for s in evidence.streams if s.channel == "tactile"]
    assert keys == ["right_glove"]
    assert evidence.modalities == ("glove_sensor",)


# ── SLAM 列的多相机命名 ─────────────────────────────────────
#
# 双目/多相机把第二路存成 observation.<相机名>_slam_*，而采集只认不带前缀的
# 两个名字 —— 那种集**有 SLAM 数据、却一个 SLAM 检查都不跑**，且不报任何错
# （实测 Test94/episode_000003 只有 gripper_2 前缀，整集 SLAM 检查全空）。
def test_slam_single_camera_prefers_trajectory():
    """一个相机两列都在时只取一条 —— trajectory 是高频样本缓冲。"""
    assert collect._slam_columns(
        {"observation.slam_trajectory", "observation.slam_pose"}
    ) == ["observation.slam_trajectory"]


def test_slam_prefixed_camera_is_recognized():
    """★ 回归：只有 observation.<相机名>_slam_* 时也要认出来。"""
    picked = collect._slam_columns(
        {"observation.gripper_2_slam_trajectory", "observation.gripper_2_slam_pose"})
    assert picked == ["observation.gripper_2_slam_trajectory"]


def test_slam_multiple_cameras_yield_one_stream_each():
    """★ 双目：每个相机各一条流 —— 之前是全局 break，只取第一路。"""
    picked = collect._slam_columns({
        "observation.slam_pose", "observation.gripper_2_slam_pose"})
    assert sorted(picked) == ["observation.gripper_2_slam_pose",
                              "observation.slam_pose"]


def test_slam_ignores_lookalike_columns():
    """名字里含 slam 但不是位姿的列不能被当成 SLAM。"""
    assert collect._slam_columns({"observation.slam_trajectory_ns"}) == []
    assert collect._slam_columns(
        {"processing.umi_slam_action.data_4.action"}) == []
    # 伴生列在场也不影响正常列
    assert collect._slam_columns(
        {"observation.slam_pose", "observation.slam_trajectory_ns"}
    ) == ["observation.slam_pose"]


def test_gather_evidence_reads_prefixed_slam(tmp_path):
    """端到端：只有前缀命名的集也要产出 slam 流与 slam 信道。"""
    root = tmp_path / "b" / "data" / "chunk-000"
    root.mkdir(parents=True)
    (tmp_path / "b" / "meta").mkdir()
    rows = 6
    pd.DataFrame({
        "observation.gripper_2_slam_pose": [
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]] * rows,
    }).to_parquet(root / "episode_000000.parquet")

    evidence = collect.gather_evidence(tmp_path / "b")
    slam = [s for s in evidence.streams if s.channel == "slam"]
    assert [s.key for s in slam] == ["gripper_2_slam_pose"]
    assert "slam" in evidence.channels


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
