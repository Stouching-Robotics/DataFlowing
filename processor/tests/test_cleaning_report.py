"""清洗报告组装单测(app/processing/cleaning/report.py)。

覆盖:四层结果的每一层、时间轴拍平与去重、训练用途判定、fail-closed 语义。

全是纯函数 —— 不需要真视频、不需要 cv2、不需要任何 mock。

    pytest tests/test_cleaning_report.py
    python3 tests/test_cleaning_report.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.processing.cleaning import contract as C  # noqa: E402
from app.processing.cleaning import report as R  # noqa: E402


def _range(start, end, code="video.freeze", severity=C.WARN, stream="s1"):
    return C.Range(start_sec=start, end_sec=end, code=code,
                   severity=severity, stream=stream)


def _stream(name="s1", status=C.PASS, ranges=(), channels=("rgb", "time")):
    return C.StreamResult(stream=name, status=status, ranges=tuple(ranges),
                          channels=tuple(channels))


# ── 第二层:时间轴 ───────────────────────────────────────────
def test_timeline_sorted_by_time():
    streams = [
        _stream("s1", ranges=[_range(5, 6), _range(1, 2)]),
        _stream("s2", ranges=[_range(3, 4)]),
    ]
    axis = R.build_timeline(streams)
    assert [row["start_sec"] for row in axis] == [1, 3, 5]


def test_timeline_dedupes_identical_rows():
    dup = _range(1, 2)
    axis = R.build_timeline([_stream("s1", ranges=[dup, dup])])
    assert len(axis) == 1


def test_timeline_fills_stream_from_parent():
    """range 自己没写 stream 时,从所属的 stream 补上。"""
    bare = C.Range(start_sec=1, end_sec=2, code="x", severity=C.WARN)
    axis = R.build_timeline([_stream("cam_a", ranges=[bare])])
    assert axis[0]["stream"] == "cam_a"


def test_timeline_accepts_dict_streams():
    """报告从磁盘读回来时 streams 是 dict,不是 StreamResult。"""
    axis = R.build_timeline([{
        "stream": "s1",
        "ranges": [{"start_sec": 1, "end_sec": 2, "code": "x", "severity": C.WARN}],
    }])
    assert axis[0]["code"] == "x"


def test_timeline_empty():
    assert R.build_timeline([]) == []


# ── 第三层:整集结论 ─────────────────────────────────────────
def test_episode_takes_worst_stream():
    episode = R.rollup_episode([_stream("a", C.PASS), _stream("b", C.FAIL)])
    assert episode["status"] == C.FAIL
    assert episode["failed_streams"] == ["b"]


def test_episode_includes_findings_in_rollup():
    """跨模态检查没有对应的 stream,它的结论也要参与整集判定。"""
    finding = C.Finding(rule="cross.sync", version="1.0", status=C.FAIL,
                        scope="cross_modal")
    episode = R.rollup_episode([_stream("a", C.PASS)], [finding])
    assert episode["status"] == C.FAIL


def test_episode_all_pass():
    episode = R.rollup_episode([_stream("a", C.PASS), _stream("b", C.PASS)])
    assert episode["status"] == C.PASS
    assert episode["summary"] == "All checks passed"


def test_episode_summary_counts_each_bucket():
    episode = R.rollup_episode([
        _stream("a", C.FAIL), _stream("b", C.WARN), _stream("c", C.PENDING),
    ])
    assert "1 stream(s) failed" in episode["summary"]
    assert "1 stream(s) with warnings" in episode["summary"]
    assert "1 stream(s) pending review" in episode["summary"]


def test_episode_empty_streams_is_pass():
    """没有流可检查 → 这一层判 PASS(是否 fail-closed 由 build_report/passed 决定)。"""
    assert R.rollup_episode([])["status"] == C.PASS


# ── 第四层:训练用途 ─────────────────────────────────────────
def test_training_use_fails_on_missing_channel():
    rows = {row["use"]: row for row in R.rollup_training_use({"rgb", "time"})}
    assert rows["pure_vision"]["status"] == C.PASS
    assert rows["vision_tactile_joint"]["status"] == C.FAIL
    assert "not collected" in rows["vision_tactile_joint"]["reason"]
    assert rows["vision_tactile_joint"]["missing"] == ["tactile"]


def test_training_use_reports_all_missing_channels():
    rows = {row["use"]: row for row in R.rollup_training_use(set())}
    assert set(rows["pure_vision"]["missing"]) == {"rgb", "time"}


def test_training_use_inherits_episode_status():
    """信道齐全但整集有问题 → 该用途跟着判 warn/fail,而不是 pass。"""
    rows = {row["use"]: row for row in R.rollup_training_use({"rgb", "time"}, C.WARN)}
    assert rows["pure_vision"]["status"] == C.WARN
    assert rows["pure_vision"]["reason"] == "data quality problem"


def test_training_use_covers_every_declared_rule():
    rows = R.rollup_training_use({"rgb", "time"})
    assert len(rows) == len(R.TRAINING_USE_RULES)


# ── 完整报告 ────────────────────────────────────────────────
def test_build_report_shape():
    rep = R.build_report(
        [_stream("cam_a", C.WARN, [_range(1, 2)])],
        [],
        episode_id="ep1", project="proj",
        modalities={"actual": ["mono_rgb"]},
        channels={"rgb", "time"},
        ruleset_revision="abc123",
        template_source="workflow",
    )
    assert rep["schema_version"] == R.SCHEMA_VERSION
    assert rep["episode_id"] == "ep1"
    assert rep["ruleset_revision"] == "abc123"
    assert rep["template_source"] == "workflow"
    assert rep["episode"]["status"] == C.WARN
    assert rep["passed"] is False          # 只有 PASS 算通过,WARN 不算
    assert len(rep["ranges"]) == 1
    assert len(rep["streams"]) == 1
    assert rep["training_use"]


def test_build_report_passed_only_when_all_clean():
    rep = R.build_report([_stream("a", C.PASS)], [], channels={"rgb", "time"})
    assert rep["episode"]["status"] == C.PASS
    assert rep["passed"] is True


def test_warn_is_not_passed():
    """WARN 不算通过 —— 这是"卡在人工审核"的判定依据,必须显式锁住。"""
    assert R.passed_for(C.WARN) is False
    assert R.passed_for(C.PASS) is True


def test_build_report_pulls_channels_from_streams():
    """没显式传 channels 时,从各条流自己声明的信道汇总。"""
    rep = R.build_report([_stream("a", C.PASS, channels=("rgb", "time", "tactile"))])
    rows = {row["use"]: row for row in rep["training_use"]}
    assert rows["vision_tactile_joint"]["status"] == C.PASS


def test_empty_report_is_fail_closed():
    """没有数据 ≠ 数据合格。"""
    rep = R.empty_report(episode_id="ep1", reason="no_data")
    assert rep["passed"] is False
    assert rep["episode"]["status"] == C.ERROR
    assert rep["reason"] == "no_data"
    assert rep["streams"] == []


if __name__ == "__main__":
    import inspect
    import tempfile

    _failures = 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            if "tmp_path" in inspect.signature(_fn).parameters:
                with tempfile.TemporaryDirectory() as _tmp:
                    _fn(Path(_tmp))
            else:
                _fn()
            print(f"PASS {_name}")
        except Exception as _exc:  # noqa: BLE001
            _failures += 1
            print(f"FAIL {_name}: {_exc}")
    raise SystemExit(1 if _failures else 0)
