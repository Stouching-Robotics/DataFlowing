"""Strict parsing for the Fays ORB bridge stdout protocol.

This module is deliberately side-effect free.  It does not know about Tk,
process handles, files, or the application composition root.
"""

from dataclasses import dataclass
import math
import re
from types import MappingProxyType

# 桥接 stdout 诊断协议的三张字段表。原版挂在 fps_history.FpsHistory 上，
# 本包不复制 fps_history 全模块，只内嵌这三张被协议解析依赖的常量表。
ORB_DIAGNOSTIC_SPECS = (
    ("state", "orb_tracking_state", "int"),
    ("inliers", "orb_inliers", "int"),
    ("maps", "orb_maps", "int"),
    ("active_kf", "orb_active_kf", "int"),
    ("active_mp", "orb_active_mp", "int"),
    ("local_kf", "orb_local_kf", "int"),
    ("local_mp", "orb_local_mp", "int"),
    ("lm_queue", "orb_lm_queue", "int"),
    ("imu_batch_avg", "orb_imu_batch_avg", "float"),
    ("imu_integrated", "orb_imu_integrated", "int"),
    ("imu_queue", "orb_imu_queue", "int"),
    ("kf_created", "orb_kf_created", "int"),
    ("kf_destroyed", "orb_kf_destroyed", "int"),
    ("kf_heap_live", "orb_kf_heap_live", "int"),
    ("kf_nonactive_live", "orb_kf_nonactive_live", "int"),
    ("mp_created", "orb_mp_created", "int"),
    ("mp_destroyed", "orb_mp_destroyed", "int"),
    ("mp_heap_live", "orb_mp_heap_live", "int"),
    ("mp_nonactive_live", "orb_mp_nonactive_live", "int"),
    ("mp_cleanup_passes", "orb_mp_cleanup_passes", "int"),
    ("mp_cleanup_candidates", "orb_mp_cleanup_candidates", "int"),
    ("mp_cleanup_retired", "orb_mp_cleanup_retired", "int"),
    ("rss_kb", "orb_rss_kb", "int"),
    ("swap_kb", "orb_swap_kb", "int"),
    ("minor_faults", "orb_minor_faults", "int"),
    ("major_faults", "orb_major_faults", "int"),
)
ORB_MAP_DIAGNOSTIC_SPECS = (
    ("state", "orb_tracking_state", "int"),
    ("inliers", "orb_inliers", "int"),
    ("maps", "orb_maps", "int"),
    ("active_kf", "orb_active_kf", "int"),
    ("active_mp", "orb_active_mp", "int"),
    ("local_kf", "orb_local_kf", "int"),
    ("local_mp", "orb_local_mp", "int"),
    ("lm_queue", "orb_lm_queue", "int"),
    ("kf_created", "orb_kf_created", "int"),
    ("mp_created", "orb_mp_created", "int"),
)
MP_CLEANUP_SPECS = (
    ("passes", "orb_mp_payload_cleanup_passes", "int"),
    ("candidates", "orb_mp_payload_cleanup_candidates", "int"),
    ("compacted", "orb_mp_payload_cleanup_compacted", "int"),
    ("queued", "orb_mp_payload_cleanup_queued", "int"),
    ("duration_ms", "orb_mp_payload_cleanup_duration_ms", "float"),
    ("rss_before_kb", "orb_mp_payload_cleanup_rss_before_kb", "int"),
    ("rss_after_kb", "orb_mp_payload_cleanup_rss_after_kb", "int"),
    ("trigger_elapsed_s", "orb_mp_payload_cleanup_trigger_s", "float"),
)


FAYS_STAGE_KEYS = ("wait_ms", "preprocess_ms", "middle_ms", "post_ms")

