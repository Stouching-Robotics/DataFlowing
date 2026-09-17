#!/bin/bash
# ============================================================================
# FS-VI-S80M → ORB-SLAM3  纯双目模式 (无 IMU)  — 对照实验用
# ============================================================================
# 目的: 对比 stereo vs stereo-inertial 的漂移差异
#   - 纯双目 X/Y 不漂 → IMU 外参有问题，需要重新标定
#   - 纯双目 X/Y 也漂 → 相机内参或基线有问题
# ============================================================================
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SDK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ORB_ROOT="$HOME/ORB-SLAM"
ARCH=$(uname -m)

VOCAB="${ORB_ROOT}/Vocabulary/ORBvoc.txt"
ORB_CONFIG="${SCRIPT_DIR}/s80m_stereo.yaml"
CAM_CONFIG="${SDK_ROOT}/config/fays_vikit_s80m.yaml"
mkdir -p "${SCRIPT_DIR}/trajectories"
TRAJ="${SCRIPT_DIR}/trajectories/stereo_only_$(date +%Y%m%d_%H%M%S).txt"

echo "========================================"
echo " FS-VI-S80M → ORB-SLAM3 (纯双目, 无IMU)"
echo " 对照实验: 验证 IMU 外参是否是漂移元凶"
echo "========================================"

# 清理旧进程 + USB 设备强制复位
pkill -f fayssense_orb_slam 2>/dev/null || true
sleep 0.5
pkill -9 -f fayssense_orb_slam 2>/dev/null || true
sleep 0.3

"${SCRIPT_DIR}/reset_usb" 2>/dev/null || true
sleep 1.5

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

# 库路径
export LD_LIBRARY_PATH="${SDK_ROOT}/thirdparty/opencv-4.2.0-linux-${ARCH}/lib:${SDK_ROOT}/lib/fays_atrak/${ARCH}/Release:${SDK_ROOT}/thirdparty/ft602-linux-${ARCH}:${SDK_ROOT}/compat_libs:${ORB_ROOT}/lib:${LD_LIBRARY_PATH}"

BIN="${SCRIPT_DIR}/build/fayssense_orb_slam"
if [ ! -f "$BIN" ]; then
    echo "[BUILD] Compiling..."
    cd "$SCRIPT_DIR" && mkdir -p build && cd build
    cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc)
fi

echo ""
echo "============================================"
echo " 测试步骤:"
echo " 1. 相机静止, 按 ENTER 设原点"
echo " 2. 沿 Z 轴方向移动 ~40cm"
echo " 3. 观察 X/Y 轴的漂移量"
echo " 4. 与 stereo-inertial 结果对比"
echo "============================================"
echo "[RUN] 纯双目模式. Ctrl+C to exit."
exec "$BIN" "$VOCAB" "$ORB_CONFIG" "$CAM_CONFIG" "$TRAJ"
