#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""左右手触觉 → 仿生手掌映射：两只手必须共用同一套坐标（2026-09-21）。

    venv_lite/bin/python tools/tests/test_glove_hand_mapping.py

**为什么要有这份测试**：固件把左手接成了右手帧的 `values[::-1, ::-1].T`
（转置 + 180°），所以左手的「沿手指方向」是**行**轴、跨手指方向是**列**轴，
而右手正好相反。左手的 `hand_ble_config_left.json` 一旦按直觉写成右手的
「左右镜像」（2026-09-21 之前的旧版就是这么写的：rows 每项取 16-r），
五指就会去读左边那副手套里全死的行 4-15 —— 画面里只有小指偶尔动一下，
而日志、录制、帧率**一切正常**，是个纯显示层静默错。本测试锁三条：

  1. 等价：把同一块压力矩阵按左右手各自的坐标喂进去，两只手渲染出的
     画面必须**逐像素相同**（这就是「共用一套坐标」的可执行定义）；
  2. 区域：按规范表单独点亮某一个部位，亮点必须只落在该部位的锚点块里
     （右手拿规范帧、左手拿它的反变换帧，两边都要成立）；
  3. 反向：把左手配置改回旧的行镜像写法，第 1、2 条必须塌 —— 证明这份
     测试真的抓得住这个 bug，而不是恒真；
  4. **跨实现**：主程序里有**两条互相独立**的触觉显示链 —— 仿生手掌
     `render_hand`（吃 config/sensors/hand_ble_config*.json 的两套表）与
     分区网格 `render_tactile_grid`（吃规范系 + 反变换）。给同一份物理刺激，
     两者必须把它判给**同一根手指**：网格看"亮点落在哪三列"，手掌看"亮点
     落在哪个部位块"。两条链各错各的也能让上面三条全绿，只有这条能把它们
     钉在一起（第 4 条里也带一个左手不转的反证）。
  5. 判手口径：`glove_side_of`（名字里有没有 left）是唯一判据，两条显示链
     必须同口径 —— 等值匹配 `== "left_glove"` 遇到非标准列名会静默发错手。

规范表（用户 2026-09-21 给定，行 = 跨手指方向、列 = 沿手指方向）：
    拇指 行1-3 / 食指 4-6 / 中指 7-9 / 无名指 10-12 / 小指 13-15，列 12-15；
    掌心 行1-15 x 列3-11。行递增 = 拇指侧 → 小指侧。

退出码 0 = 全部通过。
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.render_engine import HAND_ANCHORS, render_hand       # noqa: E402
from core.sensor_hand_config import load_sensor_hand_config    # noqa: E402

WINDOW = (1280, 720)          # 该尺寸下缩放为 1、CELL=14，锚点即原始坐标
PARTS = ["thumb_joint", "index_joint", "middle_joint", "ring_joint",
         "pinky_joint", "palm"]

# 规范（统一坐标）下的部位 → (行, 列)，与右手实测一致
CANONICAL = {
    "thumb_joint": (range(1, 4), range(12, 16)),
    "index_joint": (range(4, 7), range(12, 16)),
    "middle_joint": (range(7, 10), range(12, 16)),
    "ring_joint": (range(10, 13), range(12, 16)),
    "pinky_joint": (range(13, 16), range(12, 16)),
    "palm": (range(1, 16), range(3, 12)),
}

FINGER_PARTS = ["thumb_joint", "index_joint", "middle_joint", "ring_joint",
                "pinky_joint"]

_FAILS = []


def check(ok, label):
    print(("  PASS: " if ok else "  FAIL: ") + label)
    if not ok:
        _FAILS.append(label)
    return ok


def canonical_to_left(canonical: np.ndarray) -> np.ndarray:
    """右手（统一坐标）帧 → 同一物理信息在**左手原始通道号**下的排布。"""
    return np.ascontiguousarray(canonical[::-1, ::-1].T)


def render(matrix, cfg):
    frame, _ = render_hand(np.asarray(matrix, np.float32), 4000.0, cfg,
                           WINDOW, 5000.0, 30.0, 500, 0.15, True, 0.0)
    return frame


def part_block(cfg, part):
    """按 render_hand 的几何还原该部位在画布上的块 (x0, y0, x1, y1)。

    CELL 与居中规则必须与 render_hand 一致，这里只复现几何，不碰映射。
    """
    rows, cols = cfg[part]["rows"], cfg[part]["cols"]
    order = cfg[part].get("axis_order", "row_col")
    cell = int(14 * min(WINDOW[0] / 1280.0, WINDOW[1] / 720.0))
    nrr, ncc = len(rows), len(cols)
    w = ncc * cell if order == "row_col" else nrr * cell
    h = nrr * cell if order == "row_col" else ncc * cell
    ax, ay = HAND_ANCHORS[part]
    x0, y0 = ax - w // 2, ay - h // 2
    return (x0, y0, x0 + w, y0 + h)


