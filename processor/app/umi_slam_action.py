"""UMI Gripper 动作派生 —— 从 SLAM 位姿反推 imitation learning 的 action。

UMI 硬件不录 action:采集端写入的 ``action`` 列是占位全零,导出时会被
``lerobot_export._junk_columns`` 当零信息列剔除,导致数据集无法训练。
官方 UMI 的做法同样是从 SLAM 位姿反推 —— 本模块实现这套口径。

action(7 维,当前夹爪局部坐标系):

    Δp = R_tᵀ · (p_{t+1} − p_t)       # 平移增量, 米
    Δr = rotvec(R_tᵀ · R_{t+1})       # 旋转增量, 弧度
    g  = gripper_state[t+1, 0] / 100  # 夹爪开合, 0–1

本模块是纯函数(不依赖 Web 框架与数据库),便于单测与跨格式复用。
"""

from __future__ import annotations

import numpy as np

# 动作维度:Δp(3) + Δr(3) + 夹爪(1)
ACTION_DIM = 7


def quat_to_matrix(q) -> np.ndarray | None:
    """四元数 ``(x, y, z, w)`` → 3×3 旋转矩阵。

    范数过小(零四元数)说明该帧没有有效姿态,返回 ``None`` 而不是抛异常 ——
    调用方据此跳过该帧。
    """
    x, y, z, w = (float(v) for v in np.asarray(q, dtype=float).ravel()[:4])
    norm = (x * x + y * y + z * z + w * w) ** 0.5
    if norm < 1e-12:
        return None
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def matrix_to_rotvec(rotation: np.ndarray) -> np.ndarray:
    """3×3 旋转矩阵 → 旋转向量(轴×角,弧度)。

    近 180° 时 ``sin(角) → 0``,标准轴公式会除零并产生 NaN。手部大幅翻转
    是真实存在的姿态,一旦产生 NaN 会污染整列 action,因此单独走
    ``(R + I)/2`` 的对角元求轴。
    """
    trace = (float(rotation[0, 0]) + float(rotation[1, 1])
             + float(rotation[2, 2]) - 1.0) / 2.0
    angle = float(np.arccos(max(-1.0, min(1.0, trace))))

    if angle < 1e-9:
        return np.zeros(3)

    if np.pi - angle < 1e-6:
        # 近 π:用 (R+I)/2 的列向量恢复转轴,再按对角元归一化。
        symmetric = (rotation + np.eye(3)) / 2.0
        diagonal = np.clip(np.diag(symmetric), 0.0, None)
        index = int(np.argmax(diagonal))
        if diagonal[index] < 1e-9:
            return np.zeros(3)
        axis = symmetric[:, index] / (diagonal[index] ** 0.5)
        norm = float(np.linalg.norm(axis))
        return axis / norm * angle if norm > 1e-9 else np.zeros(3)

    axis = np.array([
        float(rotation[2, 1]) - float(rotation[1, 2]),
        float(rotation[0, 2]) - float(rotation[2, 0]),
        float(rotation[1, 0]) - float(rotation[0, 1]),
    ]) / (2.0 * np.sin(angle))
    return axis * angle


