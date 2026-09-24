"""视频类检查项单测。

这些检查项**不读磁盘** —— 证据由 evidence.py 采集好放进 StreamEvidence，
测试直接构造假证据即可，不需要真 mp4 或 cv2。这正是当初把 IO 做成注入的目的。

    pytest tests/test_check_video.py
    python3 tests/test_check_video.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.processing.cleaning.checks import (  # noqa: E402
    CheckContext, StreamEvidence, get_check, PASS, WARN, FAIL, ERROR,
)


def _ctx(streams, **kwargs) -> CheckContext:
    """装一个最小的执行上下文。"""
    return CheckContext(batch_dir=Path("/tmp/nonexistent"),
                        streams=tuple(streams), **kwargs)


def _rgb(key="umi_rgb", **video) -> StreamEvidence:
    return StreamEvidence(key=key, channel="rgb", fps=30.0, video=video)


# ── video.frame_drop ────────────────────────────────────────
def test_frame_drop_passes_within_tolerance():
    check = get_check("video.frame_drop")
    ctx = _ctx([_rgb(frame_count=1188, reason="passed")], data_rows=1190)
    findings = check.run(ctx)
    assert len(findings) == 1
    assert findings[0].status == PASS


def test_frame_drop_warns_on_small_gap():
    check = get_check("video.frame_drop")
    # 差 20 帧 / 容差 max(2, 1200*0.005=6) = 6 → 20 > 6*5=30? 否 → WARN
    ctx = _ctx([_rgb(frame_count=1180, reason="passed")], data_rows=1200)
    findings = check.run(ctx)
    assert findings[0].status == WARN
    assert findings[0].metrics["diff"] == 20
    assert findings[0].metrics["tolerance"] == 6


def test_frame_drop_fails_on_large_gap():
    check = get_check("video.frame_drop")
    # 差 500 帧 >> 容差*5 → FAIL
    ctx = _ctx([_rgb(frame_count=700, reason="passed")], data_rows=1200)
    assert check.run(ctx)[0].status == FAIL


def test_frame_drop_tolerance_floor_for_small_batches():
    """小批次按绝对帧数兜底：100 行 × 0.5% = 0 → 用下限 2。"""
    check = get_check("video.frame_drop")
    ctx = _ctx([_rgb(frame_count=98, reason="passed")], data_rows=100)
    findings = check.run(ctx)
    assert findings[0].metrics["tolerance"] == 2
    assert findings[0].status == PASS


def test_frame_drop_ignores_unreadable_video():
    """打不开的流不该被算成"丢帧" —— 那是 decode_error 的职责。"""
    check = get_check("video.frame_drop")
    ctx = _ctx([_rgb(frame_count=0, reason="video_open_failed")], data_rows=1200)
    assert check.run(ctx) == []


def test_frame_drop_uses_node_params_over_defaults():
    """节点配置的阈值要盖过 default_params。"""
    check = get_check("video.frame_drop")
    ctx = _ctx([_rgb(frame_count=900, reason="passed")], data_rows=1000,
               params={"tolerance_ratio": 0.5, "tolerance_min": 500})
    assert check.run(ctx)[0].status == PASS


# ── video.black_screen ──────────────────────────────────────
def test_black_screen_passes_when_no_ranges():
    check = get_check("video.black_screen")
    assert check.run(_ctx([_rgb(frame_count=300, reason="passed")])) == []


def test_black_screen_reports_ranges_and_frames():
    check = get_check("video.black_screen")
    # 30 秒里黑 1 秒 = 3.3%，低于 fail_ratio(5%) → WARN
    ctx = _ctx([_rgb(frame_count=900, fps=30.0, reason="black_screen",
                     black_ranges=[[12.0, 13.0]])],
               duration_sec=30.0)
    finding = check.run(ctx)[0]
    assert finding.status == WARN
    assert finding.metrics["segments"] == 1
    assert len(finding.ranges) == 1
    item = finding.ranges[0]
    assert item.start_sec == 12.0 and item.end_sec == 13.0
    assert item.start_frame == 360 and item.end_frame == 390


def test_black_screen_fails_when_ratio_exceeds_threshold():
    """占比超过 5% → FAIL。30 秒里黑 3 秒 = 10%。"""
    check = get_check("video.black_screen")
    ctx = _ctx([_rgb(frame_count=900, reason="black_screen",
                     black_ranges=[[0.0, 3.0]])],
               duration_sec=30.0)
    assert check.run(ctx)[0].status == FAIL


def test_black_screen_drops_ranges_below_min_sec():
    """短于 min_sec(0.5s) 的黑屏段忽略 —— 避免逐帧闪烁刷屏。"""
    check = get_check("video.black_screen")
    ctx = _ctx([_rgb(frame_count=900, reason="black_screen",
                     black_ranges=[[1.0, 1.2]])],
               duration_sec=30.0)
    assert check.run(ctx) == []


# ── video.freeze ────────────────────────────────────────────
def test_freeze_warns_and_escalates_by_duration():
    check = get_check("video.freeze")
    short = _ctx([_rgb(frame_count=900, reason="freeze_suspected",
                       freeze_ranges=[[5.0, 8.0]])])
    assert check.run(short)[0].status == WARN          # 3 秒 < fail_sec(10)

    long = _ctx([_rgb(frame_count=900, reason="freeze_suspected",
                      freeze_ranges=[[5.0, 20.0]])])
    assert check.run(long)[0].status == FAIL           # 15 秒 >= 10


def test_freeze_metrics_report_longest():
    """longest_sec 取最长的那一段：4.0s 和 5.0s → 5.0。"""
    check = get_check("video.freeze")
    ctx = _ctx([_rgb(frame_count=900, reason="freeze_suspected",
                     freeze_ranges=[[5.0, 9.0], [20.0, 25.0]])])
    finding = check.run(ctx)[0]
    assert finding.metrics["segments"] == 2
    assert finding.metrics["longest_sec"] == 5.0


# ── video.decode_error ──────────────────────────────────────
def test_decode_error_passes_without_errors():
    check = get_check("video.decode_error")
    ctx = _ctx([_rgb(frame_count=300, reason="passed", decode_error_count=0)])
    assert check.run(ctx)[0].status == PASS


def test_decode_error_fails_and_marks_frames():
    check = get_check("video.decode_error")
    ctx = _ctx([_rgb(frame_count=300, fps=30.0, reason="decode_error",
                     decode_error_count=3, decode_errors=[30, 60, 90])])
    finding = check.run(ctx)[0]
    assert finding.status == FAIL
    assert len(finding.ranges) == 3
    assert finding.ranges[0].start_frame == 30
    assert finding.ranges[0].start_sec == 1.0


def test_decode_error_marks_opencv_missing_as_error_not_fail():
    """缺 OpenCV 是环境问题，不该记成数据质量事故。"""
    check = get_check("video.decode_error")
    ctx = _ctx([_rgb(reason="opencv_unavailable")])
    assert check.run(ctx)[0].status == ERROR


def test_decode_error_marks_unopenable_video_as_fail():
    check = get_check("video.decode_error")
    ctx = _ctx([_rgb(reason="video_open_failed", error="moov atom not found")])
    finding = check.run(ctx)[0]
    assert finding.status == FAIL
    assert "moov atom" in finding.metrics["error"]


# ── 跨流 ────────────────────────────────────────────────────
def test_checks_iterate_all_rgb_streams():
    """双目项目有两路 RGB，都要检查。"""
    check = get_check("video.frame_drop")
    ctx = _ctx([_rgb("cam_left", frame_count=700, reason="passed"),
                _rgb("cam_right", frame_count=1200, reason="passed")],
               data_rows=1200)
    findings = check.run(ctx)
    assert {item.stream for item in findings} == {"cam_left", "cam_right"}
    assert [item.status for item in findings].count(FAIL) == 1


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception:
            failures += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{'FAILED' if failures else 'OK'} ({failures} 个失败)")
    sys.exit(1 if failures else 0)
