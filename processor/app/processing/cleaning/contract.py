"""清洗契约 —— 模态、信道、严重度的公共定义。

这个文件把"平台里同一个东西有好几个名字"这件事收敛到一处:

    device_naming.DEVICE_LABELS        mono_rgb / stereo_rgb / ...
    workflow_dispatch.semantic_type    同上（同一套值，只是没人敢这么说）
    api/projects._device_input_sources mono_camera / stereo_camera  ← 名字不一样
    处理模块 slug                       mono_camera / stereo_camera

**本模块不新建第 7 套枚举** —— DEVICE_MODALITIES 就是 device_naming 那 6 个值的
权威声明，其余几套通过 ``normalize_device_modality()`` 单向映射进来。新增设备类型时
只需改这一处，测试会锁住映射完整性。

信道(channel) 是比设备模态更细的粒度: PDF 里"IMU 可选""SLAM 必须"这类判断
是**信道级**的，硬塞进设备模态会毁掉它。所以设备模态 → 信道的推导放在这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

# ── 设备模态 ──────────────────────────────────────────────────
# 与 app/device_naming.py:15 DEVICE_LABELS 的键**逐字一致**。
# 改这里必须同步改那里，tests/test_cleaning_contract.py 会断言两边相等。
DEVICE_MODALITIES: tuple[str, ...] = (
    "mono_rgb",
    "stereo_rgb",
    "rgbd_camera",
    "stereo_rgbd_camera",
    "glove_sensor",
    "gripper_device",
)

# 「摄像机类」模态 —— 判定 collection_type 时要用
CAMERA_MODALITIES: frozenset[str] = frozenset({
    "mono_rgb", "stereo_rgb", "rgbd_camera", "stereo_rgbd_camera",
})

# 会产出 RGB 视频的**全部**设备卡片 —— ``video.*`` 类检查适用于它们每一个。
# 比 CAMERA_MODALITIES 多一个 gripper_device：UMI 夹爪自带鱼眼相机，
# 视频检查对同样它成立。
VIDEO_CAPABLE_MODALITIES: tuple[str, ...] = (
    "mono_rgb", "stereo_rgb", "rgbd_camera", "stereo_rgbd_camera",
    "gripper_device",
)

# ── 信道 ──────────────────────────────────────────────────────
# 检查项按信道声明需求，而不是按设备模态 —— 这样一条"SLAM 连续性"检查
# 既能被 UMI 夹爪触发，将来也能被别的带 SLAM 的设备复用。
CHANNELS: tuple[str, ...] = (
    "time",           # 时间戳 / 帧号
    "rgb",            # RGB 视频
    "depth",          # 深度
    "slam",           # SLAM 位姿/轨迹
    "gripper_state",  # 夹爪开合
    "force",          # 力 / 触觉（夹爪力）
    "tactile",        # 触觉阵列（手套）
    "imu",            # 惯性单元
    "keypoints",      # 手部关键点
    "action",         # 动作（派生列）
    # 设备的连接状态（``status.left_glove`` 这类**字符串**列）。
    # 单独算一个信道而不是塞进 tactile 的 series：它是"设备这一帧在不在"，
    # 与阵列读数正交 —— 手套掉线时阵列会静默保持最后一个值，看起来完全正常。
    "device_status",
)

# 设备模态 → 该设备**可能**提供的信道。
# 注意这是"可能"不是"必然" —— 实际有哪些信道由 collect.py 从真实列名推导，
# 这里只用来回答"这个模态需不需要某类检查"。
MODALITY_CHANNELS: dict[str, tuple[str, ...]] = {
    "mono_rgb":           ("rgb", "time"),
    "stereo_rgb":         ("rgb", "time"),
    "rgbd_camera":        ("rgb", "depth", "time"),
    "stereo_rgbd_camera": ("rgb", "depth", "time"),
    "glove_sensor":       ("tactile", "device_status", "imu", "time"),
    "gripper_device":     ("rgb", "slam", "gripper_state", "force", "action", "time"),
}

# 非设备来源的信道 —— 由处理节点产出，不属于任何输入设备
DERIVED_CHANNELS: frozenset[str] = frozenset({"keypoints", "action"})


def channels_for(modalities: Iterable[str]) -> tuple[str, ...]:
    """一组设备模态覆盖的全部信道（去重、保序）。纯函数。"""
    seen: dict[str, None] = {}
    for modality in modalities:
        for channel in MODALITY_CHANNELS.get(str(modality), ()):
            seen.setdefault(channel, None)
    return tuple(seen)


def pose_is_valid(item) -> bool:
    """位姿能不能用 —— 空元组和**全零占位**都不算。

    ★ 全零占位长得像合法位姿：长度 7、能索引、能相减。但它的四元数模是 0，
      任何涉及它的差分都会算出"瞬移"：

          两个零四元数的点积 = 0 → acos(0) = π/2 → 夹角 180°

      30fps 下就是 180°×30 = **94.25 rad/s** —— 一个物理上荒谬的角速度。
      实测 Test94 的 ``observation.slam_pose`` 有 437/599 行是全零，SLAM 连续性
      因此报出 465 处"跳变"（599 帧里几乎每一帧），把整集假判成 FAIL 并拦住批次。

    判据只看四元数模：**位置可以合法地是 (0,0,0)**（原点就是合法位姿），
    四元数模为 0 则一定不是合法朝向。

    放在 contract 而不是 collect：检查项要用它，而 collect 依赖 checks
    （检查项自动发现），collect ← checks 的导入会成环。
    """
    if not item or len(item) < 7:
        return False
    try:
        return sum(float(value) ** 2 for value in item[3:7]) > 1e-12
    except (TypeError, ValueError):
        return False


def normalize_device_modality(value: object) -> str | None:
    """把别处的设备/输入类型名映射到 canonical 设备模态。

    覆盖两套现存命名:
      * ``stereo_camera`` / ``mono_camera``（api/projects、processing 模块 slug）
      * ``fisheye_camera`` / ``rgb_camera``（历史工作流节点类型）
    认不出来返回 ``None``（调用方应跳过该值，而不是终止）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text in DEVICE_MODALITIES:
        return text
    return _ALIASES.get(text)