def poses_from_trajectory(trajectories, timestamps,
                          frame_count: int) -> np.ndarray | None:
    """从 ``observation.slam_trajectory`` 的样本缓冲重建逐帧位姿 ``(N, 7)``。

    slam_trajectory 是采集端的高频(50Hz)样本缓冲,每格存
    ``[t, x, y, z, qx, qy, qz, qw]`` 的整数倍。数据帧是 30fps,所以两个
    数据帧之间到达的样本会落在其中一格的格子里 —— 某一帧自己的格子为空,
    不代表那个时刻没有位姿,**相邻格子里往往就有**。

    因此正确做法是把所有格子的样本汇总,按时间戳逐帧取最近的:
    实测 30% 的「丢帧」里有 97% 能被真实样本覆盖,且单帧位移上限从
    113.9mm 降到 24.8mm(插值方案会横跨 SLAM 重定位跳变)。

    时间基映射:slam 时间戳与数据时间戳之间是**线性关系**(实测斜率
    1.02,采集端时钟略快)。用「每个非空格子的最后一组对应本帧时刻」
    的锚点做最小二乘拟合,不依赖外部时钟。

    返回 ``(frame_count, 7)``;样本不足返回 ``None``。
    """
    import numpy as np

    if frame_count <= 0:
        return None
    stamp = np.asarray(timestamps, dtype=float).reshape(-1)[:frame_count]
    if stamp.size < frame_count:
        stamp = np.concatenate([stamp,
                                np.full(frame_count - stamp.size, np.nan)])

    # 1) 汇总样本,并收集时间基锚点(非空格子的最后一组 ≈ 本帧时刻)
    samples: list[tuple[float, list[float]]] = []
    anchor_slam: list[float] = []
    anchor_data: list[float] = []
    has_anchor = ~np.isnan(stamp)
    for index in range(min(frame_count, len(trajectories))):
        values = np.asarray(trajectories[index], dtype=float).reshape(-1)
        groups = values.size // 8
        if groups <= 0:
            continue
        for g in range(groups):
            chunk = values[g * 8:g * 8 + 8]
            if not np.isfinite(chunk).all():
                continue
            samples.append((float(chunk[0]), [float(v) for v in chunk[1:8]]))
        if has_anchor[index]:
            anchor_slam.append(float(values[(groups - 1) * 8]))
            anchor_data.append(float(stamp[index]))
    if len(samples) < 2 or len(anchor_slam) < 2:
        return None

    # 2) 线性映射 slam 时间 → 数据时间:data = slope*slam + intercept
    slam_t = np.array(anchor_slam, dtype=float)
    data_t = np.array(anchor_data, dtype=float)
    slope, intercept = np.polyfit(slam_t, data_t, 1)
    if not np.isfinite(slope) or slope <= 0:
        return None

    samples.sort(key=lambda item: item[0])
    sample_t = (np.array([item[0] for item in samples], dtype=float)
                * slope + intercept)
    sample_p = np.array([item[1] for item in samples], dtype=float)

    # 3) 逐帧取时间上最近的样本。超过约一帧半间隔就认为真的没有数据,
    #    标为缺失留给插值,避免把远处的样本硬贴到当前帧。
    poses = np.zeros((frame_count, 7), dtype=float)
    missing = np.zeros(frame_count, dtype=bool)
    dt = np.nanmedian(np.diff(stamp)) if frame_count > 1 else 1.0 / 30.0
    if not np.isfinite(dt) or dt <= 0:
        dt = 1.0 / 30.0
    tolerance = dt * 1.5
    for index in range(frame_count):
        if np.isnan(stamp[index]):
            missing[index] = True
            continue
        j = int(np.abs(sample_t - stamp[index]).argmin())
        if abs(sample_t[j] - stamp[index]) > tolerance:
            missing[index] = True
            continue
        poses[index] = sample_p[j]

    # 4) 真实样本覆盖不到的少数帧(实测约 3%)才插值兜底
    valid = np.where(~missing)[0]
    if len(valid) < 2:
        return None
    for index in np.where(missing)[0]:
        lo = valid[valid < index]
        hi = valid[valid > index]
        if len(lo) and len(hi):
            a, b = int(lo[-1]), int(hi[0])
            w = (index - a) / (b - a)
            poses[index] = (1 - w) * poses[a] + w * poses[b]
        else:
            poses[index] = poses[int(lo[-1])] if len(lo) else poses[int(hi[0])]

    norms = np.linalg.norm(poses[:, 3:7], axis=1, keepdims=True)
    poses[:, 3:7] = poses[:, 3:7] / np.maximum(norms, 1e-12)
    return poses


