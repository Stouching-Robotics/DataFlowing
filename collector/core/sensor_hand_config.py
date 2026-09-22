"""
传感器（BLE 手套）仿生手掌配置与有效性过滤 —— 零 Qt 依赖，纯函数。

供回放对话框与手套面板共用同一套口径：
  - load_sensor_hand_config  按传感器角色加载仿生手掌映射（左/右手套不同）
  - valid_sensor_names       只保留时间线中确实存在 16×16 压力矩阵的传感器列
"""

from __future__ import annotations

from core.render_engine import (
    DEFAULT_HAND, _load_json, CONFIG_FILE, CONFIG_FILE_LEFT, glove_side_of,
)


def load_sensor_hand_config(sensor_name: str) -> dict:
    """按传感器角色加载仿生手掌映射配置（左/右手套不同）。

    ⚠️ 判手**必须**走 `glove_side_of`（看名字里有没有 left），别写成
    `sensor_name == "left_glove"`：列名来自录制时的 `observation.<sensor>`，
    哪天绑定路径给出 `glove_left` / `left_glove_ble` 这类名字，等值写法会
    静默把**右手的仿生手掌配置发给左手** —— 左手画面整体镜像，而日志、帧率
    一切正常（与 2026-09-21 修掉的那次左手触觉 90° 错同属一类静默错）。
    """
    config_file = (CONFIG_FILE_LEFT
                   if glove_side_of(sensor_name) == "left" else CONFIG_FILE)
    cfg = {k: dict(v) for k, v in DEFAULT_HAND.items()}
    for k, v in _load_json(config_file, {}).items():
        if k in cfg:
            cfg[k].update(v)
        else:
            cfg[k] = v   # 左传感器配置文件有 DEFAULT_HAND 之外的部位
    return cfg


def valid_sensor_names(timeline, names: list) -> list:
    """只保留时间线中确实存在 16×16 压力矩阵（256 宽）的传感器列。

    无 sensors 键的会话会从 features 推断（observation.imu 等
    非手套特征会被误判成传感器），按实际列宽过滤掉，避免出现
    永远"无信号"的幽灵传感器格。
    """
    if timeline is None:
        return []
    out = []
    for n in names:
        data = timeline.obs.get(f"observation.{n}")
        if (data is not None and getattr(data, "ndim", 0) == 2
                and data.shape[1] == 256):
            out.append(n)
    return out
