"""
传感器数据渲染引擎 —— 6 种可视化模式。

所有渲染函数接收 (processed_data, max_signal, config, window_size)
返回 BGR 格式的 numpy 帧数组，由 UI 层转为 QPixmap 显示。

末节的 render_tactile_grid 是厂商分区网格（Glove-test V1.4 移植）：
录制页与回放页的默认触觉画面，实现与
tools/demos/pooled_viewer_demo 里的同名函数**逐像素一致**，
由 tools/tests/test_tactile_grid_render.py 锁住。
"""

import json
import os
from collections import OrderedDict

import numpy as np
import cv2

from config import settings

# ── 常量 ──────────────────────────────────────────────
MATRIX_ROWS = 16
MATRIX_COLS = 16
VIRIDIS_LUT: np.ndarray = None

# ── 配置路径 ──────────────────────────────────────────
# 仿生手掌（左/右手）映射配置。原定义在 sensors.sensor_panel，
# 迁到这里：glove_widget / playback_dialog 需要路径但不应引入 bleak。
CONFIG_DIR = os.path.join(settings.BASE_DIR, "config", "sensors")
CONFIG_FILE = os.path.join(CONFIG_DIR, "hand_ble_config.json")
CONFIG_FILE_LEFT = os.path.join(CONFIG_DIR, "hand_ble_config_left.json")


def _get_viridis_lut() -> np.ndarray:
    """延迟初始化 Viridis 颜色查找表。"""
    global VIRIDIS_LUT
    if VIRIDIS_LUT is None:
        lut = np.zeros((256, 1, 3), dtype=np.uint8)
        for i in range(256):
            lut[i, 0, 0] = i
        VIRIDIS_LUT = cv2.applyColorMap(lut, cv2.COLORMAP_VIRIDIS)
    return VIRIDIS_LUT


# ── HUD 绘制 ──────────────────────────────────────────

def _draw_hud(frame: np.ndarray, lines: list, color=(255, 255, 255)):
    """在帧左上角绘制半透明 HUD 信息栏。"""
    font, scale, thick, lh, pad = cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1, 22, 10
    max_w = (
        max(cv2.getTextSize(l, font, scale, thick)[0][0] for l in lines)
        if lines else 0
    )
    overlay = frame.copy()
    cv2.rectangle(
        overlay, (0, 0), (max_w + pad * 2, len(lines) * lh + pad * 2),
        (0, 0, 0), -1,
    )
    cv2.addWeighted(overlay, 0.5, frame, 0.5, 0, frame)
    for i, line in enumerate(lines):
        cv2.putText(
            frame, line, (pad, pad + lh - 5 + i * lh),
            font, scale, color, thick, cv2.LINE_AA,
        )


# ── 配置加载 ──────────────────────────────────────────

def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(default, dict):
                    default.update(loaded)
                    return default
                return loaded
        except Exception:
            pass
    return default


# ═══════════════════════════════════════════════════════
#  模式 1: 热力图
# ═══════════════════════════════════════════════════════

def render_heatmap(
    processed: np.ndarray,
    max_signal: float,
    config: dict,
    window_size: tuple,
    current_vmax: float,
    fps: float,
    noise_gate: int,
    dyn_ratio: float,
    spatial_on: bool,
) -> tuple:
    """返回 (frame, new_vmax)。"""
    ww, wh = window_size
    rows = config.get("rows", list(range(16)))
    cols = config.get("cols", list(range(16)))
    order = config.get("axis_order", "row_col")

    # 增强参数（来自 config，向后兼容）
    subpixel = config.get("subpixel", True)
    gamma = config.get("gamma", 1.0)
    blur_ksize = config.get("blur", 5)

    nr, nc = len(rows), len(cols)
    if nr == 0 or nc == 0:
        frame = np.zeros((wh, ww, 3), dtype=np.uint8)
        _draw_hud(frame, ["Press [M] to select Heatmap Rows/Cols!"], (0, 0, 255))
        return frame, current_vmax

    # 提取子矩阵
    sub = processed[np.ix_(rows, cols)]
    if order != "row_col":
        sub = sub.T

    # ── Subpixel 超分 ──────────────────────────────
    if subpixel and sub.shape[0] >= 2 and sub.shape[1] >= 2:
        H0, W0 = sub.shape
        shrink = 1.5
        padded = np.pad(sub, 1, mode='constant')
        top = padded[0:H0, 1:W0 + 1]
        bottom = padded[2:H0 + 2, 1:W0 + 1]
        left = padded[1:H0 + 1, 0:W0]
        right = padded[1:H0 + 1, 2:W0 + 2]
        sum_x = left + right + sub + 1e-6
        sum_y = top + bottom + sub + 1e-6
        dx = np.clip((right - left) / sum_x * shrink, -1, 1)
        dy = np.clip((bottom - top) / sum_y * shrink, -1, 1)
        super_res = np.zeros((H0 * 2, W0 * 2), dtype=np.float32)
        super_res[0::2, 0::2] = sub * np.maximum(0, 1.0 - dx - dy)
        super_res[0::2, 1::2] = sub * np.maximum(0, 1.0 + dx - dy)
        super_res[1::2, 0::2] = sub * np.maximum(0, 1.0 - dx + dy)
        super_res[1::2, 1::2] = sub * np.maximum(0, 1.0 + dx + dy)
        sub = super_res

    H, W = sub.shape
    scale_w = (ww - 200) / W
    scale_h = (wh - 120) / H
    scale = min(scale_w, scale_h)
    target_w = int(W * scale)
    target_h = int(H * scale)

    resized = cv2.resize(sub, (target_w, target_h), interpolation=cv2.INTER_CUBIC)
    if blur_ksize > 0:
        k = blur_ksize if blur_ksize % 2 == 1 else blur_ksize + 1
        resized = cv2.GaussianBlur(resized, (k, k), 0)

    # 平滑 vmax
    vmax_cand = max(5000, np.percentile(resized, 99.7))
    new_vmax = current_vmax * 0.95 + vmax_cand * 0.05 if vmax_cand > current_vmax else vmax_cand

    # ── Gamma 校正 ────────────────────────────────
    norm = np.clip(resized / new_vmax, 0, 1) if new_vmax > 0 else resized
    if gamma != 1.0:
        norm = np.power(norm, gamma)
    img_8u = (norm * 255).astype(np.uint8)
    color = cv2.applyColorMap(img_8u, cv2.COLORMAP_VIRIDIS)

    frame = np.zeros((wh, ww, 3), dtype=np.uint8)
    sx = (ww - target_w) // 2
    sy = (wh - target_h) // 2 + 20
    frame[sy:sy + target_h, sx:sx + target_w] = color

    _draw_hud(frame, [
        f"Heatmap | FPS:{fps:.1f} | Max:{int(max_signal)}",
        f"Gate:{noise_gate} | Dyn:{dyn_ratio:.2f} | Filter:{'ON' if spatial_on else 'OFF'}",
        f"SP:{'ON' if subpixel else 'OFF'} G:{gamma:.1f} B:{blur_ksize} | [M] Config",
    ])
    return frame, new_vmax


