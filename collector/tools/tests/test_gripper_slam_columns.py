#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SLAM 落盘契约自检：只落 slam_trajectory，不再落 slam_pose（2026-09-10 定案）。

    QT_QPA_PLATFORM=offscreen venv/bin/python \
        tools/tests/test_gripper_slam_columns.py

背景：slam_pose 是「每帧一个位姿」的定长 list<float32>[7] 状态列，但位姿流
是 20/20/60ms 突发、parquet 行是 1/30s 均匀网格 —— 实测 26.5% 的行拿不到
位姿被填成 [0]*7，下游会把零当真实位姿读。同一回调里已经写入的
slam_trajectory（变长、每点带真实时间戳）信息严格更多，故停止落 slam_pose。

覆盖:
  1. 喂 slam_pose / gripper_2_slam_pose → 静默忽略：不建列、不写 features
  2. slam_trajectory → 列存在、类型 list<float64>、空行是 []、features 带
     encoding=flat_points_8_txyz_qxyzw、点值逐位往返
  3. 双 rig 前缀命名空间互不干扰
  4. 删分支后 state / force / force_matrix 仍各按原类型落盘（没被 else 误吞）
  5. 全空 episode（一帧夹爪数据都没有）不建任何夹爪列，且不抛异常
  6. slam_trajectory_ns → 类型必须是 list<int64> 而非标量 int64（钉死
     「专用分支排在通用 _ns 之前」）、与 slam_trajectory 逐行等长、
     features dtype/shape/encoding
  7. pipeline 侧 write_slam_trajectory/_pop_gripper_snapshots：时刻与点
     锁步、缺戳补 0 不跳过、弹出后清空、全程无戳（旧 native 二进制）
     不建 _ns 列
退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from core.egodata_writer import EgoDataWriter                # noqa: E402

POSE7 = [1.5, -2.25, 0.125, 0.0, 0.0, 0.0, 1.0]
# 一个双点窗口：[t,x,y,z,qx,qy,qz,qw] × 2
TRAJ = [10.0, 1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 1.0,
        10.02, 1.1, 2.1, 3.1, 0.0, 0.0, 0.0, 1.0]

_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def build(task_dir: str, frames) -> EgoDataWriter:
    """frames[i] = 第 i 帧的 gripper dict（None = 该帧不传 gripper）。"""
    writer = EgoDataWriter()
    writer._task_dir = task_dir
    writer._episode_index = 0
    for i, gripper in enumerate(frames):
        writer.write_frame_row(i, i / 30.0, gripper=gripper or {})
    return writer


def read_schema(task_dir: str):
    return pq.read_schema(
        os.path.join(task_dir, "data", "chunk-000", "episode-000.parquet"))


def read_col(task_dir: str, column: str):
    return pq.read_table(
        os.path.join(task_dir, "data", "chunk-000", "episode-000.parquet"),
        columns=[column]).column(column).to_pylist()


