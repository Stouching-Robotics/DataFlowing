"""夹爪板状态、百分比与双路 Fz 锁存的唯一状态源。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import threading
from types import MappingProxyType
from typing import Mapping, Optional, Sequence, Tuple


DEFAULT_FZ_THRESHOLD_MN = 300
MIN_FZ_THRESHOLD_MN = 10
MAX_FZ_THRESHOLD_MN = 5000
RELEASE_PERCENT_THRESHOLD = 20.0
RELEASE_CONFIRMATION_COUNT = 3
ACTIVE_BOARD_STATES = frozenset({"1", "2"})


class GripEvent(str, Enum):
    NONE = "none"
    TRIGGERED = "triggered"
    RELEASED = "released"


@dataclass(frozen=True)
class GripSnapshot:
    """供 UI/录制读取的不可变夹爪快照。"""

    board_state: Mapping[str, str]
    actual: str
    as5600: Mapping[str, str]
    raw_state: str
    raw_actual: str
    raw_as5600: str
    updated: str
    fz_threshold_mn: int
    grip_latched: bool
    release_count: int
    percent: Optional[float]
    raw_angle: Optional[str]


def _freeze_string_mapping(
    values: Optional[Mapping[object, object]],
) -> Mapping[str, str]:
    return MappingProxyType({
        str(key): str(value) for key, value in (values or {}).items()
    })


def normalize_fz_threshold(value: object) -> int:
    """按原 UI 契约把阈值限制到 10–5000 mN。"""

    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Fz threshold must be finite")
    return max(
        MIN_FZ_THRESHOLD_MN,
        min(MAX_FZ_THRESHOLD_MN, int(number)),
    )


def clamp_percent(value: object) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("grip percent must be finite")
    return max(0.0, min(100.0, number))


def firmware_percent(
    board_state: Mapping[str, object],
) -> Tuple[Optional[float], bool, Optional[str]]:
    """读取固件 ``PCT/GRIP/RAW``，保持原三元组返回契约。"""

    try:
        percent = clamp_percent(board_state.get("PCT", 0))
    except (TypeError, ValueError):
        return None, False, None
    gripped = str(board_state.get("GRIP", "0")) == "1"
    raw_value = board_state.get("RAW")
    raw_angle = None if raw_value is None else str(raw_value)
    return percent, gripped, raw_angle


def _force_z(force: Optional[Sequence[object]]) -> float:
    if force is None or len(force) < 3:
        return 0.0
    try:
        value = float(force[2])
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) else 0.0


class GripState:
    """唯一写入 board/Fz 锁存状态的线程安全控制对象。

    该对象内部保存可变状态；消费者只能通过 :meth:`snapshot` 取得深拷贝并
    冻结后的视图，不暴露入口字段转发器。
    """

    def __init__(self, fz_threshold_mn: int = DEFAULT_FZ_THRESHOLD_MN) -> None:
        self._lock = threading.RLock()
        self._board_state: dict[str, str] = {}
        self._actual = "--"
        self._as5600: dict[str, str] = {}
        self._raw_state = "--"
        self._raw_actual = "--"
        self._raw_as5600 = "--"
        self._updated = "--"
        self._fz_threshold_mn = normalize_fz_threshold(fz_threshold_mn)
        self._grip_latched = False
        self._release_count = 0

    def snapshot(self) -> GripSnapshot:
        with self._lock:
            percent, _gripped, raw_angle = firmware_percent(
                self._board_state)
            return GripSnapshot(
                board_state=_freeze_string_mapping(self._board_state),
                actual=self._actual,
                as5600=_freeze_string_mapping(self._as5600),
                raw_state=self._raw_state,
                raw_actual=self._raw_actual,
                raw_as5600=self._raw_as5600,
                updated=self._updated,
                fz_threshold_mn=self._fz_threshold_mn,
                grip_latched=self._grip_latched,
                release_count=self._release_count,
                percent=percent,
                raw_angle=raw_angle,
            )

    def replace_board_snapshot(
        self,
        board_snapshot: Mapping[str, object],
    ) -> GripSnapshot:
        """原子接收串口解析后的完整 board 快照。"""

        nested = board_snapshot.get("state", {})
        state_values = nested if isinstance(nested, Mapping) else {}
        as5600 = board_snapshot.get("as5600", {})
        as5600_values = as5600 if isinstance(as5600, Mapping) else {}
        with self._lock:
            self._board_state = {
                str(key): str(value) for key, value in state_values.items()
            }
            if self._grip_latched:
                self._board_state["GRIP"] = "1"
            self._actual = str(board_snapshot.get("actual", "--"))
            self._as5600 = {
                str(key): str(value) for key, value in as5600_values.items()
            }
            self._raw_state = str(board_snapshot.get("raw_state", "--"))
            self._raw_actual = str(board_snapshot.get("raw_actual", "--"))
            self._raw_as5600 = str(board_snapshot.get("raw_as5600", "--"))
            self._updated = str(board_snapshot.get("updated", "--"))
        return self.snapshot()

    def update_firmware_state(
        self,
        values: Mapping[str, object],
        *,
        updated: Optional[object] = None,
    ) -> GripSnapshot:
        """只替换固件 ``STATE`` 字段，供串口状态循环使用。"""

        with self._lock:
            self._board_state = {
                str(key): str(value) for key, value in values.items()
            }
            if self._grip_latched:
                self._board_state["GRIP"] = "1"
            if updated is not None:
                self._updated = str(updated)
        return self.snapshot()

    def set_fz_threshold(self, value: object) -> int:
        threshold = normalize_fz_threshold(value)
        with self._lock:
            self._fz_threshold_mn = threshold
        return threshold

    def evaluate_forces(
        self,
        left_force: Optional[Sequence[object]],
        right_force: Optional[Sequence[object]],
    ) -> GripEvent:
        """应用双路 Fz 阈值、锁存和连续三次张开释放滞后。"""

        left_fz = _force_z(left_force)
        right_fz = _force_z(right_force)
        with self._lock:
            state_code = self._board_state.get("ST", "0")
            if state_code not in ACTIVE_BOARD_STATES:
                return GripEvent.NONE
            try:
                percent = clamp_percent(
                    self._board_state.get("PCT", "0"))
            except (TypeError, ValueError):
                return GripEvent.NONE

            if self._grip_latched:
                if percent < RELEASE_PERCENT_THRESHOLD:
                    self._release_count += 1
                    if self._release_count >= RELEASE_CONFIRMATION_COUNT:
                        self._grip_latched = False
                        self._release_count = 0
                        self._board_state.pop("GRIP", None)
                        return GripEvent.RELEASED
                else:
                    self._release_count = 0
                return GripEvent.NONE

            has_force = (
                left_fz >= self._fz_threshold_mn
                or right_fz >= self._fz_threshold_mn
            )
            if not has_force:
                return GripEvent.NONE

            self._grip_latched = True
            self._release_count = 0
            self._board_state["GRIP"] = "1"
            return GripEvent.TRIGGERED

    def reset_latch(self) -> None:
        with self._lock:
            self._grip_latched = False
            self._release_count = 0
            self._board_state.pop("GRIP", None)
