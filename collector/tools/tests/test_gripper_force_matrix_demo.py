#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gripper_force_matrix_demo 离屏自检（无 pytest 依赖，直接运行）:

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_gripper_force_matrix_demo.py

覆盖:
  1. 解码/热力图与主程序口径一致（对比 core.gripper.tactile_process_worker）
  2. 合成 episode：稀疏列保持上一帧、rig1/rig2 发现、默认选样本多的夹爪、
     力列缺失时回退三平面求和
  3. GUI 路径：加载 / 渲染 / 跳帧 / 播放节拍 / 切夹爪 / 显示增益 / 目录加载
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
    """与 ui/main_window.encode_gripper_force_matrix 同一口径（int16 规格）。"""
    flat = np.asarray(matrix, np.float32).reshape(matrix.shape[0], -1)
    q = np.clip(flat, -32767.0, 32767.0).astype(np.int16)
    diff = np.empty_like(q)
    diff[:, 0] = q[:, 0]
    diff[:, 1:] = q[:, 1:] - q[:, :-1]
    return np.frombuffer(diff.tobytes(), dtype=np.int16).tolist()


def encode_matrix_f32(matrix: np.ndarray) -> list:
    """同一编码器的 float32 规格分支：原值直存、扁平、元素为 float。"""
    return np.asarray(matrix, np.float32).reshape(-1).tolist()


def encode_matrix_scaled(matrix: np.ndarray, scale: int) -> list:
    """同一编码器的 int16×N 定标分支：×N → rint → 行差分，元素仍是 int。

    注意元素类型与未定标的 int16 档完全一样 —— 这正是 demo 必须去
    info.json 读 scale 的原因，靠数据本身分辨不出来。
    """
    flat = np.asarray(matrix, np.float32).reshape(matrix.shape[0], -1)
    q = np.clip(np.rint(flat * np.float32(scale)),
                -32767.0, 32767.0).astype(np.int16)
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


