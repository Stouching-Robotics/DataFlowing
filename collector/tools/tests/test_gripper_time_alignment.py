#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨模态时间戳对齐回归：力/RGB 各自的采集时刻落盘 + 按时间取最近邻。

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_gripper_time_alignment.py

背景：同一行里的 RGB 帧与力样本不是同时刻采集的——RGB 走外部队列取
**队头最旧帧**、力走 latest-wins 单槽取**当下最新**样本，所以「行号相同」
≠「时刻相同」，力恒领先 RGB 且领先量随录制时长增长。修法是让每一行自描述：
两条支路各自落盘采集时刻（宿主单调钟纳秒），下游按时间重采样。

覆盖:
  1. hardware_ns 是 64 位（>2^31）——32 位截断是这条链路上的头号坑
  2. 力/矩阵的 _ns 列落盘，值与写入时逐位相同（int64，不被截断）
  3. 缺失 _ns 时不建列（稀疏契约；老调用方不传 capture_ns 仍可用）
  4. features 里声明 encoding=host_monotonic_ns，下游知道怎么解读
  5. Pipeline 两个 write_tactile_* 把 ns 放进对应快照键
  6. nearest_index 边界：空/单点/精确命中/等距取左
  7. 稳态漂移：力领先 3 帧且逐秒增长 → 范围内残差 ≤ 半个行周期
  8. 段首陈旧帧：早于任何力样本的帧必须被判越界（下游据此丢弃）
  9. 稀疏力样本：force_ns==0 的行不得参与配对（0 是"本行无样本"不是时刻）
 10. 修复前的旧 episode 被明确拒绝，不拿行号硬凑
退出码 0 = 全部通过。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import numpy as np                                              # noqa: E402

from core.egodata_writer import EgoDataWriter                    # noqa: E402
from scripts.align_modalities import (                           # noqa: E402
    align_episode,
    load_ns_columns,
    nearest_index,
)

OUT_ROOT = "/tmp/gripper_time_alignment_test"
NS_BASE = 287_000_000_000_000        # 开机纳秒量级：远超 2^31，验得出截断
_PERIOD = 1.0 / 30.0
_HALF = _PERIOD * 1e9 / 2            # 半个行周期 ≈ 16.67 ms ≈ 0.5 帧

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def _find_episode(root: str) -> str:
    """定位录制落盘的 data parquet（writer 会在 root 下建 session/ 层）。"""
    found = glob.glob(os.path.join(root, "**", "data", "chunk-*",
                                   "episode-*.parquet"), recursive=True)
    return found[0] if found else ""


def _start(root: str, writer: EgoDataWriter) -> bool:
    return writer.start_episode(
        root, {"gripper_rgb": (960, 1280)}, 30.0, sensors=[],
        device_ids=["gripper:1"],
        devices=[{"key": "gripper:1", "kind": "gripper", "name": "夹爪",
                  "slots": ["gripper_rgb"]}],
        calibrations={})


# ── 1-4. 落盘链路 ────────────────────────────────────────────

