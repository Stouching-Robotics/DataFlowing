"""手套检查项 —— 每一项都必须**被证明能抓到失败**。

真实数据上 5 项全绿（6 集、左右手各 5-6 条流、零误报）。但"全绿"证明不了什么：
**检查项从来没触发过，和检查项根本跑不起来，报告长得一模一样。** 所以这里每条
都构造一份明确的坏数据，断言它被抓到；再造一份好数据，断言不误报。

阈值全部来自实测：
  * 阵列 16×16 = 256 维
  * 取值门限 500（0 到 500 之间没有值）
  * 设备更新 ~7.5Hz → 受压时静止**恰好 4 帧**（正常，不是卡死）
  * 整集最低有 11.4% 的帧有压力

    pytest tests/test_cleaning_glove.py
    python3 tests/test_cleaning_glove.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # noqa: E402

from app.processing.cleaning.checks import (  # noqa: E402
    CheckContext, StreamEvidence, applicable_checks, get_check,
    PASS, WARN, FAIL,
)

SIZE = 256


# ── 构造 ────────────────────────────────────────────────────
def _frame(value: float = 0.0, size: int = SIZE) -> tuple:
    return tuple([value] * size)


def _context(series: dict, *, channel="tactile", key="left_glove",
             fps=30.0) -> CheckContext:
    stream = StreamEvidence(key=key, channel=channel, series=series)
    return CheckContext(batch_dir=Path("/nonexistent"), fps=fps, streams=(stream,))


def _run(slug: str, ctx: CheckContext, params: dict | None = None):
    ctx.params = params or {}
    findings = get_check(slug).run(ctx)
    assert findings, f"{slug} 一条结论都没产出 —— 报告会显示为『没问题』"
    return findings


def _only(slug, ctx, params=None):
    findings = _run(slug, ctx, params)
    statuses = {f.status for f in findings}
    assert len(statuses) == 1, f"期望单一结论，得到 {statuses}"
    return findings[0]


# ── 可达性：检查项必须真的会被选中 ───────────────────────────
def test_glove_checks_are_applicable():
    """★ 防"检查项静默不跑"—— 声明与闸门对不上时它们一条都不会执行。"""
    slugs = {c.slug for c in applicable_checks(
        channels={"tactile", "device_status", "time"},
        modalities={"glove_sensor"})}
    for expected in ("glove.array_shape", "glove.device_status",
                     "glove.frozen_array", "glove.gate_floor",
                     "glove.no_contact"):
        assert expected in slugs, f"{expected} 在手套数据上不可达"


def test_glove_checks_skip_other_devices():
    """UMI 夹爪上不该跑手套检查 —— 设备卡片闸门要生效。"""
    slugs = {c.slug for c in applicable_checks(
        channels={"tactile", "slam", "time"}, modalities={"gripper_device"})}
    assert not any(s.startswith("glove.") for s in slugs)


def test_device_status_check_needs_its_channel():
    """没有状态列（老批次）时不该跑 —— 跑起来只会报"没数据"。"""
    slugs = {c.slug for c in applicable_checks(
        channels={"tactile", "time"}, modalities={"glove_sensor"})}
    assert "glove.device_status" not in slugs
    assert "glove.frozen_array" in slugs


# ── 1. 连接状态 ─────────────────────────────────────────────
def test_status_constant_connected_passes():
    ctx = _context({"device_status": tuple(["connected"] * 100)}, channel="device_status")
    assert _only("glove.device_status", ctx).status is PASS


def test_status_change_mid_recording_fails():
    """★ 从"连着"变成别的 = 掉线，与状态词表无关，见到即拦。"""
    values = tuple(["connected"] * 50 + ["disconnected"] * 50)
    ctx = _context({"device_status": values}, channel="device_status")
    finding = _only("glove.device_status", ctx)
    assert finding.status is FAIL
    assert finding.metrics["first_change_frame"] == 50


def test_status_unknown_constant_only_warns():
    """词表只见过 connected；别的取值不拦批次（拦错了比不拦更糟）。"""
    ctx = _context({"device_status": tuple(["ok"] * 100)}, channel="device_status")
    assert _only("glove.device_status", ctx).status is WARN


def test_status_all_blank_warns():
    ctx = _context({"device_status": tuple([""] * 100)}, channel="device_status")
    assert _only("glove.device_status", ctx).status is WARN


# ── 2. 阵列完整性 ───────────────────────────────────────────
def test_shape_full_array_passes():
    ctx = _context({"tactile": tuple([_frame()] * 50)})
    assert _only("glove.array_shape", ctx).status is PASS


def test_shape_catches_truncation():
    """★ 正是那个真实踩过的 bug：256 维被静默截成 3 维。"""
    ctx = _context({"tactile": tuple([_frame(size=3)] * 500)})
    finding = _only("glove.array_shape", ctx)
    assert finding.status is FAIL
    assert finding.metrics["bad_ratio"] == 1.0
    assert finding.metrics["observed_sizes"] == [3]


def test_shape_single_null_frame_only_warns():
    """偶发一帧缺失不该拦住整批。"""
    values = [_frame(600.0)] * 999 + [()]
    ctx = _context({"tactile": tuple(values)})
    assert _only("glove.array_shape", ctx).status is WARN


# ── 3. 取值下限 ─────────────────────────────────────────────
def test_gate_floor_normal_values_pass():
    ctx = _context({"tactile": (_frame(0.0), _frame(508.09), _frame(1500.0))})
    assert _only("glove.gate_floor", ctx).status is PASS


def test_gate_floor_catches_encoding_change():
    """★ 固件改成直接输出原始 ADC 时，值会掉到门限以下 —— 而别的检查全绿。"""
    values = [_frame(v) for v in (5.0, 20.0, 137.0, 400.0)] + [_frame(900.0)]
    ctx = _context({"tactile": tuple(values)})
    finding = _only("glove.gate_floor", ctx)
    assert finding.status is FAIL
    assert finding.metrics["below_ratio"] == 0.8


def test_gate_floor_all_zero_is_not_its_business():
    """整集全零是 glove.no_contact 的事，不在这里重复报。"""
    findings = get_check("glove.gate_floor").run(_context({"tactile": (_frame(),)}))
    assert findings == []


# ── 4. 阵列卡死 ─────────────────────────────────────────────
def test_frozen_normal_device_rate_passes():
    """★ 设备本来就每 4 帧才更新一次 —— 静止 4 帧是**正常**，不是卡死。"""
    values = []
    for block in range(100):
        values.extend([_frame(600.0 + block)] * 4)
    ctx = _context({"tactile": tuple(values)})
    assert _only("glove.frozen_array", ctx).status is PASS


def test_frozen_detects_stall():
    values = [_frame(600.0)] * 500 + [_frame(900.0)] * 10
    ctx = _context({"tactile": tuple(values)})
    finding = _only("glove.frozen_array", ctx)
    assert finding.status is FAIL                      # 500 帧 ≥ fail_frames(300)
    assert finding.metrics["longest_run_frames"] == 500
    # 区间要能定位到哪儿卡了
    assert finding.ranges and finding.ranges[0].start_frame == 0


def test_frozen_ignores_all_zero_stretches():
    """★ 实测踩过的坑：全零段天然"完全相同"，算进去会报出大片假区间。

    真实数据里一集有 88% 的帧全零，最长"静止"575 帧 —— 全部来自全零段。
    """
    values = [_frame(0.0)] * 800 + [_frame(600.0)] * 10
    ctx = _context({"tactile": tuple(values)})
    assert _only("glove.frozen_array", ctx).status is PASS


def test_frozen_warn_threshold_between_normal_and_stall():
    """40~300 帧是警告区（正常 4 帧，卡死 300+）。"""
    values = [_frame(600.0)] * 100 + [_frame(700.0)] * 10
    ctx = _context({"tactile": tuple(values)})
    assert _only("glove.frozen_array", ctx).status is WARN


# ── 5. 无接触 ───────────────────────────────────────────────
def test_no_contact_with_pressure_passes():
    ctx = _context({"tactile": (_frame(0.0), _frame(620.0))})
    assert _only("glove.no_contact", ctx).status is PASS


def test_no_contact_all_zero_warns():
    ctx = _context({"tactile": tuple([_frame(0.0)] * 500)})
    finding = _only("glove.no_contact", ctx)
    assert finding.status is WARN
    assert finding.metrics["active_frames"] == 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
