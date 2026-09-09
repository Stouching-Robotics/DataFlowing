"""KSQ 夹爪硬件适配器。

设备模块不持有应用或 Tk 对象。串口、Sightac 句柄和触觉计算子进程分别只有
这里声明的控制器能够创建和关闭，其他子系统只读取不可变快照或调用公开 API。
"""

from .gripper_connection import (
    ConnectionSnapshot,
    ConnectionState,
    GripperConnectionController,
)
from .gripper_serial import GripperSerial
from .serial_command_worker import (
    SerialCommandCancelled,
    SerialCommandWorker,
)
from .tactile_discovery import TactileDiscovery
from .tactile_process_manager import (
    TactileProcessBridge,
    TactileProcessManager,
    TactileSnapshot,
    TactileState,
)

__all__ = [
    "ConnectionSnapshot",
    "ConnectionState",
    "GripperConnectionController",
    "GripperSerial",
    "SerialCommandCancelled",
    "SerialCommandWorker",
    "TactileDiscovery",
    "TactileProcessBridge",
    "TactileProcessManager",
    "TactileSnapshot",
    "TactileState",
]
