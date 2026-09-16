#!/usr/bin/env bash
# ============================================================
#  DAQ 极简采集版 —— Linux 一键部署脚本（与 Windows start_lite.bat 行为一致）
#
#  用法:
#    ./start_lite.sh               部署(按需) + 启动极简采集
#    ./start_lite.sh reinstall     删除 venv_lite 强制重装（出问题首选）
#    ./start_lite.sh help          打开 使用说明_lite.md
#
#  只安装 requirements-lite.txt 白名单依赖（独立 venv_lite/，
#  与主程序 venv/ 互不影响）；wheels/ 与 data/ 两版本共用。
#  依赖安装顺序: 离线 wheels/ 包 → 阿里云镜像 → 清华镜像 → 官方源
#
#  夹爪(UMI/Fays S80M): 原生资源 core/gripper/native（约 460MB）随本包
#  下发，[B] 会逐项校验，缺失报 [错误 B] 并拒启；Windows 包不带该资源。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

FORCE=0
case "${1:-}" in
    reinstall)    FORCE=1 ;;
    help|guide)   MODE=help ;;
esac

if [ "${MODE:-run}" = help ]; then
    [ -f "使用说明_lite.md" ] && xdg-open "使用说明_lite.md" 2>/dev/null || true
    echo "常用命令: ./start_lite.sh [reinstall|help]"
    exit 0
fi

# ── [G] 解压层次自检 ──
if [ ! -f main_lite.py ] || [ ! -f requirements-lite.txt ]; then
    echo "[错误 G] 未找到 main_lite.py —— 目录层次不对。start_lite.sh 与 main_lite.py 必须在同一目录。"
    exit 1
fi

# ── [B] 夹爪原生资源自检（Linux 包独有，缺失即拒启）──
# 夹爪（UMI / Fays S80M）的原生栈约 460MB，作为**必需资源**随 Linux 分发包
# 下发；Windows 包不带（原生栈是 ELF x86-64，物理上跑不了，那边靠载荷缺失
# 自然降级为纯手套版，见 start_lite.bat 的注释）。
# 清单与 core/gripper/paths.py 的 required_resources() 一一对应：这里只查
# 存在性与可执行位（不 import —— 那要等 venv 装好，见 [E]）；打包脚本
# scripts/pack_lite.py 会在收集时逐项核对这份清单，改了 paths.py 忘了改这里
# 会**在打包当场报错**，所以两处不会悄悄漂移。
# 逃生舱 KSQ_GRIPPER_SKIP_CHECK=1：决策上载荷是必需资源，但硬停启动的代价
# 是整机不可用，留一个显式开关（跳过则夹爪组为空，D435/UVC/手套照常）。
GRIPPER_NATIVE="${KSQ_GRIPPER_NATIVE_ROOT:-core/gripper/native}"
GRIPPER_SIGHTAC="${KSQ_SIGHTAC_ROOT:-core/gripper/sightac_sdk}"
if [ "${KSQ_GRIPPER_SKIP_CHECK:-0}" = "1" ]; then
    echo "[B] KSQ_GRIPPER_SKIP_CHECK=1 —— 跳过夹爪资源自检，夹爪将不可用"
else
    MISSING=""
    check_res() {   # $1=标签 $2=路径 $3=1 表示需要可执行位
        if [ ! -e "$2" ]; then
            MISSING="${MISSING}  · $1 —— 缺 $2\n"
        elif [ "$3" = "1" ] && [ ! -x "$2" ]; then
            MISSING="${MISSING}  · $1 —— $2 没有可执行位（chmod +x 修复）\n"
        fi
    }
    check_res "Fays ORB 桥接" "$GRIPPER_NATIVE/dist/fays_opencv48/bin/fayssense_orb_slam_sn219_opencv48_mark_only" 1
    check_res "Fays 标定探测" "$GRIPPER_NATIVE/dist/fays_aikit/bin/fays_vikit_calibration_probe" 1
    check_res "ORB-SLAM3 核心库" "$GRIPPER_NATIVE/dist/orb_mark_only/lib/libORB_SLAM3.so" 0
    check_res "ORB 词典" "$GRIPPER_NATIVE/ORBvoc.txt" 0
    check_res "libuvc 相机服务" "$GRIPPER_NATIVE/camera_service/build/ksq-camera-service" 1
    check_res "Sightac SDK" "$GRIPPER_SIGHTAC/api_new" 0
    check_res "Fays SDK lib" "$GRIPPER_NATIVE/FaysSense_VI_Kit_Release/lib" 0
    if [ -n "$MISSING" ]; then
        echo "[错误 B] 夹爪原生资源缺失 —— 本分发包不完整："
        printf '%b' "$MISSING"
        echo "  原因通常是解压少了一层目录、拷贝中断，或覆盖升级时漏了 core/gripper/native/。"
        echo "  修复: ① 重新完整解压分发包；② 从原始包补齐 core/gripper/native/（约 460MB）；"
        echo "        ③ 仅想先用 D435/UVC/手套: KSQ_GRIPPER_SKIP_CHECK=1 ./start_lite.sh"
        echo "  自定义安装位置时可用 KSQ_GRIPPER_NATIVE_ROOT=<native 目录> 指路。"
        exit 1
    fi
    echo "[B] 夹爪资源自检通过（7 项）"
