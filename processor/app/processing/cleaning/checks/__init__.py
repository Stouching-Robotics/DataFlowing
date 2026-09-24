"""清洗检查项注册表 —— 一个文件 = 一个检查项，自动发现。

与 app/processing/modules/ 同一套思路（见那里的 __init__.py）:

    for _m in pkgutil.iter_modules(__path__):
        importlib.import_module(...)

新增检查项只需在 checks/ 下放一个 .py，内部用 @register_check 装饰类定义。

检查项**按信道/模态声明需求**，不按"这个 episode 是 UMI 还是 EGO"。
所以同一份检查项集合能自动适配任何数据组合:
    UMI 夹爪来了 → 只跑需要 slam/force/gripper_state 的那些
    触觉手套来了 → 只跑需要 tactile 的那些
    EGO 来了     → 只跑需要 rgb/keypoints 的那些
这就是 PDF §2「用采集模板决定跑哪些清洗规则」的落地方式。
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Callable, Iterable

from app.processing.cleaning.contract import (
    Finding, Range, PASS, WARN, FAIL, PENDING, ERROR,
    SEVERITY_LABELS, worst, is_blocking,
)

__all__ = [
    "CheckContext", "StreamEvidence", "CleaningCheck", "register_check",
    "get_check", "all_checks", "all_check_specs", "applicable_checks",
    "checks_for_device_cards",
    "Finding", "Range", "PASS", "WARN", "FAIL", "PENDING", "ERROR",
    "SEVERITY_LABELS", "worst", "is_blocking",
]

_registry: dict[str, "CleaningCheck"] = {}


@dataclass(frozen=True)
class StreamEvidence:
    """一条数据流的证据 —— 由 evidence.py 采集，检查项**只读不采**。

    把"读磁盘"和"做判断"分开，是让检查项可纯测的关键：测试直接构造本对象，
    不需要真的 mp4 / parquet / cv2。
    """

    key: str                             # 流标识，如 "umi_rgb" / "slam_trajectory"
    channel: str = ""                    # rgb | depth | slam | tactile | ...
    path: str = ""                       # 相对批次目录的路径（仅用于报错定位）
    # video_quality 的报告（黑屏/冻结/丢帧/解码）。非视频流为空 dict。
    # 结构见 app/video_quality.py::_check_stream 的返回。
    video: dict[str, Any] = dc_field(default_factory=dict)
    # parquet 概览
    columns: tuple[str, ...] = ()
    rows: int = 0
    fps: float = 0.0
    # 逐帧数值序列（按帧对齐）。evidence.py 采集时**派生好的**，检查项不自己算。
    # 例：SLAM 流 → {"pose": ((x,y,z,qx,qy,qz,qw), ...), "t": (...)}
    #     夹爪流 → {"gripper_state": ((open, gripped, raw), ...),
    #               "force": ((fx,fy,fz), ...)}
    # ★ 派生复用 app/umi_slam_action.py 的函数，保证与 action 口径同源。
    series: dict[str, tuple] = dc_field(default_factory=dict)


@dataclass
class CheckContext:
    """检查项执行上下文。

    IO 走注入（``read_columns`` / ``probe_video`` …），这样检查项本体是纯逻辑，
    测试可以喂假数据而不需要真的 mp4 或 cv2。
    """

    batch_dir: Path                      # 批次目录（含 data/ videos/ meta/）
    episode_id: str = ""
    channels: frozenset[str] = frozenset()      # 实际存在的信道
    modalities: tuple[str, ...] = ()            # 实际存在的设备模态
    params: dict[str, Any] = dc_field(default_factory=dict)   # 节点配置里的阈值
    duration_sec: float = 0.0
    fps: float = 0.0
    progress: Callable[[float], None] = lambda _value: None

    # ── 证据包（evidence.py 采集好，检查项只读）────────────────
    streams: tuple["StreamEvidence", ...] = ()
    # parquet 侧的汇总：行数 / 时间戳列等，跨流共用的信息
    data_rows: int = 0
    data_columns: tuple[str, ...] = ()

    # ── 注入式 IO ────────────────────────────────────────────
    read_columns: Callable[[Path], list[str]] = lambda _p: []
    read_head: Callable[[Path, int], list[dict]] = lambda _p, _n: []
    probe_video: Callable[[Path], dict] = lambda _p: {}

    # ── 证据查询便捷方法 ──────────────────────────────────────
    def streams_of(self, channel: str) -> tuple["StreamEvidence", ...]:
        """取某信道的全部流。检查项用它筛选自己要看的流。"""
        return tuple(item for item in self.streams if item.channel == channel)

    def param(self, name: str, default: Any = None) -> Any:
        """取阈值配置；节点未配置时回落到检查项声明的默认值。"""
        value = self.params.get(name)
        return default if value is None else value


class CleaningCheck:
    """清洗检查项基类。

    子类填元数据 + 实现 ``run()``，用 ``@register_check`` 装饰即可被自动发现。
    """

    slug: str = ""                       # "video.frame_drop"
    label: str = ""
    version: str = "1.0"                 # 改了要 bump —— 进 ruleset_revision
    description: str = ""

    # 必需的信道/设备模态；全部满足才跑
    requires_channels: tuple[str, ...] = ()
    requires_modality: tuple[str, ...] = ()
    # 需要 ≥2 类设备模态同时存在（PDF 的"跨模态联合检查"）
    cross_modal: bool = False

    # ★ 本项属于哪些【设备卡片】—— 前端按它把检查项分到设置面板的各 tab。
    #
    # 取值来自 contract.DEVICE_MODALITIES，空元组 = 不绑定单个设备
    # （跨设备检查用，它们只在"跨设备" tab 里出现）。
    #
    # 为什么用声明而不是靠目录分：同一个检查项往往多张卡片都要用 ——
    # ``video.frame_drop`` 对 RGB Camera、RGB-D Camera、UMI Gripper 都成立，
    # 按目录分只能放一处。声明式的话它能同时出现在多个 tab 里，
    # 且各自的阈值配置互不影响（见 node.data.config.devices.<卡片>.params）。
    device_cards: tuple[str, ...] = ()

    default_severity: str = FAIL
    # 阈值默认值 —— 节点配置可覆盖。PDF 明确要求"阈值不要照搬"，
    # 所以这里给的是**平台初始值**，必须在真实数据上验证后再固化。
    default_params: dict[str, Any] = dc_field(default_factory=dict)

    def param(self, ctx: "CheckContext", name: str, fallback: Any = None) -> Any:
        """取阈值。顺序：节点配置 → 本项声明的 default_params → fallback。

        检查项一律用这个方法读阈值，不要写 ``ctx.params.get(..., 字面量)`` ——
        那样声明的 default_params 就成了死代码，改它不生效。
        """
        value = (ctx.params or {}).get(name)
        if value is not None:
            return value
        if name in self.default_params:
            return self.default_params[name]
        return fallback

    def run(self, ctx: CheckContext) -> list[Finding]:
        raise NotImplementedError

    # ── 便捷构造 ─────────────────────────────────────────────
    def finding(self, ctx: CheckContext, status: str, *,
                stream: str = "", message: str = "",
                metrics: dict | None = None,
                ranges: Iterable[Range] = ()) -> Finding:
        return Finding(
            rule=self.slug, version=self.version, status=status,
            scope="cross_modal" if self.cross_modal else "stream",
            stream=stream, message=message, metrics=dict(metrics or {}),
            ranges=tuple(ranges),
        )


def checks_for_device_cards(cards: Iterable[str]) -> dict[str, list[CleaningCheck]]:
    """按【设备卡片】把检查项分组 —— 前端设置面板的 tab 就按这个渲染。

    只返回传进来的卡片（= 工作流里实际连接的设备），未连接的不会出现。
    一个检查项可能落在多个卡片下（如 ``video.frame_drop`` 对每种带摄像头的
    设备都成立），各 tab 里的阈值配置互不影响。

    例::

        checks_for_device_cards(["rgbd_camera", "glove_sensor"])
        → {"rgbd_camera": [video.frame_drop, video.black_screen, ...],
           "glove_sensor": [glove.tactile_health, ...]}
    """
    wanted = [str(card) for card in cards]
    grouped: dict[str, list[CleaningCheck]] = {card: [] for card in wanted}
    for check in all_checks():
        for card in check.device_cards:
            if card in grouped:
                grouped[card].append(check)
    return grouped


def register_check(cls):
    """按 slug 注册检查项（装饰器）。"""
    if not cls.slug:
        raise ValueError(f"Check {cls.__name__} has no slug")
    if cls.slug in _registry:
        raise ValueError(f"Duplicate check slug: {cls.slug}")
    _registry[cls.slug] = cls()
    return cls


def get_check(slug: str) -> CleaningCheck | None:
    return _registry.get(slug)


def all_checks() -> list[CleaningCheck]:
    """全部已注册检查项（按 slug 排序，保证 ruleset_revision 稳定）。"""
    return [_registry[key] for key in sorted(_registry)]


def all_check_specs() -> list[dict]:
    """检查项目录 —— 给 API / UI 用。

    前端配置面板直接渲染这个：按 ``category`` 分组，每个检查项一行开关，
    下面按 ``default_params`` 动态生成输入框。新增检查项前端零改动。
    """
    return [
        {
            "slug": check.slug,
            # 分类取自 slug 前缀（video.frame_drop → video），与 checks/ 下的
            # 目录名一致。前端按它分组。
            "category": check.slug.split(".", 1)[0] if "." in check.slug else "other",
            "label": check.label,
            "version": check.version,
            "description": check.description,
            "requires_channels": list(check.requires_channels),
            "requires_modality": list(check.requires_modality),
            "cross_modal": check.cross_modal,
            "device_cards": list(check.device_cards),
            "default_severity": check.default_severity,
            "default_params": dict(check.default_params),
        }
        for check in all_checks()
    ]


def applicable_checks(channels: Iterable[str],
                      modalities: Iterable[str],
                      *,
                      cross_modal_active: bool = False,
                      enabled: Iterable[str] | None = None,
                      disabled: Iterable[str] | None = None) -> list[CleaningCheck]:
    """筛出该跑哪些检查项。纯函数。

    - ``requires_channels`` / ``requires_modality`` 必须**全部**具备
    - ``cross_modal`` 的额外要求至少两类模态同时存在
    - ``enabled`` / ``disabled`` 是节点配置里的按类型开关
    """
    have_channels = {str(item) for item in channels}
    have_modalities = {str(item) for item in modalities}
    allow = {str(item) for item in enabled} if enabled is not None else None
    deny = {str(item) for item in disabled or ()}

    result: list[CleaningCheck] = []
    for check in all_checks():
        if not set(check.requires_channels) <= have_channels:
            continue
        if not set(check.requires_modality) <= have_modalities:
            continue
        # ★ 设备卡片闸门 —— 声明了归属的检查项只在【对应设备】上跑。
        #
        # 光按信道筛不够：`umi.action_present` 只要"有 action 列"就满足，
        # 而手套项目同样有一列（采集端写的全零占位）—— 结果 UMI 的检查跑到了
        # 手套项目上、报出一个对方根本不关心的失败。
        # 空 device_cards = 跨设备检查，不设闸门。
        if check.device_cards and not (set(check.device_cards) & have_modalities):
            continue
        if check.cross_modal and not cross_modal_active:
            continue
        if allow is not None and check.slug not in allow:
            continue
        if check.slug in deny:
            continue
        result.append(check)
    return result


# 自动发现检查项。
#
# 用 walk_packages 而非 iter_modules —— 前者递归进子包，后者只认平铺的 .py。
# 检查项按数据类型分类放在子包里，目录即分类，便于定位:
#
#     checks/video/frame_drop.py      视频
#     checks/umi/slam_continuity.py   UMI 夹爪
#     checks/glove/baseline_drift.py  触觉手套
#     checks/ego/hand_side.py         EGO 头戴
#     checks/depth/presence.py        深度
#     checks/cross/video_data_sync.py 跨模态
#
# 子包不需要写任何注册代码 —— 空 __init__.py 即可，里面的模块会被自动导入。
# 新增一项检查 = 在对应目录放一个 .py，注册表/节点/前端都不用改。
for _info in pkgutil.walk_packages(__path__, prefix=f"{__name__}."):
    if _info.name.rsplit(".", 1)[-1].startswith("_"):
        continue
    importlib.import_module(_info.name)