def make_synthetic_episode(root: str, n: int = 12, float32: bool = False,
                           name: str = "synthetic_gripper_task",
                           scale: int = 1, info_scale=None,
                           meta_scale=None) -> str:
    """合成一份双夹爪 episode：

    - rig1 左侧：矩阵只在第 2 帧有新样本，第 3~5 帧为空（测保持上一帧）；
      第 6 帧新样本。力列缺失（测回退三平面求和）
    - rig1 右侧：每帧都有样本 + 力列（测力列优先）
    - rig2 左侧：只有 1 个样本（测默认选样本多的夹爪 = rig1）

    float32=True 时三列都按 float32 规格写（原值直存、无量化），
    取值用带小数的值以证明小数位真的被保留。
    scale>1 时按 int16×scale 写（列类型仍是 int16）—— 倍率写哪儿由下面
    两个参数决定，两者不一致的用例见 [2e]：

    - `info_scale`：写进任务级 info.json features.scale（回退来源），
      None = 与 `scale` 相同；
    - `meta_scale`：非 None 时另写一份 meta/episodes 行（每段权威来源），
      值就是它 —— 与 `scale` 不一致即模拟「同任务里换过档位，info.json
      被后一段顶掉」的现场。
    """
    task = os.path.join(root, name)
    os.makedirs(os.path.join(task, "data", "chunk-000"), exist_ok=True)
    os.makedirs(os.path.join(task, "meta"), exist_ok=True)

    enc = encode_matrix_f32 if float32 else (
        (lambda m: encode_matrix_scaled(m, scale)) if scale > 1
        else encode_matrix)
    item = pa.float32() if float32 else pa.int16()
    cols = ["observation.gripper_left_force_matrix",
            "observation.gripper_right_force_matrix",
            "observation.gripper_2_gripper_left_force_matrix"]
    if info_scale is None:
        info_scale = scale
    features = {c: {"dtype": "float32" if float32 else "int16",
                    "shape": [250, 250, 3],
                    "encoding": ("row_flat_raw" if float32
                                 else "row_diff_quantized"),
                    "scale": 1 if float32 else info_scale}
                for c in cols}
    with open(os.path.join(task, "meta", "info.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"fps": 30, "features": features}, fh)

    if meta_scale is not None:
        # 走 writer 自己的行构造器，保证与生产 schema 同型（少一列就会
        # 拼不成一张表，这里正好把两端的契约钉在一起）
        from core.egodata_writer import _episode_rows_table
        meta_dir = os.path.join(task, "meta", "episodes", "chunk-000")
        os.makedirs(meta_dir, exist_ok=True)
        specs = {c: {"dtype": "float32" if float32 else "int16",
                     "shape": [250, 250, 3],
                     "encoding": ("row_flat_raw" if float32
                                  else "row_diff_quantized"),
                     "scale": 1 if float32 else meta_scale}
                 for c in cols}
        pq.write_table(_episode_rows_table([{
            "episode_index": 1, "task_index": 0, "start_frame_index": 0,
            "end_frame_index": n - 1, "length": n, "created_at": 0.0,
            "duration_sec": n / 30.0, "drop_stats": "{}",
            "video_codec": "{}", "calibration": "{}",
            "force_matrix_specs": json.dumps(specs),
        }]), os.path.join(meta_dir, "episode-000.parquet"))

    # float32 规格用带小数的幅值：int16 规格下 0.9 会整幅归零
    lv = (5.75, 9.125) if float32 else (5.0, 9.0)
    rv = (3.375, 0.875)

    empty = []
    left_rows = [empty, empty, enc(contact_matrix(lv[0])),
                 empty, empty, empty, enc(contact_matrix(lv[1]))]
    left_rows += [empty] * (n - len(left_rows))
    right_rows = [enc(contact_matrix(rv[0] + i * rv[1]))
                  for i in range(n)]
    rig2_rows = [empty, empty, empty, enc(contact_matrix(2.0))]
    rig2_rows += [empty] * (n - len(rig2_rows))
    right_force = [[float(i), -float(i) / 2, 100.0 + i] for i in range(n)]

    table = pa.table({
        "frame_index": pa.array(range(n), pa.int64()),
        "observation.gripper_left_force_matrix":
            pa.array(left_rows, pa.list_(item)),
        "observation.gripper_right_force_matrix":
            pa.array(right_rows, pa.list_(item)),
        "observation.gripper_right_force":
            pa.array(right_force, pa.list_(pa.float32(), 3)),
        "observation.gripper_2_gripper_left_force_matrix":
            pa.array(rig2_rows, pa.list_(item)),
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
    plane = rng.uniform(0, 30, (250, 250)).astype(np.float32)
    check("scale=50 即主程序固定量程",
          np.array_equal(mod.pressure_to_heatmap(plane, scale=50.0),
                         mod.pressure_to_heatmap(plane)))
    check("增益滑块对数映射往返",
          all(abs(mod._slider_to_gain(mod._gain_to_slider(g)) - g) < 0.05 * g
              for g in (8.0, 50.0, 128.0, 256.0)),
          f"×50 → 滑块 {mod._gain_to_slider(50.0)}")
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

        print("[1b] 主程序自适应显示量程（HeatmapScaleTracker）")
        idle = np.zeros((250, 250), np.float32)
        tracker = worker.HeatmapScaleTracker("bevel")
        floor_gain = 255.0 / tracker.floor
        check("无接触 → 增益取下限（255/FLOOR）",
              abs(tracker.scale(idle) - floor_gain) < 1e-6
              and abs(tracker.ref - tracker.floor) < 1e-9,
              f"×{floor_gain:.1f}（FLOOR={tracker.floor:g}）")
        sparse = idle.copy()
        sparse[10, 10] = 250.0             # 零星噪点 < MIN_PIXELS
        check("零星噪点不抬量程",
              abs(tracker.scale(sparse) - floor_gain) < 1e-6
              and abs(tracker.ref - tracker.floor) < 1e-9)
        contact = idle.copy()
        contact[100:120, 100:120] = 2.0    # 典型接触：p99 仍在下限之下
        check("典型接触（值 2 < FLOOR）不降增益",
              abs(tracker.scale(contact) - floor_gain) < 1e-6,
              f"ref={tracker.ref:.2f}")
        hard = idle.copy()
        hard[100:140, 100:140] = tracker.ceiling * 4   # 远超上限
        first = tracker.scale(hard)
        for _ in range(1200):              # τ≈8s@30fps，1200 帧 ≈ 5τ
            last = tracker.scale(hard)
        check("持续重压 → 参考值升到上限、增益随之下降",
              abs(tracker.ref - tracker.ceiling) < 0.2
              and abs(last - 255.0 / tracker.ceiling) < 0.5
              and first > last,
              f"×{first:.1f} → ×{last:.1f}（上限 {tracker.ceiling:g}）")
        for _ in range(2000):
            idle_gain = tracker.scale(idle)
        check("松开 → 参考值衰减回下限",
              abs(tracker.ref - tracker.floor) < 0.2
              and abs(idle_gain - floor_gain) < 0.5,
              f"ref={tracker.ref:.3f} ×{idle_gain:.1f}")

        class _FakeSensor:
            device_type = "bevel"

        fake = _FakeSensor()
        worker._HEATMAP_AUTO_CACHE["value"] = True
        gain = worker.heatmap_scale_for(fake, contact)
        check("heatmap_scale_for 把跟踪器挂在传感器上",
              abs(gain - floor_gain) < 1e-6
              and isinstance(getattr(fake, "_ksq_heatmap_scale", None),
                             worker.HeatmapScaleTracker))
        state = worker.snapshot_sensor_state(fake)
        check("_ksq_ 状态不进跨进程快照",
              "_ksq_heatmap_scale" not in state
              and not any(k.startswith("_ksq_heatmap") for k in state))
        worker._HEATMAP_AUTO_CACHE["value"] = False
        check("关闭自适应 → 回固定量程（None）",
              worker.heatmap_scale_for(fake, contact) is None)
        worker._HEATMAP_AUTO_CACHE.pop("value", None)
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

        print("[2b] 自适应量程")
        g_left = mod.auto_gain(left, episode.n_frames)
        g_right = mod.auto_gain(right, episode.n_frames)
        check("自适应增益落在钳位区间",
              all(8.0 <= g <= 128.0 for g in (g_left, g_right)),
              f"left=×{g_left:.1f} right=×{g_right:.1f}")
        check("力值大的那侧增益更小", g_right < g_left,
              f"left 峰值 9 → ×{g_left:.1f}，right 峰值 14 → ×{g_right:.1f}")
        empty_stream = mod.TactileStream(pa.table({
            "observation.gripper_left_force_matrix":
                pa.array([[] for _ in range(4)], pa.list_(pa.int16()))}),
            "", "left", 4)
        check("整段无样本 → 回退主程序量程",
              mod.auto_gain(empty_stream, 4) == 50.0)

        print("[2c] float32 规格 episode")
        fpath = make_synthetic_episode(tmp, float32=True,
                                       name="synthetic_gripper_f32")
        check("float32 列的元素类型 = float32",
              pq.read_schema(fpath).field(
                  "observation.gripper_left_force_matrix"
              ).type.value_type == pa.float32())
        fep = mod.TactileEpisode(fpath)
        fleft = fep.rigs[""]["left"]
        check("_list_column_to_numpy 不硬转 int16（小数位保留）",
              fleft.values.dtype == np.float32,
              f"values.dtype={fleft.values.dtype}")
        m = fleft.matrix(2)
        check("float32 矩阵解码形状/dtype",
              m is not None and m.shape == (250, 250, 3)
              and m.dtype == np.float32,
              f"{None if m is None else (m.shape, m.dtype)}")
        check("float32 矩阵是原值而非 cumsum（无差分）",
              m is not None and abs(float(m[120, 120, 2]) - 5.75) < 1e-6,
              f"中心 fz={None if m is None else float(m[120, 120, 2]):.4f}（期望 5.75）")
        check("float32 亚毫牛小数位保留（fx=5.75×0.25）",
              m is not None and abs(float(m[120, 120, 0]) - 1.4375) < 1e-6,
              f"中心 fx={None if m is None else float(m[120, 120, 0]):.4f}（期望 1.4375）")
        check("int16 与 float32 走同一解码入口、结果不同",
              not np.array_equal(
                  mod.decode_force_matrix(encode_matrix_f32(contact_matrix(5.75))),
                  mod.decode_force_matrix(encode_matrix(contact_matrix(5.75)))),
              "同一矩阵两种规格解码结果必须体现精度差异")
        fempty = mod.TactileStream(pa.table({
            "observation.gripper_left_force_matrix":
                pa.array([[] for _ in range(4)], pa.list_(pa.float32()))}),
            "", "left", 4)
        check("float32 空列 → 同样回退主程序量程",
              fempty.values.dtype == np.float32
              and mod.auto_gain(fempty, 4) == 50.0,
              f"dtype={fempty.values.dtype}")

        print("[2d] int16×N 定标档 episode（只有 info.json、无 meta 行）")
        for scale in (10, 100):
            spath = make_synthetic_episode(tmp, scale=scale,
                                           name=f"synthetic_gripper_x{scale}")
            stype = pq.read_schema(spath).field(
                "observation.gripper_left_force_matrix").type
            check(f"×{scale} 列类型仍是 list<int16>（与未定标档数据上无法区分）",
                  stype == pa.list_(pa.int16()), str(stype))
            sep = mod.TactileEpisode(spath)
            sleft = sep.rigs[""]["left"]
            check(f"×{scale} demo 从 info.json 读到倍率",
                  sleft.scale == scale, f"scale={sleft.scale}")
            sm = sleft.matrix(2)
            check(f"×{scale} 矩阵已 ÷{scale} 还原 mN（真值 5.0）",
                  sm is not None and abs(float(sm[120, 120, 2]) - 5.0) < 1e-4,
                  f"中心 fz={None if sm is None else float(sm[120, 120, 2]):.4f}")
            check(f"×{scale} 定标档返 float32（÷N 后需有小数值域）",
                  sm is not None and sm.dtype == np.float32,
                  f"dtype={None if sm is None else sm.dtype}")
            # 契约反证：不传 scale 就是静默放大 N 倍（画面形状还对，最难发现）
            miss = mod.decode_force_matrix(
                encode_matrix_scaled(contact_matrix(5.0), scale))
            check(f"×{scale} 漏传 scale → 数值放大 {scale} 倍（静默错）",
                  abs(float(miss[120, 120, 2]) - 5.0 * scale) < 1e-3,
                  f"漏读得 {float(miss[120, 120, 2]):.1f}，应为 5.0")

        print("[2e] 倍率来源优先级：每段 meta ＞ info.json ＞ 1")
        # ① 两处都有且不一致：同任务换过档位的现场——本段是 ×10，后来录的
        #    一段把任务级 info.json 顶成了 ×1000。必须听每段自己的。
        both = make_synthetic_episode(
            tmp, scale=10, info_scale=1000, meta_scale=10,
            name="synthetic_spec_meta_wins")
        bs = mod.TactileEpisode(both).rigs[""]["left"]
        check("meta 与 info.json 冲突 → 取 meta（×10，不被任务级覆盖）",
              bs.scale == 10, f"scale={bs.scale}（期望 10，info.json 说 1000）")
        bm = bs.matrix(2)
        check("冲突时数值按 meta 还原（真值 5.0 mN）",
              bm is not None and abs(float(bm[120, 120, 2]) - 5.0) < 1e-4,
              f"中心 fz={None if bm is None else float(bm[120, 120, 2]):.4f}")

        # ② meta 说 1（未定标/float32 档），info.json 说 1000：1 也是权威值，
        #    不能被别的段的档位顶掉——这条最容易写反。
        one = make_synthetic_episode(
            tmp, scale=1, info_scale=1000, meta_scale=1,
            name="synthetic_spec_meta_one")
        os_ = mod.TactileEpisode(one).rigs[""]["left"]
        check("meta 值为 1 也算权威 → 不退回 info.json 的 1000",
              os_.scale == 1, f"scale={os_.scale}（期望 1）")
        om = os_.matrix(2)
        check("此时按 ÷1 解（真值 5.0 mN，若按 1000 会缩成 0.005）",
              om is not None and abs(float(om[120, 120, 2]) - 5.0) < 1e-4,
              f"中心 fz={None if om is None else float(om[120, 120, 2]):.4f}")

        # ③ 只有 info.json（v1.3.x 之前的录制）：回退仍要工作
        legacy_path = make_synthetic_episode(       # 不给 meta_scale = 无 meta 行
            tmp, scale=100, name="synthetic_legacy_no_meta")
        ls = mod.TactileEpisode(legacy_path).rigs[""]["left"]
        check("无 meta 行 → 回退 info.json（老 episode 兼容）",
              ls.scale == 100, f"scale={ls.scale}（期望 100，来自 info.json）")
        lm = ls.matrix(2)
        check("回退路径数值同样还原（真值 5.0 mN）",
              lm is not None and abs(float(lm[120, 120, 2]) - 5.0) < 1e-4,
              f"中心 fz={None if lm is None else float(lm[120, 120, 2]):.4f}")

        # ④ 两处都没有 → 1（读取器返回空，调用链不崩）
        nometa = os.path.join(tmp, "synthetic_no_features")
        os.makedirs(os.path.join(nometa, "meta"), exist_ok=True)
        os.makedirs(os.path.join(nometa, "data", "chunk-000"), exist_ok=True)
        with open(os.path.join(nometa, "meta", "info.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"fps": 30}, fh)          # 老 episode：无 features 键
        nopath = os.path.join(nometa, "data", "chunk-000", "e.parquet")
        check("meta 行与 info.json 都没有 → 两个读者都返回空、不抛异常",
              mod._read_episode_matrix_specs(nopath) == {}
              and mod._read_info_features(nopath) == {})

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

        print("[3b] 显示增益")
        check("默认自适应且两侧都有增益",
              window.chk_auto.isChecked()
              and set(window._auto_gains) == {"left", "right"}
              and not window.slider_gain.isEnabled(),
              window.lbl_gain.text())
        window.chk_auto.setChecked(False)       # → toggled 信号走手动分支
        app.processEvents()
        check("关掉自适应 → 滑块可用、增益取自滑块",
              window.slider_gain.isEnabled()
              and abs(window._effective_gains()["left"] - window._gain) < 1e-6,
              window.lbl_gain.text())
        window.slider_gain.setValue(mod._gain_to_slider(50.0))
        app.processEvents()
        check("滑块拖到 ×50 = 主程序量程",
              abs(window._gain - 50.0) < 0.6, f"×{window._gain:.1f}")
        window.chk_auto.setChecked(True)
        app.processEvents()
        check("重新勾选自适应 → 增益回到按数据定标",
              abs(window._effective_gains()["left"] - g_left) < 1e-6,
              window.lbl_gain.text())
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