def _gripper_fraction(states, frame_count: int) -> np.ndarray:
    """取夹爪开合度 → 0–1。契约 ``[percent, gripped, raw]``,只用首维。

    ``gripped`` 恒为 0、``raw`` 是带噪编码器计数,都不参与 action。
    """
    fraction = np.zeros(frame_count)
    total = len(states) if states is not None else 0
    for frame in range(frame_count):
        raw = states[frame] if frame < total else None
        if raw is None:
            continue
        values = np.asarray(raw, dtype=float).ravel()
        if values.size:
            fraction[frame] = float(values[0]) / 100.0
    # 采集端开合度是 0–100;越界值(标定异常)夹到合法区间的边界,
    # 避免动作维度出现 >1 的离群值把归一化统计带偏。
    return np.clip(fraction, 0.0, 1.0)


def derive_action(trajectories, timestamps, gripper_states,
                  frame_count: int) -> dict[int, dict] | None:
    """派生 UMI 口径的 action 与 proprioception。

    位姿**只从** ``observation.slam_trajectory`` 重建 —— 它是采集端的高频
    样本缓冲,丢帧时相邻格子里仍有真实样本(实测可覆盖 97% 的丢帧);
    而 ``observation.slam_pose`` 在同样位置直接写零,只能靠插值硬补,
    还会横跨 SLAM 重定位跳变(实测产生过 113.9mm 的假位移)。

    返回 ``{frame_index: {"action": [7], "observation.state": [4]}}``:

    - ``action``(7 维):当前夹爪局部系的增量位姿 + 夹爪开合,见上文;
    - ``observation.state``(4 维):``[Δx, Δy, Δz, gripper]``,位置为
      **相对本集起点**的偏移。ACT 用单帧观测,没有 state 时模型无从得知
      夹爪当前在哪(只能看图像猜);而 SLAM 的世界原点是每集任意初始化的,
      给绝对坐标会把无关的世界位置当特征,故取相对量。

    输入按 ``frame_index`` 顺序排列。无可用样本(非 UMI 数据、或全程丢帧)
    返回 ``None``,调用方应跳过该集而非写入垃圾。

    末帧没有 ``t+1``,沿用倒数第二帧的增量 —— 与 LeRobot 的
    ``next.done`` 语义一致(末帧只是终止标记,不承载新动作)。
    """
    if frame_count <= 0:
        return None
    cleaned = poses_from_trajectory(trajectories, timestamps, frame_count)
    if cleaned is None:
        return None
    if len(cleaned) != frame_count:
        # 位姿列与帧数不一致时以较短者为准,避免错位写入。
        frame_count = min(frame_count, len(cleaned))

    fraction = _gripper_fraction(gripper_states, frame_count)
    # observation.state 的位置分量取**相对本集起点**的偏移。SLAM 的世界
    # 原点是每次采集初始化时任意确定的,直接给绝对坐标会让模型把无关的
    # 世界坐标当特征;相对起点才是可迁移的"相对自己出发点多远"。
    origin = cleaned[0, :3].copy()
    result: dict[int, dict] = {}

    for frame in range(frame_count):
        if frame < frame_count - 1:
            current = quat_to_matrix(cleaned[frame, 3:7])
            following = quat_to_matrix(cleaned[frame + 1, 3:7])
            index = frame + 1
        else:
            # 末帧沿用倒数第二帧的位姿对,只取夹爪当前值。
            if frame_count < 2:
                continue
            current = quat_to_matrix(cleaned[frame - 1, 3:7])
            following = quat_to_matrix(cleaned[frame, 3:7])
            index = frame
        if current is None or following is None:
            continue

        if frame < frame_count - 1:
            delta_position = current.T @ (cleaned[frame + 1, :3]
                                          - cleaned[frame, :3])
        else:
            delta_position = current.T @ (cleaned[frame, :3]
                                          - cleaned[frame - 1, :3])
        delta_rotation = matrix_to_rotvec(current.T @ following)

        action = np.concatenate([
            delta_position, delta_rotation, [fraction[min(index, frame_count - 1)]],
        ])
        # 非有限值宁缺勿脏:该帧不写,由下游按缺失处理。
        if not np.all(np.isfinite(action)):
            continue
        result[frame] = {
            "action": [float(value) for value in action],
            "observation.state": [
                float(value) for value in np.concatenate([
                    cleaned[frame, :3] - origin,
                    [fraction[frame]],
                ])
            ],
        }
    return result or None