# The gripper cannot physically cover these distances between consecutive
# 25 Hz SLAM samples.  The small fixed allowance absorbs normal estimator
# corrections; the speed term keeps the gate independent of the exact FPS.
POSE_MAX_ABS_POSITION_M = 10.0
POSE_STEP_ALLOWANCE_M = 0.05
POSE_MAX_LINEAR_SPEED_MPS = 3.0
POSE_MAX_GUARD_INTERVAL_S = 0.1
POSE_ANGLE_ALLOWANCE_RAD = math.radians(20.0)
POSE_MAX_ANGULAR_SPEED_RAD_S = math.radians(1080.0)

_POSE_RE = re.compile(
    r"\[([\d.]+)\]\s*XYZ:\(([-\d.e+]+),([-\d.e+]+),([-\d.e+]+)\)\s*"
    r"Quat:\(w=([-\d.e+]+),x=([-\d.e+]+),y=([-\d.e+]+),z=([-\d.e+]+)\)"
)
_FPS_RE = re.compile(
    r"^\[FPS_DATA\]\s+image=([\d.]+)\s+imu=([\d.]+)\s+"
    r"process=([\d.]+)"
    r"(?:\s+wait_ms=([\d.]+)\s+preprocess_ms=([\d.]+)"
    r"\s+middle_ms=([\d.]+)\s+post_ms=([\d.]+))?$"
)
_TIME_REGRESSION_RE = re.compile(
    r"^\[TIME_(DROP|REBASE)\]\s+ts=([-\d.e+]+)\s+previous=([-\d.e+]+)\s+"
    r"delta=([-\d.e+]+)\s+seq=(\d+)(?:\s+prev_seq=(-?\d+))?\s+"
    r"consecutive=(\d+)\s+total=(\d+)$"
)
_ERROR_RE = re.compile(
    r"(error|failed|exception|cannot|unable|denied|timeout|timed out|"
    r"no device|not found|no such|device busy|resource busy)",
    re.IGNORECASE,
)
_IGNORED_NON_FATAL_RE = re.compile(
    r"^\[FAYS-CALIB\]\s+WARN\s+SetStereoFPS\(\d+\)\s+failed$"
    # ld.so 报告某个 LD_PRELOAD 条目加载失败——它自带 "ignored."，是信息性
    # 输出，但文案里的 "cannot" 会命中 _ERROR_RE → state.error，进而把
    # wait_sdk_ready 的就绪门锁死（2026-09-10 18:32 那次假超时的起因）。
    r"|^ERROR: ld\.so: object .+ from LD_PRELOAD .*ignored\.$"
)


@dataclass(frozen=True)
class PoseSample:
    """One pose emitted by the Fays ORB bridge."""

    position: tuple
    rotation: tuple
    timestamp: float
    device_name: str = "SLAM"
    sequence: int = 0
    arrival_monotonic: object = None
    arrival_wall_time: object = None

    @property
    def valid(self):
        values = (*self.position, *self.rotation)
        quaternion_norm = math.sqrt(sum(
            value * value for value in self.rotation
        ))
        arrival_values = (
            self.arrival_monotonic,
            self.arrival_wall_time,
        )
        return (
            math.isfinite(self.timestamp)
            and all(math.isfinite(value) for value in values)
            and int(self.sequence) >= 0
            and all(
                value is None or math.isfinite(float(value))
                for value in arrival_values
            )
            and all(
                abs(value) < POSE_MAX_ABS_POSITION_M
                for value in self.position
            )
            and 0.5 < quaternion_norm < 1.5
        )