# 别名表 —— 只列真实存在于代码里的历史/并行命名。
# 新增别名时同步加进 tests/test_cleaning_contract.py 的用例。
_ALIASES: dict[str, str] = {
    # api/projects._device_input_sources 与处理模块 slug 用的是 camera 后缀
    "mono_camera": "mono_rgb",
    "stereo_camera": "stereo_rgb",
    "fisheye_camera": "mono_rgb",     # 历史节点，镜头类型对清洗不可见
    "rgb_camera": "mono_rgb",         # 历史节点
    "rgbd": "rgbd_camera",
    "stereo_rgbd": "stereo_rgbd_camera",
    # 中文/展示名兜底
    "umi": "gripper_device",
    "umi_gripper": "gripper_device",
    "glove": "glove_sensor",
}


# ── 严重度 ────────────────────────────────────────────────────
PASS = "pass"
WARN = "warn"
FAIL = "fail"
PENDING = "pending_review"
ERROR = "error"

# 越靠前越严重 —— worst() 与排序都用它
SEVERITY_ORDER: tuple[str, ...] = (ERROR, FAIL, PENDING, WARN, PASS)

# 面向用户的展示文案（报告里的 ``status_label``）。界面是英文的，这里保持一致。
SEVERITY_LABELS: dict[str, str] = {
    PASS: "Pass",
    WARN: "Warning",
    FAIL: "Fail",
    PENDING: "Pending review",
    ERROR: "Error",
}


def worst(statuses: Iterable[str]) -> str:
    """取一组状态里最严重的；空集合视为 PASS。纯函数。"""
    rank = {status: index for index, status in enumerate(SEVERITY_ORDER)}
    result = PASS
    best = len(SEVERITY_ORDER)
    for status in statuses:
        index = rank.get(str(status), len(SEVERITY_ORDER))
        if index < best:
            best = index
            result = str(status)
    return result


def is_blocking(status: str) -> bool:
    """该状态是否应当拦截（退回人工审核）。"""
    return str(status) in (FAIL, ERROR)


# ── 结构 ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Range:
    """一段时间区间 —— PDF §5「第二层结果」。"""

    start_sec: float
    end_sec: float
    code: str
    severity: str
    stream: str = ""
    channel: str = ""
    start_frame: int | None = None
    end_frame: int | None = None
    rule: str = ""
    message: str = ""
    evidence: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_sec": round(float(self.start_sec), 3),
            "end_sec": round(float(self.end_sec), 3),
            "code": self.code,
            "severity": self.severity,
            "stream": self.stream,
            "channel": self.channel,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "rule": self.rule,
            "message": self.message,
            "evidence": dict(self.evidence or {}),
        }


@dataclass(frozen=True)
class StreamResult:
    """一条数据流的检查结果 —— PDF §5「第一层结果」。"""

    stream: str
    modality: str = ""
    label: str = ""
    channels: tuple[str, ...] = ()
    status: str = PASS
    reasons: tuple[str, ...] = ()
    metrics: dict[str, Any] = dc_field(default_factory=dict)
    ranges: tuple[Range, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "modality": self.modality,
            "label": self.label,
            "channels": list(self.channels),
            "status": self.status,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics or {}),
            "ranges": [item.to_dict() for item in self.ranges],
        }


@dataclass(frozen=True)
class Finding:
    """一次检查产出的结论 —— 检查项 run() 的返回值单元。"""

    rule: str
    version: str
    status: str
    scope: str                        # stream | episode | cross_modal
    stream: str = ""
    message: str = ""
    metrics: dict[str, Any] = dc_field(default_factory=dict)
    ranges: tuple[Range, ...] = ()
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "version": self.version,
            "status": self.status,
            "scope": self.scope,
            "stream": self.stream,
            "message": self.message,
            "metrics": dict(self.metrics or {}),
            "ranges": [item.to_dict() for item in self.ranges],
            "elapsed_ms": self.elapsed_ms,
        }
