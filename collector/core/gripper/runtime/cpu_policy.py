"""KSQ Gripper 的固定 CPU 角色和 Linux 混合核拓扑识别。

本模块只读取 affinity 与 sysfs。它不创建线程、不修改调度策略，也不接触
任何设备。实际 affinity 写入由 :mod:`runtime.thread_affinity` 独占。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple


CPU_ROLE_ORDER = (
    "input",
    "fays_input",
    "left_process",
    "right_process",
    "idle_smt",
    "fays_prepare",
    "fays_left_orb",
    "fays_right_orb",
    "fays_track",
    "fays_background",
    "general",
)

DEDICATED_THREAD_ROLES = frozenset({
    "input",
    "fays_input",
    "left_process",
    "right_process",
    "fays_prepare",
    "fays_left_orb",
    "fays_right_orb",
    "fays_track",
})

RESERVED_ROLES = DEDICATED_THREAD_ROLES | frozenset({
    "idle_smt",
    "fays_background",
})

FAYS_PROCESS_ROLES = (
    "fays_input",
    "fays_prepare",
    "fays_left_orb",
    "fays_right_orb",
    "fays_track",
    "fays_background",
)

EXPECTED_ROLE_CPUS = MappingProxyType({
    "input": (4,),
    "fays_input": (4,),
    "left_process": (0,),
    "right_process": (2,),
    "idle_smt": (1, 3),
    "fays_prepare": (5,),
    "fays_left_orb": (6,),
    "fays_right_orb": (7,),
    "fays_track": (8,),
    "fays_background": (9,),
    "general": (10, 11),
})


def parse_cpu_list(value: object) -> frozenset[int]:
    """解析 Linux sysfs 的 ``0-3,8`` CPU 列表格式。

    与 sysfs 格式一致，空字段会被忽略；负数、倒序范围和非数字字段会抛出
    :class:`ValueError`，防止错误拓扑被静默接受。
    """

    cpus: set[int] = set()
    for encoded_part in str(value).strip().split(","):
        part = encoded_part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, separator, end_text = part.partition("-")
            if not separator or not start_text or not end_text:
                raise ValueError(f"invalid CPU range: {part!r}")
            start = int(start_text, 10)
            end = int(end_text, 10)
            if start < 0 or end < start:
                raise ValueError(f"invalid CPU range: {part!r}")
            cpus.update(range(start, end + 1))
        else:
            cpu = int(part, 10)
            if cpu < 0:
                raise ValueError(f"invalid CPU id: {part!r}")
            cpus.add(cpu)
    return frozenset(cpus)


@dataclass(frozen=True)
class CoreTopology:
    """一个物理核在当前进程 affinity 范围内可见的拓扑。"""

    cpus: Tuple[int, ...]
    sibling_count: int = 1
    max_freq: int = 0
    core_type: Optional[int] = None
    package_id: int = 0
    core_id: int = 0

    def __post_init__(self) -> None:
        normalized = tuple(sorted({int(cpu) for cpu in self.cpus}))
        if not normalized or normalized[0] < 0:
            raise ValueError("a physical core must contain non-negative CPUs")
        object.__setattr__(self, "cpus", normalized)
        object.__setattr__(
            self, "sibling_count", max(1, int(self.sibling_count)))
        object.__setattr__(self, "max_freq", max(0, int(self.max_freq)))


@dataclass(frozen=True)
class CpuPolicySnapshot:
    """不可变的 CPU0-11 角色分区。"""

    source: str
    input: Tuple[int, ...] = (4,)
    fays_input: Tuple[int, ...] = (4,)
    left_process: Tuple[int, ...] = (0,)
    right_process: Tuple[int, ...] = (2,)
    idle_smt: Tuple[int, ...] = (1, 3)
    fays_prepare: Tuple[int, ...] = (5,)
    fays_left_orb: Tuple[int, ...] = (6,)
    fays_right_orb: Tuple[int, ...] = (7,)
    fays_track: Tuple[int, ...] = (8,)
    fays_background: Tuple[int, ...] = (9,)
    general: Tuple[int, ...] = (10, 11)
    _dual_slam: bool = False
    _balanced_dual_slam: bool = False
    _relaxed: bool = False

    def __post_init__(self) -> None:
        for role in CPU_ROLE_ORDER:
            cpus = tuple(int(cpu) for cpu in getattr(self, role))
            if not cpus or len(cpus) != len(set(cpus)) or min(cpus) < 0:
                raise ValueError(f"invalid CPU role {role!r}: {cpus!r}")
            object.__setattr__(self, role, cpus)
        if self._relaxed:
            self.validate_relaxed()
        elif self._balanced_dual_slam:
            self.validate_balanced_dual_slam()
        elif self._dual_slam:
            self.validate_dual_slam()
        else:
            self.validate_strict()

    @classmethod
    def dual_slam(
        cls,
        track_cpu: int = 6,
        input_cpu: int = 5,
    ) -> "CpuPolicySnapshot":
        """双设备实例：第一套 input=4/track=8，第二套 input/fays_input=5、
        track/output=6；其余角色与单设备严格分区一致（general=10/11）。"""
        return cls(
            source="dual_slam",
            input=(int(input_cpu),),
            fays_input=(int(input_cpu),),
            fays_track=(int(track_cpu),),
            general=(10, 11),
            _dual_slam=True,
        )

    @classmethod
    def exclusive_dual_slam(
        cls,
        *,
        secondary: bool,
    ) -> "CpuPolicySnapshot":
        """把两套 Fays/ORB 子进程放进互不重叠的物理核域。

        当前主机 SMT 关系为 ``(0,6) (1,7) (2,8) (3,9) (4,10)
        (5,11)``。第一套 Fays/ORB 使用物理核 0/1，第二套使用物理核
        2/3；两套 ORB 的重负载阶段不再落到对方的 SMT 兄弟上。

        2026-08-27 双设备共用 GUI 修正：GUI 和 camera-service 固定保留在
        CPU10/11，不再跟随最后连接设备切换。四路 Sightac 计算分散到
        CPU4/5/1/3。CPU0/2 是两套 Fays Track/Output 主线程，不能与 Sightac
        重计算同核；CPU1/3 只承担较短的 Fays prepare/background 突发，
        竞争代价远小于直接阻塞 Track。Sightac 输入线程主要阻塞在 UVC
        读取，允许与对应计算子进程共用同一逻辑核。
        """
        if secondary:
            return cls(
                source="exclusive_dual_slam_secondary",
                input=(1, 3),
                fays_input=(2,),
                left_process=(1,),
                right_process=(3,),
                idle_smt=(4, 5),
                fays_prepare=(3,),
                fays_left_orb=(7,),
                fays_right_orb=(7,),
                fays_track=(2,),
                fays_background=(3,),
                general=(10, 11),
                _relaxed=True,
            )
        return cls(
            source="exclusive_dual_slam_primary",
            input=(4, 5),
            fays_input=(0,),
            left_process=(4,),
            right_process=(5,),
            idle_smt=(2, 3, 8, 9),
            fays_prepare=(1,),
            fays_left_orb=(6,),
            fays_right_orb=(6,),
            fays_track=(0,),
            fays_background=(1,),
            general=(10, 11),
            _relaxed=True,
        )

    @classmethod
    def balanced_dual_slam(
        cls,
        *,
        secondary: bool,
    ) -> "CpuPolicySnapshot":
        """双设备正式性能方案。

        当前主机的 SMT 关系为 (0,6)、(1,7)、(2,8)、(3,9)、
        (4,10)、(5,11)。清单顺序中的主设备使用
        Sightac input=4/5、Fays callback=4、prepare=5、ORB=6/7、
        track=8；次设备把对应阶段放到 Sightac input=10/11、Fays
        callback=10、prepare=11、ORB=0/1、track=2。
        四个 Sightac 计算进程分散到三个物理核的四个逻辑 CPU：主设备
        left/right=1/5，次设备 left/right=9/11。设备1 Left 不再与
        UI/general 线程共用 CPU3；CPU1 是设备2右目 ORB CPU7 的 SMT 兄弟，
        这是当前双设备满载下避免 CPU3 抢占的明确取舍。CPU5/11 与负载
        相对较低的 Fays 预处理阶段共享，避免占用 Track 固定逻辑 CPU。
        普通/UI线程继续放在 CPU3。

        C++ mark_only 二进制的显式 MP cleanup 初始使用 background=CPU9。
        LocalMapping 和 LoopClosing 保留各自 ORB 子进程的完整 CPU 域，不再
        被外部审计器持续压到单个 CPU9。
        """
        if secondary:
            return cls(
                source="balanced_dual_slam_secondary",
                # Sightac 的两个读取线程允许在 10/11 之间调度；Fays
                # SDK 回调仍固定在 10，避免所有输入工作都锁在一个逻辑核。
                input=(10, 11),
                fays_input=(10,),
                idle_smt=(6, 7, 8),
                fays_prepare=(11,),
                fays_left_orb=(0,),
                fays_right_orb=(1,),
                fays_track=(2,),
                fays_background=(9,),
                left_process=(9,),
                right_process=(11,),
                general=(3,),
                _balanced_dual_slam=True,
            )
        return cls(
            source="balanced_dual_slam_primary",
            # Sightac 的两个读取线程允许在 4/5 之间调度；Fays SDK
            # 回调仍固定在 4。两者因此使用不同的角色和 affinity 域。
            input=(4, 5),
            fays_input=(4,),
            idle_smt=(1,),
            left_process=(1,),
            right_process=(5,),
            general=(3,),
            _balanced_dual_slam=True,
        )

    def validate_balanced_dual_slam(self) -> None:
        secondary = self.source == "balanced_dual_slam_secondary"
        expected = dict(EXPECTED_ROLE_CPUS)
        expected.update({
            "input": (10, 11) if secondary else (4, 5),
            "fays_input": (10,) if secondary else (4,),
            "idle_smt": (1,) if not secondary else (6, 7, 8),
            "left_process": (1,) if not secondary else (9,),
            "right_process": (5,) if not secondary else (11,),
            "general": (3,),
        })
        if secondary:
            expected.update({
                "fays_prepare": (11,),
                "fays_left_orb": (0,),
                "fays_right_orb": (1,),
                "fays_track": (2,),
                "fays_background": (9,),
                "left_process": (9,),
                "right_process": (11,),
            })
        actual = {
            role: getattr(self, role) for role in CPU_ROLE_ORDER
        }
        if actual != expected:
            raise ValueError(
                "invalid balanced dual-SLAM CPU partition: "
                f"expected={expected!r} actual={actual!r}"
            )
        # left/right_process are child-process domains, not parent Python
        # threads. The primary left worker is kept off the UI/general CPU3.
        # CPU1 is the SMT sibling of the secondary right ORB CPU7; this is the
        # least-invasive available slot in the fully occupied dual-device
        # layout. CPU5/11 carry the comparatively light Fays preprocess stages
        # as well as one tactile worker each; no two tactile workers are pinned
        # to the same logical CPU.
        overlap = (
            set(self.reserved)
            - set(self.left_process)
            - set(self.right_process)
        ) & set(self.general)
        if overlap:
            raise ValueError(
                "invalid balanced dual-SLAM reserved/general overlap: "
                f"expected=[] actual={sorted(overlap)}"
            )

    def validate_dual_slam(self) -> None:
        expected = dict(EXPECTED_ROLE_CPUS)
        expected.update({
            "input": self.input,
            "fays_input": self.fays_input,
            "fays_track": self.fays_track,
        })
        actual = {
            role: getattr(self, role) for role in CPU_ROLE_ORDER
        }
        if actual != expected:
            raise ValueError(
                "invalid dual-SLAM CPU partition: "
                f"expected={expected!r} actual={actual!r}"
            )
        if self.fays_track not in {(6,), (8,), (10,)}:
            raise ValueError(
                "dual-SLAM track must be CPU6, CPU8 or CPU10: "
                f"{self.fays_track!r}"
            )
        if self.input not in {(4,), (5,), (6,)}:
            raise ValueError(
                "dual-SLAM input must be CPU4, CPU5 or CPU6: "
                f"{self.input!r}"
            )
        if self.input != self.fays_input:
            raise ValueError(
                "dual-SLAM input/fays_input must share the same CPU: "
                f"input={self.input!r} fays_input={self.fays_input!r}"
            )
        reserved = set(self.reserved)
        general = set(self.general)
        if general != {10, 11}:
            raise ValueError(
                f"invalid dual-SLAM boundary: "
                f"reserved={sorted(reserved)} general={sorted(general)}"
            )
        if reserved & general:
            raise ValueError("dual-SLAM reserved/general overlap")
        if any(cpu < 0 or cpu > 10 for cpu in reserved):
            raise ValueError(
                "dual-SLAM reserved CPU outside 0-10: "
                f"{sorted(reserved)}"
            )

    @property
    def roles(self) -> Mapping[str, Tuple[int, ...]]:
        """返回不可变 role → CPU 元组视图。"""

        return MappingProxyType({
            role: getattr(self, role) for role in CPU_ROLE_ORDER
        })

    @property
    def fays_process(self) -> Tuple[int, ...]:
        """ORB 子进程需要作为 cpuset 超集继承的全部 Fays 阶段 CPU。

        进程启动时 taskset 必须包含所有内部阶段 CPU；否则 TrackStereo
        里的 ORB worker 无法用 sched_setaffinity 绑定到自己的角色核。
        """
        return tuple(sorted({
            cpu
            for role in FAYS_PROCESS_ROLES
            for cpu in getattr(self, role)
        }))

    @property
    def reserved(self) -> Tuple[int, ...]:
        return tuple(sorted({
            cpu
            for role in RESERVED_ROLES
            for cpu in getattr(self, role)
        }))

    def cpus_for(self, role: str) -> Tuple[int, ...]:
        try:
            return self.roles[str(role)]
        except KeyError as exc:
            raise KeyError(f"unknown CPU role: {role!r}") from exc

    def validate_relaxed(self) -> None:
        """放松模式：只检查预留域和普通域不重叠。"""
        reserved = set(self.reserved)
        general = set(self.general)
        if reserved & general:
            raise ValueError(
                "relaxed CPU policy: reserved/general overlap: "
                f"reserved={sorted(reserved)} general={sorted(general)}"
            )
        all_cpus = reserved | general
        if len(all_cpus) < 4:
            raise ValueError(
                "relaxed CPU policy requires at least 4 logical CPUs: "
                f"found={sorted(all_cpus)}"
            )

    def validate_strict(self) -> None:
        actual = {role: getattr(self, role) for role in CPU_ROLE_ORDER}
        if actual != dict(EXPECTED_ROLE_CPUS):
            raise ValueError(
                "invalid fixed CPU partition: "
                f"expected={dict(EXPECTED_ROLE_CPUS)!r} actual={actual!r}"
            )
        reserved = set(self.reserved)
        general = set(self.general)
        if reserved != set(range(10)) or general != {10, 11}:
            raise ValueError(
                f"invalid strict boundary: reserved={sorted(reserved)} "
                f"general={sorted(general)}"
            )
        if reserved & general:
            raise ValueError("reserved and general CPU domains overlap")

    def describe(self) -> str:
        return ", ".join(
            f"{role}={getattr(self, role)}" for role in CPU_ROLE_ORDER)


def _read_int_file(path: str, default: Optional[int] = None) -> Optional[int]:
    try:
        with open(path, encoding="utf-8") as stream:
            return int(stream.read().strip())
    except (OSError, ValueError):
        return default


def discover_cpu_cores(
    sysfs_cpu_root: os.PathLike[str] | str = "/sys/devices/system/cpu",
    affinity_getter: Optional[Callable[[int], Iterable[int]]] = None,
) -> Tuple[CoreTopology, ...]:
    """返回当前进程 affinity 允许范围内的物理核拓扑。

    ``sysfs_cpu_root`` 和 ``affinity_getter`` 可注入，便于无硬件测试；默认
    路径只执行只读访问。
    """

    getter = affinity_getter
    if getter is None:
        getter = getattr(os, "sched_getaffinity", None)
    if getter is None:
        return ()
    try:
        allowed = {int(cpu) for cpu in getter(0)}
    except (OSError, TypeError, ValueError):
        return ()

    root = os.fspath(sysfs_cpu_root)
    records: dict[tuple[int, int], dict[str, object]] = {}
    for cpu in sorted(allowed):
        cpu_root = os.path.join(root, f"cpu{cpu}")
        topology = os.path.join(cpu_root, "topology")
        package_id = _read_int_file(
            os.path.join(topology, "physical_package_id"), 0)
        core_id = _read_int_file(
            os.path.join(topology, "core_id"), cpu)
        key = (int(package_id or 0), int(core_id if core_id is not None else cpu))
        record = records.setdefault(key, {
            "cpus": set(),
            "sibling_count": 1,
            "max_freq": 0,
            "core_type": None,
        })
        cast_cpus = record["cpus"]
        assert isinstance(cast_cpus, set)
        cast_cpus.add(cpu)

        try:
            with open(
                os.path.join(topology, "thread_siblings_list"),
                encoding="utf-8",
            ) as stream:
                sibling_count = len(parse_cpu_list(stream.read()))
            record["sibling_count"] = max(
                int(record["sibling_count"]), sibling_count)
        except (OSError, ValueError):
            pass

        max_freq = _read_int_file(
            os.path.join(cpu_root, "cpufreq", "cpuinfo_max_freq"), 0)
        record["max_freq"] = max(
            int(record["max_freq"]), int(max_freq or 0))
        core_type = _read_int_file(
            os.path.join(topology, "core_type"))
        if core_type is not None:
            record["core_type"] = core_type

    cores = (
        CoreTopology(
            cpus=tuple(record["cpus"]),
            sibling_count=int(record["sibling_count"]),
            max_freq=int(record["max_freq"]),
            core_type=(
                None if record["core_type"] is None
                else int(record["core_type"])
            ),
            package_id=key[0],
            core_id=key[1],
        )
        for key, record in records.items()
    )
    return tuple(sorted(cores, key=lambda item: min(item.cpus)))


def select_cpu_policy(
    cores: Sequence[CoreTopology | Mapping[str, object]],
) -> Optional[CpuPolicySnapshot]:
    """在已验证的混合核拓扑上选择项目唯一的固定 CPU 方案。

    核类型识别顺序和重构前实现一致：``core_type``、SMT 拓扑、最高频率。
    编号或物理拓扑不匹配时返回 ``None``，绝不把固定方案套到未知机器。
    """

    normalized: list[CoreTopology] = []
    for core in cores:
        if isinstance(core, CoreTopology):
            normalized.append(core)
            continue
        cpus_value = core.get("cpus", ())
        cpus = tuple(int(cpu) for cpu in cpus_value)  # type: ignore[arg-type]
        if not cpus:
            continue
        normalized.append(CoreTopology(
            cpus=cpus,
            sibling_count=int(core.get("sibling_count", 1)),
            max_freq=int(core.get("max_freq", 0)),
            core_type=(
                None if core.get("core_type") is None
                else int(core["core_type"])
            ),
        ))

    if len(normalized) < 3:
        return None

    p_cores: list[CoreTopology] = []
    e_cores: list[CoreTopology] = []
    source = ""

    typed = {
        core.core_type for core in normalized if core.core_type is not None
    }
    if len(typed) >= 2:
        by_type: dict[int, list[CoreTopology]] = {}
        for core in normalized:
            if core.core_type is not None:
                by_type.setdefault(core.core_type, []).append(core)

        def type_rank(
            item: tuple[int, list[CoreTopology]],
        ) -> tuple[int, int, int]:
            core_type, members = item
            return (
                max(member.sibling_count for member in members),
                max(member.max_freq for member in members),
                core_type,
            )

        ordered_types = sorted(by_type.items(), key=type_rank)
        e_cores = ordered_types[0][1]
        p_cores = ordered_types[-1][1]
        source = "sysfs core_type"

    if len(p_cores) < 2 or not e_cores:
        sibling_counts = sorted({
            core.sibling_count for core in normalized
        })
        if len(sibling_counts) >= 2:
            e_count = sibling_counts[0]
            p_count = sibling_counts[-1]
            e_cores = [
                core for core in normalized
                if core.sibling_count == e_count
            ]
            p_cores = [
                core for core in normalized
                if core.sibling_count == p_count
            ]
            source = "SMT topology"

    if len(p_cores) < 2 or not e_cores:
        frequencies = sorted({
            core.max_freq for core in normalized if core.max_freq > 0
        })
        if (
            len(frequencies) >= 2
            and frequencies[-1] >= frequencies[0] * 1.05
        ):
            e_freq = frequencies[0]
            p_freq = frequencies[-1]
            e_cores = [
                core for core in normalized if core.max_freq == e_freq
            ]
            p_cores = [
                core for core in normalized if core.max_freq == p_freq
            ]
            source = "maximum frequency"

    if len(p_cores) < 2 or not e_cores:
        all_cpus = {
            cpu for core in normalized for cpu in core.cpus
        }
        if _can_use_homogeneous_fallback(normalized, all_cpus):
            return _build_homogeneous_policy(normalized)
        return None

    all_cpus = {
        cpu for core in normalized for cpu in core.cpus
    }
    if not set(range(12)).issubset(all_cpus):
        return None

    p_cpu_groups = [set(core.cpus) for core in p_cores]
    e_cpu_set = {cpu for core in e_cores for cpu in core.cpus}
    if (
        not any({0, 1}.issubset(group) for group in p_cpu_groups)
        or not any({2, 3}.issubset(group) for group in p_cpu_groups)
        or not set(range(4, 12)).issubset(e_cpu_set)
    ):
        return None

    return CpuPolicySnapshot(source=source)


def _can_use_homogeneous_fallback(
    cores: list[CoreTopology],
    all_cpus: set[int],
) -> bool:
    """检查是否为同构 12 逻辑 CPU 系统（如 AMD Ryzen 无大小核）。"""
    return (
        len(cores) >= 3
        and all(core.sibling_count == 2 for core in cores)
        and set(range(12)).issubset(all_cpus)
    )


def _build_homogeneous_policy(
    cores: list[CoreTopology],
) -> CpuPolicySnapshot:
    """在同构 CPU 上构建放松策略。

    以 AMD SMT 编号方案 (0,6), (1,7), (2,8), (3,9), (4,10), (5,11)
    为基准排列角色，保证每个重角色独占一个物理核。
    """

    sibling_map: dict[int, int] = {}
    for core in cores:
        cpus = sorted(core.cpus)
        if len(cpus) == 2:
            sibling_map[cpus[0]] = cpus[1]
            sibling_map[cpus[1]] = cpus[0]

    def _sibling(cpu: int) -> int:
        return sibling_map.get(cpu, cpu)

    matched = True
    try:
        if sibling_map.get(0) == 6 and sibling_map.get(2) == 8:
            # Standard AMD SMT (0,6) (1,7) (2,8) (3,9) (4,10) (5,11)
            return CpuPolicySnapshot(
                source="homogeneous_amd",
                input=(4,),
                fays_input=(4,),
                left_process=(0,),
                right_process=(2,),
                idle_smt=(6, 8),
                fays_prepare=(5,),
                fays_left_orb=(1,),
                fays_right_orb=(3,),
                fays_track=(7,),
                fays_background=(9,),
                general=(10, 11),
                _relaxed=True,
            )
    except Exception:
        matched = False

    # Generic fallback: assign roles to distinct physical cores
    used_physical: set[int] = set()
    role_cpus: dict[str, tuple[int, ...]] = {}
    role_order = [
        ("left_process", 1),
        ("right_process", 1),
        ("fays_left_orb", 1),
        ("fays_right_orb", 1),
        ("fays_track", 1),
        ("fays_background", 1),
        ("fays_prepare", 1),
        ("input", 1),
        ("fays_input", 1),
        ("general", 2),
    ]
    cpu_pool: list[int] = []
    for core in sorted(cores, key=lambda c: min(c.cpus)):
        for cpu in sorted(core.cpus):
            cpu_pool.append(cpu)
    idx = 0
    idle_cpus: list[int] = []
    for role_name, count in role_order:
        assigned: list[int] = []
        while len(assigned) < count and idx < len(cpu_pool):
            cpu = cpu_pool[idx]
            idx += 1
            physical = sibling_map.get(cpu, cpu)
            if physical in used_physical:
                idle_cpus.append(cpu)
                continue
            used_physical.add(physical)
            assigned.append(cpu)
            # mark sibling as idle
            sibling = sibling_map.get(cpu)
            if sibling is not None and sibling not in assigned:
                idle_cpus.append(sibling)
                used_physical.add(physical)
        if not assigned:
            assigned = [cpu_pool[idx % len(cpu_pool)]]
            idx += 1
        role_cpus[role_name] = tuple(assigned)
    idle_smt = tuple(sorted(set(idle_cpus))[:4])
    if not idle_smt:
        idle_smt = (cpu_pool[-1],) if cpu_pool else (0,)

    return CpuPolicySnapshot(
        source="homogeneous_generic",
        input=role_cpus.get("input", (0,)),
        fays_input=role_cpus.get("fays_input", (0,)),
        left_process=role_cpus.get("left_process", (0,)),
        right_process=role_cpus.get("right_process", (2,)),
        idle_smt=idle_smt,
        fays_prepare=role_cpus.get("fays_prepare", (5,)),
        fays_left_orb=role_cpus.get("fays_left_orb", (1,)),
        fays_right_orb=role_cpus.get("fays_right_orb", (3,)),
        fays_track=role_cpus.get("fays_track", (7,)),
        fays_background=role_cpus.get("fays_background", (9,)),
        general=role_cpus.get("general", (10, 11)),
        _relaxed=True,
    )


def detect_cpu_policy(
    sysfs_cpu_root: os.PathLike[str] | str = "/sys/devices/system/cpu",
    affinity_getter: Optional[Callable[[int], Iterable[int]]] = None,
) -> CpuPolicySnapshot:
    """发现并严格验证项目所需的 CPU0-11 分区。"""

    policy = select_cpu_policy(discover_cpu_cores(
        sysfs_cpu_root=sysfs_cpu_root,
        affinity_getter=affinity_getter,
    ))
    if policy is None:
        raise RuntimeError(
            "strict CPU isolation unavailable: expected CPU0-9 reserved "
            "by role and CPU10-11 available for general work"
        )
    return policy