# ═══════════════════════════════════════════════════════
#  模式 2: 轨迹模式
# ═══════════════════════════════════════════════════════

_trace_canvas = None

def render_trace(
    processed: np.ndarray,
    max_signal: float,
    config: dict,
    window_size: tuple,
    current_vmax: float,
    fps: float,
    noise_gate: int,
    dyn_ratio: float,
    spatial_on: bool,
) -> tuple:
    """轨迹模式：叠加历史数据。"""
    global _trace_canvas
    ww, wh = window_size
    rows = config.get("rows", list(range(16)))
    cols = config.get("cols", list(range(16)))
    order = config.get("axis_order", "row_col")

    # 增强参数
    subpixel = config.get("subpixel", True)
    gamma = config.get("gamma", 1.0)
    blur_ksize = config.get("blur", 5)

    nr, nc = len(rows), len(cols)
    if nr == 0 or nc == 0:
        frame = np.zeros((wh, ww, 3), dtype=np.uint8)
        _draw_hud(frame, ["Press hotkey to config"], (0, 0, 255))
        return frame, current_vmax, False

    sub = processed[np.ix_(rows, cols)]
    if order != "row_col":
        sub = sub.T

    # ── Subpixel 超分 ──────────────────────────────
    if subpixel and sub.shape[0] >= 2 and sub.shape[1] >= 2:
        H0, W0 = sub.shape
        shrink = 1.5
        padded = np.pad(sub, 1, mode='constant')
        top = padded[0:H0, 1:W0 + 1]
        bottom = padded[2:H0 + 2, 1:W0 + 1]
        left = padded[1:H0 + 1, 0:W0]
        right = padded[1:H0 + 1, 2:W0 + 2]
        sum_x = left + right + sub + 1e-6
        sum_y = top + bottom + sub + 1e-6
        dx = np.clip((right - left) / sum_x * shrink, -1, 1)
        dy = np.clip((bottom - top) / sum_y * shrink, -1, 1)
        super_res = np.zeros((H0 * 2, W0 * 2), dtype=np.float32)
        super_res[0::2, 0::2] = sub * np.maximum(0, 1.0 - dx - dy)
        super_res[0::2, 1::2] = sub * np.maximum(0, 1.0 + dx - dy)
        super_res[1::2, 0::2] = sub * np.maximum(0, 1.0 - dx + dy)
        super_res[1::2, 1::2] = sub * np.maximum(0, 1.0 + dx + dy)
        sub = super_res

    if _trace_canvas is None or _trace_canvas.shape != sub.shape:
        _trace_canvas = np.zeros_like(sub)
    _trace_canvas = np.maximum(_trace_canvas, sub)
    display = _trace_canvas

    H, W = display.shape
    scale_w = (ww - 200) / W
    scale_h = (wh - 120) / H
    scale = min(scale_w, scale_h)
    tw, th = int(W * scale), int(H * scale)

    resized = cv2.resize(display, (tw, th), interpolation=cv2.INTER_CUBIC)
    if blur_ksize > 0:
        k = blur_ksize if blur_ksize % 2 == 1 else blur_ksize + 1
        resized = cv2.GaussianBlur(resized, (k, k), 0)

    vmax_cand = max(5000, np.percentile(resized, 99.7))
    new_vmax = current_vmax * 0.95 + vmax_cand * 0.05 if vmax_cand > current_vmax else vmax_cand

    norm = np.clip(resized / new_vmax, 0, 1) if new_vmax > 0 else resized
    if gamma != 1.0:
        norm = np.power(norm, gamma)
    img_8u = (norm * 255).astype(np.uint8)
    color = cv2.applyColorMap(img_8u, cv2.COLORMAP_VIRIDIS)

    frame = np.zeros((wh, ww, 3), dtype=np.uint8)
    sx, sy = (ww - tw) // 2, (wh - th) // 2 + 20
    frame[sy:sy + th, sx:sx + tw] = color

    _draw_hud(frame, [
        f"Trace | FPS:{fps:.1f} | Max:{int(max_signal)}",
        f"Gate:{noise_gate} | Dyn:{dyn_ratio:.2f} | Filter:{'ON' if spatial_on else 'OFF'}",
        f"SP:{'ON' if subpixel else 'OFF'} G:{gamma:.1f} B:{blur_ksize} | [X] Clear | [M] Config",
    ])
    return frame, new_vmax, True

def clear_trace_canvas():
    global _trace_canvas
    _trace_canvas = None


# ═══════════════════════════════════════════════════════
#  模式 3: 网格数据
# ═══════════════════════════════════════════════════════