def pose_rejection_reason(current, previous=None):
    """Return why a live pose must be held, or ``None`` when it is safe.

    The native bridge is the primary continuity owner.  This side-effect-free
    check is the final boundary before a pose can reach UI/recording state.
    """

    if not isinstance(current, PoseSample) or not current.valid:
        return "invalid pose components"
    if previous is None:
        return None
    if not isinstance(previous, PoseSample) or not previous.valid:
        return "invalid previous pose"

    interval = current.timestamp - previous.timestamp
    if not math.isfinite(interval) or interval <= 0.0:
        return "non-monotonic pose timestamp"
    guarded_interval = min(interval, POSE_MAX_GUARD_INTERVAL_S)

    distance = math.sqrt(sum(
        (current.position[index] - previous.position[index]) ** 2
        for index in range(3)
    ))
    distance_limit = (
        POSE_STEP_ALLOWANCE_M
        + POSE_MAX_LINEAR_SPEED_MPS * guarded_interval
    )
    if distance > distance_limit:
        return (
            f"translation step {distance:.3f}m exceeds "
            f"{distance_limit:.3f}m"
        )

    previous_norm = math.sqrt(sum(
        value * value for value in previous.rotation
    ))
    current_norm = math.sqrt(sum(
        value * value for value in current.rotation
    ))
    dot = abs(sum(
        previous.rotation[index] * current.rotation[index]
        for index in range(4)
    ) / (previous_norm * current_norm))
    angle = 2.0 * math.acos(max(-1.0, min(1.0, dot)))
    angle_limit = (
        POSE_ANGLE_ALLOWANCE_RAD
        + POSE_MAX_ANGULAR_SPEED_RAD_S * guarded_interval
    )
    if angle > angle_limit:
        return (
            f"rotation step {math.degrees(angle):.1f}deg exceeds "
            f"{math.degrees(angle_limit):.1f}deg"
        )
    return None


@dataclass(frozen=True)
class FaysRateSample:
    image_input: float
    imu_input: float
    process: float
    stage_times: object = None


@dataclass(frozen=True)
class FrameTimeRegression:
    """桥接时间戳单调性守卫的一次判定（``[TIME_DROP]``/``[TIME_REBASE]``）。

    ``verdict`` 为 ``drop``（陈旧帧已在入库前丢弃）或 ``rebase``（连续回退
    达上限，判定为时钟真跳变，换基准放行给 SLAM）。``delta`` 是相对上一条
    **保留**帧的回退量（秒，恒为负）。

    ``seq`` 是本帧的 SDK 序号，``prev_seq`` 是上一条**真正进了 SLAM** 的帧的
    SDK 序号（老版本桥接没有这个字段时为 ``None``）。两者一比即可定形态：

    * ``seq < prev_seq`` —— SDK 把一条比自己已交付过的帧还旧的帧给了我们，
      投递确实乱序，丢的是真实数据；
    * ``seq == prev_seq`` —— 重复投递同一帧，丢弃零损失；
    * ``seq > prev_seq`` —— 序号在正常前进，是**时间戳字段**配错了旧值。

    注意 ``prev_seq`` 跨度不恒为 1：入库前有 3-of-5 抽帧，正常递增时本帧
    比上一条入库帧大 1~3。
    """

    verdict: str
    ts: float
    previous: float
    delta: float
    seq: int
    consecutive: int
    total: int
    prev_seq: object = None          # Optional[int]；老桥接无此字段时为 None


@dataclass(frozen=True)
class ProtocolEvent:
    """A classified stdout line.

    ``kind`` is one of ``pose``, ``fays_rates``, ``time_drop``,
    ``time_rebase``, ``orb_diagnostic``, ``orb_stage``, ``affinity``,
    ``ready``, ``origin``, ``status``, ``error`` or ``log``.
    """

    kind: str
    payload: object
    raw: str


def parse_orb_diagnostic_line(line):
    """Parse one complete ``[ORB_DIAG]`` record or return ``None``."""
    prefix = "[ORB_DIAG] "
    if not isinstance(line, str) or not line.startswith(prefix):
        return None

    encoded = {}
    for token in line[len(prefix):].split():
        name, separator, value = token.partition("=")
        if not separator or not name or not value or name in encoded:
            return None
        encoded[name] = value

    specs = ORB_DIAGNOSTIC_SPECS
    expected = {protocol_name for protocol_name, _csv_name, _kind in specs}
    if not expected.issubset(encoded):
        return None

    parsed = {}
    try:
        for protocol_name, _csv_name, kind in specs:
            if kind == "float":
                value = float(encoded[protocol_name])
                if not math.isfinite(value):
                    return None
            else:
                value = int(encoded[protocol_name], 10)
            if protocol_name != "state" and value < 0:
                return None
            parsed[protocol_name] = value
    except (TypeError, ValueError, OverflowError):
        return None
    return MappingProxyType(parsed)