def read_features(task_dir: str) -> dict:
    path = os.path.join(task_dir, "meta", "info.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["features"]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="slam_columns_")
    try:
        print("[1] slam_pose 已停落：静默忽略，不建列也不写 features")
        # 复现历史写入端：即便某处仍在喂 slam_pose 键，也不该落盘
        w = build(os.path.join(tmp, "nopose"), [
            {"slam_pose": POSE7, "gripper_2_slam_pose": POSE7},
            {"slam_pose": POSE7},
            {},                                   # 全空帧也不能炸
        ])
        check("slam_pose 未进 _present_gripper",
              "slam_pose" not in w._present_gripper
              and "gripper_2_slam_pose" not in w._present_gripper,
              str(sorted(w._present_gripper)))
        w._write_data_parquet()
        w._write_info_json()
        schema = read_schema(os.path.join(tmp, "nopose"))
        check("parquet 无 observation.slam_pose 列",
              schema.get_field_index("observation.slam_pose") < 0,
              str(schema.names))
        check("parquet 无 observation.gripper_2_slam_pose 列",
              schema.get_field_index("observation.gripper_2_slam_pose") < 0)
        feats = read_features(os.path.join(tmp, "nopose"))
        check("features 无 observation.slam_pose",
              "observation.slam_pose" not in feats, str(sorted(feats)))
        check("features 无 observation.gripper_2_slam_pose",
              "observation.gripper_2_slam_pose" not in feats)

        print("[2] slam_trajectory 是唯一 SLAM 落盘形态")
        w2 = build(os.path.join(tmp, "traj"), [
            {"slam_trajectory": TRAJ},
            {},                                   # 空窗口 → 该行 []
            {"slam_trajectory": TRAJ[:8]},        # 单点窗口
        ])
        w2._write_data_parquet()
        w2._write_info_json()
        col = "observation.slam_trajectory"
        schema2 = read_schema(os.path.join(tmp, "traj"))
        check("列类型 = list<float64>",
              schema2.field(col).type == pa.list_(pa.float64()),
              str(schema2.field(col).type))
        got = read_col(os.path.join(tmp, "traj"), col)
        check("行数 = 3", len(got) == 3, str(len(got)))
        check("多点点值逐位往返", got[0] == TRAJ, str(got[0]))
        check("无样本帧是 [] 而不是 [0.0]*8", got[1] == [], str(got[1]))
        check("单点窗口 = 8 值", got[2] == TRAJ[:8], str(got[2]))
        feats2 = read_features(os.path.join(tmp, "traj"))[col]
        check("features dtype=float64 / shape=[8]",
              feats2["dtype"] == "float64" and feats2["shape"] == [8],
              str(feats2))
        check("features encoding=flat_points_8_txyz_qxyzw",
              feats2.get("encoding") == "flat_points_8_txyz_qxyzw", str(feats2))

        print("[3] 双 rig 前缀命名空间互不干扰")
        w3 = build(os.path.join(tmp, "dual"), [
            {"slam_trajectory": TRAJ,
             "gripper_2_slam_trajectory": TRAJ[:8]},
        ])
        w3._write_data_parquet()
        w3._write_info_json()
        check("rig1 旧键与 rig2 前缀键各建一列",
              read_col(os.path.join(tmp, "dual"),
                       "observation.slam_trajectory")[0] == TRAJ
              and read_col(os.path.join(tmp, "dual"),
                           "observation.gripper_2_slam_trajectory")[0]
              == TRAJ[:8])
        check("rig2 键也在 features 里",
              "observation.gripper_2_slam_trajectory"
              in read_features(os.path.join(tmp, "dual")))

        print("[4] 删分支后 state / force / matrix 未被 else 误吞")
        w4 = build(os.path.join(tmp, "rest"), [
            {"gripper_state": [42.0, 1.0, 3.5],
             "gripper_left_force": [1.0, -2.0, 3.0],
             "gripper_left_force_matrix": [5] * 8},
        ])
        w4._write_data_parquet()
        w4._write_info_json()
        schema4 = read_schema(os.path.join(tmp, "rest"))
        check("gripper_state 仍是 list<float32,3>",
              schema4.field("observation.gripper_state").type
              == pa.list_(pa.float32(), 3),
              str(schema4.field("observation.gripper_state").type))
        check("gripper_left_force 仍是 list<float32,3>",
              schema4.field("observation.gripper_left_force").type
              == pa.list_(pa.float32(), 3))
        check("gripper_left_force_matrix 仍是 list<int16>（元素为 int）",
              schema4.field("observation.gripper_left_force_matrix").type
              == pa.list_(pa.int16()),
              str(schema4.field(
                  "observation.gripper_left_force_matrix").type))
        check("缺失行的 state/force 仍补 [0.0]*3（契约未变）",
              read_col(os.path.join(tmp, "rest"),
                       "observation.gripper_state") == [[42.0, 1.0, 3.5]])
        feats4 = read_features(os.path.join(tmp, "rest"))
        check("三个键都还在 features 里",
              all(f"observation.{k}" in feats4 for k in
                  ("gripper_state", "gripper_left_force",
                   "gripper_left_force_matrix")),
              str(sorted(feats4)))

        print("[5] 全空 episode 不建任何夹爪列")
        w5 = build(os.path.join(tmp, "none"), [{}, {}])
        w5._write_data_parquet()
        w5._write_info_json()
        schema5 = read_schema(os.path.join(tmp, "none"))
        check("无 observation.slam_* 列",
              not [n for n in schema5.names if "slam_" in n],
              str([n for n in schema5.names if "slam_" in n]))
        check("features 无任何 slam 键",
              not [k for k in read_features(os.path.join(tmp, "none"))
                   if "slam_" in k])

        print("[6] slam_trajectory_ns = 逐点并行宿主时刻（list<int64>，非标量）")
        # 分支顺序陷阱：slam_trajectory_ns 也以 _ns 结尾，若排在通用 _ns
        # 分支之后就会被落成"每行一个标量 int64"——一行 2 个点只剩 1 个
        # 时刻，与点列表错位且下游无从察觉。这里用类型把它钉死。
        NS2 = [287607702508638, 287607702575000]
        w6 = build(os.path.join(tmp, "trajns"), [
            {"slam_trajectory": TRAJ, "slam_trajectory_ns": NS2},
            {},                                        # 空窗口 → []
            {"slam_trajectory": TRAJ[:8],
             "slam_trajectory_ns": [287607702641000]},  # 单点窗口
        ])
        w6._write_data_parquet()
        w6._write_info_json()
        colns = "observation.slam_trajectory_ns"
        schema6 = read_schema(os.path.join(tmp, "trajns"))
        check("列类型 = list<int64>（不是标量 int64）",
              schema6.field(colns).type == pa.list_(pa.int64()),
              str(schema6.field(colns).type))
        check("确实是 list 而非标量 —— 分支顺序未被通用 _ns 抢先",
              not schema6.field(colns).type.equals(pa.int64()))
        gotns = read_col(os.path.join(tmp, "trajns"), colns)
        check("多点点值逐位往返（两个点两个时刻）",
              gotns[0] == NS2, str(gotns[0]))
        check("无样本帧是 [] 而不是 0", gotns[1] == [], str(gotns[1]))
        check("单点窗口 = 1 值", gotns[2] == [287607702641000], str(gotns[2]))
        # 与 slam_trajectory 同序等长是本列的**全部意义**，逐行核对
        traj6 = read_col(os.path.join(tmp, "trajns"),
                         "observation.slam_trajectory")
        check("逐行与 slam_trajectory 等长（下标即配对）",
              all(len(a) // 8 == len(b) for a, b in zip(traj6, gotns)),
              str([(len(a) // 8, len(b)) for a, b in zip(traj6, gotns)]))
        feats6 = read_features(os.path.join(tmp, "trajns"))[colns]
        check("features dtype=int64 / shape=[1]",
              feats6["dtype"] == "int64" and feats6["shape"] == [1],
              str(feats6))
        check("features encoding=flat_parallel_to_slam_trajectory",
              feats6.get("encoding") == "flat_parallel_to_slam_trajectory",
              str(feats6))

        print("[7] pipeline 侧：时刻与点锁步累积、无戳补 0、全无戳不建列")
        from core.pipeline import CameraPipeline          # noqa: E402
        p7 = CameraPipeline()
        p7._writer = object()      # 非 None 即可（write_slam_trajectory 只判非空）
        p7._recording = True
        p7.write_slam_trajectory(POSE7, timestamp=10.0,
                                 host_ns=287607702508638)
        p7.write_slam_trajectory(POSE7, timestamp=10.02)          # 无戳
        p7.write_slam_trajectory(POSE7, timestamp=10.04,
                                 host_ns=287607702575000)
        snap7 = p7._pop_gripper_snapshots()
        check("三点窗口点/刻长度同步",
              len(snap7["slam_trajectory"]) // 8 == 3
              and len(snap7["slam_trajectory_ns"]) == 3,
              f"{len(snap7['slam_trajectory']) // 8} vs "
              f"{len(snap7['slam_trajectory_ns'])}")
        check("缺戳点补 0 而不是被跳过（否则后续点全部错位）",
              snap7["slam_trajectory_ns"] == [287607702508638, 0,
                                              287607702575000],
              str(snap7["slam_trajectory_ns"]))
        check("弹出后累加器清空",
              p7._pop_gripper_snapshots() == {}
              and not p7._gripper_traj_ns)
        p7b = CameraPipeline()
        p7b._writer = object()
        p7b._recording = True
        p7b.write_slam_trajectory(POSE7, timestamp=1.0)           # 全程无戳
        snap7b = p7b._pop_gripper_snapshots()
        check("旧二进制（全程无戳）不建 _ns 列",
              "slam_trajectory" in snap7b
              and "slam_trajectory_ns" not in snap7b,
              str(sorted(snap7b)))
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