fi

# ── [1/6] 定位 Python（>= 3.10，推荐 3.12）──
PY=""
find_python() {
    for c in python3.12 python3.11 python3.10 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            PY="$(command -v "$c")"
            return 0
        fi
    done
    return 1
}
if [ -x venv_lite/bin/python ]; then
    PY="venv_lite/bin/python"
elif ! find_python; then
    echo "[错误 A] 未找到 Python >= 3.10。请先安装:"
    echo "  Ubuntu/Debian:  sudo apt install python3.12 python3.12-venv"
    echo "  CentOS/RHEL:    sudo dnf install python3.12"
    echo "  conda:          conda create -n daq_lite python=3.12"
    exit 1
fi
echo "[1/6] 使用 Python: $PY"

# ── [2/6] venv_lite ──
if [ -x venv_lite/bin/python ] && [ "$FORCE" = 1 ]; then
    echo "[2/6] reinstall: 删除旧 venv_lite ..."
    rm -rf venv_lite
fi
if [ ! -x venv_lite/bin/python ]; then
    echo "[2/6] 创建虚拟环境 venv_lite（首次约 1 分钟）..."
    "$PY" -m venv venv_lite
fi
VPY="venv_lite/bin/python"

# ── [3/6] 依赖（requirements-lite.txt 的 mtime+size 签名驱动）──
HASH="$(md5sum requirements-lite.txt | cut -d' ' -f1)"
NEED_INSTALL=0
if [ "$FORCE" = 1 ]; then
    NEED_INSTALL=1
elif [ ! -f venv_lite/.deps-lite-ok ] || [ "$(cat venv_lite/.deps-lite-ok)" != "$HASH" ]; then
    NEED_INSTALL=1
fi

install_req() {
    local req="$1"
    if [ -d wheels ] && ls wheels/*.whl >/dev/null 2>&1; then
        echo "[3/6] 检测到 wheels/ 离线包，优先离线安装 ..."
        "$VPY" -m pip install --no-index --find-links wheels -r "$req" && return 0
        echo "[3/6] 离线包安装失败，转在线安装 ..."
    fi
    "$VPY" -m pip install -r "$req" -i https://mirrors.aliyun.com/pypi/simple/ && return 0
    "$VPY" -m pip install -r "$req" -i https://pypi.tuna.tsinghua.edu.cn/simple && return 0
    "$VPY" -m pip install -r "$req"
}

if [ "$NEED_INSTALL" = 1 ]; then
    echo "[3/6] 安装依赖（首次约 3-8 分钟，之后启动秒开）..."
    "$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || true
    if ! install_req requirements-lite.txt; then
        echo "[错误 D] 依赖下载/安装失败: 检查网络；内网环境请用 scripts/pack_wheels.py --lite 生成 wheels/ 离线包。"
        exit 1
    fi
    echo "$HASH" > venv_lite/.deps-lite-ok
fi

# ── [4/6] 冒烟自检 ──
echo "[4/6] 依赖自检 ..."
if ! "$VPY" -c "import main_lite" >/dev/null 2>&1; then
    echo "[错误 E] 依赖自检失败。查看具体原因:"
    echo "  venv_lite/bin/python -c \"import main_lite\""
    echo "重装: ./start_lite.sh reinstall"
    exit 1
fi
echo "[4/6] 依赖自检通过"

# ── [5/6] 启动 ──
echo "[5/6] 启动极简采集 ..."
echo
echo "【操作指引】"
echo "  · 设备: 插入后约 2 秒自动出现在列表，选中后点 开启"
echo "  · 录制: 先开 D435 / UVC 摄像头 / 夹爪（至少一个视频源），再点 开始/停止（正常停止=保存，⛔ 丢弃=作废）"
echo "  · 上传: 录制完成后自动上传，或在下方列表手动上传"
echo "  · 说明: ./start_lite.sh help 打开 使用说明_lite.md"
echo
exec "$VPY" main_lite.py