def test_writer_columns():
    shutil.rmtree(OUT_ROOT, ignore_errors=True)
    os.makedirs(OUT_ROOT, exist_ok=True)

    w = EgoDataWriter()
    check("start_episode 成功", _start(OUT_ROOT, w),
          f"episode_index={w.episode_index}")

    # 三条支路各记各的时刻：RGB 落后力 3 帧（队列滞留），矩阵走独立泵线程
    # 另有约 1ms 偏差——真实链路里它们本来就不等，落盘必须逐位保真。
    rows = 6
    rgb_ns, force_ns, matrix_ns = [], [], []
    for i in range(rows):
        f_ns = NS_BASE + i * 33_333_333
        r_ns = f_ns - 3 * 33_333_333
        m_ns = f_ns + 1_000_000
        rgb_ns.append(r_ns)
        force_ns.append(f_ns)
        matrix_ns.append(m_ns)
        w.write_frame_row(
            i, i / 30.0,
            hardware_ns=r_ns,
            gripper={
                "gripper_left_force": [1.0, 2.0, 3.0],
                "gripper_left_force_ns": f_ns,
                "gripper_left_force_matrix": [0] * (250 * 750),
                "gripper_left_force_matrix_ns": m_ns,
            })
    w.end_episode()
    task_dir = w.task_dir

    import pyarrow
    import pyarrow.parquet as pq
    path = _find_episode(OUT_ROOT)
    check("data parquet 落盘", bool(path), path)
    if not path:
        return None
    t = pq.read_table(path)

    # hardware_ns 必须是 64 位。修复前它是 pyqtSignal(int) 截断后的 int32，
    # 值域压在 ±2^31 内——这条断言就是那个 bug 的检测器（低于 2^31 的值
    # 截断与否看不出来，所以基时刻特意选在开机纳秒量级）。
    hw = np.asarray(t.column("hardware_ns").to_pylist(), dtype=np.int64)
    check("hardware_ns 未被 int32 截断（>2^31）",
          bool(np.all(hw > (1 << 31))), f"首值={hw[0]}")

    for col, want in (("observation.gripper_left_force_ns", force_ns),
                      ("observation.gripper_left_force_matrix_ns", matrix_ns)):
        check(f"{col} 列存在", col in t.schema.names)
        if col not in t.schema.names:
            continue
        got = np.asarray(t.column(col).to_pylist(), dtype=np.int64)
        check(f"{col} 值逐位相同且为 int64",
              bool(np.array_equal(got, np.asarray(want, dtype=np.int64)))
              and t.schema.field(col).type.equals(pyarrow.int64()),
              f"{got[:3].tolist()}")

    with open(os.path.join(task_dir, "meta", "info.json"),
              "r", encoding="utf-8") as f:
        info = json.load(f)
    feat = info.get("features", {})
    for col in ("observation.gripper_left_force_ns",
                "observation.gripper_left_force_matrix_ns"):
        spec = feat.get(col) or {}
        check(f"features 声明 {col}",
              spec.get("dtype") == "int64"
              and spec.get("shape") == [1]
              and spec.get("encoding") == "host_monotonic_ns",
              repr(spec))

    return path


def test_sparse_contract():
    """不传 capture_ns 时不建列——老调用方与稀疏契约都不能破。"""
    root = OUT_ROOT + "_sparse"
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    w = EgoDataWriter()
    w.start_episode(root, {"gripper_rgb": (960, 1280)}, 30.0, sensors=[],
                    device_ids=[], devices=[], calibrations={})
    for i in range(3):
        w.write_frame_row(i, i / 30.0, hardware_ns=NS_BASE + i * 33_333_333,
                          gripper={"gripper_left_force": [1.0, 2.0, 3.0]})
    w.end_episode()

    import pyarrow.parquet as pq
    path = _find_episode(root)
    check("data parquet 落盘", bool(path), path)
    if not path:
        return
    # 只看 observation.gripper 段：顶层 hardware_ns 本身就以 _ns 结尾，
    # 不能拿它当"建了 _ns 列"的证据
    names = [n for n in pq.read_schema(path).names
             if n.startswith("observation.gripper")]
    check("用力列仍在", "observation.gripper_left_force" in names, str(names))
    check("无 capture_ns 时不建 _ns 列",
          not any(n.endswith("_ns") for n in names), str(sorted(names)))