def lit_outside(cfg, part, matrix):
    """点亮该部位后，画面上出现的像素是否**全部**落在该部位的块内。"""
    base = render(np.zeros((16, 16), np.float32), cfg)
    img = render(matrix, cfg)
    diff = np.any(img != base, axis=2)
    total = int(diff.sum())
    if total == 0:
        return 0, 0          # 没点亮任何东西 ⇒ 由调用方判失败
    x0, y0, x1, y1 = part_block(cfg, part)
    inside = int(diff[y0:y1, x0:x1].sum())
    return total, total - inside


def part_only_matrix(part, one_hand="right"):
    """只点亮规范表里该部位的格（左手侧返回其原始坐标版本）。"""
    m = np.zeros((16, 16), np.float32)
    rows, cols = CANONICAL[part]
    for r in rows:
        for c in cols:
            m[r, c] = 3000.0
    return m if one_hand == "right" else canonical_to_left(m)


def old_style_left_config(right_cfg):
    """2026-09-21 之前的旧左手配置：rows 每项取 16-r（整块行镜像）。"""
    out = {k: dict(v) for k, v in right_cfg.items()}
    for key, part in out.items():
        if part.get("rows"):
            part["rows"] = [16 - r for r in part["rows"]]
    return out


def scenario_equivalent(right, left):
    """同一块压力矩阵，左右手渲染必须逐像素相同。"""
    rng = np.random.default_rng(20260921)
    bad = []
    for i in range(3):
        canonical = (rng.random((16, 16)) * 4000).astype(np.float32)
        same = np.array_equal(render(canonical, right),
                              render(canonical_to_left(canonical), left))
        if not same:
            bad.append(i)
    check(not bad,
          f"同一场景左右手渲染逐像素一致（{3 - len(bad)}/3 通过）")


def scenario_regions(hand, cfg):
    """单独点亮某部位，亮点只许落在该部位的锚点块里。"""
    fails = []
    for part in PARTS:
        mat = part_only_matrix(part, hand)
        total, outside = lit_outside(cfg, part, mat)
        if total == 0 or outside:
            fails.append(f"{part}(亮{total}/越界{outside})")
    check(not fails,
          f"{'右手(规范帧)' if hand == 'right' else '左手(反变换帧)'}"
          f" 六个部位各自点亮都落在自己块内"
          + ("" if not fails else " —— " + "、".join(fails)))


def scenario_old_style_fails(right, left):
    """反向验证：旧的行镜像写法必须让第 1、2 条塌掉。"""
    old = old_style_left_config(right)
    rng = np.random.default_rng(7)
    canonical = (rng.random((16, 16)) * 4000).astype(np.float32)
    eq_ok = np.array_equal(render(canonical, right),
                           render(canonical_to_left(canonical), old))

    dead = []
    for part in PARTS:
        total, outside = lit_outside(old, part, part_only_matrix(part, "left"))
        if total == 0 or outside:
            dead.append(part)
    check((not eq_ok) and len(dead) >= 4,
          f"旧左手配置（行镜像）确实不满足等价且至少 4 个部位读空"
          f"（等价={'通过(异常!)' if eq_ok else '不通过'}，"
          f"读空/越界={dead}）")


# ── 4. 跨实现：两条独立显示链必须判给同一根手指 ──────────────

