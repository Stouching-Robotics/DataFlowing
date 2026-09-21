#!/usr/bin/env bash
# ============================================================
#  DAQ 数据采集系统 —— Linux 一键部署脚本（与 Windows start.bat 行为一致）
#
#  用法:
#    ./start.sh               部署(按需) + 启动主程序
#    ./start.sh reinstall     删除 venv 强制重装（出问题首选）
#    ./start.sh extras        追加安装 mediapipe（裸手 3D 关键点）
#    ./start.sh extras-torch  追加安装 CPU 版 torch
#    ./start.sh help          打开 使用说明.md
#
#  依赖安装顺序: 离线 wheels/ 包 → 阿里云镜像 → 清华镜像 → 官方源
#
#  本脚本自带 venv，可在已激活 conda / 其它 venv 的终端里直接运行（互不影响）
#
#  默认已包含: D435/D405 深度相机（pyrealsense2）与手套实时骨架解算
#  （scipy/pydantic/polars + 随包工具包，见 [4/7]）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

# ── 环境隔离: 调用任何 Python 之前，先清掉会「串味」的外部变量 ──
# 本脚本一律用项目自带 venv，但用户可能是在 conda / 另一个 venv 里跑的，
# 或自己设过 PYTHONHOME。这些变量会穿透进我们的 venv，把解释器指到别处
# （症状: 依赖明明装了却 import 失败 / pip 装到了别的环境）。
# 直接清空并提示，不让用户去猜；只提示，不打断。
if [ -n "${VIRTUAL_ENV:-}" ]; then
    echo "[提示] 检测到已激活的虚拟环境 $VIRTUAL_ENV，本脚本不使用它（仍用项目自带 venv）"
fi
if [ -n "${CONDA_PREFIX:-}" ]; then
    echo "[提示] 检测到已激活的 conda 环境 $CONDA_PREFIX，本脚本不使用它（仍用项目自带 venv）"
fi
if [ -n "${PYTHONHOME:-}" ]; then
    echo "[提示] 已忽略外部变量 PYTHONHOME=$PYTHONHOME"
fi
if [ -n "${PYTHONPATH:-}" ]; then
    echo "[提示] 已忽略外部变量 PYTHONPATH=$PYTHONPATH"
fi
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP
# 屏蔽用户级 site-packages（~/.local/lib/pythonX.Y/...），让 venv 完全自给自足
export PYTHONNOUSERSITE=1

MODE=run
FORCE=0
case "${1:-}" in
    reinstall)    FORCE=1 ;;
    extras)       MODE=extras ;;
    extras-torch) MODE=extras-torch ;;
    help|guide)   MODE=help ;;
esac
# 非子命令参数原样透传给 main.py

if [ "$MODE" = help ]; then
    [ -f "使用说明.md" ] && xdg-open "使用说明.md" 2>/dev/null || true
    echo "常用命令: ./start.sh [reinstall|extras|extras-torch|help]"
    echo "English guide: 使用说明_EN.md"
    exit 0
fi

# ── [G] 解压层次自检 ──
if [ ! -f main.py ] || [ ! -f requirements.txt ]; then
    echo "[错误 G] 未找到 main.py —— 目录层次不对。start.sh 与 main.py 必须在同一目录。"
    exit 1
fi

# ── [1/7] 定位 Python（>= 3.10，推荐 3.12）──
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
if [ -x venv/bin/python ]; then
    PY="venv/bin/python"
elif ! find_python; then
    echo "[错误 A] 未找到 Python >= 3.10。请先安装:"
    echo "  Ubuntu/Debian:  sudo apt install python3.12 python3.12-venv"
    echo "  CentOS/RHEL:    sudo dnf install python3.12"
    echo "  conda:          conda create -n daq python=3.12"
    exit 1
fi
echo "[1/7] 使用 Python: $PY"

# ── [2/7] venv（完整性体检 + 自愈）──
# 为什么体检: venv 目录在 ≠ venv 可用。
#   ① 从别的机器拷来的 venv: python 在，但 pyvenv.cfg 指向那台机器的解释器
#      → 一跑就报找不到 Python；
#   ② pip 升级 / 断电 / 杀软打断: pip 被删到一半（目录还在、模块没了）→
#      所有 pip 命令都报 ModuleNotFoundError: pip._internal.cli。
# 这两类都不该让用户去猜: 先离线修（ensurepip 用 Python 自带组件），
# 修不动就整目录重建（同样不联网）。
venv_healthy() {
    "$VPY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        >/dev/null 2>&1 || return 1
    "$VPY" -m pip --version >/dev/null 2>&1 && return 0
    echo "[2/7] 检测到 venv 的 pip 不完整，正在离线修复 ..."
    "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$VPY" -m pip --version >/dev/null 2>&1
}

VPY="venv/bin/python"
if [ ! -x "$VPY" ]; then
    echo "[2/7] 创建虚拟环境 venv（首次约 1 分钟）..."
    "$PY" -m venv venv