def test_pipeline_snapshot_keys():
    """Pipeline 的两个 write_tactile_* 要把 ns 放进对应的快照键。"""
    from core.pipeline import CameraPipeline

    pip = CameraPipeline(OUT_ROOT + "_pip")
    pip._writer = object()          # 只为过 _put_gripper_snapshot 的非空判断
    pip._recording = True

    pip.write_tactile_force("left", [1.0, 2.0, 3.0], capture_ns=NS_BASE + 7)
    pip.write_tactile_force_matrix("left", [0, 1], capture_ns=NS_BASE + 9)
    pip.write_tactile_force("right", [4.0, 5.0, 6.0])       # 不传 ns
    snap = pip._pop_gripper_snapshots()

    check("force_ns 键与值正确",
          snap.get("gripper_left_force_ns") == NS_BASE + 7,
          repr(snap.get("gripper_left_force_ns")))
    check("force_matrix_ns 键与值正确",
          snap.get("gripper_left_force_matrix_ns") == NS_BASE + 9,
          repr(snap.get("gripper_left_force_matrix_ns")))
    check("不传 capture_ns 时不写该键（老调用方兼容）",
          "gripper_right_force_ns" not in snap,
          str(sorted(snap)))

    pip._writer = None
    pip._recording = False


# ── 6-9. 对齐数学 ────────────────────────────────────────────

def test_nearest_index_edges():
    check("空 src 报错", _raises(ValueError, lambda: nearest_index([], [1])),
          "nearest_index([], [1])")
    check("单点 src → 全 0",
          nearest_index([5], [0, 4, 9]).tolist() == [0, 0, 0])
    check("精确命中取自身",
          nearest_index([10, 20, 30], [20]).tolist() == [1])
    # 15 与 10/20 等距 → 取左（更早的样本，对因果友好）
    check("等距取左", nearest_index([10, 20], [15]).tolist() == [0])
    check("超出右端夹到末点",
          nearest_index([10, 20], [99]).tolist() == [1])
    check("超出左端夹到首点",
          nearest_index([10, 20], [-99]).tolist() == [0])


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def _scenario(rows, lag_frames_at):
    """合成一段：力在写行时刻采集，RGB 是 lag 帧前的画面。

    lag_frames_at(k) 给出第 k 行的滞后帧数。真实链路里它随录制时长增长
    （RGB 源实测 30.84 fps vs 写线程 30 fps，队列每秒多积 ~0.84 帧，行内
    看就是滞后每秒多 ~1 帧），段首还可能叠一次启动窗口的陈旧帧积压。
    """
    force_ns, rgb_ns = [], []
    for k in range(rows):
        f = NS_BASE + int(k * _PERIOD * 1e9)
        force_ns.append(f)
        rgb_ns.append(f - int(lag_frames_at(k) * _PERIOD * 1e9))
    return (np.asarray(rgb_ns, dtype=np.int64),
            np.asarray(force_ns, dtype=np.int64))


def _align(rgb_ns, force_ns):
    """返回 (配到的力行号, 残差, "在力样本时间范围内"掩码)。

    落在范围外的帧（段首早于第一个力样本的陈旧帧、段尾晚于最后一个样本的
    帧）只能夹到端点，残差天然是"离端点多远"，不受半个行周期约束——必须
    分开统计，否则一个段首陈旧帧就能把整段残差的最大值带偏。
    """
    picked = nearest_index(force_ns, rgb_ns)
    residual = np.abs(rgb_ns - force_ns[picked])
    inside = (rgb_ns >= force_ns[0]) & (rgb_ns <= force_ns[-1])
    return picked, residual, inside


def test_alignment_steady_state():
    """力领先 3 帧起、每 30 行多滞后 1 帧（≈1 帧/秒）→ 残差 ≤ 半周期。"""
    rows = 300
    rgb_ns, force_ns = _scenario(rows, lambda k: 3.0 + k / 30.0)
    picked, residual, inside = _align(rgb_ns, force_ns)

    check("范围内的帧占绝大多数", int(inside.sum()) >= rows - 10,
          f"{int(inside.sum())}/{rows}")
    check("范围内残差 ≤ 半个行周期（≈0.5 帧）",
          bool(residual[inside].max() <= _HALF + 1),
          f"中位 {np.median(residual[inside]) / 1e6:.2f} ms / 最大 "
          f"{residual[inside].max() / 1e6:.2f} ms（上界 {_HALF / 1e6:.2f} ms）")

    shift = picked - np.arange(rows)
    check("行号偏移确实在增长（说明按行号配对是错的）",
          shift[-1] < shift[0] - 5,
          f"首行 {shift[0]:+d} → 末行 {shift[-1]:+d}")


