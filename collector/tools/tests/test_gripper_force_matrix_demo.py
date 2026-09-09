#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gripper_force_matrix_demo 离屏自检（无 pytest 依赖，直接运行）:

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_gripper_force_matrix_demo.py

覆盖:
  1. 解码/热力图与主程序口径一致（对比 core.gripper.tactile_process_worker）
  2. 合成 episode：稀疏列保持上一帧、rig1/rig2 发现、默认选样本多的夹爪、
     力列缺失时回退三平面求和
  3. GUI 路径：加载 / 渲染 / 跳帧 / 播放节拍 / 切平面 / 切夹爪 / 目录加载
  4. 真实录制（data/recordings 下任一含力矩阵列的 episode）→ 加载并渲染
退出码 0 = 全部通过。
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEMO_PATH = os.path.join(REPO_ROOT, "tools", "demos",
                         "gripper_force_matrix_demo",
                         "gripper_force_matrix_demo.py")
sys.path.insert(0, REPO_ROOT)       # 供热力图口径对比导入 core.gripper

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def load_demo_module():
    spec = importlib.util.spec_from_file_location("gripper_force_matrix_demo",
                                                  DEMO_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def encode_matrix(matrix: np.ndarray) -> list:
    """与 ui/main_window.encode_gripper_force_matrix 同一口径。"""
    flat = np.asarray(matrix, np.float32).reshape(matrix.shape[0], -1)
    q = np.clip(flat, -32767.0, 32767.0).astype(np.int16)
    diff = np.empty_like(q)
    diff[:, 0] = q[:, 0]
    diff[:, 1:] = q[:, 1:] - q[:, :-1]
    return np.frombuffer(diff.tobytes(), dtype=np.int16).tolist()


def contact_matrix(value: float, center=(120, 120)) -> np.ndarray:
    """250×250×3：中心一个方块接触区，fz=value。"""
    matrix = np.zeros((250, 250, 3), np.float32)
    y, x = center
    matrix[y - 15:y + 15, x - 15:x + 15, 2] = value
    matrix[y - 15:y + 15, x - 15:x + 15, 0] = value * 0.25
    return matrix


def make_synthetic_episode(root: str, n: int = 12) -> str:
    """合成一份双夹爪 episode：

    - rig1 左侧：矩阵只在第 2 帧有新样本，第 3~5 帧为空（测保持上一帧）；
      第 6 帧新样本。力列缺失（测回退三平面求和）
    - rig1 右侧：每帧都有样本 + 力列（测力列优先）
    - rig2 左侧：只有 1 个样本（测默认选样本多的夹爪 = rig1）
    """
    task = os.path.join(root, "synthetic_gripper_task")
    os.makedirs(os.path.join(task, "data", "chunk-000"), exist_ok=True)
    os.makedirs(os.path.join(task, "meta"), exist_ok=True)
    with open(os.path.join(task, "meta", "info.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"fps": 30}, fh)

    empty = []
    left_rows = [empty, empty, encode_matrix(contact_matrix(5.0)),
                 empty, empty, empty, encode_matrix(contact_matrix(9.0))]
    left_rows += [empty] * (n - len(left_rows))
    right_rows = [encode_matrix(contact_matrix(3.0 + i))
                  for i in range(n)]
    rig2_rows = [empty, empty, empty, encode_matrix(contact_matrix(2.0))]
    rig2_rows += [empty] * (n - len(rig2_rows))
    right_force = [[float(i), -float(i) / 2, 100.0 + i] for i in range(n)]

    table = pa.table({
        "frame_index": pa.array(range(n), pa.int64()),
        "observation.gripper_left_force_matrix":
            pa.array(left_rows, pa.list_(pa.int16())),
        "observation.gripper_right_force_matrix":
            pa.array(right_rows, pa.list_(pa.int16())),
        "observation.gripper_right_force":
            pa.array(right_force, pa.list_(pa.float32(), 3)),
        "observation.gripper_2_gripper_left_force_matrix":
            pa.array(rig2_rows, pa.list_(pa.int16())),
    })
    path = os.path.join(task, "data", "chunk-000", "episode-000.parquet")
    pq.write_table(table, path)
    return path


def find_real_episode(recordings_dir: str):
    """data/recordings 下任一含 force_matrix 列的 episode。"""
    for path in sorted(glob.glob(os.path.join(
            recordings_dir, "**", "episode-*.parquet"), recursive=True)):
        try:
            names = pq.read_schema(path).names
        except Exception:
            continue
        if any(name.endswith("force_matrix") for name in names):
            return path
    return None


def main() -> int:
    mod = load_demo_module()
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    print("[1] 解码 / 热力图与主程序口径一致")
    rng = np.random.default_rng(0)
    raw = np.zeros((250, 250, 3), np.float32)
    raw[100:140, 110:150, 2] = rng.uniform(0, 40, (40, 40))
    raw[100:140, 110:150, 0] = rng.uniform(-20, 20, (40, 40))
    back = mod.decode_force_matrix(encode_matrix(raw))
    check("行差分解码往返", np.array_equal(back, raw.astype(np.int16)),
          f"dtype={back.dtype} shape={back.shape}")
    check("长度不符/空样本 → None",
          mod.decode_force_matrix(np.zeros(7, np.int16)) is None
          and mod.decode_force_matrix([]) is None)
    try:
        import core.gripper.tactile_process_worker as worker
        same = True
        for _ in range(3):
            plane = rng.uniform(0, 30, (250, 250)).astype(np.float32)
            if not np.array_equal(mod.pressure_to_heatmap(plane),
                                  worker.pressure_to_heatmap(plane)):
                same = False
                break
        check("热力图 = tactile_process_worker.pressure_to_heatmap", same)
    except Exception as exc:            # 主程序模块不可导入时不阻塞
        print(f"  [SKIP] 热力图对比（{exc}）")

    tmp = tempfile.mkdtemp(prefix="force_matrix_demo_")
    try:
        print("[2] 合成 episode 数据层")
        path = make_synthetic_episode(tmp)
        episode = mod.TactileEpisode(path)
        check("fps 从 meta/info.json 读取", episode.fps == 30.0,
              f"fps={episode.fps}")
        check("rig 发现", sorted(episode.rigs) == ["", "gripper_2_"],
              str(sorted(episode.rigs)))
        check("默认选样本最多的夹爪", episode.default_prefix() == "",
              repr(episode.default_prefix()))
        left = episode.rigs[""]["left"]
        check("稀疏列保持上一帧",
              left.matrix(2) is not None and left.matrix(4) is not None
              and np.array_equal(left.matrix(2), left.matrix(4)),
              "第 2 帧样本沿用到第 4 帧")
        check("首样本之前无数据",
              left.matrix(0) is None and left.matrix(1) is None)
        check("新样本覆盖旧值",
              not np.array_equal(left.matrix(2), left.matrix(6)),
              "第 6 帧新样本")
        check("力列缺失 → 三平面求和回退",
              left.force is None
              and left.force_at(2) == tuple(
                  float(v) for v in left.matrix(2).astype(
                      np.float32).sum(axis=(0, 1))))
        right = episode.rigs[""]["right"]
        check("力列优先于矩阵求和",
              right.force_at(5) == (5.0, -2.5, 105.0),
              str(right.force_at(5)))

        print("[3] GUI 路径")
        window = mod.DemoWindow()
        window.show()               # 离屏下 isVisible/isHidden 需先 show
        window.load(path)
        app.processEvents()
        check("加载后帧数/滑条", window.data.n_frames == 12
              and window.slider.maximum() == 11)
        check("rig 下拉两项", window.combo_rig.count() == 2,
              window.combo_rig.currentText())
        check("面板可见（两侧都有列）",
              all(panel.isVisible() for panel in window.panels.values()))
        window.render_frame(2)
        app.processEvents()
        check("渲染出热力图（非占位）",
              window.panels["left"].view._pixmap is not None)
        held = window.panels["left"].view._pixmap
        window.render_frame(0)      # 第 0 帧无样本 → 保持第 2 帧画面
        app.processEvents()
        check("无数据帧保持上一帧画面",
              window.panels["left"].view._pixmap is held and held is not None)
        window.render_frame(6)
        window._on_rig_chosen(1)                    # 夹爪 2
        app.processEvents()
        check("切夹爪：左侧有列、右侧面板隐藏",
              window._prefix == "gripper_2_"
              and window.panels["left"].isVisible()
              and window.panels["right"].isHidden())
        window._on_rig_chosen(0)
        start = window.idx
        window.toggle_play()
        check("播放启动", window.playing and window.timer.isActive())
        window.timer.timeout.emit()
        window.timer.timeout.emit()
        app.processEvents()
        check("播放推进帧号", window.idx == start + 2,
              f"{start} → {window.idx}")
        window.toggle_play()
        check("暂停停止计时器",
              not window.playing and not window.timer.isActive())
        window.seek(11)
        window.timer.timeout.emit()                 # 循环默认开
        app.processEvents()
        check("循环回卷", window.idx == 0, f"idx={window.idx}")
        window.chk_loop.setChecked(False)
        window.seek(11)
        window.timer.timeout.emit()
        app.processEvents()
        check("关循环在尾帧停住",
              window.idx == 11 and not window.timer.isActive())
        window.close()

        print("[4] 目录加载")
        window = mod.DemoWindow()
        window.load(os.path.dirname(path))
        app.processEvents()
        check("目录 → episode 下拉", window.combo_episode.count() == 1
              and window.data is not None)
        window.close()

        print("[5] 真实录制")
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        real = find_real_episode(os.path.join(repo, "data", "recordings"))
        if real:
            episode = mod.TactileEpisode(real)
            prefix = episode.default_prefix()
            sides = episode.rigs[prefix]
            peaks = {}
            for side, stream in sides.items():
                forces = [stream.force_at(i) or (0, 0, 0)
                          for i in range(episode.n_frames)]
                peaks[side] = int(np.argmax([f[2] for f in forces]))
            print(f"  真实 episode: {os.path.basename(real)} "
                  f"n={episode.n_frames} rigs={sorted(episode.rigs)} "
                  f"默认={prefix!r} 峰值帧={peaks}")
            window = mod.DemoWindow()
            window.load(real)
            app.processEvents()
            for side, peak in peaks.items():
                window.render_frame(peak)
                app.processEvents()
                check(f"真实数据渲染 {side} 峰值帧",
                      window.panels[side].view._pixmap is not None)
            check("真实数据内存占用在预期内",
                  episode.matrix_bytes() < 2_000_000_000,
                  f"{episode.matrix_bytes() / 1e6:.0f} MB")
            window.close()
        else:
            print("  [SKIP] data/recordings 下无含力矩阵列的 episode")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 — {_FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