elif [ "$FORCE" = 1 ]; then
    echo "[2/7] reinstall: 删除旧 venv ..."
    rm -rf venv
    echo "[2/7] 创建虚拟环境 venv（首次约 1 分钟）..."
    "$PY" -m venv venv
elif ! venv_healthy; then
    echo "[2/7] venv 不可用（pip 缺失或解释器异常），自动重建（不需要联网，约 1 分钟）..."
    rm -rf venv
    "$PY" -m venv venv
fi
if [ ! -x "$VPY" ]; then
    echo "[错误 C] 虚拟环境创建失败:"
    echo "  ① Ubuntu/Debian 先装: sudo apt install python3.12-venv"
    echo "  ② 磁盘空间不足（需约 2GB）；③ 目录写权限；④ 路径含特殊字符"
    exit 1
fi

# ── [3/7] 依赖 ──
HASH="$(md5sum requirements.txt | cut -d' ' -f1)"
NEED_INSTALL=0
if [ "$FORCE" = 1 ]; then
    NEED_INSTALL=1
elif [ ! -f venv/.deps-ok ] || [ "$(cat venv/.deps-ok)" != "$HASH" ]; then
    NEED_INSTALL=1
fi

install_req() {
    local req="$1"
    if [ -d wheels ] && ls wheels/*.whl >/dev/null 2>&1; then
        echo "[3/7] 检测到 wheels/ 离线包，优先离线安装 ..."
        "$VPY" -m pip install --no-index --find-links wheels -r "$req" && return 0
        echo "[3/7] 离线包安装失败，转在线安装 ..."
    fi
    "$VPY" -m pip install -r "$req" -i https://mirrors.aliyun.com/pypi/simple/ && return 0
    "$VPY" -m pip install -r "$req" -i https://pypi.tuna.tsinghua.edu.cn/simple && return 0
    "$VPY" -m pip install -r "$req"
}

err_d() {
    echo "[错误 D] 依赖下载/安装失败"
    echo "  先看上一屏的报错再对症处理:"
    echo "  · 报 ModuleNotFoundError: pip._internal.cli / No module named 'pip'"
    echo "    → venv 里的 pip 坏了（升级被打断 / 被杀软删了文件），不是网络问题。"
    echo "      执行 ./start.sh reinstall 重建 venv（约 1 分钟，不需要联网）。"
    echo "  · 报 Could not find a version / connection / timeout / 证书错误"
    echo "    → 才是网络问题: 检查网络；内网环境请用 scripts/pack_wheels.py 生成 wheels/ 离线包。"
    exit 1
}

if [ "$NEED_INSTALL" = 1 ]; then
    echo "[3/7] 安装依赖（首次约 3-10 分钟，之后启动秒开）..."
    # 这里以前有一句静默的 `pip install --upgrade pip`，已移除 —— 它是「半装
    # pip」的唯一来源: pip 升级是「先删旧、再解新」，中途被打断（关窗口 /
    # 断网 / 被杀软删）就只剩一个空壳，之后每次启动都报
    # ModuleNotFoundError: pip._internal.cli，而用户看到的是「依赖安装失败」
    # （错误 D）—— 方向完全跑偏，而且重试多少次都一样。
    # Python 3.10+ 自带的 pip 足够安装本项目的全部依赖，故不再自动升级；
    # 确有需要时手动执行（坏了的 pip 下次启动会被 [2/7] 体检修好）:
    #     venv/bin/python -m pip install --upgrade pip
    if ! install_req requirements.txt; then
        # 安装失败: 先确认 pip 本身还在不在（被半装 / 被杀软删是常见现场）
        if "$VPY" -m pip --version >/dev/null 2>&1; then
            err_d
        fi
        echo "[3/7] pip 异常，尝试离线修复 ..."
        "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 || true
        "$VPY" -m pip --version >/dev/null 2>&1 || err_d
        echo "[3/7] pip 已修复，重试安装 ..."
        install_req requirements.txt || err_d
    fi
    echo "$HASH" > venv/.deps-ok
fi

# ── [4/7] 手套工具包（手部骨架解算）──
# 实时骨架解算依赖第三方手套工具包，位于项目根同级（core/glove_keypoint_solver.py
# 的 find_toolkit_dir() 按目录名 stouch_glove_toolkit* 找）。裁剪版随 wheels/ 分发，
# 首次部署在这里展开到项目根。**缺失只警告、不拦截** —— 触觉/IMU/相机录制都不受影响。
TOOLKIT=""
find_toolkit() {
    TOOLKIT=""
    for d in stouch_glove_toolkit*/; do
        if [ -d "$d" ]; then TOOLKIT="${d%/}"; return 0; fi
    done
    return 1
}
find_toolkit || true