def parse_orb_map_diagnostic_line(line):
    """Parse one complete read-only ``[ORB_MAP_DIAG]`` record."""
    prefix = "[ORB_MAP_DIAG] "
    if not isinstance(line, str) or not line.startswith(prefix):
        return None

    encoded = {}
    for token in line[len(prefix):].split():
        name, separator, value = token.partition("=")
        if not separator or not name or not value or name in encoded:
            return None
        encoded[name] = value

    specs = ORB_MAP_DIAGNOSTIC_SPECS
    expected = {protocol_name for protocol_name, _csv_name, _kind in specs}
    if set(encoded) != expected:
        return None

    parsed = {}
    try:
        for protocol_name, _csv_name, kind in specs:
            value = (
                float(encoded[protocol_name])
                if kind == "float"
                else int(encoded[protocol_name], 10)
            )
            if kind == "float" and not math.isfinite(value):
                return None
            if protocol_name != "state" and value < 0:
                return None
            parsed[protocol_name] = value
    except (TypeError, ValueError, OverflowError):
        return None
    return MappingProxyType(parsed)


def parse_mp_cleanup_line(line):
    """Parse one complete ``[MP_CLEANUP]`` result or return ``None``."""
    prefix = "[MP_CLEANUP] "
    if not isinstance(line, str) or not line.startswith(prefix):
        return None
    encoded = {}
    for token in line[len(prefix):].split():
        name, separator, value = token.partition("=")
        if not separator or not name or not value or name in encoded:
            return None
        encoded[name] = value
    parsed = {}
    try:
        for name, _csv_name, kind in MP_CLEANUP_SPECS:
            value = float(encoded[name]) if kind == "float" else int(encoded[name])
            if not math.isfinite(value) or value < 0:
                return None
            parsed[name] = value
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return MappingProxyType(parsed)


