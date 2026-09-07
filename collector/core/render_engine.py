"""
传感器数据渲染引擎 —— 5 种可视化模式。

所有渲染函数接收 (processed_data, max_signal, config, window_size)
返回 BGR 格式的 numpy 帧数组，由 UI 层转为 QPixmap 显示。
"""

import json
import os
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