def test_alignment_start_stale_frames():
    """段首陈旧帧：启动窗口积压的旧帧早于任何力样本，必须被判越界。

    这些帧录的是开机之前（上一段残留）的画面，力侧根本不存在对应时刻的
    样本——夹到第 0 行是唯一可行的动作，但下游必须知道那是编排不是对齐。
    """
    rows = 300
    stale = 5
    rgb_ns, force_ns = _scenario(
        rows, lambda k: 60.0 if k < stale else 3.0)
    picked, residual, inside = _align(rgb_ns, force_ns)
    outside = np.flatnonzero(~inside)

    check("越界帧数正好等于陈旧帧数", outside.size == stale,
          f"越界行号 {outside.tolist()}")
    check("越界帧全部夹在第 0 行（不越界则真去配了别的行）",
          bool(np.all(picked[outside] == 0)) if outside.size else False,
          f"配到 {picked[outside].tolist() if outside.size else []}")
    check("排空后残差回到 ≤ 半个行周期",
          bool(residual[inside].max() <= _HALF + 1),
          f"范围内 {int(inside.sum())}/{rows} 帧，最大 "
          f"{residual[inside].max() / 1e6:.2f} ms")


def test_alignment_sparse_force_samples():
    """力样本稀疏时（多数行 force_ns==0）0 值行不得参与配对。

    0 的含义是"本行窗口内没有新力样本"，不是"时刻为 0"。拿它当时间戳会
    把一大批视频帧吸到第 0 行上去——这是本工具最容易被误用的地方。
    """
    rows, stride = 60, 3
    rgb_ns, force_ns = _scenario(rows, lambda k: 3.0 + k / 60.0)
    sparse = np.where(np.arange(rows) % stride == 0, force_ns, 0)

    valid = np.flatnonzero(sparse > 0)
    picked = valid[nearest_index(sparse[valid], rgb_ns)]
    residual = np.abs(rgb_ns - force_ns[picked])
    inside = (rgb_ns >= force_ns[0]) & (rgb_ns <= force_ns[-1])
    # 上界随**样本间隔**走，不是行周期：样本每 3 行才有一个 → 50 ms
    half_sample = stride * _PERIOD * 1e9 / 2

    check("配到的行全部是真实样本行",
          bool(np.all(sparse[picked] > 0)),
          f"行号 {picked[:6].tolist()}…")
    check("稀疏下残差 ≤ 半个样本间隔（间隔 ×3 → 上界也 ×3）",
          bool(residual[inside].max() <= half_sample + 1),
          f"范围内最大 {residual[inside].max() / 1e6:.2f} ms"
          f"（上界 {half_sample / 1e6:.2f} ms）；越界 "
          f"{int((~inside).sum())} 帧全在段首")


# ── 10. 旧数据必须被明确拒绝 ─────────────────────────────────

def test_rejects_pre_fix_episode():
    legacy = os.path.join(
        REPO_ROOT, "data", "recordings", "UMIGripper_Action_AI",
        "data", "chunk-000", "episode-076.parquet")
    if not os.path.isfile(legacy):
        print("  [SKIP] 旧 episode 不在本机，跳过")
        return
    rgb_ns, force_ns, diag = load_ns_columns(legacy)
    check("修复前的 episode 被判为不可对齐", rgb_ns is None,
          diag.get("reason", ""))
    check("拒绝理由点明是录于修复之前（或 32 位截断）",
          "v1.3.3 之前" in (diag.get("reason") or "")
          or "截断" in (diag.get("reason") or ""),
          diag.get("reason", ""))
    check("align_episode 同样拒绝而不是硬凑",
          _raises(ValueError, lambda: align_episode(legacy)),
          "align_episode(legacy)")


