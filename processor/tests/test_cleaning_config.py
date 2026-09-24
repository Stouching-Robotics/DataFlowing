"""质检配置解析单测(engine.resolve_config / applicable_checks 的用法 +
trigger 的工作流接线判定)。

**这个文件盯的是一类 bug：检查项静默不跑。** 报告照常生成、``passed`` 照常
写，只是有一条本该跑的检查根本没执行 —— 没有任何异常、没有任何日志。项目里
已经因此踩过好几次，所以这里每条都用"跑了几项"来断言，而不是断言没抛异常。

全是纯函数 + 一次针对临时目录的引擎调用，不需要真视频、不需要 cv2。

    pytest tests/test_cleaning_config.py
    python3 tests/test_cleaning_config.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # noqa: E402

from app.processing.cleaning import engine  # noqa: E402
from app.processing.cleaning.collect import _detect_modalities  # noqa: E402
from app.processing.cleaning.trigger import data_quality_target  # noqa: E402


# ── 模态推断 ────────────────────────────────────────────────
def test_infers_from_channels_when_no_hint():
    assert _detect_modalities({"rgb"}, None) == ("mono_rgb",)
    assert _detect_modalities({"tactile"}, None) == ("glove_sensor",)
    assert _detect_modalities({"slam", "rgb"}, None) == ("gripper_device",)


def test_alias_node_types_normalize():
    # 工作流节点类型用的是 camera 后缀，要映射到 canonical 设备模态
    assert _detect_modalities({"rgb"}, "mono_camera") == ("mono_rgb",)
    assert _detect_modalities({"depth"}, "rgbd_camera") == ("rgbd_camera",)


def test_hint_wins_for_modalities_inference_cannot_produce():
    """★ ``stereo_rgb`` 推断不出来 —— "立体"不是列名里看得见的特征。

    没有这条，双目工作流在设置面板里配的阈值会被静默忽略（查不到那个 tab）。
    """
    assert "stereo_rgb" in _detect_modalities({"rgb"}, ["stereo_rgb"])
    assert "stereo_rgbd_camera" in _detect_modalities({"depth"}, ["stereo_rgbd_camera"])


def test_hint_and_inference_are_unioned():
    """两个来源各有盲区，取并集 —— 缺一个都会有检查项不跑。"""
    got = _detect_modalities({"rgb", "depth"}, ["stereo_rgbd_camera"])
    assert "stereo_rgbd_camera" in got          # 来自 hint
    assert "rgbd_camera" in got                 # 来自数据推断


def test_multiple_hints_keep_order():
    """多设备时工作流连线的顺序 = 用户预期顺序（resolve_config 先出现的优先）。"""
    assert _detect_modalities({"rgb", "slam"}, ["gripper_device", "mono_rgb"]) == \
        ("gripper_device", "mono_rgb")


# ── 工作流接线判定 ──────────────────────────────────────────
def _graph(nodes, edges):
    return {
        "nodes": [{"id": i, "data": {"nodeType": t}} for i, t in nodes],
        "edges": [{"source": s, "target": t} for s, t in edges],
    }


def test_target_requires_incoming_edge():
    """孤零零摆一张卡片只是配置，不该触发 —— 与 video_quality_gate 同语义。"""
    assert data_quality_target(_graph([("q", "data_quality")], []), {}) is None


def test_target_accepts_gripper_device():
    """★ ``gripper_device`` 不在 video_quality_gate_config 的源类型白名单里。

    那条白名单漏一个设备类型，卡片就静默失效一次。这里用"有入边"当判据，
    所以 UMI 工作流能正常触发 —— 这是本项目的主用例。
    """
    got = data_quality_target(
        _graph([("a", "gripper_device"), ("q", "data_quality")], [("a", "q")]), {})
    assert got is not None and got[1] == ("gripper_device",)


def test_target_walks_through_process_nodes():
    """相机 → 处理节点 → 质检：直接上游是处理节点，要往回找到设备卡片。"""
    got = data_quality_target(
        _graph([("a", "mono_camera"), ("p", "rgbd_to_3d_bare_hand"), ("q", "data_quality")],
               [("a", "p"), ("p", "q")]), {})
    assert got is not None and got[1] == ("mono_rgb",)


def test_target_handles_legacy_slug():
    """合并前的旧 slug 要能识别（已保存的工作流打开时自动迁移）。"""
    for legacy in ("ai_quality_review", "data_cleaning"):
        got = data_quality_target(
            _graph([("a", "gripper_device"), ("q", legacy)], [("a", "q")]), {})
        assert got is not None, legacy


def test_target_merges_node_configs_over_card():
    """``node_configs`` 覆盖卡片里的 ``data.config``（与既有的卡片配置读取一致）。"""
    graph = _graph([("a", "gripper_device"), ("q", "data_quality")], [("a", "q")])
    graph["nodes"][1]["data"]["config"] = {"devices": {"stale": {}}}
    cfg, _ = data_quality_target(graph, {"q": {"devices": {"gripper_device": {}}}})
    assert "gripper_device" in cfg["devices"]


# ── ★ 配置是黑名单，不是白名单 ──────────────────────────────
#
# 前端设置面板只在用户**碰过**的检查项上写 entry，未触碰的不进 config。
# 曾把"显式启用的那些"当白名单传进 applicable_checks(enabled=...)，
# 后果是「在弹窗里改一个阈值 → 其余检查项全部静默停跑」：
# 实测只配 umi.gripper_range 时 9 项变 1 项，不报任何错。
#
# 下面用真实引擎跑一个最小批次（只有 parquet，无视频），断言项数。
def _minimal_batch(tmp: Path) -> Path:
    """造一个只有 UMI 标量列的批次 —— 够触发若干检查项，又不依赖 cv2。"""
    import numpy as np
    import pandas as pd

    import json

    # 目录层级不能省：find_episode_parquets 只认 data/chunk-*/episode_*.parquet
    (tmp / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (tmp / "meta").mkdir(exist_ok=True)
    (tmp / "meta" / "info.json").write_text(
        json.dumps({"fps": 30.0}), encoding="utf-8")

    rows = 30
    pd.DataFrame({
        # 列名必须与 _COLUMN_CHANNELS 逐字一致 —— 对不上不会有任何报错，
        # 只是信道识别不出来、检查项静默不跑（正是本文件要盯住的那类 bug）
        "timestamp": np.arange(rows, dtype=float) / 30.0,
        "observation.gripper_state": np.linspace(0.0, 1.0, rows),
        "observation.gripper_left_force": np.linspace(0.0, 5.0, rows),
        "action": np.tile(np.arange(7, dtype=float), (rows, 1)).tolist(),
    }).to_parquet(tmp / "data" / "chunk-000" / "episode_000000.parquet")
    return tmp


def _checks_run(config: dict) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        batch = _minimal_batch(Path(tmp) / "batch")
        report = engine.run_checks(batch, episode_id="t", config=config)
        return sorted(report["modalities"]["checks_run"])


def test_config_only_touched_entry_does_not_disable_the_rest():
    """配了一项阈值 ≠ 只跑这一项。"""
    baseline = _checks_run({})
    assert baseline, "夹具没触发任何检查项，测试无意义"

    only_one = _checks_run({"devices": {"gripper_device": {"checks": {
        "umi.gripper_range": {"enabled": True, "params": {"max_force_drift": 500}},
    }}}})
    assert only_one == baseline


def test_explicit_disable_still_works():
    """黑名单那半边必须仍然生效 —— 别把白名单 bug 修成"配置完全无效"。"""
    baseline = _checks_run({})
    disabled = _checks_run({"devices": {"gripper_device": {"checks": {
        "umi.gripper_range": {"enabled": False},
    }}}})
    assert "umi.gripper_range" not in disabled
    assert len(disabled) == len(baseline) - 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