def render_grid(
    processed: np.ndarray,
    max_signal: float,
    config: dict,
    window_size: tuple,
    fps: float,
) -> np.ndarray:
    """网格模式：每个单元格显示数值。"""
    ww, wh = window_size
    rows = config.get("rows", list(range(16)))
    cols = config.get("cols", list(range(16)))
    order = config.get("axis_order", "row_col")

    nr, nc = len(rows), len(cols)
    visual_rows = nr if order == "row_col" else nc
    visual_cols = nc if order == "row_col" else nr

    if nr == 0 or nc == 0:
        frame = np.zeros((wh, ww, 3), dtype=np.uint8)
        _draw_hud(frame, ["Press hotkey to config"], (0, 0, 255))
        return frame

    cell_size = min((ww - 100) / max(1, visual_cols), (wh - 120) / max(1, visual_rows))
    cell_size = max(5.0, min(80.0, cell_size))

    grid_w = int(visual_cols * cell_size)
    grid_h = int(visual_rows * cell_size)
    sx = (ww - grid_w) // 2
    sy = (wh - grid_h) // 2 + 30

    frame = np.zeros((wh, ww, 3), dtype=np.uint8)
    grid_vmax = max(5000.0, max_signal)

    for vi in range(visual_rows):
        for vj in range(visual_cols):
            if order == "row_col":
                r_idx, c_idx = rows[vi], cols[vj]
            else:
                r_idx, c_idx = rows[vj], cols[vi]
            if r_idx >= MATRIX_ROWS or c_idx >= MATRIX_COLS:
                continue

            val = int(processed[r_idx, c_idx])
            x1 = int(sx + vj * cell_size)
            y1 = int(sy + vi * cell_size)
            x2 = int(sx + (vj + 1) * cell_size)
            y2 = int(sy + (vi + 1) * cell_size)

            if val > 0:
                blue = min(255, int((val / grid_vmax) * 255))
                cv2.rectangle(frame, (x1, y1), (x2, y2), (blue, 0, 0), -1)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (50, 50, 50), 1)

            if val > 0 and cell_size > 15:
                fs = max(0.3, cell_size / 60.0)
                text = str(val)
                (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_PLAIN, fs, 1)
                if tw > cell_size * 0.9:
                    fs *= (cell_size * 0.9 / tw)
                    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_PLAIN, fs, 1)
                cv2.putText(
                    frame, text,
                    (x1 + (int(cell_size) - tw) // 2, y1 + (int(cell_size) + th) // 2),
                    cv2.FONT_HERSHEY_PLAIN, fs, (255, 255, 255), 1, cv2.LINE_AA,
                )

    _draw_hud(frame, [
        f"Grid | FPS:{fps:.1f} | Max:{int(max_signal)}",
        "[M] Config Matrix",
    ])
    return frame


# ═══════════════════════════════════════════════════════
#  模式 4: 仿生手掌
# ═══════════════════════════════════════════════════════

# 手部锚点坐标（相对于窗口）
HAND_ANCHORS = {
    "thumb_tip": (350, 280), "thumb_joint": (420, 360), "thumb_base": (480, 420),
    "index_tip": (510, 120), "index_joint": (540, 240), "index_base": (560, 340),
    "middle_tip": (640, 80), "middle_joint": (640, 200), "middle_base": (640, 320),
    "ring_tip": (770, 120), "ring_joint": (740, 240), "ring_base": (720, 340),
    "pinky_tip": (930, 280), "pinky_joint": (860, 360), "pinky_base": (800, 420),
    "palm": (640, 480),
}
WRIST_ANCHOR = (640, 600)

# 默认手部配置（16×16 传感器映射到手指区域）
# 右手实测（2026-09-04 录制逐格核对）：行 = 手指，拇指 1-3 … 小指
# 13-15（行 0 为空），即旧配置 [0-2…12-14] 整体偏高一行的修正；
# 列 12-15 = 指骨 [14,12,13,15] 根→尖；掌心行 1-15 x 传感列 [10,9,8,6,4]。
#
# 左手（config/sensors/hand_ble_config_left.json）**不是**这份配置的左右镜像：
# 左手帧 = 右手帧做 values[::-1, ::-1].T（转置 + 180°，固件层就这样接的线），
# 所以左手的「沿手指方向」是它的**行**轴、「跨手指方向」是它的**列**轴——
# 五指横排在行 0-3（列 0-2 小指 … 12-14 拇指；行 3 = 指根、行 0 = 指尖），
# 掌心行 4-12 x 列 0-14（列 15 与行 13-15 全死，共 61 格）。
# 左手里那份配置就是把这份**沿该变换原样拉回**：rows/cols 互换角色、每项取
# 15-x、axis_order 由 col_row 改 row_col。这样两只手渲染逐像素一致
# （tools/tests/test_glove_hand_mapping.py 锁住这条等价）。
# 只做行镜像（2026-09-21 之前的旧版）会让左手五指去读全死的行 4-15，
# 画面里只有小指/掌心偶尔有反应——即用户报的「左手触觉点映射是错的」。
DEFAULT_HAND = {
    "thumb_joint": {"rows": [1, 2, 3], "cols": [14, 12, 13, 15], "axis_order": "col_row"},
    "index_joint": {"rows": [4, 5, 6], "cols": [14, 12, 13, 15], "axis_order": "col_row"},
    "middle_joint": {"rows": [7, 8, 9], "cols": [14, 12, 13, 15], "axis_order": "col_row"},
    "ring_joint": {"rows": [10, 11, 12], "cols": [14, 12, 13, 15], "axis_order": "col_row"},
    "pinky_joint": {"rows": [13, 14, 15], "cols": [14, 12, 13, 15], "axis_order": "col_row"},
    "palm": {"rows": list(range(1, 16)), "cols": [10, 9, 8, 6, 4], "axis_order": "col_row"},
}
for k in DEFAULT_HAND:
    if k != "palm":
        DEFAULT_HAND[k]["name"] = k


def _scale_point(x: float, y: float, sx: float, sy: float) -> tuple:
    """按缩放因子缩放坐标点。"""
    return (int(x * sx), int(y * sy))


def render_hand(
    processed: np.ndarray,
    max_signal: float,
    config: dict,
    window_size: tuple,
    current_vmax: float,
    fps: float,
    noise_gate: int,
    dyn_ratio: float,
    spatial_on: bool,
    drift: float,
) -> tuple:
    """仿生手掌映射模式。

    硬编码锚点坐标基于 1280×720 画布设计，运行时根据实际 window_size
    等比缩放，适配任意尺寸（如回放面板的 640×400）。
    """
    ww, wh = window_size

    # ── 计算缩放因子（锚点基于 1280×720 设计） ──────────
    NOM_W, NOM_H = 1280.0, 720.0
    sx = ww / NOM_W
    sy = wh / NOM_H

    frame = np.full((wh, ww, 3), 15, dtype=np.uint8)
    lut = _get_viridis_lut()

    # ── 缩放后的锚点 ────────────────────────────────────
    def _sp(x, y):
        return _scale_point(x, y, sx, sy)

    wrist = _sp(*WRIST_ANCHOR)
    scaled_anchors = {k: _sp(*v) for k, v in HAND_ANCHORS.items()}

    # ── 缩放线宽和圆半径 ──────────────────────────────
    lw_scale = min(sx, sy)
    _lw = lambda v: max(1, int(v * lw_scale))
    _cr = lambda v: max(1, int(v * lw_scale))

    # 画骨骼框架
    line_c, joint_c = (60, 60, 60), (80, 80, 80)
    cv2.line(frame, wrist, scaled_anchors["palm"], line_c, _lw(6), cv2.LINE_AA)
    for fg in ["thumb", "index", "middle", "ring", "pinky"]:
        b = scaled_anchors[f"{fg}_base"]
        j = scaled_anchors[f"{fg}_joint"]
        t = scaled_anchors[f"{fg}_tip"]
        cv2.line(frame, scaled_anchors["palm"], b, line_c, _lw(5), cv2.LINE_AA)
        cv2.line(frame, b, j, line_c, _lw(4), cv2.LINE_AA)
        cv2.line(frame, j, t, line_c, _lw(3), cv2.LINE_AA)
        cv2.circle(frame, b, _cr(10), joint_c, -1, cv2.LINE_AA)
        cv2.circle(frame, j, _cr(8), joint_c, -1, cv2.LINE_AA)
        cv2.circle(frame, t, _cr(6), joint_c, -1, cv2.LINE_AA)
    cv2.circle(frame, scaled_anchors["palm"], _cr(16), joint_c, -1, cv2.LINE_AA)

    # 平滑 vmax
    vmax = max(max_signal, 5000)
    new_vmax = current_vmax * 0.95 + vmax * 0.05 if vmax < current_vmax else vmax

    CELL = max(1, int(14 * min(sx, sy)))
    for part_key, cfg in config.items():
        if part_key not in scaled_anchors:
            continue
        ax, ay = scaled_anchors[part_key]
        rows = cfg.get("rows", [])
        cols = cfg.get("cols", [])
        order = cfg.get("axis_order", "row_col")
        if not rows or not cols:
            continue

        nrr, ncc = len(rows), len(cols)
        w = ncc * CELL if order == "row_col" else nrr * CELL
        h = nrr * CELL if order == "row_col" else ncc * CELL
        cx, cy = ax - w // 2, ay - h // 2

        for i, r in enumerate(rows):
            for j, c in enumerate(cols):
                if r >= MATRIX_ROWS or c >= MATRIX_COLS:
                    continue
                val = processed[r, c]
                x1 = cx + (j if order == "row_col" else i) * CELL
                y1 = cy + (i if order == "row_col" else j) * CELL
                x2, y2 = x1 + CELL, y1 + CELL

                if val > 0:
                    idx = int(min(255, (val / new_vmax) * 255))
                    b, g, rr = lut[idx, 0]
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (int(b), int(g), int(rr)), -1)
                    # 单元格内显示数据值
                    if CELL > 14:
                        vtext = str(int(val))
                        fs = 0.4
                        (tw, th), _ = cv2.getTextSize(vtext, cv2.FONT_HERSHEY_PLAIN, fs, 1)
                        tc = (0, 0, 0) if (0.299 * rr + 0.587 * g + 0.114 * b) > 140 else (255, 255, 255)
                        cv2.putText(frame, vtext,
                                    (x1 + (CELL - tw) // 2, y1 + (CELL + th) // 2),
                                    cv2.FONT_HERSHEY_PLAIN, fs, tc, 1, cv2.LINE_AA)
                cv2.rectangle(frame, (x1, y1), (x2, y2), (100, 100, 100), 1)

    _draw_hud(frame, [
        f"Bionic Hand | FPS:{fps:.1f} | Max:{int(max_signal)} | Drift:{int(drift)}",
        f"Gate:{noise_gate} | Dyn:{dyn_ratio:.2f} | Filter:{'ON' if spatial_on else 'OFF'}",
        "[M] Config | [C] Calibrate",
    ])
    return frame, new_vmax


# ═══════════════════════════════════════════════════════
#  模式 5: 拓扑形变
# ═══════════════════════════════════════════════════════

class DeformMeshState:
    """模式5 的交互状态。"""

    def __init__(self):
        self.holes = []
        self.flip_x = False
        self.flip_y = False
        self.deform_strength = 1.0
        self.cached_x = None
        self.cached_y = None
        self.cache_valid = False
        # 绘制中
        self.drawing = False
        self.draw_start = (0, 0)
        self.draw_end = (0, 0)


def _update_mesh_cache(state: DeformMeshState, rows, cols, order, ww, wh):
    """更新形变网格缓存。"""
    nr, nc = len(rows), len(cols)
    if nr == 0 or nc == 0:
        state.cached_x = state.cached_y = None
        return

    CELL = 26
    tw = nc * CELL if order == "row_col" else nr * CELL
    th = nr * CELL if order == "row_col" else nc * CELL
    sx = (ww - tw) // 2
    sy = (wh - th) // 2 + 30

    jj, ii = np.meshgrid(np.arange(nc), np.arange(nr))
    if order == "row_col":
        mx = sx + jj * CELL
        my = sy + ii * CELL
    else:
        mx = sx + ii * CELL
        my = sy + jj * CELL

    if state.flip_x:
        mx, my = np.fliplr(mx), np.fliplr(my)
    if state.flip_y:
        mx, my = np.flipud(mx), np.flipud(my)

    # 应用孔洞形变
    holes = list(state.holes)
    if state.drawing:
        cx1, cy1 = state.draw_start
        cx2, cy2 = state.draw_end
        rx, ry = max(1, abs(cx2 - cx1)), max(1, abs(cy2 - cy1))
        if rx > 5 and ry > 5:
            holes.append({"cx": cx1, "cy": cy1, "rx": rx, "ry": ry})

    for h in holes:
        cx, cy, rx, ry = h["cx"], h["cy"], h["rx"], h["ry"]
        vx, vy = mx - cx, my - cy
        d = np.sqrt((vx / rx) ** 2 + (vy / ry) ** 2) + 1e-6
        target_d = d + state.deform_strength * np.exp(-d * 1.5)
        scale = target_d / d
        mx = cx + vx * scale
        my = cy + vy * scale

    state.cached_x, state.cached_y = mx, my
    state.cache_valid = True


def render_deform_mesh(
    processed: np.ndarray,
    max_signal: float,
    config: dict,
    window_size: tuple,
    current_vmax: float,
    fps: float,
    state: DeformMeshState,
) -> tuple:
    """拓扑形变模式。"""
    ww, wh = window_size
    frame = np.full((wh, ww, 3), 15, dtype=np.uint8)
    lut = _get_viridis_lut()

    vmax = max(5000, max_signal)
    new_vmax = current_vmax * 0.95 + vmax * 0.05 if vmax < current_vmax else vmax

    rows = config.get("rows", list(range(16)))
    cols = config.get("cols", list(range(16)))
    order = config.get("axis_order", "row_col")

    if not state.cache_valid:
        _update_mesh_cache(state, rows, cols, order, ww, wh)

    mx, my = state.cached_x, state.cached_y
    if mx is None:
        _draw_hud(frame, ["Press [M] to select Matrix Rows/Cols!"], (0, 0, 255))
        return frame, new_vmax

    nr, nc = len(rows), len(cols)

    # 画网格线
    pts = np.stack([mx, my], axis=-1).astype(np.int32)
    cv2.polylines(frame, pts, False, (60, 60, 60), 1, cv2.LINE_AA)
    pts_t = np.ascontiguousarray(np.transpose(pts, (1, 0, 2)))
    cv2.polylines(frame, pts_t, False, (60, 60, 60), 1, cv2.LINE_AA)

    # 画孔洞
    holes = list(state.holes)
    if state.drawing:
        cx1, cy1 = state.draw_start
        cx2, cy2 = state.draw_end
        rx, ry = max(1, abs(cx2 - cx1)), max(1, abs(cy2 - cy1))
        if rx > 5 and ry > 5:
            holes.append({"cx": cx1, "cy": cy1, "rx": rx, "ry": ry})
    for h in holes:
        cv2.ellipse(frame, (h["cx"], h["cy"]), (h["rx"], h["ry"]),
                     0, 0, 360, (90, 90, 90), 2, cv2.LINE_AA)

    # 画数据点
    for i in range(nr):
        for j in range(nc):
            ri, cj = rows[i], cols[j]
            if ri >= MATRIX_ROWS or cj >= MATRIX_COLS:
                continue
            val = processed[ri, cj]
            px, py = int(mx[i, j]), int(my[i, j])
            if val > 0:
                idx = int(min(255, (val / new_vmax) * 255))
                b, g, rr = lut[idx, 0]
                cv2.circle(frame, (px, py), 10, (int(b), int(g), int(rr)), -1, cv2.LINE_AA)
            else:
                cv2.circle(frame, (px, py), 3, (80, 80, 80), -1, cv2.LINE_AA)

    s = state.deform_strength
    _draw_hud(frame, [
        f"Deform Mesh | FPS:{fps:.1f} | Max:{int(max_signal)} | Strength:{s:.1f}",
        f"Flip X:{state.flip_x} | Flip Y:{state.flip_y}",
        "[Drag] Draw Hole | [R] Clear | [U/I] Flip | [O/P] Strength | [M] Config",
    ])
    return frame, new_vmax


# ═══════════════════════════════════════════════════════
#  手部骨架渲染（MANO 21 关键点 → 3/4 视角透视画面）
#
#  移植自手套工具包 apps/rendering/replay.py 的 Camera/_grid/
#  _draw_hand（画布尺寸参数化）。tools/demos/pooled_viewer_demo
#  另有同口径自包含副本（demo 不导入 core，两边保持同步即可）。
# ═══════════════════════════════════════════════════════

# MANO 21 关键点骨骼连接 + 分组: 0 拇指 1 食指 2 中指 3 无名指 4 小指 5 掌骨
SKELETON_BONES = [
    (0, 1, 0), (1, 2, 0), (2, 3, 0), (3, 4, 0),                # thumb
    (0, 5, 1), (5, 6, 1), (6, 7, 1), (7, 8, 1),                # index
    (0, 9, 2), (9, 10, 2), (10, 11, 2), (11, 12, 2),           # middle
    (0, 13, 3), (13, 14, 3), (14, 15, 3), (15, 16, 3),         # ring
    (0, 17, 4), (17, 18, 4), (18, 19, 4), (19, 20, 4),         # pinky
    (5, 9, 5), (9, 13, 5), (13, 17, 5),                        # metacarpal
]
# 每根手指分组颜色（BGR）
SKELETON_FINGER_BGR = [
    (60, 80, 255),     # 0 拇指 红
    (60, 255, 255),    # 1 食指 黄
    (60, 220, 60),     # 2 中指 绿
    (255, 210, 60),    # 3 无名指 青
    (255, 120, 255),   # 4 小指 紫
    (210, 210, 210),   # 5 掌骨 白
]

_SKELETON_FOV_DEG = 50.0


class SkeletonCamera:
    """球坐标相机 + 透视投影（照工具包 replay.py 的 Camera，画布尺寸参数化）。"""

    def __init__(self, yaw_deg, elev_deg, dist, img_w, img_h, roll_deg=0.0):
        yaw, elev = np.deg2rad(yaw_deg), np.deg2rad(elev_deg)
        roll = np.deg2rad(roll_deg)
        cp = np.array([dist * np.cos(elev) * np.sin(yaw),
                       dist * np.sin(elev),
                       dist * np.cos(elev) * np.cos(yaw)])
        self.pos = cp
        self.fwd = -cp / np.linalg.norm(cp)
        self.right = np.cross(self.fwd, [0, 1, 0])
        self.right /= np.linalg.norm(self.right)
        self.up = np.cross(self.right, self.fwd)
        cos_r, sin_r = np.cos(roll), np.sin(roll)
        r0, u0 = self.right, self.up
        self.right = r0 * cos_r + u0 * sin_r
        self.up = -r0 * sin_r + u0 * cos_r
        self.w, self.h = img_w, img_h
        self.f = (self.h / 2) / np.tan(np.deg2rad(_SKELETON_FOV_DEG) / 2)

    def project(self, pts3d):
        """(N,3) → (N,2) 图像坐标 + (N,) 深度（相机前方为正）。"""
        v = pts3d - self.pos
        z = v @ self.fwd
        x = v @ self.right
        y = v @ self.up
        u = self.f * x / z + self.w / 2
        vv = self.h / 2 - self.f * y / z
        return np.stack([u, vv], -1), z


def fit_skeleton_dist(kpts):
    """按手部空间尺度定相机距离（照工具包 replay.py）。"""
    v = kpts[np.isfinite(kpts).all(axis=-1)]
    if len(v) == 0:
        return 0.5
    r = float(np.abs(v).max())
    return max(0.25, r * 2.6 + 0.1)


def _skeleton_bg(w, h):
    img = np.full((h, w, 3), 12, np.uint8)
    for y in range(h):
        img[y] = np.full(3, 10 + int(10 * y / h), np.uint8)
    return img


def _skeleton_grid(img, cam, r):
    """z=0 平面网格 + 三色坐标轴（照工具包 replay.py 的 _grid）。"""
    x = [(i / 6) * r for i in range(-6, 7)]
    lines = ([([v, -r, 0.0], [v, r, 0.0]) for v in x]
             + [([-r, v, 0.0], [r, v, 0.0]) for v in x])
    for p0, p1 in lines:
        pts, z = cam.project(np.array([p0, p1], np.float32))
        if (z > 0).all():
            cv2.line(img, tuple(pts[0].astype(int)), tuple(pts[1].astype(int)),
                     (40, 44, 52), 1, cv2.LINE_AA)
    origin = np.zeros((3,), np.float32)
    for axis, col in [(np.array([r, 0.0, 0.0]), (40, 60, 255)),    # X 红
                      (np.array([0.0, r, 0.0]), (40, 255, 60)),    # Y 绿
                      (np.array([0.0, 0.0, r]), (255, 60, 40))]:   # Z 蓝
        pts, z = cam.project(np.stack([origin, axis]))
        if (z > 0).all():
            cv2.line(img, tuple(pts[0].astype(int)), tuple(pts[1].astype(int)),
                     col, 2, cv2.LINE_AA)


def _draw_skeleton_hand(img, cam, kpts):
    """深度着色的骨骼连线 + 白色关节点（照工具包 replay.py 的 _draw_hand）。"""
    pts, z = cam.project(kpts)
    vis = np.isfinite(kpts).all(axis=1) & (z > 0)
    # 近亮远暗的深度着色；手指比掌骨略亮
    zmin, zmax = 0.05, 0.6
    lum = np.clip((zmax - z) / (zmax - zmin), 0.35, 1.0)
    for a, b, g in SKELETON_BONES:
        if vis[a] and vis[b]:
            k = 0.5 * (lum[a] + lum[b])
            col = tuple(int(c * k) for c in SKELETON_FINGER_BGR[g])
            cv2.line(img, tuple(pts[a].astype(int)), tuple(pts[b].astype(int)),
                     col, 3, cv2.LINE_AA)
    for p, ok in zip(pts, vis):
        if ok:
            cv2.circle(img, tuple(p.astype(int)), 2, (255, 255, 255), -1,
                       cv2.LINE_AA)


def render_skeleton(kpts, w=640, h=420, dist=None, label=""):
    """单手骨架面板（固定 3/4 视角；dist 传入为帧间防抖后的距离）。

    Args:
        kpts: (21,3) float 关键点（米）
        dist: 相机距离（None → 按当前帧 fit_skeleton_dist）
    Returns:
        BGR 画布
    """
    if dist is None:
        dist = fit_skeleton_dist(kpts)
    img = _skeleton_bg(w, h)
    cam = SkeletonCamera(yaw_deg=-25.0, elev_deg=15.0, dist=dist,
                         img_w=w, img_h=h)
    _skeleton_grid(img, cam, fit_skeleton_dist(kpts) * 0.5)
    _draw_skeleton_hand(img, cam, kpts)
    if label:
        cv2.putText(img, label, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (240, 240, 240), 2, cv2.LINE_AA)
    return img


# ══════════════════════════════════════════════════════════════════
# 触觉分区网格（录制页 / 回放页默认触觉画面）
# ══════════════════════════════════════════════════════════════════
# 移植自厂商 Glove-test V1.4 工具 glove_qt_visualizer.py 的
# PressureMatrixCanvas（QPainter → OpenCV，QColor → BGR，中文字串 →
# 英文，布局/配色照搬）:
#   render_tactile_grid —— 16x16 分区网格（手指区 y=12…15 / 手掌区 y=3…11 /
#                         空值区 y≤2；行列号 + 逐格数值 + 图例。两只手同一
#                         段：左手原始帧先归到规范系，面板内不翻转）
#   tactile_canvas_for  —— 显示区 → 该渲染多大的画布（主程序专用：回放页的
#                         传感器格比画布小得多，按显示区反推尺寸再交给显示层
#                         缩放，数值才不会被裁；demo 有自己的窗口尺寸，不用它）
#   （2026-09-04 起按用户要求移除手形热图 PressureHandCanvas 移植，只留矩阵）
# 触觉矩阵映射（2026-09-21 定案，与厂商 SDK 的规范系逐项对齐）:
#   **规范系 (canonical)** —— 行 = 跨手指方向、列 = 沿手指/掌长方向：
#     行 1-3 拇指 / 4-6 食指 / 7-9 中指 / 10-12 无名 / 13-15 小指，
#       行递增 = 拇指侧 → 小指侧（同一根手指的 3 行也按这个方向排）；
#     列 12-15 = 手指，12 → 15 = 指根 → 指尖（列 12 约在远端指节）；
#     列 3-11 = 掌心，3 → 11 = 掌根 → 指根；
#     行 0 与列 0-2 为空。有效格 195 = 掌 15×9 + 五指 5×(3×4)。
#   **右手帧就是规范系**；左手帧是规范系的 `[::-1, ::-1].T`（转置 + 180°），
#   由 `canonical_pressure_matrix()` 反变换回来 —— 所以本节的每一处
#   （坐标表、图例、分区框、空值/Spare 判据）**两只手走同一段代码**，
#   没有任何按手分支。这是 2026-09-21 之前那版的核心错误：它假设左手是
#   右手的「整块行镜像」（拇指 15-13），并给左手加了一次列翻转去凑 ——
#   而真实数据是转置关系，于是左手画面整体转了 90°（手指横躺在画面上半
#   的 4 行里），分区框/图例则落进空值区，而日志、录制、帧率一切正常。
# 面板内拇指统一朝左：网格 x 轴 = 规范行，两只手都不翻（厂商 SDK 的
# gui/rendering/tactile.py 把这条变换预先烘进左手表，同样不翻）。
#
# ⚠️ 本节的实现与 tools/demos/pooled_viewer_demo/pooled_viewer_demo.py
# 里的同名副本**逐像素一致**（该 demo 单文件自包含、不 import 主程序，
# 与 render_skeleton 同属"demo 保副本、主程序放实现"的既定模式）；
# 改这里必须同步改那份，tools/tests/test_tactile_grid_render.py 会逐像素
# 比对两份实现在同一输入下的输出。

_TACTILE_FINGER_NAMES = ("Thumb", "Index", "Middle", "Ring", "Pinky")
# 手指区分区框颜色（厂商 _FINGER_COLORS，BGR）
_TACTILE_FINGER_BGR = ((68, 68, 239), (11, 158, 245), (94, 197, 34),
                       (246, 130, 59), (247, 85, 168))
# 压力色带（厂商 PRESSURE_COLOR_BANDS_CORRECTED / _RAW，BGR）
_TACTILE_BANDS_CORRECTED = ((333, (95, 58, 30)), (666, (199, 134, 22)),
                            (999, (94, 197, 34)), (1333, (21, 204, 250)),
                            (1666, (22, 115, 249)), (float("inf"), (68, 68, 239)))
_TACTILE_BANDS_RAW = ((2000, (95, 58, 30)), (2400, (199, 134, 22)),
                      (2800, (94, 197, 34)), (3200, (21, 204, 250)),
                      (3600, (22, 115, 249)), (float("inf"), (68, 68, 239)))
_TACTILE_BG = (25, 16, 8)          # #081019
_TACTILE_CELL_IDLE = (49, 33, 18)  # #122131（无数据格底色）
_TACTILE_EMPTY_OVERLAY = (48, 38, 30)   # (30,38,48) 斜线覆盖
_TACTILE_GRID_BORDER = (239, 225, 210)  # (210,225,239,105) 简化实线
_TACTILE_TEXT = (245, 232, 220)    # #dce8f5
_TACTILE_TEXT_DIM = (210, 189, 169)     # #a9bdd2
_TACTILE_TEXT_FAINT = (185, 148, 148)   # #94a3b8
_TACTILE_EMPTY_LINE = (184, 163, 148)   # #94a3b8
_TACTILE_SPARE_LINE = (219, 213, 209)   # #d1d5db
_TACTILE_PALM_BLUE = (248, 189, 56)     # #38bdf8
_TACTILE_WHITE = (252, 250, 248)        # #f8fafc


def glove_side_of(sensor: str) -> str:
    """传感器列名 → 左右手（默认右手）。"""
    low = (sensor or "").lower()
    return "left" if "left" in low else "right"


def canonical_pressure_matrix(matrix, side: str):
    """原始触觉帧 → **规范系**（16×16 float32）；按 `side` 决定是否反变换。

    契约出处：厂商 SDK `tools/glove_sdk/gui/rendering/tactile_pressure_hand.py`
    的 `canonical_pressure_matrix()` —— 逐字一致的一份移植：

        values = np.asarray(matrix, np.float32).reshape(16, 16)
        if str(side).lower() == "right":
            return values
        return np.ascontiguousarray(values[::-1, ::-1].T)

    规范系的坐标含义见本节顶部注释。要点：**右手帧本来就是规范系**，
    左手帧是规范系的转置 + 180°（固件把左手接成了右手帧的
    `values[::-1, ::-1].T`），所以只有左手要反变换。

    ⚠️ **刻意偏离交付码一处**：判据用「名字里有没有 left」（与
    `glove_side_of` 同口径），而不是 SDK 的 `== "right"`。SDK 那个写法在
    传进完整传感器名（`"right_glove"`）时会**不等于** `"right"`，于是给
    右手套套上左手变换 —— 是静默错半边。我们这边的 `side` 目前恒为
    `glove_side_of()` 的返回值，但这条判据不该依赖调用方的纪律。

    反变换是**自逆**的（`[::-1, ::-1].T` 连做两次等于原样），所以拿不准
    帧的来历时不要用它"试一下"：两边都会得到一个像模像样的矩阵。
    """
    values = np.asarray(matrix, dtype=np.float32).reshape(16, 16)
    if "left" not in str(side).lower():
        return values
    return np.ascontiguousarray(values[::-1, ::-1].T)


def _tactile_heat_color(value, use_baseline):
    bands = _TACTILE_BANDS_CORRECTED if use_baseline else _TACTILE_BANDS_RAW
    for upper, bgr in bands:
        if value <= upper:
            return bgr
    return bands[-1][1]


_TACTILE_TEXT_DARK = (7, 16, 25)
_TACTILE_TEXT_LIGHT = (255, 246, 238)
_TACTILE_MARGIN = 54.0
_TACTILE_BOTTOM_MARGIN = 22.0
_TACTILE_LEGEND_W = 250.0


def _tactile_lut(use_baseline):
    """值 → BGR 查表（_tactile_heat_color 的等价向量化）。

    上界取色带里最后一个有限上界；再补一格给"超出上界"（原实现落
    bands[-1]，与 ≤ 上界的那一格未必同色，所以要多留一格再 clip）。
    """
    bands = _TACTILE_BANDS_CORRECTED if use_baseline else _TACTILE_BANDS_RAW
    top = int(bands[-2][0])
    lut = np.empty((top + 2, 3), np.uint8)
    for i in range(top + 2):
        lut[i] = _tactile_heat_color(i, use_baseline)
    return lut


_TACTILE_LUTS = {True: _tactile_lut(True), False: _tactile_lut(False)}

# 面板画布缓存：key = (w, h, side)，最多留 _PANEL_CACHE_MAX 块
_PANEL_CACHE_MAX = 6
_TACTILE_PANELS = OrderedDict()


class _TactilePanel:
    """一块 (w, h, side) 触觉面板的可复用画布 + 逐格脏标记。

    旧实现每帧把整块面板重画一遍：16x16 格 ×（填充 + 斜线/边框 + 数值
    putText）连同坐标标签、分区框、图例，单面板上千次 OpenCV 调用 ——
    实测 1.4~2.5 ms/面板、双手 3~5 ms（Windows 打包机上更高），30fps 下
    光触觉就吃掉一成以上预算，而且**每一帧画的像素几乎和上一帧一样**。

    现在：底色/坐标/图例这些静态部分画一次存在 base 里；每帧只重画
    **值或配色变了的格**（先擦回 base 再画，避免新旧数字叠字），实测基线
    校正后每帧只有 ~28/256 格变化。分区框/掌心框那 8 笔照旧每帧补画 ——
    纯色无抗锯齿，重画幂等，但会被上面"擦回 base"抹掉，必须补。
    """

    def __init__(self, w, h, side):
        self.w, self.h, self.side = int(w), int(h), side
        self.cell = max(10.0, min(
            (w - _TACTILE_MARGIN - _TACTILE_LEGEND_W) / 16.0,
            (h - _TACTILE_MARGIN - _TACTILE_BOTTOM_MARGIN) / 16.0))
        self.grid_left = self.grid_top = _TACTILE_MARGIN
        self.base = self._draw_chrome()
        self.canvas = self.base.copy()
        self.vals = None            # 上次画上去的值（None = 全脏）
        self.lut_key = None         # 上次用的配色档（值相同但档不同也要重画）

    # ── 静态底图：底色 + 坐标 + 图例（与格区不重叠）──
    def _draw_chrome(self):
        w, h = self.w, self.h
        cell, grid_left, grid_top = self.cell, self.grid_left, self.grid_top
        img = np.full((h, w, 3), _TACTILE_BG, np.uint8)
        cv2.putText(img, "X -> 0..15",
                    (int(grid_left) + 16 * int(cell) // 2 - 70, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, _TACTILE_TEXT_DIM,
                    1, cv2.LINE_AA)
        for display_x in range(16):
            cv2.putText(img, str(display_x),
                        (int(grid_left + display_x * cell) + int(cell) // 2 - 5,
                         int(grid_top) - 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_DIM,
                        1, cv2.LINE_AA)
        for display_row in range(16):
            cv2.putText(img, f"y={15 - display_row}",
                        (6, int(grid_top + display_row * cell + cell / 2 + 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_DIM,
                        1, cv2.LINE_AA)
        legend_left = int(grid_left + 16 * cell + 18.0)
        if legend_left < w - 10:
            cv2.putText(img, "Fingers  y=12..15",
                        (legend_left, int(grid_top) + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, _TACTILE_TEXT,
                        1, cv2.LINE_AA)
            for group in range(5):
                top = int(grid_top + 28.0 + group * 24.0)
                cv2.rectangle(img, (legend_left, top),
                              (legend_left + 15, top + 15),
                              _TACTILE_FINGER_BGR[group], -1)
                row_lo = 1 + 3 * group
                cv2.putText(img,
                            f"{_TACTILE_FINGER_NAMES[group]}: "
                            f"x={row_lo}..{row_lo+2}",
                            (legend_left + 23, top + 13),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT,
                            1, cv2.LINE_AA)
            cv2.putText(img, "Spare col: x=0, y=3..15",
                        (legend_left, int(grid_top + 166.0)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_FAINT,
                        1, cv2.LINE_AA)
            cv2.putText(img, "Palm  x=1..15, y=3..11",
                        (legend_left, int(grid_top + 208.0)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_PALM_BLUE,
                        1, cv2.LINE_AA)
            cv2.putText(img, "Empty zone: y=0..2",
                        (legend_left, int(grid_top + 232.0)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT,
                        1, cv2.LINE_AA)
            cv2.putText(img, "Top of view = y=15..0",
                        (legend_left, int(grid_top + 258.0)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_FAINT,
                        1, cv2.LINE_AA)
            cv2.putText(img, "fingers point upward",
                        (legend_left, int(grid_top + 280.0)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, _TACTILE_TEXT_FAINT,
                        1, cv2.LINE_AA)
        return img

    # ── 每帧补画的分区框/掌心框（压在格子上，会被"擦回 base"抹掉）──
    def _draw_overlay(self, img):
        cell, grid_left, grid_top = self.cell, self.grid_left, self.grid_top
        finger_height = 4.0 * cell
        for group in range(5):
            # 框要扣在该手指的行落点上：规范系里第 g 组恒为 1+3g（拇指 1-3
            # → 小指 13-15），而显示列 = 规范行，所以框恒落在显示列
            # 1+3g..3+3g，拇指向来在画面左侧。与下面 Spare 框（行 0）、
            # 掌心框（行 1..15）同一口径 —— 三者现在无一处按手分支。
            display_start_x = 1 + group * 3
            x0 = int(grid_left + display_start_x * cell)
            y0 = int(grid_top)
            cv2.rectangle(img, (x0 + 2, y0 + 2),
                          (x0 + int(3 * cell) - 2,
                           y0 + int(finger_height) - 2),
                          _TACTILE_FINGER_BGR[group], 2)
        spare_x = 0
        x0 = int(grid_left + spare_x * cell)
        cv2.rectangle(img, (x0 + 1, int(grid_top) + 1),
                      (x0 + int(cell) - 1, int(grid_top + finger_height) - 1),
                      _TACTILE_SPARE_LINE, 1)
        x0 = int(grid_left + cell)
        y0 = int(grid_top + 4.0 * cell)
        cv2.rectangle(img, (x0 + 2, y0 + 2),
                      (x0 + int(15 * cell) - 2, y0 + int(9 * cell) - 2),
                      _TACTILE_PALM_BLUE, 2)
        cv2.line(img, (x0, y0), (x0 + int(15 * cell), y0), _TACTILE_WHITE, 2)

    # ── 逐格脏重画 ──
    def paint(self, matrix, baseline, use_baseline):
        m = np.asarray(matrix, np.float32).reshape(16, 16)
        # 值：原实现是 max(0, m - base) 再 round —— 配色档位用的是入参
        # use_baseline（不是"baseline is not None"），这里照抄。
        # 基线在**原始帧**里扣：基线（回放页的逐格中位）本来就是按原始列算
        # 的，在原始帧里扣就不必给基线也加一道变换。
        if use_baseline and baseline is not None:
            m = np.maximum(
                0.0, m - np.asarray(baseline, np.float32).reshape(16, 16))
        # 归到规范系（左手帧是规范系的 [::-1,::-1].T）—— 纯下标置换，与上面
        # 的减法、与下面的 rint 都可交换，放哪一步都逐位等价；放最前面，
        # 底下就全在规范系里算了。
        # ⚠️ 必须在**取整前**过这一步：canonical_pressure_matrix 收 float32，
        # 喂 int32 会被静默升位，回头 `lut[view]` 当场 IndexError。
        vals = np.rint(canonical_pressure_matrix(m, self.side)).astype(np.int32)
        # 规范 (行 x, 列 y) → 显示 (行 = 15-y 在顶, 列 = x)。**两只手同一段**。
        view = np.ascontiguousarray(vals.T[::-1])
        # 颜色 = LUT[值]，所以"值没变"只在**同一档 LUT**下才等价于"没变"：
        # 切基线开关时，基线为 0 的格值不变、颜色却要换一档，必须整块重画。
        lut_key = bool(use_baseline)
        if (self.vals is not None and self.lut_key == lut_key
                and np.array_equal(view, self.vals)):
            return self.canvas        # 一格没变 → 整块复用（含分区框）

        img = self.canvas
        cell, grid_left, grid_top = self.cell, self.grid_left, self.grid_top
        lut = _TACTILE_LUTS[lut_key]
        colors = lut[np.clip(view, 0, len(lut) - 1)]
        # 亮度判字色：原式 0.299*R + 0.587*G + 0.114*B（BGR 存的是 R 在 [2]）
        lum = (0.299 * colors[..., 2].astype(np.float32)
               + 0.587 * colors[..., 1] + 0.114 * colors[..., 0])
        dark = lum > 145
        draw_text = cell >= 11
        stale = self.vals if self.lut_key == lut_key else None   # 换档 ⇒ 全脏
        for display_row in range(16):
            y = 15 - display_row
            row_top = grid_top + display_row * cell
            y0, y1 = int(row_top), int(row_top + cell)
            is_empty = y <= 2
            for display_x in range(16):
                if stale is not None and stale[display_row, display_x] == \
                        view[display_row, display_x]:
                    continue
                x = display_x
                x0 = int(grid_left + display_x * cell)
                x1 = int(grid_left + (display_x + 1) * cell)
                fill = (int(colors[display_row, display_x, 0]),
                        int(colors[display_row, display_x, 1]),
                        int(colors[display_row, display_x, 2]))
                img[y0:y1, x0:x1] = self.base[y0:y1, x0:x1]   # 擦掉旧内容
                cv2.rectangle(img, (x0, y0), (x1, y1), fill, -1)
                if is_empty or (y >= 3 and x == 0):
                    cv2.rectangle(img, (x0, y0), (x1, y1),
                                  _TACTILE_EMPTY_OVERLAY, -1)
                    cv2.line(img, (x0, y0), (x1, y1), _TACTILE_EMPTY_LINE, 1)
                    cv2.line(img, (x1, y0), (x0, y1), _TACTILE_EMPTY_LINE, 1)
                cv2.rectangle(img, (x0, y0), (x1, y1),
                              _TACTILE_GRID_BORDER, 1)
                if draw_text:
                    # 数字比格子宽（cell 15.5px 时 "500" 有 16px），会溢到右
                    # 邻格上。旧实现每帧把右邻格也重画一遍，溢出被邻格的填充+
                    # 边框盖掉；只重画脏格就盖不住了 —— 所以按格裁掉溢出，
                    # 与旧版落到的像素完全一致。
                    cv2.putText(img[y0:y1, x0:x1],
                                str(int(view[display_row, display_x])),
                                (2, int(cell) - 4),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.28,
                                _TACTILE_TEXT_DARK if dark[display_row, display_x]
                                else _TACTILE_TEXT_LIGHT, 1, cv2.LINE_AA)
        self.vals = view
        self.lut_key = lut_key
        self._draw_overlay(img)
        return img


def render_tactile_grid(matrix, side="right", baseline=None,
                        use_baseline=True, w=780, h=560):
    """16x16 压力矩阵 → 分区确认网格（厂商 PressureMatrixCanvas 移植）。

    入参是**原始帧**（左手那位先由 canonical_pressure_matrix 反变换到规范
    系）。网格行 = 规范列（y=15 在顶 = 指尖），网格列 = 规范行（拇指恒朝
    左，两只手都不翻）；baseline 为 16x16 中位基线（可 None，原始帧口径）。

    同一 (w, h, side) 复用同一块画布，只重画变了的格 —— 返回值是该画布
    本身，**下一次同尺寸调用会原地改写它**，需要的调用方自己拷贝。
    """
    key = (int(w), int(h), side)
    panel = _TACTILE_PANELS.get(key)
    if panel is None:
        panel = _TactilePanel(w, h, side)
        _TACTILE_PANELS[key] = panel
        while len(_TACTILE_PANELS) > _PANEL_CACHE_MAX:
            _TACTILE_PANELS.popitem(last=False)   # 拖窗口边缘会连续出新尺寸
    else:
        _TACTILE_PANELS.move_to_end(key)
    return panel.paint(matrix, baseline, use_baseline)


def tactile_canvas_for(w, h, min_cell=28.0):
    """显示区 (w, h) → 该渲染多大的画布（同宽高比，格子不小于 min_cell）。

    数值文字是**固定 0.28 字号**（四位 "2015" 宽 21px），格子小于 ~26px 时
    数字铺满整格被裁掉、看着糊成一团 —— 而显示区常常比画布小得多（回放页
    宽扁的传感器格按高度等比缩到一半），所以按显示区反推一块"够大"的画布
    交给显示层缩放：既不裁数字，也不白填宽出来的黑边。

    返回 (w', h')，宽高比与入参一致（束缚维度不会因为放大而换人）；显示区
    太小（扣掉边距后格区非正）或异常值时回退 demo 默认画布 780x560。
    """
    w, h = int(w), int(h)
    cell = min((w - _TACTILE_MARGIN - _TACTILE_LEGEND_W) / 16.0,
               (h - _TACTILE_MARGIN - _TACTILE_BOTTOM_MARGIN) / 16.0)
    if w < 1 or h < 1 or cell <= 0:
        return 780, 560
    k = max(1.0, float(min_cell) / cell)
    return max(1, int(round(w * k))), max(1, int(round(h * k)))
