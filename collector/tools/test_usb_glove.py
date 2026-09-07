"""USB (Type-C) 手套连接自检 —— 不依赖 GUI，直接读取并打印 IMU/触觉数据。

用法（项目根目录）:
    python tools/test_usb_glove.py            # 自动发现 0483:5740 串口
    python tools/test_usb_glove.py --port /dev/ttyACM0
    python tools/test_usb_glove.py --seconds 8

预期输出: 串口状态流转（waiting → connected）、IMU 四元数/有效数、
触觉矩阵峰值与帧率。所有数据不落盘，仅验证硬件与协议通路。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from core.glove_usb import RawImuStream, TactilePreprocessor  # noqa: E402
from core.glove_usb.usb_protocol import find_stm32_cdc_port  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="USB 手套连接自检")
    ap.add_argument("--port", default="", help="串口路径（默认自动发现）")
    ap.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args()

    port = args.port or find_stm32_cdc_port()
    if not port:
        print("[失败] 未找到 STM32 手套串口 (0483:5740)。\n"
              "  请确认: ①手套电源开 ②Type-C 线为数据线 ③udev 规则已装\n"
              "  （sudo cp stouch_glove_toolkit-*/99-stm32-glove.rules "
              "/etc/udev/rules.d/ && sudo udevadm control --reload-rules）")
        return 1

    statuses: list[str] = []
    def on_status(s: str):
        statuses.append(s)
        print(f"  [状态] {s}")

    print(f"连接 {port} …")
    stream = RawImuStream(serial_port=port, status_callback=on_status)
    preprocessor = TactilePreprocessor()
    tactile = stream.tactile_stream()

    imu_count = 0
    tac_count = 0
    start = time.monotonic()
    end = start + args.seconds
    imu_frame = None
    try:
        while time.monotonic() < end:
            # IMU 最新一帧（1s 超时内必有帧，80Hz）
            try:
                imu_frame = stream.read(timeout=1.0)
                imu_count += 1
            except Exception as exc:
                print(f"  [警告] IMU 读取异常: {exc}")
                break
            # 触觉不阻塞：读不到就继续
            for _ in range(16):
                try:
                    tac_frame = tactile.read(timeout=0.0)
                except Exception:
                    break
                processed, peak = preprocessor.process(tac_frame.samples)
                tac_count += 1
                if processed is not None and peak > 0:
                    print(f"  [触觉] peak={peak:7.1f}  seq={tac_frame.sequence} "
                          f"ts_us={tac_frame.timestamp_us}")
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()

    elapsed = max(time.monotonic() - start, 1e-6)
    print("─" * 56)
    if imu_frame is not None:
        valid = int(np.count_nonzero(imu_frame.valid_mask))
        print(f"IMU: {imu_count} 帧 ≈ {imu_count / elapsed:.1f} Hz, "
              f"有效传感器 {valid}/16")
        print(f"  最新四元数(前2个, XYZW):\n"
              f"  {imu_frame.quaternions_xyzw[:2]}")
        print(f"  设备时间戳: {imu_frame.device_timestamp_us} µs")
    else:
        print("IMU: 未收到任何帧")
    print(f"触觉: {tac_count} 帧 ≈ {tac_count / elapsed:.1f} Hz "
          f"（基线校准需 {preprocessor.calibration_frames} 帧）")
    if imu_count > 0:
        print("\n✔ 连接成功 —— 硬件与协议通路正常")
        return 0
    print("\n✘ 连接失败 —— 串口打开但无数据，检查固件/波特率/线材")
    return 1


if __name__ == "__main__":
    sys.exit(main())