def grid_lit_cols(matrix, side):
    """分区网格上被点亮的**显示列**（格为单位，集合）。

    入参是**固件原始帧**（与 render_tactile_grid 的口径一致，左手那份由它
    自己反变换回来）。判据用"与同 side 的全零帧逐像素比"——同 side 保证
    分区框/图例完全一致，差异只剩被点亮的格。
    """
    import core.render_engine as re
    zero = np.zeros((16, 16), np.float32)
    img = re.render_tactile_grid(matrix, side=side,
                                 use_baseline=False).copy()   # 返回的是共享画布
    base = re.render_tactile_grid(zero, side=side, use_baseline=False)
    diff = np.any(img != base, axis=2)
    p = re._TACTILE_PANELS[(780, 560, side)]
    ys, xs = np.nonzero(diff)
    gx, gy, cell = p.grid_left, p.grid_top, p.cell
    inside = ((ys >= gy) & (ys < gy + 16 * cell)
              & (xs >= gx) & (xs < gx + 16 * cell))
    xs = xs[inside]
    return {int((x - gx) // cell) for x in xs}


def grid_finger_index(matrix, side):
    """分区网格把这份刺激判给第几根手指（0=拇指…4=小指，判不出为 None）。

    手指区在显示上恒为**连续三列** 1-3 / 4-6 / … / 13-15（规范系的行 1-15）；
    亮点落到三列之外（例如 0 或 16）就不算任何一根手指 —— 左手不归位时正是
    这个形态。
    """
    cols = sorted(grid_lit_cols(matrix, side))
    if len(cols) != 3 or cols[0] < 1 or cols[2] > 15 or cols[2] - cols[0] != 2:
        return None
    return (cols[0] - 1) // 3


def hand_finger_index(matrix, cfg):
    """仿生手掌把这份刺激判给第几根手指（看亮点落在哪个部位块里）。"""
    hits = []
    for i, part in enumerate(FINGER_PARTS):
        total, outside = lit_outside(cfg, part, matrix)
        if total and not outside:
            hits.append(i)
    return hits[0] if len(hits) == 1 else None


def scenario_cross_path(right, left):
    """同一份物理刺激 → 网格与仿生手掌必须判给同一根手指（左右手都要）。"""
    bad = []
    for hand, cfg in (("right", right), ("left", left)):
        for idx, part in enumerate(FINGER_PARTS):
            canonical = part_only_matrix(part, "right")          # 规范系刺激
            mat = canonical if hand == "right" else canonical_to_left(canonical)
            gi = grid_finger_index(mat, hand)
            hi = hand_finger_index(mat, cfg)
            if gi != idx or hi != idx:
                bad.append(f"{hand}/{part}(网格{gi} 手掌{hi} 期望{idx})")
    check(not bad,
          "同一个手指的刺激：分区网格与仿生手掌判给同一根手指（10 次）"
          + ("" if not bad else " —— " + "、".join(bad)))

    # 反证：左手**不转**就喂网格 → 判不出手指（这正是"左手整体转 90°"的
    # 那条已修故障的判据）。不转时亮点会落到显示列 0-3，即有效三列之外。
    gi_bad = grid_finger_index(part_only_matrix("ring_joint", "right"), "left")
    check(gi_bad != 3,
          f"反证：左手不转就喂会判错手指（实测 {gi_bad}——None 表示亮点落到"
          f"了有效三列之外，期望 3=无名指）⇒ 上面不是恒真")


def scenario_side_predicate():
    """判手只有一个口径：名字里有没有 left（`glove_side_of`）。

    钉的是「同一个名字在两条显示链上必须判成同一只手」—— 仿生手掌的配置
    选择曾经写成 `== "left_glove"` 等值匹配，列名一旦不是这个字面量（例如
    `left_glove_ble`）就会把右手的映射发给左手，静默镜像。
    """
    from core.render_engine import glove_side_of
    bad = []
    for name, want in (("left_glove", "left"), ("right_glove", "right"),
                       ("left_glove_ble", "left"), ("glove_left", "left"),
                       ("right_glove_ble", "right"), ("glove", "right"),
                       ("", "right")):
        got = glove_side_of(name)
        # 仿生手掌那条链：拿到的配置里的锚点表必须与判手结果一致
        cfg = load_sensor_hand_config(name)
        anchored = cfg is not None and "palm" in cfg
        if got != want or not anchored:
            bad.append(f"{name!r}→{got}(期望{want})")
    check(not bad, "判手口径一致（7 个名字，含非标准列名）"
                   + ("" if not bad else " —— " + "、".join(bad)))


def main():
    right = load_sensor_hand_config("right_glove")
    left = load_sensor_hand_config("left_glove")

    print("── 1. 左右手共用一套坐标（逐像素等价）──")
    scenario_equivalent(right, left)

    print("── 2. 部位落点（规范表 → 画布锚点）──")
    scenario_regions("right", right)
    scenario_regions("left", left)

    print("── 3. 反向验证（旧写法必须塌）──")
    scenario_old_style_fails(right, left)

    print("── 4. 跨实现一致（分区网格 ⇄ 仿生手掌）──")
    scenario_cross_path(right, left)

    print("── 5. 判手口径（名字里有没有 left）──")
    scenario_side_predicate()

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项")
        for f in _FAILS:
            print("   -", f)
        return 1
    print("ALL PASS: 左右手触觉映射测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