if [ -z "$TOOLKIT" ] && [ -f wheels/toolkit/glove_toolkit.zip ]; then
    echo "[4/7] 展开随包的手套工具包（骨架解算）..."
    "$VPY" -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(".")' \
        wheels/toolkit/glove_toolkit.zip
    find_toolkit || true
fi

if [ -z "$TOOLKIT" ]; then
    echo "[4/7] [警告] 未找到手套工具包 —— 主程序照常启动，但没有实时手部骨架解算。"
    echo "        补装: 在开发机执行 python scripts/pack_toolkit.py，"
    echo "        把生成的 wheels/ 整个拷到本目录后重跑 ./start.sh"
else
    # 工具包在 ≠ 能用：解算链还要 scipy/pydantic/polars 等依赖。冒烟自检
    # (import main) 查不到这些 —— 它们都是惰性导入，缺了要到连手套时才报错。
    # 这里按「工具包 + 依赖清单版本」缓存结论，命中缓存就不重复跑（省 ~1s）。
    TK_MARK="venv/.toolkit-ok"
    TK_SIG="$TOOLKIT:$HASH"
    if [ -f "$TK_MARK" ] && [ "$(cat "$TK_MARK" 2>/dev/null)" = "$TK_SIG" ]; then
        echo "[4/7] 手套工具包就绪：$TOOLKIT"
    else
        echo "[4/7] 校验骨架解算链（$TOOLKIT）..."
        if TK_OUT="$("$VPY" -c '
import sys
sys.path.insert(0, sys.argv[1])
from glove_sdk.interfaces.solver import HandSolver
' "$TOOLKIT" 2>&1)"; then
            echo "$TK_SIG" > "$TK_MARK"
            echo "[4/7] 手套工具包就绪：实时手部骨架解算已具备"
        else
            echo "[4/7] [警告] 工具包在，但解算链导入失败（多半是依赖没装全）。"
            echo "        原因: $(printf '%s\n' "$TK_OUT" | tail -n 1)"
            echo "        重装依赖: ./start.sh reinstall"
            echo "        主程序照常启动，只是没有手部骨架。"
        fi
    fi
fi

# ── [5/7] 冒烟自检 ──
echo "[5/7] 依赖自检 ..."
if ! "$VPY" -c "import main" >/dev/null 2>&1; then
    echo "[错误 E] 依赖自检失败。查看具体原因:"
    echo "  venv/bin/python -c \"import main\""
    echo "重装: ./start.sh reinstall"
    exit 1
fi
echo "[5/7] 依赖自检通过"

# ── [6/7] 可选功能 ──
install_pkg() {
    if [ -d wheels ] && ls wheels/*.whl >/dev/null 2>&1; then
        "$VPY" -m pip install --no-index --find-links wheels "$1" && return 0
    fi
    "$VPY" -m pip install "$1" -i https://mirrors.aliyun.com/pypi/simple/ && return 0
    "$VPY" -m pip install "$1" -i https://pypi.tuna.tsinghua.edu.cn/simple && return 0
    "$VPY" -m pip install "$1"
}

case "$MODE" in
    extras)
        [ -f venv/.extras-ok ] || {
            # pyrealsense2（D435/D405）自本次起随 requirements.txt 默认安装，不再属于可选包
            echo "[6/7] 安装可选功能: mediapipe ..."
            install_pkg mediapipe || echo "  [警告] mediapipe 安装失败（主程序不受影响）"
            echo done > venv/.extras-ok
        }
        echo "[6/7] 可选功能安装完成。运行 ./start.sh 启动主程序。"
        exit 0 ;;
    extras-torch)
        [ -f venv/.torch-ok ] || {
            echo "[6/7] 安装可选功能: torch CPU 版 ..."
            install_torch() {
                if [ -d wheels ] && ls wheels/*.whl >/dev/null 2>&1; then
                    "$VPY" -m pip install --no-index --find-links wheels torch && return 0
                fi
                "$VPY" -m pip install torch --index-url https://mirrors.aliyun.com/pytorch-wheels/cpu/ && return 0
                "$VPY" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
            }
            install_torch || echo "  [警告] torch 安装失败（主程序不受影响）"
            echo done > venv/.torch-ok
        }
        echo "[6/7] 可选功能安装完成。运行 ./start.sh 启动主程序。"
        exit 0 ;;
esac

# ── [7/7] 启动 ──
echo "[7/7] 启动主程序 ..."
echo
echo "【操作指引】"
echo "  · 设备面板: 相机插入后约 2 秒自动出现，点击即可预览"
echo "  · 录制: 每路相机独立的 开始/停止；正常停止=保存，异常停止=丢弃"
echo "  · 任务/回放/上传: 见主界面与 使用说明.md"
echo
# 标记本次为 start.sh 启动 → 主程序弹出使用步骤窗口（可勾选不再显示）
DAQ_SHOW_GUIDE=1 exec "$VPY" main.py "$@"
