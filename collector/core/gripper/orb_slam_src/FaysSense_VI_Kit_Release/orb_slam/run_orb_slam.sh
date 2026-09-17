#!/bin/bash
# ============================================================================
# FS-VI-S80M → ORB-SLAM3  回调模式 (单进程, SDK 内部同步 IMU)
# ============================================================================
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SDK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ORB_ROOT="$HOME/ORB-SLAM"
ARCH=$(uname -m)

VOCAB="${ORB_ROOT}/Vocabulary/ORBvoc.txt"
ORB_CONFIG="${SCRIPT_DIR}/s80m_stereo_inertial.yaml"
CAM_CONFIG="${SDK_ROOT}/config/fays_vikit_s80m.yaml"
mkdir -p "${SCRIPT_DIR}/trajectories"
TRAJ="${SCRIPT_DIR}/trajectories/traj_$(date +%Y%m%d_%H%M%S).txt"

echo "========================================"
echo " FS-VI-S80M → ORB-SLAM3 (Callback)"
echo "========================================"

# 清理旧进程 + USB 设备强制复位
pkill -f fayssense_orb_slam 2>/dev/null || true
sleep 0.5
pkill -9 -f fayssense_orb_slam 2>/dev/null || true
sleep 0.3

# USB 软件复位 (等价物理插拔, 解决 5G USB 口设备残留问题)
"${SCRIPT_DIR}/reset_usb" 2>/dev/null || true
sleep 1.5  # 等设备重新枚举

# 确认相机可用
if ! ls /dev/video0 >/dev/null 2>&1; then
    echo "[ERROR] Camera not found after reset. Check USB connection."
    exit 1
fi

# 更新相机端口
devices=$(v4l2-ctl --list-devices 2>/dev/null)
FTDI=$(echo "$devices" | awk '/FTDI/{flag=1;next}/^[^[:space:]]/{flag=0}flag' | grep '/dev/video')
ports=($(echo "$FTDI" | grep -oP '/dev/video\K[0-9]+'))
if [ ${#ports[@]} -ge 4 ]; then
    sed -i -E "s|^stereo_dev_port:.*|stereo_dev_port: /dev/video${ports[0]}|" "$CAM_CONFIG"
    sed -i -E "s|^imu_dev_port:.*|imu_dev_port: /dev/video${ports[2]}|" "$CAM_CONFIG"
    echo "[DETECT] stereo=/dev/video${ports[0]} imu=/dev/video${ports[2]}"
fi

# 编译
BIN="${SCRIPT_DIR}/build/fayssense_orb_slam"
if [ ! -f "$BIN" ]; then
    echo "[BUILD] Compiling..."
    cd "$SCRIPT_DIR" && mkdir -p build && cd build
    cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc)
fi

# 库路径: SDK OpenCV 4.2.0 优先 → ORB-SLAM3
export LD_LIBRARY_PATH="${SDK_ROOT}/thirdparty/opencv-4.2.0-linux-${ARCH}/lib:${SDK_ROOT}/lib/fays_atrak/${ARCH}/Release:${SDK_ROOT}/thirdparty/ft602-linux-${ARCH}:${SDK_ROOT}/compat_libs:${ORB_ROOT}/lib:${LD_LIBRARY_PATH}"

echo "[RUN] Callback mode. Move camera, press ENTER to set origin. Ctrl+C to exit."
exec "$BIN" "$VOCAB" "$ORB_CONFIG" "$CAM_CONFIG" "$TRAJ"

