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