def _parse_pose(line):
    match = _POSE_RE.search(line)
    if match is None:
        return None
    try:
        timestamp = float(match.group(1))
        x, y, z = (float(match.group(index)) for index in range(2, 5))
        qw, qx, qy, qz = (
            float(match.group(index)) for index in range(5, 9)
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return PoseSample(
        position=(x, y, z),
        rotation=(qx, qy, qz, qw),
        timestamp=timestamp,
    )


def rotate_pose_z90(pose):
    """S80M 轨迹坐标约定：绕 Z 轴旋转 +90°（X=右 Y=正对 Z=上）。

    真机实测定案（2026-09-09，新 ORB 核心上线后）：native 输出
    相对物理系为 R_z(−90°)——native X=正对、Y=左、Z=上（实测：
    物理 +Y(正对) 位移 → native +X；物理 +X(右) → native −Y），
    与桥接 .cc 注释约定一致。注意：旧核心时代的约定是 R_x(+90°)
    （X=右 Y=下 Z=正对），换核后世界系约定已变，此前基于旧核的
    rotate_pose_xm90（绕 X −90°）已随旧核失效。修正 = 位置与姿态
    统一纯左乘 R_z(+90°)（= native 约定之逆，纯世界重标，无体轴
    共轭）：
    位置 (x,y,z)→(−y,x,z)；四元数 q' = q_z(+90°)⊗q 化简为
    c·(x−y, y+x, z+w, w−z)，c=√2/2。
    结果：X=右、Y=夹爪正对、Z=上（右手系，等价 ENU）。
    静止首帧姿态 = R_z(+90°)·correction（部署校正的常数残留，
    约 R_x(−90°)，非单位矩阵）——由 process_controller 的原点后
    姿态相对化（捕获首帧显示姿态为基准）吸收，首帧显示恒为
    单位矩阵，帧轴与世界轴对齐、无起始跳变。
    """
    x, y, z = pose.position
    qx, qy, qz, qw = pose.rotation
    c = math.sqrt(0.5)
    return PoseSample(
        position=(-y, x, z),
        rotation=(c * (qx - qy), c * (qy + qx), c * (qz + qw),
                  c * (qw - qz)),
        timestamp=pose.timestamp,
    )


def quat_conjugate(q):
    """四元数共轭（单位四元数时即逆）。"""
    x, y, z, w = q
    return (-x, -y, -z, w)


def quat_product(a, b):
    """Hamilton 积 a⊗b（等价矩阵左乘 a：先施加 b 再施加 a）。"""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by + ay * bw + az * bx - ax * bz,
        aw * bz + az * bw + ax * by - ay * bx,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _parse_fays_rates(line):
    match = _FPS_RE.fullmatch(line)
    if match is None:
        return None
    values = tuple(float(match.group(index)) for index in range(1, 4))
    stages = None
    if match.group(4) is not None:
        stages = MappingProxyType({
            key: float(match.group(index))
            for key, index in zip(FAYS_STAGE_KEYS, range(4, 8))
        })
    return FaysRateSample(*values, stage_times=stages)


def _parse_time_regression(line):
    match = _TIME_REGRESSION_RE.fullmatch(line)
    if match is None:
        return None
    prev_seq = match.group(6)
    return FrameTimeRegression(
        verdict=match.group(1).lower(),
        ts=float(match.group(2)),
        previous=float(match.group(3)),
        delta=float(match.group(4)),
        seq=int(match.group(5)),
        consecutive=int(match.group(7)),
        total=int(match.group(8)),
        prev_seq=int(prev_seq) if prev_seq is not None else None,
    )


def parse_slam_line(line):
    """Classify one stripped Fays bridge stdout line."""
    if not isinstance(line, str):
        return None
    raw = line.strip()
    if not raw:
        return None
    # This SDK build exposes SetStereoFPS as an unavailable/stubbed entry.
    # The serial-matched YAML already selects the requested 25 FPS mode, so
    # this non-fatal diagnostic must not put the GUI into the Error state.
    if _IGNORED_NON_FATAL_RE.fullmatch(raw):
        return None

    rates = _parse_fays_rates(raw)
    if rates is not None:
        return ProtocolEvent("fays_rates", rates, raw)

    regression = _parse_time_regression(raw)
    if regression is not None:
        return ProtocolEvent(
            "time_rebase" if regression.verdict == "rebase" else "time_drop",
            regression, raw,
        )

    map_diagnostic = parse_orb_map_diagnostic_line(raw)
    if map_diagnostic is not None:
        return ProtocolEvent("orb_diagnostic", map_diagnostic, raw)

    diagnostic = parse_orb_diagnostic_line(raw)
    if diagnostic is not None:
        return ProtocolEvent("orb_diagnostic", diagnostic, raw)
    cleanup = parse_mp_cleanup_line(raw)
    if cleanup is not None:
        return ProtocolEvent("mp_cleanup", cleanup, raw)

    if raw.startswith("[ORB_STAGE]"):
        return ProtocolEvent("orb_stage", raw, raw)
    if raw.startswith("[FAYS-AFFINITY]"):
        return ProtocolEvent("affinity", raw, raw)
    if "READY" in raw and "Press ENTER" in raw:
        return ProtocolEvent("ready", raw, raw)
    if "NEW ORIGIN" in raw or "ORIGIN SET" in raw:
        return ProtocolEvent("origin", raw, raw)

    pose = _parse_pose(raw)
    if pose is not None:
        return ProtocolEvent("pose", pose, raw)

    if (
        "IMU init" in raw
        or "Recording" in raw
        or "Segment" in raw
        or raw.startswith("[POSE_HOLD]")
        or raw.startswith("[POSE_REBASE]")
    ):
        return ProtocolEvent("status", raw, raw)
    if _ERROR_RE.search(raw):
        return ProtocolEvent("error", raw, raw)
    return ProtocolEvent("log", raw, raw)