# ── 11. 真落盘的 episode 走一遍 align_episode（端到端） ──────

def test_align_episode_end_to_end():
    """自建一段「力稀疏 + RGB 滞后」的 episode，整个 align_episode 跑通。"""
    root = OUT_ROOT + "_e2e"
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    rows, stride = 90, 3
    rgb_all = []
    w = EgoDataWriter()
    w.start_episode(root, {"gripper_rgb": (960, 1280)}, 30.0, sensors=[],
                    device_ids=[], devices=[], calibrations={})
    for k in range(rows):
        lag = 3.0 + k / 30.0
        rgb = NS_BASE + int(k * _PERIOD * 1e9) - int(lag * _PERIOD * 1e9)
        rgb_all.append(rgb)
        gripper = {}
        if k % stride == 0:                    # 每 3 行才有一次力样本
            gripper = {
                "gripper_left_force": [1.0, 2.0, 3.0],
                "gripper_left_force_ns": NS_BASE + int(k * _PERIOD * 1e9),
                "gripper_left_force_matrix": [0] * (250 * 750),
                "gripper_left_force_matrix_ns":
                    NS_BASE + int(k * _PERIOD * 1e9) + 1_000_000,
            }
        w.write_frame_row(k, k / 30.0, hardware_ns=rgb, gripper=gripper)
    w.end_episode()

    path = _find_episode(root)
    check("e2e parquet 落盘", bool(path), path)
    if not path:
        return

    mapping, residual = align_episode(path, side="left", which="matrix")
    check("align_episode 返回的行号全是真实样本行",
          bool(np.all(mapping % stride == 0)),
          f"{mapping[:8].tolist()}…")

    # "越界"按**时间范围**判定，与脚本 `_fmt_stats` 报给下游的口径一致：
    # 帧早于第一个力样本（或晚于最后一个）就是没有对应样本，只能夹端点。
    # 这和"残差超过半个样本间隔"不是一回事——后者里还有只是离得远的帧。
    valid_ns = np.array([NS_BASE + int(k * _PERIOD * 1e9)
                         for k in range(0, rows, stride)], dtype=np.int64)
    rgb_ns = np.asarray(rgb_all, dtype=np.int64)
    outside = (rgb_ns < valid_ns[0]) | (rgb_ns > valid_ns[-1])

    check("越界帧只在段首（早于第一个力样本，只能夹到它）",
          bool(np.all(np.flatnonzero(outside) < 5))
          and bool(np.all(mapping[outside] == 0)),
          f"越界 {int(outside.sum())} 帧：行号 "
          f"{np.flatnonzero(outside).tolist()} → 配到行 "
          f"{mapping[outside].tolist()}")
    check("范围内残差 ≤ 半个样本间隔（每 3 行一个样本 → 50 ms）",
          bool(np.all(residual[~outside] <= stride * _PERIOD * 1e9 / 2 + 1)),
          f"范围内 {int((~outside).sum())}/{rows} 帧，最大 "
          f"{residual[~outside].max() / 1e6:.2f} ms")


def main() -> int:
    print("[1-4] 落盘链路：力/矩阵采集时刻列")
    test_writer_columns()
    print("[3b] 稀疏契约：不传 capture_ns 不建列")
    test_sparse_contract()
    print("[5] Pipeline 快照键")
    test_pipeline_snapshot_keys()
    print("[6] nearest_index 边界")
    test_nearest_index_edges()
    print("[7] 稳态漂移对齐")
    test_alignment_steady_state()
    print("[8] 段首陈旧帧越界")
    test_alignment_start_stale_frames()
    print("[9] 稀疏力样本")
    test_alignment_sparse_force_samples()
    print("[10] 旧数据拒绝")
    test_rejects_pre_fix_episode()
    print("[11] align_episode 端到端")
    test_align_episode_end_to_end()

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 — {_FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
