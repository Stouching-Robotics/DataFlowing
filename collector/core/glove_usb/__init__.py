"""Stouch STM32 手套 USB CDC 协议栈（自工具包移植的明文子集）。

只依赖 numpy + pyserial，无 PyArmor/许可证负担。来源:
stouch_glove_toolkit-Stouch_Glove_Toolkit-beta 的 glove_io/ + glove_sdk/
明文模块（errors/types/usb_protocol/streams/tactile_processing），
导入路径改为 core.glove_usb.*，其余代码与上游保持一致。
"""

from core.glove_usb.streams import RawImuStream, SensorStream, TactileStream
from core.glove_usb.tactile_processing import TactilePreprocessor
from core.glove_usb.types import RawImuFrame, TactileFrame
from core.glove_usb.usb_protocol import (
    STM32_USB_PID,
    STM32_USB_VID,
    find_stm32_cdc_port,
)

__all__ = [
    "RawImuStream", "SensorStream", "TactileStream",
    "TactilePreprocessor", "RawImuFrame", "TactileFrame",
    "STM32_USB_PID", "STM32_USB_VID", "find_stm32_cdc_port",
]
