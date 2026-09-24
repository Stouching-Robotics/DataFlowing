"""全零占位位姿 —— 一个把整集假判成 FAIL 的 bug 的回归锁。

bug 长什么样
------------
``observation.slam_pose`` 里没采到的帧写的是**全零占位** ``(0,0,0,0,0,0,0)``。
它长得完全像合法位姿：长度 7、能索引、能相减。但它的四元数模是 0，于是

    两个零四元数的点积 = 0
    acos(0) = π/2  →  夹角 = 2 × π/2 = **π = 180°**
    30fps 下角速度 = π × 30 = **94.248 rad/s**

**任何涉及零占位的相邻对都会报一次"180° 跳变"** —— 不只是"真实↔零"的过渡，
两个零之间也一样。

实测 Test94 的 slam_pose 有 437/599 行是全零，SLAM 连续性因此报出 **465 处**
"跳变"（599 帧里几乎每一帧），整集被判 FAIL → **拦回人工审核**。而峰值角速度
94.248 就是上面那个 π×30，是识别这个 bug 的指纹。

    pytest tests/test_cleaning_pose_validity.py
    python3 tests/test_cleaning_pose_validity.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # noqa: E402

from app.processing.cleaning import collect  # noqa: E402
from app.processing.cleaning.checks import (  # noqa: E402
    CheckContext, StreamEvidence, get_check, PASS, WARN, FAIL,
)
from app.processing.cleaning.contract import pose_is_valid  # noqa: E402

# π × 30fps —— 零四元数产生的"每帧 180°"角速度
ZERO_QUAT_RPS = 94.248

# 一个模不为 0 的合法朝向
VALID = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
ZERO = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _context(poses, fps=30.0):
    stream = StreamEvidence(key="slam_pose", channel="slam", fps=fps,
                            series={"pose": tuple(poses)})
    return CheckContext(batch_dir=Path("/nonexistent"), fps=fps, streams=(stream,))


def _continuity(poses, fps=30.0):
    ctx = _context(poses, fps)
    ctx.params = {}
    findings = get_check("umi.slam_continuity").run(ctx)
    assert findings, "一条结论都没产出"
    return findings[0]


# ── 判据本身 ────────────────────────────────────────────────
def test_zero_quaternion_is_invalid():
    assert pose_is_valid(VALID) is True
    assert pose_is_valid(ZERO) is False          # ← 核心
    assert pose_is_valid(()) is False            # 空元组本来就不算
    assert pose_is_valid((1.0, 2.0, 3.0)) is False


def test_zero_position_is_still_a_valid_pose():
    """位置可以合法地是原点；只有四元数模为 0 才不是合法朝向。"""
    assert pose_is_valid((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)) is True


# ── 核心回归 ────────────────────────────────────────────────
def test_zero_placeholders_do_not_create_jumps():
    """★ 全是零占位的一集不该报任何跳变。"""
    finding = _continuity([ZERO] * 500)
    assert finding.status is PASS, f"零占位被当成了跳变：{finding.message}"
    assert finding.metrics["jumps"] == 0


def test_mixed_valid_and_zero_placeholders_do_not_create_jumps():
    """★ 真实↔零占位的过渡同样不该报 —— 这正是 Test94 的情况。"""
    poses = []
    for index in range(200):
        poses.append(VALID if index % 3 == 0 else ZERO)
    finding = _continuity(poses)
    assert finding.status is PASS, f"混合占位报出 {finding.metrics['jumps']} 处跳变"
    assert finding.metrics["jumps"] == 0


def test_zero_placeholder_would_have_produced_the_94_rps_signature():
    """把守卫去掉，峰值角速度应当落回 94.248 —— 确认这确实是那个 bug。"""
    finding = _continuity([ZERO] * 100)
    assert finding.metrics["peak_angular_rps"] < ZERO_QUAT_RPS / 10


# ── 真阳性不能被一起修掉 ─────────────────────────────────────
def test_real_relocation_jump_is_still_detected():
    """两帧合法位姿之间突然位移 → 仍要报出来。"""
    near = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    far = (10.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)      # 10 米瞬移
    finding = _continuity([near] * 50 + [far] * 50)
    assert finding.status is WARN
    assert finding.metrics["jumps"] == 1
    assert finding.metrics["peak_linear_mps"] > 100   # 10m / (1/30s) = 300 m/s


def test_many_real_jumps_still_fail():
    near = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    far = (5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    poses = []
    for _ in range(30):
        poses.extend([near, far])
    finding = _continuity(poses)
    assert finding.status is FAIL
    assert finding.metrics["jumps"] >= 10


# ── 采集层的 valid_count 也要认这个 ─────────────────────────
def test_collector_valid_count_excludes_zero_placeholders():
    """位姿流的 valid_count 若把零占位算作有效，别的检查也会被带偏。"""
    values = [list(VALID), list(ZERO), None, list(VALID)]
    counts = tuple(
        1 if collect.pose_is_valid(tuple(item)) else 0
        for item in collect.vector_series(values, 7))
    assert counts == (1, 0, 0, 1)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
